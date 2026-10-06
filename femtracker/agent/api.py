"""Small client for the FEM Tracker API. The agents work on attempts (one run of a job)."""
import json

import requests

from femtracker import settings as config

_S = requests.Session()


def call(method, path, **kw):
    r = _S.request(method, config.SERVER.rstrip("/") + path, timeout=kw.pop("timeout", 60), **kw)
    if r.status_code == 204:
        return None
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def spec(aid):
    return call("GET", f"/api/attempts/{aid}/script")


def set_run(aid, run_status, **fields):
    return call("POST", f"/api/attempts/{aid}/run", json={"run_status": run_status, **fields})


def set_population(aid, status=None, error=None, **fields):
    return call("POST", f"/api/attempts/{aid}/populate", json={"populate_status": status, "error": error, **fields})


def add_result(aid, name, description="", metrics=None, path=None, **pointer):
    """pointer: local_path / remote_path / size_bytes for files kept outside the database."""
    data = {"name": name, "description": description, "metrics": json.dumps(metrics or {}),
            **{k: str(v) for k, v in pointer.items() if v is not None}}
    if path:
        with open(path, "rb") as fh:
            return call("POST", f"/api/attempts/{aid}/results", data=data,
                        files={"file": (path.replace("\\", "/").split("/")[-1], fh)}, timeout=3600)
    return call("POST", f"/api/attempts/{aid}/results", data=data)


def record_provenance(aid, inputs, git, store_paths):
    """inputs: [{name, sha256, size, remote_ref?}]; store_paths: {sha256: local path} of files to keep a copy of."""
    if store_paths:
        missing = set(call("POST", "/api/inputs/missing", json=list(store_paths)))
        for sha in missing:
            with open(store_paths[sha], "rb") as fh:
                call("POST", "/api/inputs", files=[("files", (store_paths[sha].replace("\\", "/").split("/")[-1], fh))],
                     timeout=3600)
    return call("POST", f"/api/attempts/{aid}/provenance", json={"inputs": inputs, "git": git})


def report(role, state, message=""):
    try:
        call("POST", "/api/agents", json={"name": config.AGENT_NAME, "role": role, "state": state,
                                          "message": message, "interval_s": config.POLL_SECONDS}, timeout=10)
    except Exception:
        pass
