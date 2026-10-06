"""End-to-end tests of runs, population and versioning, through the fake HPC."""
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from conftest import ABAQUS, TMP, api, drive, job, latest, make_inputs, study


def populated(jid, number=None):
    def check():
        j = latest(jid)
        return j["stage"] in ("populated", "failed") and (number is None or j["attempt_number"] == number)
    return check


def test_run_populate_and_provenance():
    folder = make_inputs(TMP / "pc" / "bracket")
    sid = study("Provenance", folder, cleanup=True)["id"]
    jid = job(sid, "t4 F1000", load_N=1000, thickness_mm=4)["id"]
    drive(populated(jid))
    j = latest(jid)
    assert j["stage"] == "populated", j
    a = api("GET", f"/api/jobs/{jid}/attempts")[0]
    # frozen snapshot
    assert a["number"] == 1 and a["frozen_at"]
    assert a["params"] == {"load_N": 1000, "thickness_mm": 4}
    assert "j=t4_F1000" in a["script"] and a["effective_run_config"]["cleanup"] is True
    # input files recorded and stored, including the generated job_params.json
    names = {i["name"]: i for i in a["inputs"]}
    assert set(names) == {"Pre_Processing.py", "Post_Processing.py", "bracket_template.inp", "job_params.json"}
    assert names["bracket_template.inp"]["stored"] == 1
    data = api("GET", f"/api/inputs/{names['bracket_template.inp']['sha256']}").content
    assert data == (folder / "bracket_template.inp").read_bytes()
    # git state of the input folder
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True).stdout.strip()
    assert a["git"]["commit"] == head and a["git"]["dirty"] is False
    # results and metrics, remote folder cleaned after population
    assert a["metrics"]["Max_Mises"] == 460.0
    assert a["remote_cleaned_at"] and not Path(a["run_dir"]).exists()
    assert a["run_dir"].endswith(f"/study_{sid}/job_{jid}/attempt_1")


def test_parameters_freeze_and_run_again_keeps_old_attempt():
    folder = make_inputs(TMP / "pc" / "again")
    sid = study("Run again", folder)["id"]
    jid = job(sid, "t4 F2000", load_N=2000, thickness_mm=4)["id"]
    drive(populated(jid))
    api("PATCH", f"/api/jobs/{jid}", json={"params": {"load_N": 1}}, expect=409)
    api("POST", f"/api/jobs/{jid}/attempts", json={"note": "thicker plate", "params": {"load_N": 2000, "thickness_mm": 5}},
        expect=201)
    # a second attempt can't be queued while one is waiting
    api("POST", f"/api/jobs/{jid}/attempts", json={}, expect=409)
    drive(populated(jid, 2))
    attempts = api("GET", f"/api/jobs/{jid}/attempts")
    a2, a1 = attempts
    assert (a2["number"], a2["note"], a2["params"]["thickness_mm"]) == (2, "thicker plate", 5)
    assert a1["params"]["thickness_mm"] == 4 and a1["metrics"]["Max_Mises"] == 920.0
    assert a2["metrics"]["Max_Mises"] == 588.8
    assert a1["run_dir"] != a2["run_dir"] and Path(a1["run_dir"]).exists()
    # the study comparison uses the latest populated attempt
    rows = api("GET", f"/api/studies/{sid}/metrics")
    assert [(r["attempt_number"], r["metrics"]["Max_Mises"]) for r in rows if r["metrics"]] == [(2, 588.8)]
    # the three input files didn't change, so both attempts point at the same stored copies
    stored1 = {i["name"]: i["sha256"] for i in a1["inputs"] if i["stored"]}
    stored2 = {i["name"]: i["sha256"] for i in a2["inputs"] if i["stored"]}
    assert stored1 == stored2 and len(stored1) == 3
    params1 = next(i for i in a1["inputs"] if i["name"] == "job_params.json")
    params2 = next(i for i in a2["inputs"] if i["name"] == "job_params.json")
    assert params1["sha256"] != params2["sha256"]
    con = sqlite3.connect(os.environ["FEM_DATA_DIR"] + "/fem_tracker.db")
    assert con.execute(f"SELECT COUNT(*) FROM input_blobs WHERE sha256 IN ({','.join('?' * 3)})",
                       list(stored1.values())).fetchone()[0] == 3
    con.close()


def test_repopulating_keeps_earlier_round():
    folder = make_inputs(TMP / "pc" / "repop")
    sid = study("Repopulate", folder, cleanup=False)["id"]
    jid = job(sid, "t4 F1500", load_N=1500, thickness_mm=4)["id"]
    drive(populated(jid))
    aid = latest(jid)["attempt_id"]
    first = api("GET", f"/api/attempts/{aid}/results")
    api("POST", f"/api/attempts/{aid}/populate", json={"populate_status": "pending"})
    drive(lambda: api("GET", f"/api/attempts/{aid}")["population"] == 2 and latest(jid)["stage"] == "populated")
    current = api("GET", f"/api/attempts/{aid}/results")
    everything = api("GET", f"/api/attempts/{aid}/results?include_superseded=true")
    assert len(current) == len(first) and all(r["population"] == 2 for r in current)
    old = [r for r in everything if r["population"] == 1]
    assert len(old) == len(first) and all(r["superseded_at"] for r in old)
    local = TMP / "results" / f"study_{sid}" / f"job_{jid}" / "attempt_1"
    assert (local / "population_1" / "Post_Results.txt").exists() and (local / "population_2" / "Post_Results.txt").exists()
    # automatic results are part of the record and can't be deleted; hand-added ones can
    api("DELETE", f"/api/results/{current[0]['id']}", expect=204)
    assert len(api("GET", f"/api/attempts/{aid}/results")) == len(current)


def test_failed_attempt_then_fixed():
    folder = make_inputs(TMP / "pc" / "fail")
    sid = study("Failure", folder)["id"]
    jid = job(sid, "overload", load_N=3000, thickness_mm=4)["id"]
    drive(populated(jid))
    j = latest(jid)
    assert j["stage"] == "failed" and j["exit_code"] == 1
    api("POST", f"/api/jobs/{jid}/attempts", json={"note": "lower load", "params": {"load_N": 2800, "thickness_mm": 4}})
    drive(populated(jid, 2))
    a2, a1 = api("GET", f"/api/jobs/{jid}/attempts")
    assert (a1["run_status"], a2["run_status"], a2["populate_status"]) == ("failed", "finished", "done")
    assert "exited with code 1" in a1["run_error"]


def test_dirty_git_is_recorded():
    folder = make_inputs(TMP / "pc" / "dirty")
    sid = study("Dirty", folder)["id"]
    (folder / "bracket_template.inp").write_text("*HEADING\nchanged\n@load_N@\n")
    jid = job(sid, "dirty", load_N=1000, thickness_mm=4)["id"]
    drive(populated(jid))
    g = api("GET", f"/api/jobs/{jid}/attempts")[0]["git"]
    assert g["dirty"] is True and any("bracket_template.inp" in f for f in g["changed_files"])


def test_cancel_running_attempt():
    folder = make_inputs(TMP / "pc" / "cancel", git=False)
    sid = study("Cancel", folder)["id"]
    jid = job(sid, "slow", load_N=1000, thickness_mm=4, solve_seconds=60)["id"]
    drive(lambda: latest(jid)["run_status"] == "running")
    api("POST", f"/api/attempts/{latest(jid)['attempt_id']}/cancel")
    drive(lambda: latest(jid)["run_status"] == "cancelled")
    a = api("GET", f"/api/jobs/{jid}/attempts")[0]
    assert a["git"] is None
    assert "terminated" in (Path(a["run_dir"]) / "cancel.log").read_text()


def test_remote_inputs_are_symlinked_and_hashed_not_uploaded():
    folder = make_inputs(TMP / "pc" / "remote_in", git=False)
    shared = TMP / "hpc" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    mesh = shared / "mesh_template.inp"
    mesh.write_text("*HEADING\nshared mesh, lives on the HPC already\n")
    sid = api("POST", "/api/studies", json={"name": "Remote inputs", "run_config": {
        "local_dir": str(folder), "inputs": ["*.py"], "remote_inputs": [str(mesh)],
        "commands": [f"{ABAQUS} python Pre_Processing.py {{job_name}}",
                     f"{ABAQUS} j={{job_name}} cpus=4",
                     'grep -q "COMPLETED SUCCESSFULLY" {job_name}.sta',
                     f"{ABAQUS} python Post_Processing.py {{job_name}}"],
        "retrieve": ["Post_Results.txt"], "cancel_commands": [f"{ABAQUS} terminate job={{job_name}}"]}})["id"]
    jid = job(sid, "remote mesh", load_N=1000, thickness_mm=4)["id"]
    drive(populated(jid))
    j = latest(jid)
    assert j["stage"] == "populated", j
    a = api("GET", f"/api/jobs/{jid}/attempts")[0]
    names = {i["name"]: i for i in a["inputs"]}
    assert set(names) == {"Pre_Processing.py", "Post_Processing.py", "job_params.json", str(mesh)}
    rec = names[str(mesh)]
    # hashed on the HPC and recorded, but never fetched into the database
    assert rec["remote_ref"] == 1 and rec["stored"] == 0
    assert rec["sha256"] == hashlib.sha256(mesh.read_bytes()).hexdigest()
    assert rec["size"] == len(mesh.read_bytes())
    # symlinked into the attempt's folder under its own name, not copied
    link = Path(a["run_dir"]) / "mesh_template.inp"
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(mesh)
    assert a["metrics"]["Max_Mises"] == 460.0


def test_run_type_supplies_commands_study_falls_back_to_it():
    folder = make_inputs(TMP / "pc" / "run_type", git=False)
    rt = api("POST", "/api/run-types", json={"name": "Abaqus bracket", "run_config": {
        "commands": [f"{ABAQUS} python Pre_Processing.py {{job_name}}",
                     f"{ABAQUS} j={{job_name}} cpus=4",
                     'grep -q "COMPLETED SUCCESSFULLY" {job_name}.sta',
                     f"{ABAQUS} python Post_Processing.py {{job_name}}"],
        "retrieve": ["Post_Results.txt"], "cancel_commands": [f"{ABAQUS} terminate job={{job_name}}"]}})
    assert rt["study_count"] == 0
    # the study only says where its inputs are; everything else comes from the run type
    sid = api("POST", "/api/studies", json={"name": "Run type fallback",
        "run_config": {"local_dir": str(folder), "inputs": ["*.py"]}})["id"]
    api("POST", f"/api/studies/{sid}/run-type", json={"run_type_id": rt["id"]})
    s = api("GET", f"/api/studies/{sid}")
    assert s["run_type_id"] == rt["id"] and s["run_type_name"] == "Abaqus bracket"
    assert s["effective_run_config"]["commands"] == rt["run_config"]["commands"]
    assert api("GET", f"/api/run-types/{rt['id']}")["study_count"] == 1

    jid = job(sid, "t4 F1000", load_N=1000, thickness_mm=4)["id"]
    j = api("GET", f"/api/jobs/{jid}")
    assert j["effective_run_config"]["commands"] == rt["run_config"]["commands"]
    drive(populated(jid))
    assert latest(jid)["stage"] == "populated", latest(jid)
    a = api("GET", f"/api/jobs/{jid}/attempts")[0]
    # frozen into the attempt, so later edits to the run type don't change past results
    assert a["effective_run_config"]["commands"] == rt["run_config"]["commands"]
    assert a["metrics"]["Max_Mises"] == 460.0

    # a study without a run type still works the old way, with its own commands
    api("PATCH", f"/api/run-types/{rt['id']}", json={"run_config": {**rt["run_config"], "retrieve": ["changed.txt"]}})
    sid2 = api("POST", "/api/studies", json={"name": "No run type", "run_config": {
        "local_dir": str(folder), "inputs": ["*.py"], "commands": rt["run_config"]["commands"],
        "retrieve": ["Post_Results.txt"]}})["id"]
    s2 = api("GET", f"/api/studies/{sid2}")
    assert s2["run_type_id"] is None and s2["effective_run_config"]["retrieve"] == ["Post_Results.txt"]
    # the earlier study still sees the run type's latest commands (not frozen at the study level)
    assert api("GET", f"/api/studies/{sid}")["effective_run_config"]["retrieve"] == ["changed.txt"]


def test_study_history():
    sid = study("History", TMP)["id"]
    api("PATCH", f"/api/studies/{sid}", json={"results_summary": "first conclusion"})
    api("PATCH", f"/api/studies/{sid}", json={"results_summary": "first conclusion"})  # unchanged: no new revision
    api("PATCH", f"/api/studies/{sid}", json={"results_summary": "revised conclusion"})
    h = api("GET", f"/api/studies/{sid}/history?field=results_summary")
    assert [r["value"] for r in h] == ["revised conclusion", "first conclusion"]


def test_import_folder_as_attempt():
    from femtracker.agent import populate
    folder = make_inputs(TMP / "pc" / "imp", git=False)
    sid = study("Import", folder)["id"]
    old = TMP / "old_run"
    old.mkdir()
    (old / "Post_Results.txt").write_text("Max_Mises = 204.1\n")
    j = api("POST", "/api/jobs", json={"study_id": sid, "name": "legacy", "params": {"load_N": 2500}, "queue": False})
    a = populate.import_folder(j["id"], str(old), "from the old batch script")
    a = api("GET", f"/api/jobs/{j['id']}/attempts")[0]
    assert (a["source"], a["run_status"], a["populate_status"], a["note"]) == \
        ("import", "finished", "done", "from the old batch script")
    assert a["metrics"] == {"Max_Mises": 204.1}
    assert [i["name"] for i in a["inputs"]] == ["Post_Results.txt"]


def test_export_is_deterministic_and_keeps_git(tmp_path):
    from femtracker import export, settings
    sid = next(s["id"] for s in api("GET", "/api/studies") if s["name"] == "Provenance")  # from the first test above
    target, n = export.write_folder(settings.DB_PATH, sid, str(tmp_path))
    snapshot = {p: p.read_bytes() for p in Path(target).rglob("*") if p.is_file()}
    (Path(target) / ".git").mkdir()
    (Path(target) / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    export.write_folder(settings.DB_PATH, sid, str(tmp_path))
    again = {p: p.read_bytes() for p in Path(target).rglob("*") if p.is_file() and ".git" not in p.parts}
    assert again == snapshot
    assert (Path(target) / ".git" / "HEAD").exists()
    rel = {str(p.relative_to(target)) for p in snapshot}
    assert {"study.json", "history.json", "metrics.csv"} <= rel
    attempt = next(p for p in rel if p.endswith("attempt_1/attempt.json"))
    info = json.loads((Path(target) / attempt).read_text())
    assert info["params"] and info["git"]["commit"] and info["inputs"]
    assert any(p.endswith("attempt_1/fem_run.sh") for p in rel)
    assert any(p.endswith("attempt_1/inputs/bracket_template.inp") for p in rel)
    header = (Path(target) / "metrics.csv").read_text().splitlines()[0]
    assert header.startswith("job_id,job,attempt,result,param:load_N")
    # refuses to overwrite a folder it didn't create
    other = tmp_path / "mine"
    other.mkdir()
    (other / Path(target).name).mkdir()
    try:
        export.write_folder(settings.DB_PATH, sid, str(other))
        raise AssertionError("should have refused")
    except FileExistsError:
        pass
