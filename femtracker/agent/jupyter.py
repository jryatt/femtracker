"""One-click Jupyter on the HPC: start it remotely, open a tunnel, hand back a URL to open.

Mirrors run_agent.py's own background-start pattern (tmux if available, else nohup) and uses a
login shell for the actual Jupyter command so module load / conda activate work the same way they
do for solver commands. Unlike remote.run()'s one-shot calls, the SSH/plink tunnel has to stay
alive for as long as the browser tab is open, so it's a persistent subprocess.Popen tracked here,
not a blocking remote.run() call.
"""
import re
import socket
import subprocess
import threading
import time

from femtracker import settings as config
from femtracker.agent import remote
from femtracker.agent.remote import NO_WINDOW, RemoteError, q

TOKEN_RE = re.compile(r"token=([0-9a-fA-F]+)")
SESSION = "fem_jupyter"

_LOCK = threading.Lock()
_STATE = {"popen": None, "local_port": None, "token": None, "workdir": None}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_token(log_path, budget=20.0, interval=1.0):
    """Jupyter takes a moment to boot (longer with module load / conda activate), so poll its log."""
    end = time.time() + budget
    last = ""
    while time.time() < end:
        last = remote.run(f"cat {q(log_path)} 2>/dev/null || true")
        m = TOKEN_RE.search(last)
        if m:
            return m.group(1)
        time.sleep(interval)
    tail = "\n".join(last.strip().splitlines()[-15:])
    raise RuntimeError(f"Jupyter did not report a token within {budget:.0f}s. Last lines of its log:\n{tail}")


def _tunnel_cmd(local_port):
    forward = f"{local_port}:127.0.0.1:{config.JUPYTER_PORT}"
    port = ["-p", config.SSH_PORT] if config.SSH_PORT else []
    if config.TRANSPORT == "openssh":
        key = ["-i", config.SSH_KEY] if config.SSH_KEY else []
        return ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", *key, *port,
                "-L", forward, remote.target()]
    port = ["-P", config.SSH_PORT] if config.SSH_PORT else []
    return [config.PLINK, "-ssh", "-batch", "-N", "-i", config.PPK, *port, "-L", forward, remote.target()]


def _url():
    return f"http://127.0.0.1:{_STATE['local_port']}/lab?token={_STATE['token']}"


def _start_remote(workdir, log_path, pid_path):
    cmd = config.JUPYTER_COMMAND.format(port=config.JUPYTER_PORT)
    # tmux detaches into its own pty, so the session's command needs its own redirect baked in.
    # Without tmux, nohup/bash inherit our *calling* pipe until they redirect it themselves - which
    # for a login shell can be well after it's done sourcing profile scripts - so redirect at the
    # outer invocation instead, where our own script's shell sets it up before forking anything.
    cmd_redirected = f"{cmd} > {q(log_path)} 2>&1"
    remote.run(f"""
D={q(workdir)}
mkdir -p "$D"
already=0
if command -v tmux >/dev/null 2>&1 && tmux has-session -t {SESSION} 2>/dev/null; then already=1
elif [ -f {q(pid_path)} ] && kill -0 "$(cat {q(pid_path)})" 2>/dev/null; then already=1
fi
if [ "$already" = 1 ]; then
  echo ALREADY
else
  rm -f {q(log_path)} {q(pid_path)}
  cd "$D"
  if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t {SESSION} 2>/dev/null || true
    tmux new-session -d -s {SESSION} bash -l -c {q(cmd_redirected)}
  else
    nohup bash -l -c {q(cmd)} > {q(log_path)} 2>&1 < /dev/null &
    echo $! > {q(pid_path)}
  fi
  echo STARTED
fi
""")


def start() -> dict:
    """Idempotent: safe to call again while already running (e.g. a second click)."""
    workdir = (config.JUPYTER_WORKDIR or "").rstrip("/")
    if not workdir:
        raise RuntimeError("[jupyter] workdir (or [hpc] remote_workdir) is empty; set it in the settings file")
    with _LOCK:
        if _STATE["token"] and (config.TRANSPORT == "local" or (_STATE["popen"] and _STATE["popen"].poll() is None)):
            return {"url": _url(), "already_running": True}
        log_path, pid_path = f"{workdir}/.fem_jupyter.log", f"{workdir}/.fem_jupyter.pid"
        _start_remote(workdir, log_path, pid_path)
        token = _fetch_token(log_path)
        if config.TRANSPORT == "local":
            local_port, popen = config.JUPYTER_PORT, None
        else:
            local_port = _free_port()
            try:
                popen = subprocess.Popen(_tunnel_cmd(local_port), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         creationflags=NO_WINDOW)
            except FileNotFoundError as e:
                raise RuntimeError(f"Could not start the SSH tunnel: {e}")
            for _ in range(50):
                if _port_open(local_port):
                    break
                if popen.poll() is not None:
                    raise RuntimeError("The SSH tunnel exited immediately: "
                                       + (popen.stdout.read() or "").strip()[-2000:])
                time.sleep(0.2)
            else:
                popen.terminate()
                raise RuntimeError("Timed out waiting for the SSH tunnel to come up")
        _STATE.update(popen=popen, local_port=local_port, token=token, workdir=workdir)
        return {"url": _url(), "already_running": False}


def status() -> dict:
    running = bool(_STATE["token"]) and (config.TRANSPORT == "local"
                                         or (_STATE["popen"] is not None and _STATE["popen"].poll() is None))
    return {"running": running, "url": _url() if running else None}


def stop(remote_too: bool = False):
    """Always tears down the tunnel. Leaves the remote Jupyter server running unless remote_too,
    since it may be slow to (re)allocate and worth keeping across FEM Tracker restarts."""
    with _LOCK:
        popen = _STATE.get("popen")
        if popen is not None:
            popen.terminate()
            try:
                popen.wait(5)
            except subprocess.TimeoutExpired:
                popen.kill()
        if remote_too and _STATE.get("workdir"):
            pid_path = f"{_STATE['workdir']}/.fem_jupyter.pid"
            try:
                remote.run(f"""
tmux kill-session -t {SESSION} 2>/dev/null || true
if [ -f {q(pid_path)} ]; then kill "$(cat {q(pid_path)})" 2>/dev/null || true; rm -f {q(pid_path)}; fi
""")
            except RemoteError:
                pass
        _STATE.update(popen=None, local_port=None, token=None, workdir=None)
