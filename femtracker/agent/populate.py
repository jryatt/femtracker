"""STEP 2 - POPULATE. Runs on your PC: brings finished runs back from the HPC into the database.

  FEMTracker.exe populate                every finished run, then exit
  FEMTracker.exe populate --loop         keep going  (FEMTracker.exe start does this for you)
  FEMTracker.exe populate --job 42       populate job 42's latest attempt again (a new round;
                                         earlier rounds are kept)
  FEMTracker.exe populate --job 42 --local-dir "C:\\old_runs\\run_3"
                                         import a folder you downloaded yourself as a new attempt

For each finished attempt:
  1. list the run setup's "retrieve" patterns in the remote run folder (fails if a pattern matches nothing)
  2. pscp them to LOCAL_RESULTS_ROOT/study_<s>/job_<j>/attempt_<n>/population_<k>, plus run.log
  3. store them: files up to MAX_BLOB_MB as blobs, bigger ones as a path (see LARGE_FILES)
     and metrics from manifest.json, metrics.json or key/value lines in text results (extract_metrics)
  4. if the run setup says so, delete the remote run folder - only after everything above succeeded
"""
import argparse
import glob
import json
import logging
import os
import re
import shutil
import threading
import traceback

from femtracker import settings as config
from femtracker.agent import api, remote, run_agent
from femtracker.agent.remote import q

TEXT_EXT = (".txt", ".csv", ".dat", ".out", ".log")
NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


log = logging.getLogger("femtracker.populate").info


def extract_metrics(local_dir, files):
    """Hook: turn result files into scalar metrics. Edit this for your post-processing output.

    Default: metrics.json if present, otherwise every line such as
    'Max_Mises = 212.4', 'max_disp: 0.83', 'Reaction_Force,1520' or 'U_max 0.83'
    in the retrieved text files (Post_Results.txt and friends)."""
    mj = os.path.join(local_dir, "metrics.json")
    if os.path.isfile(mj):
        return json.load(open(mj))
    metrics = {}
    for f in files:
        if not f.lower().endswith(TEXT_EXT) or os.path.basename(f) == "run.log" or os.path.getsize(f) > 5e6:
            continue
        for line in open(f, errors="replace"):
            m = re.match(rf"^\s*([A-Za-z_][\w .()/\[\]-]*?)\s*(?:[=:,;\t]|\s)\s*({NUM})\s*$", line)
            if m:
                metrics[m.group(1).strip()] = float(m.group(2))
    return metrics


def list_remote(d, patterns):
    """Return {pattern: [(size, relative_path), ...]} for the remote run folder."""
    lines = [f"cd {q(d)} || {{ echo 'NO_DIR'; exit 0; }}", "shopt -s nullglob"]
    for i, p in enumerate(patterns):
        # patterns stay unquoted so the remote shell expands the wildcards
        lines.append(f'echo "### {i}"; for f in {p}; do [ -f "$f" ] && printf "%s\\t%s\\n" "$(wc -c < "$f" | tr -d " ")" "$f"; done')
    out = remote.run("\n".join(lines))
    if out.strip() == "NO_DIR":
        raise FileNotFoundError(f"Remote run folder is gone: {d}")
    found, cur = {p: [] for p in patterns}, None
    for line in out.splitlines():
        if line.startswith("### "):
            cur = patterns[int(line[4:])]
        elif cur is not None and "\t" in line:
            size, rel = line.split("\t", 1)
            found[cur].append((int(size), rel))
    return found


def fetch(a, spec, local):
    """Download the retrieve patterns. Returns [(local_path_or_None, rel, size, remote_path)]."""
    d = a["run_dir"]
    patterns = spec["retrieve"] + ["run.log"]
    found = list_remote(d, patterns)
    missing = [p for p in spec["retrieve"] if not found[p]]
    if missing:
        raise FileNotFoundError(f"Nothing in {d} matches: {', '.join(missing)}")
    items, seen = [], set()
    for p in patterns:
        for size, rel in found[p]:
            if rel in seen:
                continue
            seen.add(rel)
            rpath = f"{d}/{rel}"
            big = size > config.MAX_BLOB_MB * 1e6
            if big and config.LARGE_FILES == "remote" and not spec["cleanup"]:
                items.append((None, rel, size, rpath))  # leave it on the HPC, store a pointer
                continue
            dest = os.path.join(local, os.path.dirname(rel))
            log(f"  downloading {rel} ({size / 1e6:.1f} MB)")
            remote.download(rpath, dest)
            items.append((os.path.join(dest, os.path.basename(rel)), rel, size, rpath))
    return items


def scan_local(local):
    """--local-dir mode: everything already in the folder."""
    items = []
    for f in sorted(glob.glob(os.path.join(local, "**", "*"), recursive=True)):
        if os.path.isfile(f):
            items.append((f, os.path.relpath(f, local).replace("\\", "/"), os.path.getsize(f), None))
    return items


def store(a, items, local):
    aid = a["id"]
    by_rel = {rel: (path, size, rpath) for path, rel, size, rpath in items}

    def put(name, rel, description="", metrics=None):
        path, size, rpath = by_rel[rel]
        if path is None:
            api.add_result(aid, name, description, metrics, remote_path=rpath, size_bytes=size)
        elif size > config.MAX_BLOB_MB * 1e6:
            api.add_result(aid, name, description, metrics, local_path=os.path.abspath(path), size_bytes=size)
        else:
            api.add_result(aid, name, description, metrics, path=path)

    manifest = by_rel.get("manifest.json")
    if manifest and manifest[0]:
        for entry in json.load(open(manifest[0]))["results"]:
            if entry.get("file") and entry["file"] not in by_rel:
                raise FileNotFoundError(f"manifest.json lists {entry['file']}, which was not retrieved")
            if entry.get("file"):
                put(entry["name"], entry["file"], entry.get("description", ""), entry.get("metrics"))
            else:
                api.add_result(aid, entry["name"], entry.get("description", ""), entry.get("metrics"))
        return

    local_files = [p for p, _, _, _ in items if p]
    log_tail = ""
    if "run.log" in by_rel and by_rel["run.log"][0]:
        log_tail = "".join(open(by_rel["run.log"][0], errors="replace").readlines()[-15:])
    api.add_result(aid, "Run summary", f"Last lines of run.log:\n{log_tail}" if log_tail else "",
                   extract_metrics(local, local_files))
    for _, rel, _, _ in items:
        put("Run log" if rel == "run.log" else rel, rel)


def label(a):
    return f"job {a['job_id']} attempt {a['number']}"


def cleanup(a):
    d = a["run_dir"] or ""
    root = config.REMOTE_WORKDIR.rstrip("/") + "/"
    if not (d.startswith(root) and re.search(r"/study_\d+/job_\d+/attempt_\d+$", d)):
        log(f"  not deleting {d}: outside REMOTE_WORKDIR")
        return
    remote.run(f"rm -rf {q(d)}")
    api.set_population(a["id"], remote_cleaned=True)
    log(f"  deleted remote folder {d}")


def populate_attempt(a, local_dir=None):
    spec = a["spec"]
    if local_dir:
        local, items = local_dir, scan_local(local_dir)
    else:  # every population round gets its own folder; earlier rounds are left untouched
        local = os.path.join(config.LOCAL_RESULTS_ROOT, f"study_{a['study_id']}", f"job_{a['job_id']}",
                             f"attempt_{a['number']}", f"population_{a['population']}")
        if os.path.isdir(local):
            shutil.rmtree(local)
        os.makedirs(local)
        items = fetch(a, spec, local)
    store(a, items, local)
    api.set_population(a["id"], "done", local_dir=os.path.abspath(local))
    log(f"{label(a)}: populated ({len(items)} files, round {a['population']})")
    if spec["cleanup"] and not local_dir:
        try:
            cleanup(a)
        except Exception as e:
            log(f"  could not delete the remote folder: {e}")


def run_once(limit, attempt_id=None, local_dir=None):
    attempts = api.call("POST", "/api/populate/claim",
                        json={"agent": config.AGENT_NAME, "limit": limit, "attempt_id": attempt_id})
    for a in attempts:
        try:
            populate_attempt(a, local_dir)
        except Exception as e:
            api.set_population(a["id"], "failed", f"{e}\n\n{traceback.format_exc()[-1500:]}")
            log(f"{label(a)}: population failed: {e}")
    return len(attempts)


def latest_attempt(job_id, finished=True):
    for a in api.call("GET", f"/api/jobs/{job_id}/attempts"):  # newest first
        if not finished or a["run_status"] == "finished":
            return a
    return None


def import_folder(job_id, local_dir, note=""):
    """Record results produced outside FEM Tracker as a new, imported attempt of the job."""
    if not os.path.isdir(local_dir):
        raise SystemExit(f"Folder not found: {local_dir}")
    a = api.call("POST", f"/api/jobs/{job_id}/attempts",
                 json={"note": note or f"Imported from {local_dir}", "imported": True})
    files = [p for p, *_ in scan_local(local_dir)]
    inputs = [{"name": os.path.relpath(p, local_dir).replace("\\", "/"), "sha256": run_agent.sha256_file(p),
               "size": os.path.getsize(p)} for p in files]
    api.record_provenance(a["id"], inputs, run_agent.git_state(local_dir), {})
    if not run_once(1, a["id"], local_dir):
        raise SystemExit("Could not populate the imported attempt")
    return a


def loop(stop: threading.Event = None, limit=10):
    stop = stop or threading.Event()
    logger = logging.getLogger("femtracker.populate")
    while not stop.is_set():
        problems = config.hpc_problems()
        if problems:
            api.report("populate", "not_configured", "; ".join(problems))
        else:
            try:
                while not stop.is_set() and run_once(limit) == limit:
                    pass
                api.report("populate", "ok", config.LOCAL_RESULTS_ROOT)
            except Exception as e:
                if stop.is_set():  # shutting down; the server is already gone
                    break
                logger.warning(f"pass failed, retrying in {config.POLL_SECONDS:.0f}s: {e}")
                api.report("populate", "error", str(e))
        stop.wait(config.POLL_SECONDS)
    api.report("populate", "stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="FEMTracker.exe populate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", type=int, help="populate this job's latest finished attempt again")
    ap.add_argument("--local-dir", help="with --job: import results from this folder as a new attempt")
    ap.add_argument("--import-run", action="store_true", help="kept for compatibility; implied by --local-dir")
    ap.add_argument("--note", default="", help="with --local-dir: why this attempt exists")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args(argv)
    if a.local_dir and a.job is None:
        ap.error("--local-dir needs --job")
    problems = [] if a.local_dir else config.hpc_problems()
    if problems:
        raise SystemExit("Not configured yet:\n  " + "\n  ".join(problems) + f"\nEdit {config.CONFIG_PATH}")
    if a.job is not None:
        if a.local_dir:
            att = import_folder(a.job, a.local_dir, a.note)
            print(f"job {a.job}: imported as attempt {att['number']}")
            return
        att = latest_attempt(a.job)
        if not att:
            raise SystemExit(f"job {a.job}: no finished attempt to populate")
        api.set_population(att["id"], "pending")
        run_once(1, att["id"])
        return
    log(f"populate agent '{config.AGENT_NAME}' -> {config.LOCAL_RESULTS_ROOT}")
    if a.loop:
        try:
            loop(limit=a.limit)
        except KeyboardInterrupt:
            pass
    else:
        while run_once(a.limit) == a.limit:
            pass
