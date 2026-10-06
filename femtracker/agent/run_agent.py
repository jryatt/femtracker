"""STEP 1 - RUN. Runs on your PC and drives the HPC over plink/pscp.

  FEMTracker.exe run-agent submit    upload inputs and start queued jobs (up to max_active_runs at once)
  FEMTracker.exe run-agent sync      check running jobs, honour cancel requests
  FEMTracker.exe run-agent loop      both, every poll_seconds  (FEMTracker.exe start does this for you)

For each job, like the PuTTY batch script:
  1. mkdir <REMOTE_WORKDIR>/study_<id>/job_<id>   (one folder per job, so parallel runs never collide)
  2. pscp the input files, job_params.json and the generated fem_run.sh
  3. start fem_run.sh in a detached tmux session, so it survives a dropped connection
Instead of blocking in a wait loop, sync checks all jobs in one SSH call. fem_run.sh writes a
heartbeat while it runs and run_status.json with the exit code when it ends, on the shared
filesystem, so the check works whichever login node answers.

A run setup's remote_inputs are different from inputs: they're already on the HPC (e.g. a large
shared mesh file on scratch), so step 2 never copies them. Instead they're symlinked into the
attempt's folder under their own name and hashed with sha256sum over SSH, so provenance still
records exactly which version was used without ever transferring the bytes anywhere.
"""
import argparse
import glob
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timezone

from femtracker import settings as config
from femtracker.agent import api, remote
from femtracker.agent.remote import q

log = logging.getLogger("femtracker.run").info


def label(a):
    return f"job {a['job_id']} attempt {a['number']}"


def resolve_inputs(spec):
    files = []
    for pattern in spec["inputs"]:
        path = pattern if os.path.isabs(pattern) else os.path.join(spec["local_dir"] or ".", pattern)
        matches = sorted(f for f in glob.glob(path) if os.path.isfile(f))
        if not matches:
            where = spec["local_dir"] or os.getcwd()
            raise FileNotFoundError(f"No input file matches '{pattern}' in {where}")
        files += [m for m in matches if m not in files]
    return files


def prepare_remote_dir(d, remote_inputs):
    """Create the attempt's folder on the HPC, symlink in files that are already there (never
    uploaded), and hash each one remotely for provenance, all in one SSH round trip."""
    per_file = "\n".join(f"""ln -sf {q(p)} "$D/$(basename {q(p)})"
echo "@@REF@@ {q(p)}"
fem_sha256 {q(p)}
wc -c < {q(p)} | tr -d ' '""" for p in remote_inputs)
    out = remote.run(f"""set -e
D={q(d)}
if [ -e "$D" ]; then mv "$D" "$D.old_$(date +%s)"; fi
mkdir -p "$D"
fem_sha256() {{ if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{{print $1}}'; \
else shasum -a 256 "$1" | awk '{{print $1}}'; fi; }}
{per_file}
""")
    hashes = {}
    for block in out.split("@@REF@@ ")[1:]:
        path, _, rest = block.partition("\n")
        sha, _, rest = rest.partition("\n")
        size, _, _ = rest.partition("\n")
        hashes[path.strip()] = (sha.strip(), int(size.strip()))
    return hashes


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(folder):
    """Commit, branch and uncommitted changes of the git repository holding the input folder."""
    if not config.RECORD_GIT or not folder or not os.path.isdir(folder):
        return None

    def git(*args):
        p = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=30,
                           creationflags=remote.NO_WINDOW)
        return p.stdout.strip() if p.returncode == 0 else None
    try:
        commit = git("rev-parse", "HEAD")
    except (FileNotFoundError, subprocess.TimeoutExpired):  # git not installed
        return None
    if not commit:
        return None
    changes = git("status", "--porcelain", "--", ".") or ""
    return {"commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(changes), "changed_files": changes.splitlines()[:50],
            "remote": git("config", "--get", "remote.origin.url"), "path": git("rev-parse", "--show-prefix") or ""}


def record_provenance(a, spec, inputs, remote_inputs, remote_hashes):
    records, store = [], {}
    for f in inputs:
        sha, size = sha256_file(f), os.path.getsize(f)
        records.append({"name": os.path.basename(f), "sha256": sha, "size": size})
        if size <= config.STORE_INPUTS_MAX_MB * 1e6:
            store[sha] = f
    for p in remote_inputs:
        sha, size = remote_hashes[p]
        records.append({"name": p, "sha256": sha, "size": size, "remote_ref": True})
    records.append({"name": "job_params.json", "sha256": hashlib.sha256(spec["params_json"].encode()).hexdigest(),
                    "size": len(spec["params_json"].encode())})
    api.record_provenance(a["id"], records, git_state(spec["local_dir"]), store)


def submit_attempt(a):
    spec = a["spec"]
    d = remote.attempt_dir(a)
    inputs = resolve_inputs(spec)
    remote_inputs = spec.get("remote_inputs", [])
    remote_hashes = prepare_remote_dir(d, remote_inputs)
    record_provenance(a, spec, inputs, remote_inputs, remote_hashes)
    with tempfile.TemporaryDirectory() as tmp:
        script, params = os.path.join(tmp, "fem_run.sh"), os.path.join(tmp, "job_params.json")
        with open(script, "w", newline="\n") as fh:  # LF endings, even on Windows
            fh.write(spec["script"])
        with open(params, "w", newline="\n") as fh:
            fh.write(spec["params_json"])
        remote.upload(inputs + [script, params], d)
    how = remote.run(f"""set -e
D={q(d)}; S={q(spec['session'])}
chmod +x "$D/fem_run.sh"
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "$S" 2>/dev/null || true
  tmux new-session -d -s "$S" "bash -l '$D/fem_run.sh'"
  echo tmux
else
  nohup bash -l "$D/fem_run.sh" >/dev/null 2>&1 < /dev/null &
  echo nohup
fi
""").strip().splitlines()[-1]
    api.set_run(a["id"], "submitted", run_dir=d, remote_host=remote.host_label(),
                session_name=spec["session"] if how == "tmux" else "nohup")
    log(f"{label(a)}: started in {d} ({how}, {len(inputs)} input files, {len(remote_inputs)} referenced on the HPC)")


def submit():
    active = len(api.call("GET", f"/api/run/active?agent={config.AGENT_NAME}"))
    free = config.MAX_ACTIVE_RUNS - active
    if free <= 0:
        return
    for a in api.call("POST", "/api/run/claim", json={"agent": config.AGENT_NAME, "limit": free}):
        try:
            submit_attempt(a)
        except Exception as e:
            api.set_run(a["id"], "failed", error=f"Could not start the job: {e}")
            log(f"{label(a)}: could not start: {e}")


def tail(d, n=40):
    try:
        return remote.run(f"tail -n {n} {q(d)}/run.log 2>/dev/null || true")
    except Exception:
        return ""


def cancel(a):
    spec = api.spec(a["id"])
    extra = "\n".join(spec["cancel_commands"])
    remote.run(f"""D={q(a['run_dir'])}
tmux kill-session -t {q(a['session_name'] or spec['session'])} 2>/dev/null
pkill -f "$D/fem_run.sh" 2>/dev/null
cd "$D" 2>/dev/null && {{
{extra or ':'}
}} >> cancel.log 2>&1
true
""")
    api.set_run(a["id"], "cancelled", error="Cancelled from the web UI")
    log(f"{label(a)}: cancelled")


def age_min(ts):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60 if ts else 0


def sync():
    attempts = api.call("GET", f"/api/run/active?agent={config.AGENT_NAME}")
    watch = []
    for a in attempts:
        try:
            if a["run_status"] == "submitting" and not a["run_dir"]:  # a previous pass died halfway
                api.set_run(a["id"], "failed", error="The agent stopped while starting this attempt. Run it again.")
            elif a["cancel_requested"]:
                cancel(a)
            else:
                watch.append(a)
        except Exception as e:
            log(f"{label(a)}: {e}")
    if not watch:
        return

    probe = []  # one SSH round trip for every running attempt
    for a in watch:
        d = q(a["run_dir"])
        probe.append(f"""echo "@@@ {a['id']}"
if [ -f {d}/run_status.json ]; then echo DONE; cat {d}/run_status.json
elif [ -f {d}/.fem_heartbeat ]; then echo "HB $(( $(date +%s) - $(cat {d}/.fem_heartbeat) )) $(cat {d}/.fem_started 2>/dev/null)"
else echo WAITING; fi""")
    out = remote.run("\n".join(probe))
    states = {}
    for block in out.split("@@@ ")[1:]:
        aid, _, rest = block.partition("\n")
        states[int(aid)] = rest.strip()

    for a in watch:
        aid, st = a["id"], states.get(a["id"], "")
        try:
            if st.startswith("DONE"):
                res = json.loads(st[4:])
                times = {"started_at": res.get("started_at"), "finished_at": res.get("finished_at"),
                         "exit_code": res.get("exit_code")}
                if res.get("exit_code") == 0:
                    api.set_run(aid, "finished", **times)
                    log(f"{label(a)}: finished")
                else:
                    api.set_run(aid, "failed", error=f"A command exited with code {res.get('exit_code')}. "
                                f"Last lines of run.log:\n{tail(a['run_dir'])}", **times)
                    log(f"{label(a)}: failed (exit {res.get('exit_code')})")
            elif st.startswith("HB"):
                parts = st.split()
                silent = int(parts[1]) / 60
                if silent > config.HEARTBEAT_TIMEOUT_MIN:
                    api.set_run(aid, "failed", error=f"No heartbeat for {silent:.1f} min and no run_status.json: "
                                f"the job was killed or the node went down.\n{tail(a['run_dir'])}")
                    log(f"{label(a)}: lost")
                elif a["run_status"] != "running":
                    api.set_run(aid, "running", started_at=parts[2] if len(parts) > 2 else None)
                    log(f"{label(a)}: running")
            elif st == "WAITING" and age_min(a["submitted_at"]) > config.START_TIMEOUT_MIN:
                api.set_run(aid, "failed", error=f"Started {config.START_TIMEOUT_MIN:.0f}+ min ago but fem_run.sh "
                            "never ran. Check that tmux works on the HPC and that the folder is writable.")
                log(f"{label(a)}: never started")
        except Exception as e:
            log(f"{label(a)}: {e}")


def one_pass():
    sync()
    submit()


def loop(stop: threading.Event = None):
    """Run passes until stopped. Reports its state to the web page every pass."""
    stop = stop or threading.Event()
    logger = logging.getLogger("femtracker.run")
    while not stop.is_set():
        problems = config.hpc_problems()
        if problems:
            api.report("run", "not_configured", "; ".join(problems))
        else:
            try:
                one_pass()
                api.report("run", "ok", f"{config.USER}@{config.HOST}" if config.TRANSPORT != "local" else "local")
            except Exception as e:
                if stop.is_set():  # shutting down; the server is already gone
                    break
                logger.warning(f"pass failed, retrying in {config.POLL_SECONDS:.0f}s: {e}")
                api.report("run", "error", str(e))
        stop.wait(config.POLL_SECONDS)
    api.report("run", "stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="FEMTracker.exe run-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["submit", "sync", "loop"])
    a = ap.parse_args(argv)
    problems = config.hpc_problems()
    if problems:
        raise SystemExit("Not configured yet:\n  " + "\n  ".join(problems) + f"\nEdit {config.CONFIG_PATH}")
    log(f"run agent '{config.AGENT_NAME}' -> {remote.host_label()}:{config.REMOTE_WORKDIR} "
        f"(max {config.MAX_ACTIVE_RUNS} at once)")
    if a.command == "loop":
        try:
            loop()
        except KeyboardInterrupt:
            pass
    elif a.command == "sync":
        sync()
    else:
        submit()
