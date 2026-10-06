"""All settings come from config.ini (created with defaults on first run).

Where things live on Windows:
  settings  %APPDATA%\\FEMTracker\\config.ini
  database  %LOCALAPPDATA%\\FEMTracker\\fem_tracker.db
  logs      %LOCALAPPDATA%\\FEMTracker\\logs\\
  results   Documents\\FEM Tracker results\\
Portable mode: if config.ini sits next to FEMTracker.exe, everything stays in that folder.
Any setting can also be overridden with an environment variable, e.g. FEM_HOST or FEM_PORT.
"""
import configparser
import os
import shutil
import socket
import sys
from pathlib import Path

APP = "FEMTracker"
WINDOWS = os.name == "nt"


def app_dir() -> Path:
    """Folder of FEMTracker.exe when frozen, else the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def portable() -> bool:
    return (app_dir() / "config.ini").is_file() and not os.environ.get("FEM_CONFIG")


def config_path() -> Path:
    if os.environ.get("FEM_CONFIG"):
        return Path(os.environ["FEM_CONFIG"])
    if portable():
        return app_dir() / "config.ini"
    base = Path(os.environ["APPDATA"]) if WINDOWS and os.environ.get("APPDATA") else Path.home() / ".config"
    return base / APP / "config.ini"


def data_dir() -> Path:
    if os.environ.get("FEM_DATA_DIR"):
        return Path(os.environ["FEM_DATA_DIR"])
    if portable():
        return app_dir() / "data"
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP
    return Path.home() / ".local" / "share" / "femtracker"


def default_results_dir() -> Path:
    if portable():
        return app_dir() / "results"
    return Path.home() / "Documents" / "FEM Tracker results"


def find_putty(exe: str) -> str:
    for d in (os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        p = Path(d) / "PuTTY" / f"{exe}.exe"
        if p.is_file():
            return str(p)
    return shutil.which(exe) or ""


DEFAULT_INI = r"""; FEM Tracker settings. Restart FEM Tracker after editing.
; Lines starting with ; are comments. Leave a value empty to use the default.

[server]
; The web page is at http://127.0.0.1:8000 . Use address = 0.0.0.0 to let colleagues open it too.
address = 127.0.0.1
port = 8000
; Database file. Empty = %LOCALAPPDATA%\FEMTracker\fem_tracker.db
database =
open_browser = yes

[hpc]
; How to reach the HPC: putty (plink/pscp), openssh (ssh/scp) or local
transport = putty
; Same values as in your PuTTY batch scripts
user =
host =
; PuTTY private key (.ppk). Connect once with PuTTY first so the host key is cached.
ppk =
; Empty = found automatically in C:\Program Files\PuTTY
plink =
pscp =
; OpenSSH private key, only for transport = openssh
ssh_key =
; Folder on the HPC where each job gets study_<id>/job_<id>. Use a shared filesystem (scratch).
remote_workdir =

[folders]
; Where results are downloaded. Empty = Documents\FEM Tracker results
local_results_root =

[run]
; Jobs running on the HPC at the same time (licence tokens, fair share)
max_active_runs = 4
; A running job whose heartbeat stops for this long is marked failed
heartbeat_timeout_min = 10
start_timeout_min = 15
poll_seconds = 60

[populate]
; Files up to this size are stored in the database
max_blob_mb = 200
; Bigger files: remote = leave on the HPC and store the path, download = keep a copy in the results folder
large_files = remote

[provenance]
; Each attempt records the input files it used. Files up to this size are also stored in the
; database (each distinct file once), so any result can be traced back and re-run exactly.
store_inputs_max_mb = 100
; Record the git commit of the local input folder, if it is in a git repository
record_git = yes

[agents]
; Name shown in the web page. Empty = this computer's name
name =
run_agent = yes
populate_agent = yes

[notebook]
; Pasted or uploaded screen grabs larger than this are refused.
max_image_mb = 20
; A folder attached to an entry is zipped and stored as one object; refused if the zip is bigger than this.
max_attachment_mb = 200
"""

# (section, key, env name, type)
FIELDS = [
    ("server", "address", "BIND_HOST", str), ("server", "port", "PORT", int), ("server", "database", "DB", str),
    ("server", "open_browser", "OPEN_BROWSER", bool),
    ("hpc", "transport", "TRANSPORT", str), ("hpc", "user", "USER_NAME", str), ("hpc", "host", "HOST", str),
    ("hpc", "ppk", "PPK", str), ("hpc", "plink", "PLINK", str), ("hpc", "pscp", "PSCP", str),
    ("hpc", "ssh_key", "SSH_KEY", str), ("hpc", "remote_workdir", "REMOTE_WORKDIR", str),
    ("folders", "local_results_root", "LOCAL_RESULTS_ROOT", str),
    ("run", "max_active_runs", "MAX_ACTIVE_RUNS", int), ("run", "heartbeat_timeout_min", "HEARTBEAT_TIMEOUT_MIN", float),
    ("run", "start_timeout_min", "START_TIMEOUT_MIN", float), ("run", "poll_seconds", "POLL_SECONDS", float),
    ("populate", "max_blob_mb", "MAX_BLOB_MB", float), ("populate", "large_files", "LARGE_FILES", str),
    ("provenance", "store_inputs_max_mb", "STORE_INPUTS_MAX_MB", float),
    ("provenance", "record_git", "RECORD_GIT", bool),
    ("agents", "name", "AGENT_NAME", str), ("agents", "run_agent", "RUN_AGENT", bool),
    ("agents", "populate_agent", "POPULATE_AGENT", bool),
    ("notebook", "max_image_mb", "MAX_IMAGE_MB", float),
    ("notebook", "max_attachment_mb", "MAX_ATTACHMENT_MB", float),
]


def _cast(value, typ, where):
    if typ is bool:
        v = str(value).strip().lower()
        if v in ("1", "yes", "true", "on"): return True
        if v in ("0", "no", "false", "off"): return False
        raise ValueError(f"{where}: expected yes or no, got {value!r}")
    try:
        return typ(value)
    except ValueError:
        kind = "a whole number" if typ is int else "a number"
        raise ValueError(f"{where}: expected {kind}, got {value!r}")


def ensure_config() -> Path:
    p = config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_INI, encoding="utf-8")
    return p


def load():
    """(Re)load settings into this module's globals, e.g. settings.HOST."""
    g = globals()
    defaults = configparser.ConfigParser(interpolation=None)
    defaults.read_string(DEFAULT_INI)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(DEFAULT_INI)
    path = ensure_config()
    cp.read(path, encoding="utf-8")
    for section, key, env, typ in FIELDS:
        raw = os.environ.get("FEM_" + env)
        where = f"FEM_{env}" if raw is not None else f"[{section}] {key} in {path}"
        if raw is None:
            raw = cp.get(section, key, fallback="").strip() or defaults.get(section, key)
        g[env] = raw if typ is str else _cast(raw, typ, where)

    g["CONFIG_PATH"] = path
    g["DATA_DIR"] = data_dir()
    g["LOG_DIR"] = g["DATA_DIR"] / "logs"
    g["DB_PATH"] = g["DB"] or str(g["DATA_DIR"] / "fem_tracker.db")
    g["LOCAL_RESULTS_ROOT"] = g["LOCAL_RESULTS_ROOT"] or str(default_results_dir())
    g["PLINK"] = g["PLINK"] or find_putty("plink")
    g["PSCP"] = g["PSCP"] or find_putty("pscp")
    g["AGENT_NAME"] = g["AGENT_NAME"] or socket.gethostname()
    g["USER"] = g["USER_NAME"]
    connect = "127.0.0.1" if g["BIND_HOST"] in ("0.0.0.0", "::", "") else g["BIND_HOST"]
    g["SERVER"] = os.environ.get("FEM_SERVER") or f"http://{connect}:{g['PORT']}"


def hpc_problems() -> list:
    """What is still missing before the agents can reach the HPC. Empty list = ready."""
    out = []
    if TRANSPORT not in ("putty", "openssh", "local"):
        out.append(f"transport must be putty, openssh or local, not {TRANSPORT!r}")
    if TRANSPORT != "local":
        if not USER: out.append("[hpc] user is empty")
        if not HOST: out.append("[hpc] host is empty")
    if TRANSPORT == "putty":
        if not PLINK or not Path(PLINK).is_file(): out.append("plink.exe not found; install PuTTY or set [hpc] plink")
        if not PSCP or not Path(PSCP).is_file(): out.append("pscp.exe not found; install PuTTY or set [hpc] pscp")
        if not PPK: out.append("[hpc] ppk is empty")
        elif not Path(PPK).is_file(): out.append(f"key file not found: {PPK}")
    w = REMOTE_WORKDIR
    if not w:
        out.append("[hpc] remote_workdir is empty")
    elif not w.startswith("/") or any(c in w for c in " '\"$`\\") or w.rstrip("/").count("/") < 2:
        out.append("[hpc] remote_workdir must be an absolute path at least two folders deep, without spaces or quotes")
    return out


class OldSchema(RuntimeError):
    """The database was created by an older, incompatible version."""


LOAD_ERROR = None
try:
    load()
except ValueError as e:  # reported nicely by the command line instead of a traceback
    LOAD_ERROR = e
