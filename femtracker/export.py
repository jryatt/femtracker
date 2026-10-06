"""Export a study as plain files that diff well in git.

<study-slug>/
  README.md                     what this folder is
  study.json                    name, description, study results, run setup
  history.json                  every saved version of those texts and the run setup
  metrics.csv                   one row per current result: job, attempt, parameters, metrics
  jobs/<id>_<name>/
    job.json                    name, description, priority, parameters and overrides for the next attempt
    notes/results.json (+files) results added by hand
    attempt_<n>/
      attempt.json              frozen parameters and run setup, git state, input files, status, times, errors
      fem_run.sh                the exact script that ran on the HPC
      inputs/<files>            input files as uploaded (if they were stored)
      population_<k>/results.json (+files)   results of each population round, newest is current
  notebook/<id>_<name>.md (or .html)   one file per entry, with front matter (title, kind, tags,
                                       dates, linked jobs); asset: and result: image links and
                                       [[job:N]]/[[attempt:N/M]]/[[result:N]]/[[metrics:job:N]]
                                       references are rewritten to relative paths or plain links,
                                       so the file renders on GitHub/GitLab on its own
    assets/<sha256>.<ext>              every pasted/uploaded image an exported entry refers to
    attachments/<sha256>_<name>.zip    every zipped folder an exported entry refers to
    history.json                       every saved revision of every entry

Output is deterministic: keys are sorted and the export adds no timestamps of its own, so exporting
again after a change and committing shows only what really changed. Files larger than max_file_mb
are listed with their checksum and location instead of being copied.
"""
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import zipfile

MARKER = ".femtracker-export"


def slug(s, n=40):
    return (re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower() or "item")[:n]


def dumps(obj):
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _json(v):
    try:
        return json.loads(v) if isinstance(v, str) and v[:1] in "{[" else v
    except json.JSONDecodeError:
        return v


MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
REF_TOKEN_RE = re.compile(r"\[\[(job|attempt|result|metrics|git|attachment):([^\]\[\s]+)\]\]")
MD_IMG_RE = re.compile(r"(!\[[^\]]*\]\()(asset|result):([^)\s]+)(\))")
HTML_IMG_RE = re.compile(r'src="(asset|result):([^"]+)"')


def _job_metrics(c, jid):
    merged = {}
    for r in c.execute(
        "SELECT metrics FROM results WHERE job_id=? AND superseded_at IS NULL AND (attempt_id IS NULL OR "
        "attempt_id=(SELECT id FROM attempts WHERE job_id=? AND populate_status IN ('done','skipped') "
        "ORDER BY number DESC LIMIT 1)) ORDER BY id", (jid, jid)):
        merged.update(_json(r[0]) or {})
    return merged


def _find_attempts_by_commit(c, prefix):
    prefix = prefix.lower()
    out = []
    for r in c.execute("SELECT a.job_id, a.number, a.git, j.name AS job_name FROM attempts a "
                       "JOIN jobs j ON j.id=a.job_id WHERE a.git IS NOT NULL ORDER BY a.id DESC"):
        git = _json(r["git"])
        if git and (git.get("commit") or "").lower().startswith(prefix):
            out.append({"job_id": r["job_id"], "number": r["number"], "job_name": r["job_name"]})
    return out


def _export_ref(c, kind, arg, fmt, result_paths, attachments_used):
    """[[kind:arg]] -> a relative markdown/HTML link, a plain note, or a "not found" marker."""
    def link(href, label):
        return f"[{label}]({href})" if fmt == "markdown" else f'<a href="{href}">{label}</a>'
    try:
        if kind == "job":
            jid = int(arg)
            r = c.execute("SELECT name FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                raise ValueError
            return link(f"../jobs/{jid:04d}_{slug(r['name'])}/job.json", f"Job #{jid} {r['name']}")
        if kind == "attempt":
            jid, num = (int(x) for x in arg.split("/"))
            r = c.execute("SELECT name FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r or not c.execute("SELECT 1 FROM attempts WHERE job_id=? AND number=?", (jid, num)).fetchone():
                raise ValueError
            return link(f"../jobs/{jid:04d}_{slug(r['name'])}/attempt_{num}/attempt.json",
                       f"Job #{jid} {r['name']}, attempt {num}")
        if kind == "result":
            rid = int(arg)
            r = c.execute("SELECT name FROM results WHERE id=?", (rid,)).fetchone()
            if not r:
                raise ValueError
            path = result_paths.get(rid)
            return link(f"../{path}", f"Result #{rid} {r['name']}") if path else f"Result #{rid} {r['name']}"
        if kind == "metrics":
            if not arg.startswith("job:"):
                raise ValueError
            jid = int(arg[len("job:"):])
            r = c.execute("SELECT name FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                raise ValueError
            merged = _job_metrics(c, jid)
            if not merged:
                return f"*No metrics yet for job #{jid} {r['name']}.*"
            if fmt == "markdown":
                return "\n".join(["| Metric | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in merged.items()])
            rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in merged.items())
            return f"<table><tr><th>Metric</th><th>Value</th></tr>{rows}</table>"
        if kind == "git":
            if len(arg) < 4:
                raise ValueError
            matches = _find_attempts_by_commit(c, arg)
            if not matches:
                raise ValueError
            m = matches[0]
            label = f"{arg} — Job #{m['job_id']} {m['job_name']}, attempt {m['number']}"
            if len(matches) > 1:
                label += f" (+{len(matches) - 1} more)"
            return link(f"../jobs/{m['job_id']:04d}_{slug(m['job_name'])}/attempt_{m['number']}/attempt.json", label)
        if kind == "attachment":
            sha = arg.lower()
            r = c.execute("SELECT filename FROM notebook_attachments WHERE sha256=?", (sha,)).fetchone()
            if not r:
                raise ValueError
            attachments_used.add(sha)
            return link(f"attachments/{sha}_{r['filename']}", r["filename"])
    except (ValueError, IndexError):
        pass
    return f"[[{kind}:{arg}]] (not found)"


def _rewrite_notebook_body(c, fmt, body, result_paths):
    """Rewrite asset:/result: image links and [[...]] references to paths relative to notebook/."""
    assets_used, attachments_used = set(), set()

    def img_target(kind, arg):
        if kind == "asset":
            sha = arg.lower()
            a = c.execute("SELECT mime FROM notebook_assets WHERE sha256=?", (sha,)).fetchone()
            if not a:
                return None
            assets_used.add(sha)
            return f"assets/{sha}.{MIME_EXT.get(a['mime'], 'bin')}"
        try:
            path = result_paths.get(int(arg))
        except ValueError:
            return None
        return f"../{path}" if path else None

    if fmt == "markdown":
        body = MD_IMG_RE.sub(lambda m: f"{m.group(1)}{img_target(m.group(2), m.group(3)) or '#broken-link'}{m.group(4)}",
                             body)
    else:
        body = HTML_IMG_RE.sub(lambda m: f'src="{img_target(m.group(1), m.group(2)) or "#broken-link"}"', body)
    body = REF_TOKEN_RE.sub(lambda m: _export_ref(c, m.group(1), m.group(2), fmt, result_paths, attachments_used), body)
    return body, assets_used, attachments_used


def add_notebook(c, sid, files, result_paths, limit):
    entries = c.execute("SELECT * FROM notebook_entries WHERE study_id=? ORDER BY id", (sid,)).fetchall()
    if not entries:
        return
    used_assets, used_attachments, history = set(), set(), []
    for e in entries:
        body, assets_in_body, attachments_in_body = _rewrite_notebook_body(c, e["format"], e["body"], result_paths)
        used_assets |= assets_in_body
        used_attachments |= attachments_in_body
        jobs = [{"id": j["id"], "name": j["name"]} for j in c.execute(
            "SELECT j.id, j.name FROM notebook_entry_jobs ej JOIN jobs j ON j.id=ej.job_id "
            "WHERE ej.entry_id=? ORDER BY j.id", (e["id"],))]
        meta = {"title": e["title"], "kind": e["kind"], "tags": _json(e["tags"]), "created_at": e["created_at"],
                "updated_at": e["updated_at"], "archived_at": e["archived_at"], "jobs": jobs}
        front = "---\n" + "\n".join(f"{k}: {json.dumps(meta[k], sort_keys=True)}" for k in sorted(meta)) + "\n---\n\n"
        ext = "md" if e["format"] == "markdown" else "html"
        files[f"notebook/{e['id']:04d}_{slug(e['title'])}.{ext}"] = (front + body).encode("utf-8")
        for r in c.execute("SELECT * FROM notebook_revisions WHERE entry_id=? ORDER BY id", (e["id"],)):
            history.append({"entry_id": e["id"], "saved_at": r["created_at"], "title": r["title"], "kind": r["kind"],
                            "tags": _json(r["tags"]), "format": r["format"], "body": r["body"]})
    files["notebook/history.json"] = dumps(history)
    for sha in sorted(used_assets):
        a = c.execute("SELECT mime, size, data FROM notebook_assets WHERE sha256=?", (sha,)).fetchone()
        if a and a["size"] <= limit:
            files[f"notebook/assets/{sha}.{MIME_EXT.get(a['mime'], 'bin')}"] = bytes(a["data"])
    for sha in sorted(used_attachments):
        a = c.execute("SELECT filename, size, data FROM notebook_attachments WHERE sha256=?", (sha,)).fetchone()
        if a and a["size"] <= limit:
            files[f"notebook/attachments/{sha}_{a['filename']}"] = bytes(a["data"])


def build(db_path, sid, max_file_mb=20):
    """Return (folder_name, {relative_path: bytes})."""
    limit = max_file_mb * 1e6
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    s = c.execute("SELECT * FROM studies WHERE id=?", (sid,)).fetchone()
    if not s:
        raise KeyError(f"No study {sid}")
    root = f"study_{sid}_{slug(s['name'])}"
    files = {}
    files[MARKER] = b"Written by FEM Tracker's export. The whole folder is replaced on the next export.\n"
    files["README.md"] = (
        f"# {s['name']}\n\nExported from FEM Tracker. Each job has one folder per attempt; each attempt holds the\n"
        "frozen parameters and run setup, the exact HPC script, the input files and every population round of\n"
        "results. `metrics.csv` lists the current results. Re-export and commit to record changes.\n").encode()
    cls = c.execute("SELECT * FROM study_classes WHERE id=?", (s["class_id"],)).fetchone() if s["class_id"] else None
    files["study.json"] = dumps({"id": sid, "name": s["name"], "description": s["description"],
                                 "results_summary": s["results_summary"], "run_config": _json(s["run_config"]),
                                 "class": cls["name"] if cls else None})
    files["history.json"] = dumps([{"field": r["field"], "saved_at": r["created_at"], "value": _json(r["value"])}
                                   for r in c.execute("SELECT * FROM study_revisions WHERE study_id=? ORDER BY id", (sid,))])
    if cls:
        kpis = c.execute("SELECT * FROM study_class_kpis WHERE class_id=? ORDER BY position, id",
                         (cls["id"],)).fetchall()
        pins = {r["class_kpi_id"]: r for r in c.execute(
            "SELECT p.class_kpi_id, p.job_id, j.name AS job_name FROM study_kpi_pins p "
            "JOIN jobs j ON j.id=p.job_id WHERE p.study_id=?", (sid,))}
        kpi_list = []
        for k in kpis:
            pin = pins.get(k["id"])
            value = _job_metrics(c, pin["job_id"]).get(k["metric_key"]) if pin else None
            kpi_list.append({"name": k["name"], "metric_key": k["metric_key"], "unit": k["unit"],
                             "job_id": pin["job_id"] if pin else None,
                             "job_name": pin["job_name"] if pin else None, "value": value})
        files["kpis.json"] = dumps({"class": cls["name"], "kpis": kpi_list})

    result_paths = {}   # result id -> exported file path, for notebook [[result:N]] / result: image refs

    def add_results(folder, rows):
        listing, used = [], set()
        for r in rows:
            item = {"id": r["id"], "name": r["name"], "description": r["description"], "metrics": _json(r["metrics"]),
                    "created_at": r["created_at"]}
            if r["filename"]:
                item.update(filename=r["filename"], size_bytes=r["size_bytes"], sha256=r["sha256"])
                if r["blob"] is not None and r["size_bytes"] <= limit:
                    name = r["filename"] if r["filename"] not in used else f"{r['id']}_{r['filename']}"
                    used.add(name)
                    files[f"{folder}/{name}"] = bytes(r["blob"])
                    item["file"] = name
                    result_paths[r["id"]] = f"{folder}/{name}"
                else:
                    item["location"] = r["local_path"] or r["remote_path"] or "database (larger than the export limit)"
            listing.append(item)
        if listing:
            files[f"{folder}/results.json"] = dumps(listing)

    metric_rows = []
    for j in c.execute("SELECT * FROM jobs WHERE study_id=? ORDER BY id", (sid,)).fetchall():
        jdir = f"jobs/{j['id']:04d}_{slug(j['name'])}"
        files[f"{jdir}/job.json"] = dumps({"id": j["id"], "name": j["name"], "description": j["description"],
                                           "priority": j["priority"], "params": _json(j["params"]),
                                           "run_config_overrides": _json(j["run_config"])})
        add_results(f"{jdir}/notes", c.execute("SELECT * FROM results WHERE job_id=? AND attempt_id IS NULL ORDER BY id",
                                               (j["id"],)).fetchall())
        reported = None
        for a in c.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY number", (j["id"],)).fetchall():
            adir = f"{jdir}/attempt_{a['number']}"
            inputs = [dict(x) for x in c.execute(
                "SELECT i.name, i.sha256, i.size, b.data FROM attempt_inputs i LEFT JOIN input_blobs b ON b.sha256=i.sha256 "
                "WHERE i.attempt_id=? ORDER BY i.name", (a["id"],))]
            for i in inputs:
                data = i.pop("data")
                i["copied"] = data is not None and i["size"] <= limit
                if i["copied"]:
                    files[f"{adir}/inputs/{i['name']}"] = bytes(data)
            keep = ("number", "note", "source", "run_status", "populate_status", "population", "submitted_by",
                    "remote_host", "run_dir", "session_name", "queued_at", "frozen_at", "submitted_at", "started_at",
                    "finished_at", "exit_code", "run_error", "populated_at", "populate_error", "local_dir",
                    "remote_cleaned_at")
            info = {k: a[k] for k in keep}
            info.update(params=_json(a["params"]), run_config=_json(a["run_config"]), git=_json(a["git"]), inputs=inputs)
            files[f"{adir}/attempt.json"] = dumps(info)
            if a["script"]:
                files[f"{adir}/fem_run.sh"] = a["script"].encode()
            for (pop,) in c.execute("SELECT DISTINCT population FROM results WHERE attempt_id=? ORDER BY population",
                                    (a["id"],)).fetchall():
                add_results(f"{adir}/population_{pop}", c.execute(
                    "SELECT * FROM results WHERE attempt_id=? AND population=? ORDER BY id", (a["id"], pop)).fetchall())
            if a["populate_status"] in ("done", "skipped"):
                reported = a
        current = []
        if reported is not None:
            current += [(reported["number"], r) for r in c.execute(
                "SELECT * FROM results WHERE attempt_id=? AND superseded_at IS NULL ORDER BY id", (reported["id"],))]
        current += [("notes", r) for r in c.execute(
            "SELECT * FROM results WHERE job_id=? AND attempt_id IS NULL ORDER BY id", (j["id"],))]
        params = _json(reported["params"]) if reported is not None else _json(j["params"])
        for number, r in current:
            m = _json(r["metrics"]) or {}
            if m:
                metric_rows.append((j["id"], j["name"], number, r["name"], params or {}, m))

    pkeys = sorted({k for *_, p, _m in metric_rows for k in p if not isinstance(p[k], (dict, list))})
    mkeys = sorted({k for *_, m in metric_rows for k in m})
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["job_id", "job", "attempt", "result"] + [f"param:{k}" for k in pkeys] + mkeys)
    for jid, jname, number, rname, p, m in metric_rows:
        w.writerow([jid, jname, number, rname] + [p.get(k, "") for k in pkeys] + [m.get(k, "") for k in mkeys])
    files["metrics.csv"] = buf.getvalue().encode()
    add_notebook(c, sid, files, result_paths, limit)
    c.close()
    return root, files


def write_zip(db_path, sid, fileobj, max_file_mb=20):
    root, files = build(db_path, sid, max_file_mb)
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            z.writestr(f"{root}/{path}", files[path])
    return root


def write_folder(db_path, sid, out_dir, max_file_mb=20):
    """Write into out_dir/<study folder>, replacing an earlier export of the same study."""
    root, files = build(db_path, sid, max_file_mb)
    target = os.path.join(out_dir, root)
    if os.path.exists(target):
        if not os.path.isfile(os.path.join(target, MARKER)):
            raise FileExistsError(f"{target} exists and is not a FEM Tracker export; not overwriting it")
        for entry in os.listdir(target):          # keep .git etc. if someone made this folder a repository
            if entry.startswith(".git"):
                continue
            p = os.path.join(target, entry)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    for path, data in files.items():
        full = os.path.join(target, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
    return target, len(files)
