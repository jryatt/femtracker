"""Test harness: a fake HPC (plink/pscp/abaqus stand-ins running locally) and a real FEM Tracker server.

Runs on Linux or macOS (bash 3.2 or later). The agents' plink/pscp code paths are exercised for real.
"""
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="femtest_"))
BIN = TMP / "bin"
BIN.mkdir()

FAKE_PLINK = """#!/bin/bash
# plink -ssh -batch -i KEY -m FILE user@host  ->  run FILE locally
while [ $# -gt 0 ]; do case "$1" in -m) f="$2"; shift 2;; -i) shift 2;; *) shift;; esac; done
exec bash "$f"
"""
FAKE_PSCP = """#!/bin/bash
# pscp -batch -q -i KEY SRC... DEST   (user@host: prefixes stripped)
args=(); while [ $# -gt 0 ]; do case "$1" in -i) shift 2;; -*) shift;; *) args+=("${1#*@*:}"); shift;; esac; done
# bash 3.2 (macOS /bin/bash) has no negative array indexes
n=${#args[@]}; dest="${args[$((n-1))]}"
i=0; while [ $i -lt $((n-1)) ]; do cp "${args[$i]}" "$dest" || exit 1; i=$((i+1)); done
"""
FAKE_ABAQUS = """#!/bin/bash
# Stand-in for Abaqus: pre/post-processing via 'python', a solve that honours load/thickness, terminate.
P=job_params.json
val(){ python3 -c "import json;print(json.load(open('$P')).get('$1','$2'))"; }
case "$1" in
  python)
    if [ "$2" = Pre_Processing.py ]; then echo "*HEADING" > "$3.inp"
    else python3 - <<'PY'
import json
p=json.load(open("job_params.json")); F=float(p["load_N"]); t=float(p.get("thickness_mm",4))
open("Post_Results.txt","w").write("Max_Mises = %.1f\\nU_max = %.3f\\n" % (F*0.46*(4/t)**2, F*0.00041*(4/t)**3))
PY
    fi;;
  terminate) echo "terminated $2";;
  *) for a in "$@"; do case $a in j=*) jn=${a#j=};; esac; done
     sleep "$(val solve_seconds 1)"
     if [ "$(val load_N 0)" -ge 3000 ]; then echo " THE ANALYSIS HAS NOT BEEN COMPLETED" > "$jn.sta"; exit 0; fi
     head -c 1500000 /dev/zero > "$jn.odb"
     echo " THE ANALYSIS HAS COMPLETED SUCCESSFULLY" > "$jn.sta";;
esac
"""
for name, body in (("plink", FAKE_PLINK), ("pscp", FAKE_PSCP), ("abaqus", FAKE_ABAQUS)):
    p = BIN / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
(TMP / "key.ppk").write_text("fake")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
os.environ.update({
    "FEM_CONFIG": str(TMP / "config.ini"), "FEM_DATA_DIR": str(TMP / "data"), "FEM_PORT": str(PORT),
    "FEM_OPEN_BROWSER": "no", "FEM_TRANSPORT": "putty", "FEM_USER_NAME": "tester", "FEM_HOST": "hpc",
    "FEM_PPK": str(TMP / "key.ppk"), "FEM_PLINK": str(BIN / "plink"), "FEM_PSCP": str(BIN / "pscp"),
    "FEM_REMOTE_WORKDIR": str(TMP / "hpc" / "scratch"), "FEM_LOCAL_RESULTS_ROOT": str(TMP / "results"),
    "FEM_MAX_BLOB_MB": "1", "FEM_POLL_SECONDS": "1", "FEM_MAX_ACTIVE_RUNS": "4", "FEM_AGENT_NAME": "testpc",
    "FEM_MAX_IMAGE_MB": "1", "FEM_MAX_ATTACHMENT_MB": "1",
})
sys.path.insert(0, str(ROOT))
(TMP / "hpc" / "scratch").mkdir(parents=True)
BASE = f"http://127.0.0.1:{PORT}"
ABAQUS = str(BIN / "abaqus")


@pytest.fixture(scope="session", autouse=True)
def server():
    proc = subprocess.Popen([sys.executable, "-m", "femtracker", "server", "--no-browser"], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            requests.get(BASE + "/api/health", timeout=1)
            break
        except requests.ConnectionError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read())
            time.sleep(0.1)
    yield BASE
    proc.terminate()
    proc.wait(10)


def api(method, path, expect=None, **kw):
    r = requests.request(method, BASE + path, timeout=30, **kw)
    if expect is not None:
        assert r.status_code == expect, (r.status_code, r.text)
    elif not r.ok:
        raise AssertionError(f"{method} {path}: {r.status_code} {r.text}")
    return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else r


def drive(done, timeout=40):
    """Run agent passes until done() is true."""
    from femtracker.agent import populate, run_agent
    end = time.time() + timeout
    while time.time() < end:
        run_agent.sync()
        run_agent.submit()
        populate.run_once(10)
        if done():
            return
        time.sleep(0.5)
    raise AssertionError("timed out waiting for the agents")


def make_inputs(folder, git=True):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Pre_Processing.py").write_text("# pre\n")
    (folder / "Post_Processing.py").write_text("# post\n")
    (folder / "bracket_template.inp").write_text("*HEADING\n@load_N@\n")
    if git:
        run = lambda *a: subprocess.run(["git", *a], cwd=folder, check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "inputs")
    return folder


def study(name, folder, cleanup=False, retrieve=("Post_Results.txt", "*.odb")):
    return api("POST", "/api/studies", json={"name": name, "run_config": {
        "local_dir": str(folder), "inputs": ["*.py", "*.inp"],
        "commands": [f"{ABAQUS} python Pre_Processing.py {{job_name}}",
                     f"{ABAQUS} j={{job_name}} cpus=4",
                     'grep -q "COMPLETED SUCCESSFULLY" {job_name}.sta',
                     f"{ABAQUS} python Post_Processing.py {{job_name}}"],
        "retrieve": list(retrieve), "cleanup": cleanup,
        "cancel_commands": [f"{ABAQUS} terminate job={{job_name}}"]}})


def job(sid, name, **params):
    return api("POST", "/api/jobs", json={"study_id": sid, "name": name, "params": params})


def latest(jid):
    return api("GET", f"/api/jobs/{jid}")
