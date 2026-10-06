"""FEM Tracker web API and database.

Versioning model
  study     description and conclusions; every edit is kept (study_revisions)
  job       what to compute: name, parameters, run setup overrides
  attempt   one run of a job. When it is handed to the HPC it freezes a snapshot of the
            parameters, effective run setup and generated script, and records the input files
            (content-addressed, deduplicated) and the git state of the input folder.
            Running a job again creates a new attempt; old attempts are never overwritten.
  results   belong to an attempt and a population round. Populating again supersedes the
            previous round instead of deleting it. Hand-added results belong to the job.
  notebook  entries belong to a study (notebook_entries); every save keeps a full snapshot
            (notebook_revisions). "Deleting" one sets archived_at instead of removing it.
            Pasted/uploaded images are content-addressed (notebook_assets, by sha256, like
            input_blobs); references to jobs, attempts, results and assets found in an
            entry's body are re-parsed and stored (notebook_entry_jobs, notebook_entry_assets)
            on every save, so "notebook entries about this job" stays accurate.

Each attempt moves through two independent steps:
  1. RUN       queued -> submitting -> submitted -> running -> finished | failed | cancelled
  2. POPULATE  pending -> populating -> done | failed | skipped
"""
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from femtracker import __version__, notebook, settings

DB_PATH = settings.DB_PATH
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
RUN_STATUSES = ("queued", "submitting", "submitted", "running", "finished", "failed", "cancelled")
ACTIVE = ("queued", "submitting", "submitted", "running")
POP_STATUSES = ("pending", "populating", "done", "failed", "skipped")
NOTEBOOK_KINDS = ("note", "finding", "decision")
SCHEMA_VERSION = 5

RUN_CONFIG_KEYS = ("local_dir", "inputs", "remote_inputs", "commands", "retrieve", "cleanup", "cancel_commands")
DEFAULT_RUN_CONFIG = {"local_dir": "", "inputs": [], "remote_inputs": [], "commands": [], "retrieve": [],
                      "cleanup": False, "cancel_commands": []}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- A reusable template: a named kind of study (e.g. "Fatigue Analysis") and the KPIs every
-- study of that kind should report, so results compare consistently across studies.
CREATE TABLE IF NOT EXISTS study_classes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    parent_id   INTEGER REFERENCES study_classes(id) ON DELETE SET NULL,  -- single-parent inheritance
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_class_kpis (     -- the KPIs a class expects, in display order
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id   INTEGER NOT NULL REFERENCES study_classes(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,                     -- e.g. "Max Stress"
    metric_key TEXT NOT NULL,                     -- the metrics field it expects, e.g. "Max_Mises"
    unit       TEXT DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_kpis ON study_class_kpis(class_id, position);

CREATE TABLE IF NOT EXISTS study_kpi_pins (       -- which job (in a study of that class) supplies each KPI
    study_id     INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    class_kpi_id INTEGER NOT NULL REFERENCES study_class_kpis(id) ON DELETE CASCADE,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (study_id, class_kpi_id)
);

-- A reusable run setup template for one kind of job: Abaqus, LS-DYNA, a shell pipeline, etc. A
-- study picks one and its run setup only needs to say what's different (inputs, local folder);
-- commands and the rest fall back to the run type, the same way a job's setup falls back to its study's.
CREATE TABLE IF NOT EXISTS run_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    run_config  TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    results_summary TEXT DEFAULT '',
    run_config      TEXT DEFAULT '{}',
    class_id        INTEGER REFERENCES study_classes(id) ON DELETE SET NULL,
    run_type_id     INTEGER REFERENCES run_types(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_revisions (      -- every saved version of the texts and run setup
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,                     -- description, results_summary, run_config, name
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rev_study ON study_revisions(study_id, field, id);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    params      TEXT DEFAULT '{}',   -- parameters for the next attempt
    run_config  TEXT DEFAULT '{}',   -- run setup overrides for the next attempt
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_study ON jobs(study_id);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    number           INTEGER NOT NULL,
    note             TEXT DEFAULT '',     -- why this attempt was made
    source           TEXT NOT NULL DEFAULT 'run',   -- 'run' or 'import'
    queued_at        TEXT NOT NULL,

    -- frozen when the attempt is handed to the HPC (or imported)
    params           TEXT,
    run_config       TEXT,                -- effective run setup
    script           TEXT,                -- fem_run.sh exactly as uploaded
    session_name     TEXT,
    git              TEXT,                -- JSON: commit, branch, dirty, remote, path
    frozen_at        TEXT,

    -- step 1: run
    run_status       TEXT NOT NULL DEFAULT 'queued'
                     CHECK (run_status IN ('queued','submitting','submitted','running','finished','failed','cancelled')),
    submitted_by     TEXT,
    remote_host      TEXT,
    run_dir          TEXT,
    submitted_at     TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    exit_code        INTEGER,
    run_error        TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,

    -- step 2: populate
    populate_status  TEXT NOT NULL DEFAULT 'pending'
                     CHECK (populate_status IN ('pending','populating','done','failed','skipped')),
    population       INTEGER NOT NULL DEFAULT 0,   -- how many times it has been populated
    populated_by     TEXT,
    populated_at     TEXT,
    populate_error   TEXT,
    local_dir        TEXT,
    remote_cleaned_at TEXT,
    UNIQUE (job_id, number)
);
CREATE INDEX IF NOT EXISTS idx_att_run ON attempts(run_status, id);
CREATE INDEX IF NOT EXISTS idx_att_pop ON attempts(run_status, populate_status, id);

CREATE TABLE IF NOT EXISTS input_blobs (          -- input files, stored once per content
    sha256 TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    data   BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS attempt_inputs (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,                     -- file name as uploaded, or the remote path if remote_ref
    sha256     TEXT NOT NULL,                     -- for a remote_ref, hashed on the HPC, never downloaded
    size       INTEGER NOT NULL,
    stored     INTEGER NOT NULL DEFAULT 0,        -- 1 if a copy is in input_blobs
    remote_ref INTEGER NOT NULL DEFAULT 0,        -- 1 if this was already on the HPC (symlinked, not uploaded)
    PRIMARY KEY (attempt_id, name)
);

CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_id    INTEGER REFERENCES attempts(id) ON DELETE CASCADE,  -- NULL for hand-added results
    population    INTEGER,                        -- population round within the attempt
    superseded_at TEXT,                           -- set when a newer population replaced it
    name          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    metrics       TEXT DEFAULT '{}',
    source        TEXT NOT NULL DEFAULT 'manual', -- 'populate' or 'manual'
    filename      TEXT,
    content_type  TEXT,
    size_bytes    INTEGER DEFAULT 0,
    sha256        TEXT,
    blob          BLOB,
    local_path    TEXT,
    remote_path   TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id);
CREATE INDEX IF NOT EXISTS idx_results_att ON results(attempt_id, superseded_at);

CREATE TABLE IF NOT EXISTS agents (
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    state      TEXT NOT NULL,
    message    TEXT DEFAULT '',
    interval_s REAL DEFAULT 60,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (name, role)
);

-- Added in schema version 5. Existing databases keep every table and row above; this only adds tables.
CREATE TABLE IF NOT EXISTS notebook_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note','finding','decision')),
    tags        TEXT DEFAULT '[]',
    format      TEXT NOT NULL DEFAULT 'markdown' CHECK (format IN ('markdown','html')),
    body        TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notebook_study ON notebook_entries(study_id, archived_at, id);

CREATE TABLE IF NOT EXISTS notebook_revisions (   -- one full snapshot per save, newest has the highest id
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    tags       TEXT NOT NULL,
    format     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notebook_rev ON notebook_revisions(entry_id, id);

CREATE TABLE IF NOT EXISTS notebook_assets (      -- pasted/uploaded images, content-addressed
    sha256     TEXT PRIMARY KEY,
    mime       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notebook_entry_assets (  -- re-derived from the body on every save
    entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
    sha256   TEXT NOT NULL REFERENCES notebook_assets(sha256) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, sha256)
);

CREATE TABLE IF NOT EXISTS notebook_entry_jobs (    -- re-derived from the body on every save
    entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_ej_job ON notebook_entry_jobs(job_id);

CREATE TABLE IF NOT EXISTS notebook_attachments (  -- a zipped folder attached to an entry, content-addressed
    sha256     TEXT PRIMARY KEY,
    filename   TEXT NOT NULL,
    size       INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notebook_entry_attachments (  -- re-derived from the body on every save
    entry_id INTEGER NOT NULL REFERENCES notebook_entries(id) ON DELETE CASCADE,
    sha256   TEXT NOT NULL REFERENCES notebook_attachments(sha256) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, sha256)
);
"""

STAGE_SQL = """CASE
  WHEN a.id IS NULL THEN 'queued'
  WHEN a.run_status='cancelled' THEN 'cancelled'
  WHEN a.run_status='failed' OR a.populate_status='failed' THEN 'failed'
  WHEN a.run_status='queued' THEN 'queued'
  WHEN a.run_status IN ('submitting','submitted','running') THEN 'on_cluster'
  WHEN a.populate_status IN ('done','skipped') THEN 'populated'
  ELSE 'awaiting_population' END"""
STAGES = ("queued", "on_cluster", "awaiting_population", "populated", "failed", "cancelled")
LATEST = "(SELECT id FROM attempts WHERE job_id=j.id ORDER BY number DESC LIMIT 1)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def txn(c):
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


OldSchema = settings.OldSchema


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        has_jobs = c.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
        if has_jobs and version < 4:
            raise OldSchema(f"{DB_PATH} was made by an older FEM Tracker. Move it aside and start again.")
        # version 4 -> 5 only adds tables (CREATE TABLE IF NOT EXISTS below), so existing data is untouched.
        c.executescript(SCHEMA)
        # study_classes/class_id were added after 5 went out; CREATE TABLE IF NOT EXISTS above only
        # covers a brand new studies table, so an existing one needs the column added explicitly.
        cols = {r[1] for r in c.execute("PRAGMA table_info(studies)")}
        if "class_id" not in cols:
            c.execute("ALTER TABLE studies ADD COLUMN class_id INTEGER REFERENCES study_classes(id) ON DELETE SET NULL")
        if "run_type_id" not in cols:
            c.execute("ALTER TABLE studies ADD COLUMN run_type_id INTEGER REFERENCES run_types(id) ON DELETE SET NULL")
        class_cols = {r[1] for r in c.execute("PRAGMA table_info(study_classes)")}
        if "parent_id" not in class_cols:
            c.execute("ALTER TABLE study_classes ADD COLUMN parent_id INTEGER "
                     "REFERENCES study_classes(id) ON DELETE SET NULL")
        input_cols = {r[1] for r in c.execute("PRAGMA table_info(attempt_inputs)")}
        if "remote_ref" not in input_cols:
            c.execute("ALTER TABLE attempt_inputs ADD COLUMN remote_ref INTEGER NOT NULL DEFAULT 0")
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


JSON_FIELDS = ("params", "metrics", "run_config", "run_type_run_config", "study_run_config", "git", "job_params",
              "job_run_config", "tags")


def row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_FIELDS:
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    d.pop("blob", None)
    has_run_type = "run_type_run_config" in d
    run_type_cfg = d.pop("run_type_run_config", None)
    if "study_run_config" in d:
        d["effective_run_config"] = effective(run_type_cfg, d.pop("study_run_config"), d.get("run_config") or d.get("job_run_config"))
    elif has_run_type:
        d["effective_run_config"] = effective(run_type_cfg, d.get("run_config"))
    return d


def effective(*cfgs):
    """Later configs override earlier ones: run type, then study, then job (each optional)."""
    out = dict(DEFAULT_RUN_CONFIG)
    for cfg in cfgs:
        if cfg:
            out.update(cfg)
    return out


def clean_run_config(cfg: dict) -> dict:
    unknown = set(cfg) - set(RUN_CONFIG_KEYS)
    if unknown:
        raise HTTPException(422, f"Unknown run setup keys: {sorted(unknown)}")
    for k in ("inputs", "remote_inputs", "commands", "retrieve", "cancel_commands"):
        if k in cfg and not (isinstance(cfg[k], list) and all(isinstance(x, str) for x in cfg[k])):
            raise HTTPException(422, f"{k} must be a list of strings")
    for p in cfg.get("remote_inputs", []):
        if p.strip() and not p.strip().startswith("/"):
            raise HTTPException(422, f"remote_inputs must be absolute paths already on the HPC, not '{p}'")
    return cfg


def parse_json(text: Optional[str], field: str) -> str:
    try:
        return json.dumps(json.loads(text or "{}"))
    except json.JSONDecodeError:
        raise HTTPException(422, f"{field} must be valid JSON")


app = FastAPI(title="FEM Tracker", version=__version__)
init_db()


@app.get("/api/health")
def health():
    return {"app": "FEM Tracker", "version": __version__}


# ---------- Agent status ----------
class AgentReport(BaseModel):
    name: str
    role: str
    state: str
    message: str = ""
    interval_s: float = 60


@app.post("/api/agents")
def report_agent(r: AgentReport):
    with db() as c:
        c.execute("INSERT INTO agents(name,role,state,message,interval_s,last_seen) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(name,role) DO UPDATE SET state=excluded.state, message=excluded.message, "
                  "interval_s=excluded.interval_s, last_seen=excluded.last_seen",
                  (r.name, r.role, r.state, r.message[:2000], r.interval_s, now()))
    return {"ok": True}


@app.get("/api/agents")
def list_agents():
    out = []
    with db() as c:
        for r in c.execute("SELECT * FROM agents ORDER BY role, name"):
            d = dict(r)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["last_seen"])).total_seconds()
            if age > 86400:
                continue
            d["stale"] = d["state"] != "stopped" and age > 3 * d["interval_s"] + 30
            out.append(d)
    return out


# ---------- Studies ----------
class StudyIn(BaseModel):
    name: str
    description: str = ""
    results_summary: str = ""
    run_config: dict = {}


class StudyPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    results_summary: Optional[str] = None
    run_config: Optional[dict] = None


STUDY_COUNTS = "SELECT s.*, sc.name AS class_name, rt.name AS run_type_name, rt.run_config AS run_type_run_config, " \
    "COUNT(j.id) AS job_count, " + ", ".join(f"SUM(({STAGE_SQL})='{st}') AS {st}" for st in STAGES) + \
    f" FROM studies s LEFT JOIN study_classes sc ON sc.id=s.class_id LEFT JOIN run_types rt ON rt.id=s.run_type_id " \
    f"LEFT JOIN jobs j ON j.study_id = s.id LEFT JOIN attempts a ON a.id={LATEST}"


def add_revision(c, sid, field, value):
    last = c.execute("SELECT value FROM study_revisions WHERE study_id=? AND field=? ORDER BY id DESC LIMIT 1",
                     (sid, field)).fetchone()
    if last is None or last["value"] != value:
        c.execute("INSERT INTO study_revisions(study_id,field,value,created_at) VALUES (?,?,?,?)", (sid, field, value, now()))


@app.get("/api/studies")
def list_studies():
    with db() as c:
        return [row(r) for r in c.execute(STUDY_COUNTS + " GROUP BY s.id ORDER BY s.updated_at DESC").fetchall()]


@app.post("/api/studies", status_code=201)
def create_study(s: StudyIn):
    t = now()
    cfg = json.dumps(clean_run_config(s.run_config), sort_keys=True)
    with db() as c, txn(c):
        cur = c.execute("INSERT INTO studies(name,description,results_summary,run_config,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?)", (s.name, s.description, s.results_summary, cfg, t, t))
        sid = cur.lastrowid
        for f, v in (("name", s.name), ("description", s.description), ("results_summary", s.results_summary),
                     ("run_config", cfg)):
            if v not in ("", "{}"):
                add_revision(c, sid, f, v)
    return get_study(sid)


@app.get("/api/studies/{sid}")
def get_study(sid: int):
    with db() as c:
        r = c.execute(STUDY_COUNTS + " WHERE s.id=? GROUP BY s.id", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Study not found")
    return row(r)


@app.patch("/api/studies/{sid}")
def update_study(sid: int, p: StudyPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if "run_config" in fields:
        fields["run_config"] = json.dumps(clean_run_config(fields["run_config"]), sort_keys=True)
    if fields:
        with db() as c, txn(c):
            for k, v in fields.items():
                add_revision(c, sid, k, v)
            fields["updated_at"] = now()
            c.execute(f"UPDATE studies SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), sid))
    return get_study(sid)


@app.get("/api/studies/{sid}/history")
def study_history(sid: int, field: Optional[str] = None):
    q, args = "SELECT * FROM study_revisions WHERE study_id=?", [sid]
    if field:
        q += " AND field=?"; args.append(field)
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id DESC", args)]


@app.delete("/api/studies/{sid}", status_code=204)
def delete_study(sid: int):
    with db() as c:
        c.execute("DELETE FROM studies WHERE id=?", (sid,))
    return Response(status_code=204)


# ---------- Run types: reusable run setups for a kind of job (Abaqus, LS-DYNA, a shell script...) ----------
# A study's run setup falls back to its run type's, the same way a job's run setup falls back to its study's.
class RunTypeIn(BaseModel):
    name: str
    description: str = ""
    run_config: dict = {}


class RunTypePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    run_config: Optional[dict] = None


RUN_TYPE_SELECT = "SELECT rt.*, COUNT(s.id) AS study_count FROM run_types rt LEFT JOIN studies s ON s.run_type_id=rt.id"


@app.get("/api/run-types")
def list_run_types():
    with db() as c:
        return [row(r) for r in c.execute(RUN_TYPE_SELECT + " GROUP BY rt.id ORDER BY rt.name").fetchall()]


@app.post("/api/run-types", status_code=201)
def create_run_type(t: RunTypeIn):
    cfg = json.dumps(clean_run_config(t.run_config), sort_keys=True)
    ts = now()
    with db() as c, txn(c):
        cur = c.execute("INSERT INTO run_types(name,description,run_config,created_at,updated_at) VALUES (?,?,?,?,?)",
                        (t.name, t.description, cfg, ts, ts))
        tid = cur.lastrowid
    return get_run_type(tid)


@app.get("/api/run-types/{tid}")
def get_run_type(tid: int):
    with db() as c:
        r = c.execute(RUN_TYPE_SELECT + " WHERE rt.id=? GROUP BY rt.id", (tid,)).fetchone()
    if not r:
        raise HTTPException(404, "Run type not found")
    return row(r)


@app.patch("/api/run-types/{tid}")
def update_run_type(tid: int, p: RunTypePatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if "run_config" in fields:
        fields["run_config"] = json.dumps(clean_run_config(fields["run_config"]), sort_keys=True)
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM run_types WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "Run type not found")
        if fields:
            fields["updated_at"] = now()
            c.execute(f"UPDATE run_types SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), tid))
    return get_run_type(tid)


@app.delete("/api/run-types/{tid}", status_code=204)
def delete_run_type(tid: int):
    with db() as c:
        c.execute("DELETE FROM run_types WHERE id=?", (tid,))
    return Response(status_code=204)


class StudyRunTypeAssign(BaseModel):
    run_type_id: Optional[int] = None


@app.post("/api/studies/{sid}/run-type")
def set_study_run_type(sid: int, body: StudyRunTypeAssign):
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM studies WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Study not found")
        if body.run_type_id is not None and not c.execute(
                "SELECT 1 FROM run_types WHERE id=?", (body.run_type_id,)).fetchone():
            raise HTTPException(404, "Run type not found")
        c.execute("UPDATE studies SET run_type_id=?, updated_at=? WHERE id=?", (body.run_type_id, now(), sid))
    return get_study(sid)


REPORTED_ATTEMPT = ("(SELECT id FROM attempts WHERE job_id=j.id AND populate_status IN ('done','skipped') "
                    "ORDER BY number DESC LIMIT 1)")


# ---------- Study classes: reusable KPI templates, for consistent reporting across studies ----------
# Single-parent inheritance, like a Python class: a class's "effective" KPIs are its own plus every
# ancestor's, walked from itself up to the root. A KPI defined here shadows an ancestor's KPI of the
# same metric_key, the same way overriding an attribute shadows the inherited one.
class StudyClassIn(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class StudyClassPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class KpiIn(BaseModel):
    name: str
    metric_key: str
    unit: str = ""


class KpiPatch(BaseModel):
    name: Optional[str] = None
    metric_key: Optional[str] = None
    unit: Optional[str] = None


def ancestor_chain(c, cid):
    """[cid, parent, grandparent, ...] up to the root. Guards against a cycle ever looping forever."""
    chain, seen, current = [], set(), cid
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        r = c.execute("SELECT parent_id FROM study_classes WHERE id=?", (current,)).fetchone()
        current = r[0] if r else None
    return chain


def effective_kpis(c, cid):
    """This class's own KPIs plus everything inherited, closest definition of a metric_key wins."""
    seen_keys, out = set(), []
    for class_id in ancestor_chain(c, cid):
        cls = c.execute("SELECT name FROM study_classes WHERE id=?", (class_id,)).fetchone()
        for k in c.execute("SELECT * FROM study_class_kpis WHERE class_id=? ORDER BY position, id", (class_id,)):
            if k["metric_key"] in seen_keys:
                continue  # a closer (more specific) class already defines this metric key
            seen_keys.add(k["metric_key"])
            d = dict(k)
            d["source_class_id"] = class_id
            d["source_class_name"] = cls["name"]
            d["inherited"] = class_id != cid
            out.append(d)
    return out


def get_study_class(cid: int):
    with db() as c:
        r = c.execute("SELECT * FROM study_classes WHERE id=?", (cid,)).fetchone()
        if not r:
            raise HTTPException(404, "Study class not found")
        d = row(r)
        d["kpis"] = [dict(x) for x in c.execute(
            "SELECT * FROM study_class_kpis WHERE class_id=? ORDER BY position, id", (cid,))]
        d["effective_kpis"] = effective_kpis(c, cid)
        parent = c.execute("SELECT name FROM study_classes WHERE id=?", (d["parent_id"],)).fetchone() \
            if d["parent_id"] else None
        d["parent_name"] = parent["name"] if parent else None
        d["children"] = [dict(x) for x in c.execute(
            "SELECT id, name FROM study_classes WHERE parent_id=? ORDER BY name", (cid,))]
        d["study_count"] = c.execute("SELECT COUNT(*) FROM studies WHERE class_id=?", (cid,)).fetchone()[0]
    return d


@app.get("/api/study-classes")
def list_study_classes():
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM study_classes ORDER BY name")]
    return [get_study_class(i) for i in ids]


@app.post("/api/study-classes", status_code=201)
def create_study_class(s: StudyClassIn):
    t = now()
    with db() as c, txn(c):
        if s.parent_id is not None and not c.execute(
                "SELECT 1 FROM study_classes WHERE id=?", (s.parent_id,)).fetchone():
            raise HTTPException(404, "Parent class not found")
        cur = c.execute("INSERT INTO study_classes(name,description,parent_id,created_at,updated_at) "
                        "VALUES (?,?,?,?,?)", (s.name, s.description, s.parent_id, t, t))
        cid = cur.lastrowid
    return get_study_class(cid)


@app.get("/api/study-classes/{cid}")
def study_class_detail(cid: int):
    return get_study_class(cid)


@app.patch("/api/study-classes/{cid}")
def update_study_class(cid: int, p: StudyClassPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if fields:
        fields["updated_at"] = now()
        with db() as c:
            if not c.execute("SELECT 1 FROM study_classes WHERE id=?", (cid,)).fetchone():
                raise HTTPException(404, "Study class not found")
            c.execute(f"UPDATE study_classes SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                      (*fields.values(), cid))
    return get_study_class(cid)


class ClassParentAssign(BaseModel):
    parent_id: Optional[int] = None


@app.post("/api/study-classes/{cid}/parent")
def set_class_parent(cid: int, body: ClassParentAssign):
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM study_classes WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "Study class not found")
        if body.parent_id is not None:
            if not c.execute("SELECT 1 FROM study_classes WHERE id=?", (body.parent_id,)).fetchone():
                raise HTTPException(404, "Parent class not found")
            if cid in ancestor_chain(c, body.parent_id):
                raise HTTPException(422, "That would create a cycle: a class can't inherit from itself "
                                         "or from a class that already inherits from it.")
        c.execute("UPDATE study_classes SET parent_id=?, updated_at=? WHERE id=?", (body.parent_id, now(), cid))
    return get_study_class(cid)


@app.delete("/api/study-classes/{cid}", status_code=204)
def delete_study_class(cid: int):
    with db() as c:
        c.execute("DELETE FROM study_classes WHERE id=?", (cid,))
    return Response(status_code=204)


@app.post("/api/study-classes/{cid}/kpis", status_code=201)
def add_class_kpi(cid: int, k: KpiIn):
    with db() as c:
        if not c.execute("SELECT 1 FROM study_classes WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "Study class not found")
        pos = c.execute("SELECT COALESCE(MAX(position),-1)+1 FROM study_class_kpis WHERE class_id=?",
                        (cid,)).fetchone()[0]
        c.execute("INSERT INTO study_class_kpis(class_id,name,metric_key,unit,position,created_at) "
                  "VALUES (?,?,?,?,?,?)", (cid, k.name, k.metric_key, k.unit, pos, now()))
    return get_study_class(cid)


@app.patch("/api/study-classes/{cid}/kpis/{kid}")
def update_class_kpi(cid: int, kid: int, p: KpiPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if fields:
        with db() as c:
            c.execute(f"UPDATE study_class_kpis SET {', '.join(f'{k}=?' for k in fields)} WHERE id=? AND class_id=?",
                      (*fields.values(), kid, cid))
    return get_study_class(cid)


@app.delete("/api/study-classes/{cid}/kpis/{kid}", status_code=204)
def delete_class_kpi(cid: int, kid: int):
    with db() as c:
        c.execute("DELETE FROM study_class_kpis WHERE id=? AND class_id=?", (kid, cid))
    return Response(status_code=204)


class StudyClassAssign(BaseModel):
    class_id: Optional[int] = None


@app.post("/api/studies/{sid}/class")
def set_study_class(sid: int, body: StudyClassAssign):
    with db() as c, txn(c):
        cur = c.execute("SELECT class_id FROM studies WHERE id=?", (sid,)).fetchone()
        if not cur:
            raise HTTPException(404, "Study not found")
        if body.class_id is not None and not c.execute(
                "SELECT 1 FROM study_classes WHERE id=?", (body.class_id,)).fetchone():
            raise HTTPException(404, "Study class not found")
        if body.class_id != cur["class_id"]:
            c.execute("DELETE FROM study_kpi_pins WHERE study_id=?", (sid,))
        c.execute("UPDATE studies SET class_id=?, updated_at=? WHERE id=?", (body.class_id, now(), sid))
    return get_study(sid)


def kpi_job_metrics(c, job_id):
    """A job's current metrics: its latest populated attempt, plus hand-added results."""
    merged = {}
    for r in c.execute(f"SELECT r.metrics FROM results r JOIN jobs j ON j.id=r.job_id "
                       f"WHERE r.job_id=? AND r.superseded_at IS NULL AND (r.attempt_id IS NULL OR "
                       f"r.attempt_id={REPORTED_ATTEMPT}) ORDER BY r.id", (job_id,)):
        merged.update(json.loads(r[0] or "{}"))
    return merged


@app.get("/api/studies/{sid}/kpis")
def study_kpis(sid: int):
    with db() as c:
        s = c.execute("SELECT class_id FROM studies WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "Study not found")
        if not s["class_id"]:
            return []
        kpis = effective_kpis(c, s["class_id"])
        pins = {r["class_kpi_id"]: r for r in c.execute(
            "SELECT p.class_kpi_id, p.job_id, j.name AS job_name FROM study_kpi_pins p "
            "JOIN jobs j ON j.id=p.job_id WHERE p.study_id=?", (sid,))}
        out = []
        for k in kpis:
            pin = pins.get(k["id"])
            value = kpi_job_metrics(c, pin["job_id"]).get(k["metric_key"]) if pin else None
            out.append({"id": k["id"], "name": k["name"], "metric_key": k["metric_key"], "unit": k["unit"],
                       "source_class_name": k["source_class_name"], "inherited": k["inherited"],
                       "job_id": pin["job_id"] if pin else None, "job_name": pin["job_name"] if pin else None,
                       "value": value})
    return out


class PinIn(BaseModel):
    job_id: int


@app.put("/api/studies/{sid}/kpis/{kid}")
def pin_kpi(sid: int, kid: int, body: PinIn):
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM studies WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Study not found")
        if not c.execute("SELECT 1 FROM study_class_kpis WHERE id=?", (kid,)).fetchone():
            raise HTTPException(404, "KPI not found")
        if not c.execute("SELECT 1 FROM jobs WHERE id=? AND study_id=?", (body.job_id, sid)).fetchone():
            raise HTTPException(422, "That job isn't in this study")
        c.execute("INSERT INTO study_kpi_pins(study_id,class_kpi_id,job_id,created_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(study_id,class_kpi_id) DO UPDATE SET job_id=excluded.job_id, created_at=excluded.created_at",
                  (sid, kid, body.job_id, now()))
    return study_kpis(sid)


@app.delete("/api/studies/{sid}/kpis/{kid}", status_code=204)
def unpin_kpi(sid: int, kid: int):
    with db() as c:
        c.execute("DELETE FROM study_kpi_pins WHERE study_id=? AND class_kpi_id=?", (sid, kid))
    return Response(status_code=204)


@app.get("/api/studies/{sid}/metrics")
def study_metrics(sid: int):
    """Current results of each job's latest populated attempt, plus hand-added results."""
    with db() as c:
        rows = c.execute(f"""
            SELECT r.id AS result_id, r.name AS result_name, r.metrics, r.source, j.id AS job_id, j.name AS job_name,
                   a.number AS attempt_number, a.params
            FROM results r JOIN jobs j ON j.id=r.job_id LEFT JOIN attempts a ON a.id=r.attempt_id
            WHERE j.study_id=? AND r.superseded_at IS NULL
              AND (r.attempt_id IS NULL OR r.attempt_id={REPORTED_ATTEMPT})
            ORDER BY j.id, r.attempt_id IS NULL, r.id""", (sid,)).fetchall()
    return [row(r) for r in rows]


# ---------- Jobs ----------
class JobIn(BaseModel):
    study_id: int
    name: str
    description: str = ""
    params: dict = {}
    run_config: dict = {}
    priority: int = 0
    queue: bool = True          # False: create without an attempt (used when importing results)


class JobPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    params: Optional[dict] = None
    run_config: Optional[dict] = None
    priority: Optional[int] = None


JOB_SELECT = f"""
SELECT j.*, s.name AS study_name, s.run_config AS study_run_config, rt.run_config AS run_type_run_config,
  {STAGE_SQL} AS stage,
  a.id AS attempt_id, a.number AS attempt_number, a.run_status, a.populate_status, a.queued_at, a.submitted_at,
  a.started_at, a.finished_at, a.cancel_requested, a.session_name, a.run_dir, a.exit_code,
  (SELECT COUNT(*) FROM attempts x WHERE x.job_id=j.id) AS attempt_count,
  (SELECT COUNT(*) FROM results r WHERE r.job_id=j.id AND r.superseded_at IS NULL
     AND (r.attempt_id=a.id OR r.attempt_id IS NULL)) AS result_count
FROM jobs j JOIN studies s ON s.id=j.study_id LEFT JOIN run_types rt ON rt.id=s.run_type_id
  LEFT JOIN attempts a ON a.id={LATEST}
"""


@app.get("/api/jobs")
def list_jobs(study_id: Optional[int] = None, limit: int = Query(500, le=5000)):
    q, args = JOB_SELECT + " WHERE 1=1", []
    if study_id is not None:
        q += " AND j.study_id=?"; args.append(study_id)
    with db() as c:
        return [row(r) for r in c.execute(q + " ORDER BY j.id DESC LIMIT ?", (*args, limit)).fetchall()]


def new_attempt(c, jid, note="", source="run"):
    n = c.execute("SELECT COALESCE(MAX(number),0)+1 FROM attempts WHERE job_id=?", (jid,)).fetchone()[0]
    cur = c.execute("INSERT INTO attempts(job_id,number,note,source,queued_at) VALUES (?,?,?,?,?)",
                    (jid, n, note, source, now()))
    return cur.lastrowid


@app.post("/api/jobs", status_code=201)
def create_job(j: JobIn):
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM studies WHERE id=?", (j.study_id,)).fetchone():
            raise HTTPException(404, "Study not found")
        cur = c.execute("INSERT INTO jobs(study_id,name,description,params,run_config,priority,created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (j.study_id, j.name, j.description, json.dumps(j.params),
                                                   json.dumps(clean_run_config(j.run_config)), j.priority, now()))
        jid = cur.lastrowid
        if j.queue:
            new_attempt(c, jid)
        c.execute("UPDATE studies SET updated_at=? WHERE id=?", (now(), j.study_id))
    return get_job(jid)


@app.get("/api/jobs/{jid}")
def get_job(jid: int):
    with db() as c:
        r = c.execute(JOB_SELECT + " WHERE j.id=?", (jid,)).fetchone()
    if not r:
        raise HTTPException(404, "Job not found")
    return row(r)


def job_has_frozen_attempt(c, jid):
    return c.execute("SELECT 1 FROM attempts WHERE job_id=? AND frozen_at IS NOT NULL", (jid,)).fetchone() is not None


@app.patch("/api/jobs/{jid}")
def update_job(jid: int, p: JobPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if "run_config" in fields:
        fields["run_config"] = clean_run_config(fields["run_config"])
    with db() as c:
        if ("params" in fields or "run_config" in fields) and job_has_frozen_attempt(c, jid):
            raise HTTPException(409, "This job has already run; its parameters are frozen. "
                                     "Use 'Run again' to make a new attempt with different parameters.")
        for k in ("params", "run_config"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        if fields:
            c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), jid))
    return get_job(jid)


@app.delete("/api/jobs/{jid}", status_code=204)
def delete_job(jid: int):
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
    return Response(status_code=204)


# ---------- Attempts ----------
ATTEMPT_SELECT = f"""
SELECT a.*, j.name AS job_name, j.study_id, j.priority, j.params AS job_params, j.run_config AS job_run_config,
       s.name AS study_name, s.run_config AS study_run_config, rt.run_config AS run_type_run_config, {STAGE_SQL} AS stage,
       (SELECT COUNT(*) FROM results r WHERE r.attempt_id=a.id AND r.superseded_at IS NULL) AS result_count
FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN studies s ON s.id=j.study_id
  LEFT JOIN run_types rt ON rt.id=s.run_type_id
"""


def attempt_row(r, c=None):
    d = row(r)
    if d is None:
        return None
    if d.get("frozen_at"):
        d["effective_run_config"] = d["run_config"]
    else:  # not handed to the HPC yet: show what it would use now
        d["params"] = d.get("job_params")
    d.pop("job_params", None)
    d.pop("job_run_config", None)
    return d


@app.get("/api/attempts/{aid}")
def get_attempt(aid: int):
    with db() as c:
        r = c.execute(ATTEMPT_SELECT + " WHERE a.id=?", (aid,)).fetchone()
        if not r:
            raise HTTPException(404, "Attempt not found")
        d = attempt_row(r)
        d["inputs"] = [dict(x) for x in c.execute(
            "SELECT name, sha256, size, stored, remote_ref FROM attempt_inputs WHERE attempt_id=? ORDER BY name",
            (aid,))]
    return d


@app.get("/api/jobs/{jid}/attempts")
def list_attempts(jid: int):
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM attempts WHERE job_id=? ORDER BY number DESC", (jid,))]
    out = []
    for aid in ids:
        a = get_attempt(aid)
        with db() as c:
            merged = {}
            for r in c.execute("SELECT metrics FROM results WHERE attempt_id=? AND superseded_at IS NULL ORDER BY id",
                               (aid,)):
                merged.update(json.loads(r[0] or "{}"))
        a["metrics"] = merged
        out.append(a)
    return out


class AttemptIn(BaseModel):
    note: str = ""
    params: Optional[dict] = None       # change the parameters for this and later attempts
    run_config: Optional[dict] = None   # change the job's run setup overrides
    imported: bool = False              # results produced outside FEM Tracker: starts as a finished run


@app.post("/api/jobs/{jid}/attempts", status_code=201)
def run_again(jid: int, body: AttemptIn):
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            raise HTTPException(404, "Job not found")
        busy = c.execute("SELECT number FROM attempts WHERE job_id=? AND run_status IN "
                         "('queued','submitting','submitted','running')", (jid,)).fetchone()
        if busy:
            raise HTTPException(409, f"Attempt {busy[0]} is still queued or running. Cancel it first.")
        if body.params is not None:
            c.execute("UPDATE jobs SET params=? WHERE id=?", (json.dumps(body.params), jid))
        if body.run_config is not None:
            c.execute("UPDATE jobs SET run_config=? WHERE id=?", (json.dumps(clean_run_config(body.run_config)), jid))
        aid = new_attempt(c, jid, body.note, "import" if body.imported else "run")
        if body.imported:
            freeze(c, aid)
            c.execute("UPDATE attempts SET run_status='finished', finished_at=? WHERE id=?", (now(), aid))
    return get_attempt(aid)


# ---------- The remote job script ----------
def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "job"


def placeholders(job_id, study_id, job_name, number, params) -> dict:
    vals = {k: v for k, v in (params or {}).items() if isinstance(v, (str, int, float, bool))}
    vals.update(job_id=job_id, study_id=study_id, job_name=safe_name(job_name), attempt=number)
    return vals


def fill(text: str, vals: dict) -> str:
    return re.sub(r"(?<!\$)\{(\w+)\}", lambda m: str(vals[m.group(1)]) if m.group(1) in vals else m.group(0), text)


def render_script(job_id, job_name, number, cfg, vals):
    cmds = [fill(x, vals) for x in cfg["commands"] if x.strip()]
    chain = " &&\n".join(cmds) if cmds else "echo 'No commands configured in the run setup' >&2; false"
    return f"""#!/bin/bash -l
# FEM Tracker job {job_id} attempt {number}: {job_name}  (generated, do not edit)
cd "$(dirname "$0")" || exit 1
date -u +%Y-%m-%dT%H:%M:%SZ > .fem_started
( while true; do date +%s > .fem_heartbeat; sleep 60; done ) &
FEM_HB=$!
trap 'kill $FEM_HB 2>/dev/null' EXIT

{{
set -x
{chain}
}} > run.log 2>&1
FEM_RC=$?

kill $FEM_HB 2>/dev/null
printf '{{"exit_code": %d, "started_at": "%s", "finished_at": "%s"}}\\n' \\
  "$FEM_RC" "$(cat .fem_started)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > run_status.json.tmp
mv run_status.json.tmp run_status.json
exit $FEM_RC
"""


def freeze(c, aid):
    """Snapshot parameters, effective run setup and script into the attempt. Called once."""
    r = c.execute("SELECT a.number, a.frozen_at, j.id, j.name, j.study_id, j.params, j.run_config, s.run_config, "
                  "rt.run_config AS run_type_run_config FROM attempts a JOIN jobs j ON j.id=a.job_id "
                  "JOIN studies s ON s.id=j.study_id LEFT JOIN run_types rt ON rt.id=s.run_type_id WHERE a.id=?",
                  (aid,)).fetchone()
    if r["frozen_at"]:
        return
    params = json.loads(r[5] or "{}")
    rt_cfg = json.loads(r["run_type_run_config"]) if r["run_type_run_config"] else None
    cfg = effective(rt_cfg, json.loads(r[7] or "{}"), json.loads(r[6] or "{}"))
    vals = placeholders(r[2], r[4], r[3], r[0], params)
    session = f"fem_{r[2]}_{r[0]}_{vals['job_name']}"[:60]
    c.execute("UPDATE attempts SET params=?, run_config=?, script=?, session_name=?, frozen_at=? WHERE id=?",
              (json.dumps(params, sort_keys=True), json.dumps(cfg, sort_keys=True),
               render_script(r[2], r[3], r[0], cfg, vals), session, now(), aid))


def spec_for(a: dict) -> dict:
    """What the agents need: filled-in commands and file lists, from the frozen snapshot."""
    cfg = a["effective_run_config"]
    vals = placeholders(a["job_id"], a["study_id"], a["job_name"], a["number"], a["params"])
    script = a.get("script") or render_script(a["job_id"], a["job_name"], a["number"], cfg, vals)
    return {"attempt_id": a["id"], "number": a["number"], "frozen": bool(a.get("frozen_at")),
            "session": a.get("session_name") or f"fem_{a['job_id']}_{a['number']}_{vals['job_name']}"[:60],
            "script": script, "commands": [fill(x, vals) for x in cfg["commands"] if x.strip()],
            "cancel_commands": [fill(x, vals) for x in cfg["cancel_commands"] if x.strip()],
            "retrieve": [fill(p, vals) for p in cfg["retrieve"] if p.strip()],
            "inputs": [fill(p, vals) for p in cfg["inputs"] if p.strip()],
            "remote_inputs": [fill(p, vals) for p in cfg.get("remote_inputs", []) if p.strip()],
            "local_dir": cfg["local_dir"], "cleanup": bool(cfg["cleanup"]),
            "params_json": json.dumps(a["params"] or {}, indent=2, sort_keys=True)}


@app.get("/api/attempts/{aid}/script")
def attempt_script(aid: int):
    return spec_for(get_attempt(aid))


# ---------- Provenance ----------
class InputRecord(BaseModel):
    name: str
    sha256: str
    size: int
    remote_ref: bool = False


class ProvenanceIn(BaseModel):
    inputs: List[InputRecord] = []
    git: Optional[dict] = None


@app.post("/api/inputs/missing")
def missing_inputs(hashes: List[str]):
    """Which of these file contents the database doesn't have yet (so only those are uploaded)."""
    with db() as c:
        have = {r[0] for r in c.execute(
            f"SELECT sha256 FROM input_blobs WHERE sha256 IN ({','.join('?' * len(hashes))})", hashes)} if hashes else set()
    return [h for h in hashes if h not in have]


@app.post("/api/inputs")
async def upload_inputs(files: List[UploadFile] = File(...)):
    stored = []
    with db() as c:
        for f in files:
            data = await f.read()
            h = hashlib.sha256(data).hexdigest()
            c.execute("INSERT OR IGNORE INTO input_blobs(sha256,size,data) VALUES (?,?,?)", (h, len(data), sqlite3.Binary(data)))
            stored.append(h)
    return stored


@app.post("/api/attempts/{aid}/provenance")
def record_provenance(aid: int, p: ProvenanceIn):
    with db() as c, txn(c):
        c.execute("DELETE FROM attempt_inputs WHERE attempt_id=?", (aid,))
        for i in p.inputs:
            stored = c.execute("SELECT 1 FROM input_blobs WHERE sha256=?", (i.sha256,)).fetchone() is not None
            c.execute("INSERT INTO attempt_inputs(attempt_id,name,sha256,size,stored,remote_ref) VALUES (?,?,?,?,?,?)",
                      (aid, i.name, i.sha256, i.size, int(stored), int(i.remote_ref)))
        c.execute("UPDATE attempts SET git=? WHERE id=?", (json.dumps(p.git, sort_keys=True) if p.git else None, aid))
    return get_attempt(aid)


@app.get("/api/inputs/{sha}")
def download_input(sha: str, name: str = "input.bin"):
    with db() as c:
        r = c.execute("SELECT data FROM input_blobs WHERE sha256=?", (sha,)).fetchone()
    if not r:
        raise HTTPException(404, "This input file was not stored (it was larger than the provenance limit)")
    return Response(content=bytes(r[0]), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{os.path.basename(name)}"'})


# ---------- Step 1: run ----------
class ClaimIn(BaseModel):
    agent: str
    limit: int = 10
    attempt_id: Optional[int] = None


class RunUpdate(BaseModel):
    run_status: str
    remote_host: Optional[str] = None
    session_name: Optional[str] = None
    run_dir: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


def claimed(c, ids):
    return [dict(get_attempt(i), spec=spec_for(get_attempt(i))) for i in ids]


@app.post("/api/run/claim")
def claim_for_run(body: ClaimIn):
    """Hand queued attempts to a run agent (highest job priority first, then oldest) and freeze them."""
    with db() as c, txn(c):
        ids = [r[0] for r in c.execute(
            "SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job_id WHERE a.run_status='queued' "
            "ORDER BY j.priority DESC, a.queued_at, a.id LIMIT ?", (max(body.limit, 0),))]
        for i in ids:
            freeze(c, i)
            c.execute("UPDATE attempts SET run_status='submitting', submitted_by=? WHERE id=?", (body.agent, i))
    return claimed(c, ids)


@app.get("/api/run/active")
def active_runs(agent: Optional[str] = None):
    q, args = ATTEMPT_SELECT + " WHERE a.run_status IN ('submitting','submitted','running')", []
    if agent:
        q += " AND a.submitted_by=?"; args.append(agent)
    with db() as c:
        return [attempt_row(r) for r in c.execute(q + " ORDER BY a.id", args).fetchall()]


@app.post("/api/attempts/{aid}/run")
def update_run(aid: int, u: RunUpdate):
    if u.run_status not in RUN_STATUSES or u.run_status == "queued":
        raise HTTPException(422, "run_status must be one of submitted, running, finished, failed, cancelled "
                                 "(use 'Run again' to queue a new attempt)")
    t = now()
    ts_col = {"submitted": "submitted_at", "running": "started_at", "finished": "finished_at",
              "failed": "finished_at", "cancelled": "finished_at"}.get(u.run_status)
    sets, args = ["run_status=?", "run_error=?"], [u.run_status, u.error]
    for col in ("remote_host", "session_name", "run_dir", "exit_code"):
        sets.append(f"{col}=COALESCE(?,{col})"); args.append(getattr(u, col))
    if u.started_at:
        sets.append("started_at=?"); args.append(u.started_at)
    if u.finished_at and ts_col == "finished_at":
        sets.append("finished_at=?"); args.append(u.finished_at); ts_col = None
    if ts_col:
        sets.append(f"{ts_col}=COALESCE({ts_col},?)"); args.append(t)
    if u.run_status in ("finished", "failed", "cancelled"):
        sets.append("cancel_requested=0")
    with db() as c:
        c.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE id=?", (*args, aid))
    return get_attempt(aid)


@app.post("/api/attempts/{aid}/cancel")
def cancel_attempt(aid: int):
    a = get_attempt(aid)
    with db() as c:
        if a["run_status"] == "queued":
            c.execute("UPDATE attempts SET run_status='cancelled', finished_at=? WHERE id=?", (now(), aid))
        elif a["run_status"] in ("submitting", "submitted", "running"):
            c.execute("UPDATE attempts SET cancel_requested=1 WHERE id=?", (aid,))
        else:
            raise HTTPException(409, f"A {a['run_status']} run can't be cancelled")
    return get_attempt(aid)


# ---------- Step 2: populate ----------
class PopulateUpdate(BaseModel):
    populate_status: Optional[str] = None
    error: Optional[str] = None
    local_dir: Optional[str] = None
    remote_cleaned: Optional[bool] = None


@app.post("/api/populate/claim")
def claim_for_population(body: ClaimIn):
    """Start a new population round. The previous round's results are superseded, not deleted."""
    with db() as c, txn(c):
        q, args = "SELECT id FROM attempts WHERE run_status='finished' AND populate_status='pending'", []
        if body.attempt_id is not None:
            q += " AND id=?"; args.append(body.attempt_id)
        ids = [r[0] for r in c.execute(q + " ORDER BY finished_at, id LIMIT ?", (*args, body.limit))]
        t = now()
        for i in ids:
            c.execute("UPDATE attempts SET populate_status='populating', populated_by=?, populate_error=NULL, "
                      "population=population+1 WHERE id=?", (body.agent, i))
            c.execute("UPDATE results SET superseded_at=? WHERE attempt_id=? AND source='populate' "
                      "AND superseded_at IS NULL", (t, i))
    return claimed(c, ids)


@app.post("/api/attempts/{aid}/populate")
def update_population(aid: int, u: PopulateUpdate):
    sets, args = [], []
    if u.populate_status is not None:
        if u.populate_status not in POP_STATUSES:
            raise HTTPException(422, f"populate_status must be one of {POP_STATUSES}")
        sets += ["populate_status=?", "populate_error=?"]; args += [u.populate_status, u.error]
        if u.populate_status in ("done", "skipped"):
            sets.append("populated_at=?"); args.append(now())
    if u.local_dir is not None:
        sets.append("local_dir=?"); args.append(u.local_dir)
    if u.remote_cleaned:
        sets.append("remote_cleaned_at=?"); args.append(now())
    if sets:
        with db() as c:
            c.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE id=?", (*args, aid))
    return get_attempt(aid)


# ---------- Queues ----------
@app.get("/api/queue")
def queue_overview():
    with db() as c:
        stages = {s: 0 for s in STAGES}
        for r in c.execute(f"SELECT {STAGE_SQL} AS st, COUNT(*) n FROM jobs j LEFT JOIN attempts a ON a.id={LATEST} "
                           "GROUP BY st"):
            stages[r["st"]] = r["n"]
        run_q = c.execute(ATTEMPT_SELECT + " WHERE a.run_status IN ('queued','submitting','submitted','running') "
                          "ORDER BY a.run_status='queued', j.priority DESC, a.queued_at, a.id").fetchall()
        pop_q = c.execute(ATTEMPT_SELECT + " WHERE a.run_status='finished' AND a.populate_status IN "
                          "('pending','populating','failed') ORDER BY a.populate_status='populating' DESC, a.finished_at"
                          ).fetchall()
    return {"stages": stages, "run_queue": [attempt_row(r) for r in run_q],
            "populate_queue": [attempt_row(r) for r in pop_q]}


# ---------- Results ----------
@app.get("/api/attempts/{aid}/results")
def attempt_results(aid: int, include_superseded: bool = False):
    q = "SELECT * FROM results WHERE attempt_id=?" + ("" if include_superseded else " AND superseded_at IS NULL")
    with db() as c:
        return [row(r) for r in c.execute(q + " ORDER BY population DESC, id", (aid,)).fetchall()]


@app.get("/api/jobs/{jid}/results")
def job_results(jid: int):
    """Results added by hand to the job."""
    with db() as c:
        return [row(r) for r in c.execute("SELECT * FROM results WHERE job_id=? AND attempt_id IS NULL ORDER BY id",
                                          (jid,)).fetchall()]


async def _store_result(jid, aid, population, name, description, metrics, source, local_path, remote_path,
                        size_bytes, file):
    metrics_json = parse_json(metrics, "metrics")
    data = fname = ctype = None
    if file is not None and file.filename:
        data = await file.read()
        fname, ctype = file.filename, file.content_type or "application/octet-stream"
    elif local_path or remote_path:
        fname = os.path.basename((local_path or remote_path).replace("\\", "/"))
    with db() as c:
        cur = c.execute(
            "INSERT INTO results(job_id,attempt_id,population,name,description,metrics,source,filename,content_type,"
            "size_bytes,sha256,blob,local_path,remote_path,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, aid, population, name, description, metrics_json, source, fname, ctype,
             len(data) if data else size_bytes, hashlib.sha256(data).hexdigest() if data else None,
             sqlite3.Binary(data) if data else None, local_path or None, remote_path or None, now()))
        return row(c.execute("SELECT * FROM results WHERE id=?", (cur.lastrowid,)).fetchone())


@app.post("/api/attempts/{aid}/results", status_code=201)
async def add_attempt_result(aid: int, name: str = Form(...), description: str = Form(""), metrics: str = Form("{}"),
                             local_path: str = Form(""), remote_path: str = Form(""), size_bytes: int = Form(0),
                             file: Optional[UploadFile] = File(None)):
    a = get_attempt(aid)
    return await _store_result(a["job_id"], aid, a["population"], name, description, metrics, "populate",
                               local_path, remote_path, size_bytes, file)


@app.post("/api/jobs/{jid}/results", status_code=201)
async def add_manual_result(jid: int, name: str = Form(...), description: str = Form(""), metrics: str = Form("{}"),
                            file: Optional[UploadFile] = File(None)):
    get_job(jid)
    return await _store_result(jid, None, None, name, description, metrics, "manual", "", "", 0, file)


@app.get("/api/results/{rid}/blob")
def download_blob(rid: int):
    with db() as c:
        r = c.execute("SELECT filename, content_type, blob FROM results WHERE id=?", (rid,)).fetchone()
    if not r or r["blob"] is None:
        raise HTTPException(404, "No file stored in the database for this result")
    return Response(content=bytes(r["blob"]), media_type=r["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{r["filename"] or f"result_{rid}.bin"}"'})


@app.delete("/api/results/{rid}", status_code=204)
def delete_result(rid: int):
    with db() as c:
        c.execute("DELETE FROM results WHERE id=? AND source='manual'", (rid,))
    return Response(status_code=204)


# ---------- Notebook ----------
class NotebookIn(BaseModel):
    title: str
    kind: str = "note"
    tags: List[str] = []
    format: str = "markdown"
    body: str = ""


class NotebookPatch(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    tags: Optional[List[str]] = None
    format: Optional[str] = None
    body: Optional[str] = None


class RenderIn(BaseModel):
    format: str = "markdown"
    body: str = ""


def clean_notebook_fields(kind, fmt):
    if kind not in NOTEBOOK_KINDS:
        raise HTTPException(422, f"kind must be one of {NOTEBOOK_KINDS}")
    if fmt not in ("markdown", "html"):
        raise HTTPException(422, "format must be markdown or html")


JOB_REF_RE = re.compile(r"\[\[(job|attempt):([^\]\[\s]+)\]\]")
GIT_REF_RE = re.compile(r"\[\[git:([^\]\[\s]+)\]\]")
ASSET_REF_RE = re.compile(r"asset:([a-fA-F0-9]{64})")
ATTACHMENT_REF_RE = re.compile(r"\[\[attachment:([a-fA-F0-9]{64})\]\]")


def sync_notebook_refs(c, eid, body):
    """Re-derive notebook_entry_jobs, notebook_entry_assets and notebook_entry_attachments from the body."""
    job_ids = set()
    for kind, arg in JOB_REF_RE.findall(body):
        try:
            job_ids.add(int(arg if kind == "job" else arg.split("/")[0]))
        except ValueError:
            pass
    for prefix in GIT_REF_RE.findall(body):
        job_ids |= {m["job_id"] for m in notebook.find_attempts_by_commit(c, prefix)}
    asset_shas = {m.lower() for m in ASSET_REF_RE.findall(body)}
    attachment_shas = {m.lower() for m in ATTACHMENT_REF_RE.findall(body)}
    c.execute("DELETE FROM notebook_entry_jobs WHERE entry_id=?", (eid,))
    for jid in job_ids:
        if c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            c.execute("INSERT OR IGNORE INTO notebook_entry_jobs(entry_id,job_id) VALUES (?,?)", (eid, jid))
    c.execute("DELETE FROM notebook_entry_assets WHERE entry_id=?", (eid,))
    for sha in asset_shas:
        if c.execute("SELECT 1 FROM notebook_assets WHERE sha256=?", (sha,)).fetchone():
            c.execute("INSERT OR IGNORE INTO notebook_entry_assets(entry_id,sha256) VALUES (?,?)", (eid, sha))
    c.execute("DELETE FROM notebook_entry_attachments WHERE entry_id=?", (eid,))
    for sha in attachment_shas:
        if c.execute("SELECT 1 FROM notebook_attachments WHERE sha256=?", (sha,)).fetchone():
            c.execute("INSERT OR IGNORE INTO notebook_entry_attachments(entry_id,sha256) VALUES (?,?)", (eid, sha))


def get_notebook_entry(eid: int):
    with db() as c:
        r = c.execute("SELECT * FROM notebook_entries WHERE id=?", (eid,)).fetchone()
        if not r:
            raise HTTPException(404, "Notebook entry not found")
        d = row(r)
        d["rendered_html"] = notebook.render_body(c, d["format"], d["body"])
        d["linked_jobs"] = [dict(x) for x in c.execute(
            "SELECT j.id, j.name FROM notebook_entry_jobs e JOIN jobs j ON j.id=e.job_id "
            "WHERE e.entry_id=? ORDER BY j.id", (eid,))]
        d["assets"] = [x[0] for x in c.execute(
            "SELECT sha256 FROM notebook_entry_assets WHERE entry_id=? ORDER BY sha256", (eid,))]
        d["attachments"] = [dict(x) for x in c.execute(
            "SELECT a.sha256, a.filename, a.size FROM notebook_entry_attachments ea "
            "JOIN notebook_attachments a ON a.sha256=ea.sha256 WHERE ea.entry_id=? ORDER BY a.filename", (eid,))]
    return d


@app.get("/api/studies/{sid}/notebook")
def list_notebook(sid: int, kind: Optional[str] = None, q: Optional[str] = None, include_archived: bool = False):
    query = ("SELECT id, study_id, title, kind, tags, format, created_at, updated_at, archived_at "
            "FROM notebook_entries WHERE study_id=?")
    args = [sid]
    if not include_archived:
        query += " AND archived_at IS NULL"
    if kind:
        query += " AND kind=?"; args.append(kind)
    if q:
        query += " AND (title LIKE ? OR body LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    with db() as c:
        return [row(r) for r in c.execute(query + " ORDER BY id DESC", args).fetchall()]


@app.post("/api/studies/{sid}/notebook", status_code=201)
def create_notebook(sid: int, n: NotebookIn):
    clean_notebook_fields(n.kind, n.format)
    t = now()
    tags_json = json.dumps(n.tags)
    with db() as c, txn(c):
        if not c.execute("SELECT 1 FROM studies WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Study not found")
        cur = c.execute("INSERT INTO notebook_entries(study_id,title,kind,tags,format,body,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (sid, n.title, n.kind, tags_json, n.format, n.body, t, t))
        eid = cur.lastrowid
        c.execute("INSERT INTO notebook_revisions(entry_id,title,kind,tags,format,body,created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (eid, n.title, n.kind, tags_json, n.format, n.body, t))
        sync_notebook_refs(c, eid, n.body)
    return get_notebook_entry(eid)


@app.get("/api/notebook/{eid}")
def notebook_detail(eid: int):
    return get_notebook_entry(eid)


@app.patch("/api/notebook/{eid}")
def update_notebook(eid: int, p: NotebookPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    with db() as c, txn(c):
        cur = c.execute("SELECT * FROM notebook_entries WHERE id=?", (eid,)).fetchone()
        if not cur:
            raise HTTPException(404, "Notebook entry not found")
        title = fields.get("title", cur["title"])
        kind = fields.get("kind", cur["kind"])
        fmt = fields.get("format", cur["format"])
        body = fields.get("body", cur["body"])
        tags_json = json.dumps(fields["tags"]) if "tags" in fields else cur["tags"]
        clean_notebook_fields(kind, fmt)
        t = now()
        c.execute("UPDATE notebook_entries SET title=?, kind=?, tags=?, format=?, body=?, updated_at=? WHERE id=?",
                  (title, kind, tags_json, fmt, body, t, eid))
        if (title, kind, tags_json, fmt, body) != (cur["title"], cur["kind"], cur["tags"], cur["format"], cur["body"]):
            c.execute("INSERT INTO notebook_revisions(entry_id,title,kind,tags,format,body,created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (eid, title, kind, tags_json, fmt, body, t))
        sync_notebook_refs(c, eid, body)
    return get_notebook_entry(eid)


@app.post("/api/notebook/{eid}/archive")
def archive_notebook(eid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM notebook_entries WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, "Notebook entry not found")
        c.execute("UPDATE notebook_entries SET archived_at=? WHERE id=?", (now(), eid))
    return get_notebook_entry(eid)


@app.post("/api/notebook/{eid}/restore")
def restore_notebook(eid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM notebook_entries WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, "Notebook entry not found")
        c.execute("UPDATE notebook_entries SET archived_at=NULL WHERE id=?", (eid,))
    return get_notebook_entry(eid)


@app.get("/api/notebook/{eid}/history")
def notebook_history(eid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM notebook_entries WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, "Notebook entry not found")
        return [row(r) for r in c.execute(
            "SELECT * FROM notebook_revisions WHERE entry_id=? ORDER BY id DESC", (eid,)).fetchall()]


@app.post("/api/notebook/render")
def render_notebook(r: RenderIn):
    if r.format not in ("markdown", "html"):
        raise HTTPException(422, "format must be markdown or html")
    with db() as c:
        return {"html": notebook.render_body(c, r.format, r.body)}


@app.post("/api/notebook/assets", status_code=201)
async def upload_asset(file: UploadFile = File(...)):
    data = await file.read()
    limit = settings.MAX_IMAGE_MB * 1e6
    if len(data) > limit:
        raise HTTPException(413, f"Image is larger than the {settings.MAX_IMAGE_MB:.0f} MB limit "
                                 f"([notebook] max_image_mb in config.ini)")
    try:
        mime, w, h = notebook.detect_image(data)
    except notebook.BadImage as e:
        raise HTTPException(422, str(e))
    sha = hashlib.sha256(data).hexdigest()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO notebook_assets(sha256,mime,size,width,height,data,created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (sha, mime, len(data), w, h, sqlite3.Binary(data), now()))
        r = c.execute("SELECT sha256, mime, size, width, height, created_at FROM notebook_assets WHERE sha256=?",
                      (sha,)).fetchone()
    return dict(r)


@app.get("/api/notebook/assets/{sha}")
def get_asset(sha: str):
    with db() as c:
        r = c.execute("SELECT mime, data FROM notebook_assets WHERE sha256=?", (sha,)).fetchone()
    if not r:
        raise HTTPException(404, "Image not found")
    return Response(content=bytes(r["data"]), media_type=r["mime"],
                    headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@app.get("/api/notebook/results/{rid}/image")
def get_result_image(rid: int):
    with db() as c:
        r = c.execute("SELECT blob FROM results WHERE id=?", (rid,)).fetchone()
    if not r or r["blob"] is None:
        raise HTTPException(404, "No image stored for this result")
    try:
        mime, _, _ = notebook.detect_image(bytes(r["blob"]))
    except notebook.BadImage:
        raise HTTPException(404, "This result is not one of the supported image types")
    return Response(content=bytes(r["blob"]), media_type=mime,
                    headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@app.get("/api/studies/{sid}/notebook/images")
def notebook_image_picker(sid: int):
    """Results already in the database that look like images, for the "insert existing image" picker."""
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.id, r.job_id, j.name AS job_name, r.name, r.filename, r.content_type, r.size_bytes, r.created_at "
            "FROM results r JOIN jobs j ON j.id=r.job_id WHERE j.study_id=? AND r.superseded_at IS NULL "
            "AND r.content_type LIKE 'image/%' ORDER BY r.id DESC", (sid,)).fetchall()]


@app.get("/api/studies/{sid}/notebook/assets")
def notebook_asset_picker(sid: int):
    """Screen grabs already pasted into this study's notebook (any entry), for the same picker."""
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT DISTINCT a.sha256, a.mime, a.size, a.width, a.height, a.created_at "
            "FROM notebook_assets a JOIN notebook_entry_assets ea ON ea.sha256=a.sha256 "
            "JOIN notebook_entries e ON e.id=ea.entry_id WHERE e.study_id=? "
            "ORDER BY a.created_at DESC", (sid,)).fetchall()]


def _safe_arcname(name: str) -> str:
    """A relative path with no leading slash and no .. segments, for a name inside our own zip."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "file"


@app.post("/api/notebook/attachments", status_code=201)
async def upload_attachment(files: List[UploadFile] = File(...)):
    """Zip the uploaded files (their names carry the relative path) into one content-addressed blob."""
    if not files:
        raise HTTPException(422, "No files were uploaded")
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            arcname = _safe_arcname(f.filename or "file")
            names.append(arcname)
            z.writestr(arcname, await f.read())
    data = buf.getvalue()
    limit = settings.MAX_ATTACHMENT_MB * 1e6
    if len(data) > limit:
        raise HTTPException(413, f"This folder zips up to more than the {settings.MAX_ATTACHMENT_MB:.0f} MB limit "
                                 f"([notebook] max_attachment_mb in config.ini)")
    root = names[0].split("/")[0] if names else "attachment"
    filename = f"{safe_name(root)}.zip"
    sha = hashlib.sha256(data).hexdigest()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO notebook_attachments(sha256,filename,size,data,created_at) "
                  "VALUES (?,?,?,?,?)", (sha, filename, len(data), sqlite3.Binary(data), now()))
        r = c.execute("SELECT sha256, filename, size, created_at FROM notebook_attachments WHERE sha256=?",
                      (sha,)).fetchone()
    return dict(r)


@app.get("/api/notebook/attachments/{sha}")
def get_attachment(sha: str):
    with db() as c:
        r = c.execute("SELECT filename, data FROM notebook_attachments WHERE sha256=?", (sha,)).fetchone()
    if not r:
        raise HTTPException(404, "Attachment not found")
    return Response(content=bytes(r["data"]), media_type="application/zip",
                    headers={"X-Content-Type-Options": "nosniff",
                            "Content-Disposition": f'attachment; filename="{r["filename"]}"'})


@app.get("/api/jobs/{jid}/notebook")
def job_notebook(jid: int):
    """Not-archived notebook entries that reference this job (directly, or via an attempt of it)."""
    with db() as c:
        return [row(r) for r in c.execute(
            "SELECT e.id, e.study_id, e.title, e.kind, e.tags, e.format, e.created_at, e.updated_at, e.archived_at "
            "FROM notebook_entry_jobs ej JOIN notebook_entries e ON e.id=ej.entry_id "
            "WHERE ej.job_id=? AND e.archived_at IS NULL ORDER BY e.id DESC", (jid,)).fetchall()]


# ---------- Export ----------
@app.get("/api/studies/{sid}/export")
def export_zip(sid: int, max_file_mb: float = 20):
    from femtracker import export
    get_study(sid)
    buf = io.BytesIO()
    name = export.write_zip(DB_PATH, sid, buf, max_file_mb)
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


# ---------- Front end ----------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
