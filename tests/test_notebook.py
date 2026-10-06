"""Notebook: entries, revisions, archiving, images, the sanitizer, references and export."""
import io
import json
import os
import sqlite3
import struct
import subprocess
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from conftest import TMP, api, drive, job, make_inputs, study


def make_png(width=4, height=3, extra=b""):
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes([0]) + bytes([255, 0, 0]) * width)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"") + extra


def make_gif(width=5, height=6):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 20


def make_jpeg(width=40, height=30):
    sof = struct.pack(">H", 11) + bytes([8]) + struct.pack(">HH", height, width) + bytes([1]) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xc0" + sof + b"\xff\xd9"


def notebook_entry(sid, **kw):
    body = {"title": "Untitled", "kind": "note", "tags": [], "format": "markdown", "body": ""}
    body.update(kw)
    return api("POST", f"/api/studies/{sid}/notebook", json=body)


def test_revisions_kept_and_archive_does_not_delete():
    sid = study("Notebook revisions", TMP)["id"]
    e = notebook_entry(sid, title="First", body="v1")
    eid = e["id"]
    assert len(api("GET", f"/api/notebook/{eid}/history")) == 1
    api("PATCH", f"/api/notebook/{eid}", json={"body": "v2"})
    api("PATCH", f"/api/notebook/{eid}", json={"body": "v2"})  # unchanged: no new revision
    api("PATCH", f"/api/notebook/{eid}", json={"body": "v3", "title": "Second"})
    h = api("GET", f"/api/notebook/{eid}/history")
    assert [r["body"] for r in h] == ["v3", "v2", "v1"]
    assert h[0]["title"] == "Second"

    api("POST", f"/api/notebook/{eid}/archive")
    assert eid not in [x["id"] for x in api("GET", f"/api/studies/{sid}/notebook")]
    shown = api("GET", f"/api/studies/{sid}/notebook?include_archived=true")
    assert any(x["id"] == eid and x["archived_at"] for x in shown)
    full = api("GET", f"/api/notebook/{eid}")
    assert full["body"] == "v3" and full["archived_at"]  # archiving kept everything

    api("POST", f"/api/notebook/{eid}/restore")
    assert eid in [x["id"] for x in api("GET", f"/api/studies/{sid}/notebook")]
    assert api("GET", f"/api/notebook/{eid}")["archived_at"] is None


def test_pasting_same_image_twice_stores_one_asset():
    sid = study("Notebook images", TMP)["id"]
    png = make_png()
    a1 = api("POST", "/api/notebook/assets", files={"file": ("shot.png", png, "image/png")})
    a2 = api("POST", "/api/notebook/assets", files={"file": ("again.png", png, "image/png")})
    assert a1["sha256"] == a2["sha256"]
    assert (a1["mime"], a1["width"], a1["height"]) == ("image/png", 4, 3)
    con = sqlite3.connect(os.environ["FEM_DATA_DIR"] + "/fem_tracker.db")
    n = con.execute("SELECT COUNT(*) FROM notebook_assets WHERE sha256=?", (a1["sha256"],)).fetchone()[0]
    con.close()
    assert n == 1

    r = api("GET", f"/api/notebook/assets/{a1['sha256']}")
    assert r.content == png
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["content-type"] == "image/png"


def test_non_image_and_svg_and_oversized_are_refused():
    api("POST", "/api/notebook/assets", files={"file": ("fake.png", b"not actually a png", "image/png")}, expect=422)
    api("POST", "/api/notebook/assets", files={"file": ("x.svg", b"<svg></svg>", "image/svg+xml")}, expect=422)
    # a GIF and a JPEG with only header bytes (no real pixel data) are still accepted: we sniff the
    # format, we don't require a renderable image
    gif = api("POST", "/api/notebook/assets", files={"file": ("x.gif", make_gif(), "image/gif")})
    assert gif["mime"] == "image/gif" and (gif["width"], gif["height"]) == (5, 6)
    jpeg = api("POST", "/api/notebook/assets", files={"file": ("x.jpg", make_jpeg(), "image/jpeg")})
    assert jpeg["mime"] == "image/jpeg" and (jpeg["width"], jpeg["height"]) == (40, 30)
    # FEM_MAX_IMAGE_MB is set small in conftest for this test
    big = make_png(extra=b"\x00" * 2_000_000)
    r = api("POST", "/api/notebook/assets", files={"file": ("big.png", big, "image/png")}, expect=413)
    assert "max_image_mb" in r["detail"]


SANITIZER_CASES = [
    # (name, html body, must appear in the rendered output, must NOT appear)
    ("script tag is removed", "<script>alert(1)</script><p>safe</p>", ["safe"], ["<script", "alert(1)"]),
    ("onerror handler is removed", '<img src="/api/x" onerror="alert(1)">', [], ["onerror"]),
    ("javascript: link is removed", '<a href="javascript:alert(1)">click</a>', [], ["javascript:"]),
    ("iframe is removed", '<iframe src="https://evil.example"></iframe><p>safe</p>', ["safe"], ["<iframe"]),
    ("data: image is removed", '<img src="data:image/png;base64,AAAA">', [], ["data:image"]),
    ("a table survives", "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>",
     ["<table>", "<th>A</th>", "<td>1</td>"], []),
    ("a fenced code block survives", "<pre><code>plain text, not a [[job:1]] link</code></pre>",
     ["<pre>", "[[job:1]]"], ["<a "]),
    ("a heading survives", "<h2>Section</h2>", ["<h2>Section</h2>"], []),
    ("an allowed image survives", '<img src="/api/notebook/assets/abc" alt="x">', ['src="/api/notebook/assets/abc"'], []),
    ("a disallowed image source is dropped", '<img src="https://evil.example/x.png" alt="x">', [], ["evil.example"]),
]


def test_asset_picker_lists_pasted_images_once_saved_into_an_entry():
    sid = study("Notebook asset picker", TMP)["id"]
    other_sid = study("Another study", TMP)["id"]
    png = make_png()
    asset = api("POST", "/api/notebook/assets", files={"file": ("shot.png", png, "image/png")})
    # uploaded but not yet referenced by any saved entry: not listed yet
    assert api("GET", f"/api/studies/{sid}/notebook/assets") == []
    notebook_entry(sid, title="Has a screenshot", body=f"![x](asset:{asset['sha256']})")
    listed = api("GET", f"/api/studies/{sid}/notebook/assets")
    assert [a["sha256"] for a in listed] == [asset["sha256"]]
    assert (listed[0]["width"], listed[0]["height"], listed[0]["mime"]) == (4, 3, "image/png")
    # scoped to the study: doesn't leak into another study's picker
    assert api("GET", f"/api/studies/{other_sid}/notebook/assets") == []


def test_sanitizer_cases():
    for name, body, must_have, must_not_have in SANITIZER_CASES:
        out = api("POST", "/api/notebook/render", json={"format": "html", "body": body})["html"]
        for s in must_have:
            assert s in out, f"{name}: expected {s!r} in {out!r}"
        for s in must_not_have:
            assert s not in out, f"{name}: did not expect {s!r} in {out!r}"


def test_refs_render_as_links_missing_ref_is_a_marker_and_job_notebook_list_follows_edits():
    folder = TMP / "nb_refs"
    folder.mkdir(exist_ok=True)
    sid = study("Notebook refs", folder)["id"]
    jid = job(sid, "refjob")["id"]
    result = api("POST", f"/api/jobs/{jid}/results", data={"name": "r1", "description": "", "metrics": "{}"})

    body = f"[[job:{jid}]] [[attempt:{jid}/1]] [[result:{result['id']}]] [[job:999999]]"
    e = notebook_entry(sid, title="Refs", body=body)
    html = e["rendered_html"]
    assert f'data-kind="job" data-id="{jid}"' in html
    assert f'data-kind="attempt" data-id="{jid}/1"' in html
    assert f'data-kind="result" data-id="{result["id"]}"' in html
    assert "999999" in html and "not found" in html

    linked = [x["id"] for x in api("GET", f"/api/jobs/{jid}/notebook")]
    assert e["id"] in linked

    api("PATCH", f"/api/notebook/{e['id']}", json={"body": "nothing refers to that job now"})
    assert e["id"] not in [x["id"] for x in api("GET", f"/api/jobs/{jid}/notebook")]


def test_findings_list_is_findings_not_archived_only():
    sid = study("Notebook findings", TMP)["id"]
    f1 = notebook_entry(sid, title="A finding", kind="finding", body="x")
    notebook_entry(sid, title="Just a note", kind="note", body="x")
    f2 = notebook_entry(sid, title="Archived finding", kind="finding", body="x")
    api("POST", f"/api/notebook/{f2['id']}/archive")

    findings = api("GET", f"/api/studies/{sid}/notebook?kind=finding")
    ids = {x["id"] for x in findings}
    assert f1["id"] in ids and f2["id"] not in ids
    assert all(x["kind"] == "finding" for x in findings)


def test_export_contains_markdown_assets_rewritten_links_and_is_deterministic(tmp_path):
    from femtracker import export, settings

    folder = TMP / "nb_export"
    folder.mkdir(exist_ok=True)
    sid = study("Notebook export", folder)["id"]
    jid = job(sid, "exportjob")["id"]
    png = make_png()
    asset = api("POST", "/api/notebook/assets", files={"file": ("shot.png", png, "image/png")})
    body = f"See [[job:{jid}]].\n\n![a screenshot](asset:{asset['sha256']})\n"
    notebook_entry(sid, title="Export me", kind="finding", body=body)

    target, _ = export.write_folder(settings.DB_PATH, sid, str(tmp_path))
    files = {p: p.read_bytes() for p in Path(target).rglob("*") if p.is_file()}
    rel = {str(p.relative_to(target)) for p in files}
    md_path = next(p for p in rel if p.startswith("notebook/") and p.endswith(".md"))
    text = (Path(target) / md_path).read_text()
    assert "title: \"Export me\"" in text
    assert f"jobs/{jid:04d}_exportjob/job.json" in text
    asset_path = f"notebook/assets/{asset['sha256']}.png"
    assert asset_path in rel
    assert f"asset:{asset['sha256']}" not in text  # the asset: scheme itself must not leak into the export
    assert f"assets/{asset['sha256']}.png" in text
    history = json.loads((Path(target) / "notebook/history.json").read_text())
    assert history and history[0]["title"] == "Export me"

    export.write_folder(settings.DB_PATH, sid, str(tmp_path))
    again = {p: p.read_bytes() for p in Path(target).rglob("*") if p.is_file()}
    assert again == files


def test_git_ref_links_to_the_attempt_that_used_that_commit():
    folder = make_inputs(TMP / "nb_git")
    sid = study("Notebook git ref", folder)["id"]
    jid = job(sid, "gitjob", load_N=1000, thickness_mm=4)["id"]

    def done():
        j = api("GET", f"/api/jobs/{jid}")
        return j["stage"] in ("populated", "failed")
    drive(done)
    a = api("GET", f"/api/jobs/{jid}/attempts")[0]
    commit = a["git"]["commit"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True).stdout.strip()
    assert commit == head

    short = commit[:8]
    e = notebook_entry(sid, title="Commit ref", body=f"Ran on [[git:{short}]] and [[git:deadbeef]] (missing).")
    html = e["rendered_html"]
    assert f'data-kind="git" data-id="{jid}/{a["number"]}"' in html
    assert short in html and commit not in html  # the short prefix is shown, not the full hash
    assert "deadbeef" in html and "not found" in html


def test_attachment_zips_a_folder_dedups_and_is_referenced():
    sid = study("Notebook attachment", TMP)["id"]
    tree = {"inputs/model.inp": "*HEADING\n", "inputs/sub/mesh.inp": "*NODE\n"}
    parts = [("files", (f"myinputs/{rel}", content, "text/plain")) for rel, content in tree.items()]

    att1 = api("POST", "/api/notebook/attachments", files=parts)
    att2 = api("POST", "/api/notebook/attachments", files=parts)
    assert att1["sha256"] == att2["sha256"]
    assert att1["filename"] == "myinputs.zip"
    con = sqlite3.connect(os.environ["FEM_DATA_DIR"] + "/fem_tracker.db")
    n = con.execute("SELECT COUNT(*) FROM notebook_attachments WHERE sha256=?", (att1["sha256"],)).fetchone()[0]
    con.close()
    assert n == 1

    r = api("GET", f"/api/notebook/attachments/{att1['sha256']}")
    assert r.headers["content-type"] == "application/zip"
    assert "myinputs.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert set(z.namelist()) == {f"myinputs/{rel}" for rel in tree}
        assert z.read("myinputs/inputs/model.inp").decode() == tree["inputs/model.inp"]

    e = notebook_entry(sid, title="Has an attachment", body=f"Inputs: [[attachment:{att1['sha256']}]]")
    assert f'/api/notebook/attachments/{att1["sha256"]}' in e["rendered_html"]
    assert "myinputs.zip" in e["rendered_html"]
    full = api("GET", f"/api/notebook/{e['id']}")
    assert [a["sha256"] for a in full["attachments"]] == [att1["sha256"]]


def test_oversized_attachment_is_refused():
    # random bytes, so DEFLATE can't compress it down under the (small, test-only) limit
    parts = [("files", ("big/file.bin", os.urandom(2_000_000), "application/octet-stream"))]
    r = api("POST", "/api/notebook/attachments", files=parts, expect=413)
    assert "max_attachment_mb" in r["detail"]


LEGACY_V4_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE studies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
    results_summary TEXT DEFAULT '', run_config TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE study_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    field TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    name TEXT NOT NULL, description TEXT DEFAULT '', params TEXT DEFAULT '{}', run_config TEXT DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL, note TEXT DEFAULT '', source TEXT NOT NULL DEFAULT 'run', queued_at TEXT NOT NULL,
    params TEXT, run_config TEXT, script TEXT, session_name TEXT, git TEXT, frozen_at TEXT,
    run_status TEXT NOT NULL DEFAULT 'queued', submitted_by TEXT, remote_host TEXT, run_dir TEXT,
    submitted_at TEXT, started_at TEXT, finished_at TEXT, exit_code INTEGER, run_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0, populate_status TEXT NOT NULL DEFAULT 'pending',
    population INTEGER NOT NULL DEFAULT 0, populated_by TEXT, populated_at TEXT, populate_error TEXT,
    local_dir TEXT, remote_cleaned_at TEXT, UNIQUE (job_id, number)
);
CREATE TABLE input_blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, data BLOB NOT NULL);
CREATE TABLE attempt_inputs (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE, name TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, stored INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (attempt_id, name)
);
CREATE TABLE results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE CASCADE, population INTEGER, superseded_at TEXT,
    name TEXT NOT NULL, description TEXT DEFAULT '', metrics TEXT DEFAULT '{}', source TEXT NOT NULL DEFAULT 'manual',
    filename TEXT, content_type TEXT, size_bytes INTEGER DEFAULT 0, sha256 TEXT, blob BLOB, local_path TEXT,
    remote_path TEXT, created_at TEXT NOT NULL
);
CREATE TABLE agents (
    name TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL, message TEXT DEFAULT '',
    interval_s REAL DEFAULT 60, last_seen TEXT NOT NULL, PRIMARY KEY (name, role)
);
"""


def test_migration_from_schema_version_4_keeps_existing_data():
    import femtracker.server as server

    legacy_path = str(TMP / "legacy_v4.db")
    con = sqlite3.connect(legacy_path)
    con.executescript(LEGACY_V4_SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT INTO studies(id,name,created_at,updated_at) VALUES (1,'Old study',?,?)", (now, now))
    con.execute("INSERT INTO jobs(id,study_id,name,created_at) VALUES (1,1,'Old job',?)", (now,))
    con.execute("INSERT INTO attempts(id,job_id,number,queued_at) VALUES (1,1,1,?)", (now,))
    con.execute("INSERT INTO results(id,job_id,name,created_at) VALUES (1,1,'Old result',?)", (now,))
    con.commit()
    con.execute("PRAGMA user_version=4")
    con.commit()
    con.close()

    original_path = server.DB_PATH
    try:
        server.DB_PATH = legacy_path
        server.init_db()
        con = sqlite3.connect(legacy_path)
        con.row_factory = sqlite3.Row
        assert con.execute("PRAGMA user_version").fetchone()[0] == 5
        assert con.execute("SELECT name FROM studies WHERE id=1").fetchone()["name"] == "Old study"
        assert con.execute("SELECT name FROM jobs WHERE id=1").fetchone()["name"] == "Old job"
        assert con.execute("SELECT number FROM attempts WHERE id=1").fetchone()["number"] == 1
        assert con.execute("SELECT name FROM results WHERE id=1").fetchone()["name"] == "Old result"
        for t in ("notebook_entries", "notebook_revisions", "notebook_assets", "notebook_entry_assets",
                 "notebook_entry_jobs", "notebook_attachments", "notebook_entry_attachments",
                 "study_classes", "study_class_kpis", "study_kpi_pins"):
            assert con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone(), f"{t} missing"
        assert "class_id" in {r[1] for r in con.execute("PRAGMA table_info(studies)")}
        assert "parent_id" in {r[1] for r in con.execute("PRAGMA table_info(study_classes)")}
        con.close()
    finally:
        server.DB_PATH = original_path
