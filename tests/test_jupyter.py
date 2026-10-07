"""femtracker.agent.jupyter: start/status/stop, exercised over transport=local so no real SSH
tunnel is needed (a fake `jupyter` stand-in script plays the part of the real HPC command).

The shared test harness (conftest.py) runs the live server with transport=putty against fake
plink/pscp stand-ins that don't do real networking, so a real SSH/plink tunnel can't be exercised
here - only the transport=local path (no tunnel: the "remote" is this machine) is covered by an
automated test. The putty/openssh tunnel path needs a real second host to test against.
"""
import os
import stat
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from femtracker import server, settings
from femtracker.agent import jupyter

FAKE_JUPYTER = """#!/bin/bash
sleep 0.3
echo "[I 2024-01-01 00:00:00.000 ServerApp] Jupyter Server is running at:"
echo "http://127.0.0.1:8888/lab?token=deadbeefcafe0123"
exec sleep 60
"""  # exec, not a plain final command: no forked child left behind for stop()'s kill to miss


@pytest.fixture
def local_transport(tmp_path):
    """Switch settings to transport=local with a fake jupyter command, then restore afterward."""
    saved_env = os.environ.copy()
    workdir = tmp_path / "jupyter_workdir"
    workdir.mkdir()
    fake = tmp_path / "fake_jupyter"
    fake.write_text(FAKE_JUPYTER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    os.environ.update({
        "FEM_TRANSPORT": "local",
        "FEM_JUPYTER_WORKDIR": str(workdir),
        "FEM_JUPYTER_COMMAND": f"{fake} --port={{port}}",
        "FEM_JUPYTER_PORT": "9999",
    })
    settings.load()
    try:
        yield workdir
    finally:
        jupyter.stop(remote_too=True)
        os.environ.clear()
        os.environ.update(saved_env)
        settings.load()


def test_start_status_stop(local_transport):
    assert jupyter.status() == {"running": False, "url": None}

    r = jupyter.start()
    assert r["already_running"] is False
    assert r["url"] == "http://127.0.0.1:9999/lab?token=deadbeefcafe0123"
    assert jupyter.status() == {"running": True, "url": r["url"]}

    r2 = jupyter.start()  # a second click while already running: no error, same URL
    assert r2["already_running"] is True
    assert r2["url"] == r["url"]

    pid_path = local_transport / ".fem_jupyter.pid"
    pid = int(pid_path.read_text().strip())
    os.kill(pid, 0)  # still alive

    jupyter.stop(remote_too=True)
    assert jupyter.status() == {"running": False, "url": None}
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed


def test_restart_detects_already_running_remotely(local_transport):
    """Losing the in-memory state (e.g. FEM Tracker restarted) shouldn't spawn a second process
    if one is already running on the HPC - the remote side is checked via the pid file."""
    r1 = jupyter.start()
    pid_path = local_transport / ".fem_jupyter.pid"
    pid1 = pid_path.read_text().strip()

    jupyter._STATE.update(popen=None, local_port=None, token=None, workdir=None)

    r2 = jupyter.start()
    assert pid_path.read_text().strip() == pid1  # no new process
    assert r2["url"] == r1["url"]


def test_stop_without_remote_too_leaves_remote_process(local_transport):
    jupyter.start()
    pid_path = local_transport / ".fem_jupyter.pid"
    pid = int(pid_path.read_text().strip())

    jupyter.stop(remote_too=False)
    try:
        assert jupyter.status() == {"running": False, "url": None}
        os.kill(pid, 0)  # still alive - only the in-memory state was cleared
    finally:
        os.kill(pid, 15)  # clean up: stop() with remote_too lost track of it above


def test_jupyter_routes_reject_non_loopback_clients():
    """These routes hand back a live Jupyter token (remote code execution on the HPC), so unlike
    the rest of the API they must stay off-limits to anyone but the machine FEM Tracker runs on."""
    for host in ("127.0.0.1", "::1"):
        server._require_loopback(SimpleNamespace(client=SimpleNamespace(host=host)))  # no raise

    for host in ("10.0.0.5", "192.168.1.20", None):
        client = SimpleNamespace(host=host) if host else None
        with pytest.raises(HTTPException) as exc:
            server._require_loopback(SimpleNamespace(client=client))
        assert exc.value.status_code == 403


def test_start_without_workdir_raises():
    saved_env = os.environ.copy()
    os.environ["FEM_JUPYTER_WORKDIR"] = ""
    os.environ["FEM_REMOTE_WORKDIR"] = ""
    try:
        settings.load()
        with pytest.raises(RuntimeError):
            jupyter.start()
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        settings.load()
