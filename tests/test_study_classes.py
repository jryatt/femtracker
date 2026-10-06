"""Study classes: reusable KPI templates, pinned to a job in each study, for consistent reporting."""
from conftest import TMP, api, job, study


def make_class(name="Static Strength", kpis=(("Max Stress", "Max_Mises", "MPa"),)):
    c = api("POST", "/api/study-classes", json={"name": name, "description": "d"})
    for kname, key, unit in kpis:
        c = api("POST", f"/api/study-classes/{c['id']}/kpis", json={"name": kname, "metric_key": key, "unit": unit})
    return c


def test_create_class_with_kpis_and_list_shows_counts():
    c = make_class(kpis=(("Max Stress", "Max_Mises", "MPa"), ("Mass", "Mass_kg", "kg")))
    assert [k["name"] for k in c["kpis"]] == ["Max Stress", "Mass"]
    assert c["study_count"] == 0
    listed = next(x for x in api("GET", "/api/study-classes") if x["id"] == c["id"])
    assert len(listed["kpis"]) == 2


def test_assigning_a_class_shows_unpinned_kpis_then_pinning_resolves_live_value():
    c = make_class()
    kid = c["kpis"][0]["id"]
    sid = study("KPI study", TMP)["id"]
    s = api("POST", f"/api/studies/{sid}/class", json={"class_id": c["id"]})
    assert s["class_name"] == "Static Strength"
    jid = job(sid, "j1")["id"]

    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis == [{"id": kid, "name": "Max Stress", "metric_key": "Max_Mises", "unit": "MPa",
                     "source_class_name": "Static Strength", "inherited": False,
                     "job_id": None, "job_name": None, "value": None}]

    api("PUT", f"/api/studies/{sid}/kpis/{kid}", json={"job_id": jid})
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis[0]["job_id"] == jid and kpis[0]["value"] is None  # pinned, but nothing reported yet

    api("POST", f"/api/jobs/{jid}/results", data={"name": "r", "description": "", "metrics": '{"Max_Mises": 180.5}'})
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis[0]["value"] == 180.5

    api("DELETE", f"/api/studies/{sid}/kpis/{kid}", expect=204)
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis[0]["job_id"] is None


def test_metric_key_mismatch_shows_not_reported_not_a_wrong_value():
    c = make_class(kpis=(("Max Stress", "Max_Mises", "MPa"),))
    kid = c["kpis"][0]["id"]
    sid = study("KPI mismatch", TMP)["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": c["id"]})
    jid = job(sid, "j1")["id"]
    api("PUT", f"/api/studies/{sid}/kpis/{kid}", json={"job_id": jid})
    # the job reports a differently-named metric: the KPI stays "not reported" rather than guessing
    api("POST", f"/api/jobs/{jid}/results", data={"name": "r", "description": "", "metrics": '{"peak_stress": 180.5}'})
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis[0]["value"] is None


def test_reassigning_a_studys_class_clears_its_pins():
    c1 = make_class("Class A")
    c2 = make_class("Class B")
    sid = study("Reassign", TMP)["id"]
    jid = job(sid, "j1")["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": c1["id"]})
    api("PUT", f"/api/studies/{sid}/kpis/{c1['kpis'][0]['id']}", json={"job_id": jid})
    assert api("GET", f"/api/studies/{sid}/kpis")[0]["job_id"] == jid

    api("POST", f"/api/studies/{sid}/class", json={"class_id": c2["id"]})
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert len(kpis) == 1 and kpis[0]["job_id"] is None  # new class's KPI, unpinned

    api("POST", f"/api/studies/{sid}/class", json={"class_id": None})
    assert api("GET", f"/api/studies/{sid}/kpis") == []


def test_deleting_a_class_clears_the_studys_class_but_keeps_the_study():
    c = make_class()
    sid = study("Orphaned", TMP)["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": c["id"]})
    api("DELETE", f"/api/study-classes/{c['id']}", expect=204)
    s = api("GET", f"/api/studies/{sid}")
    assert s["class_id"] is None and s["name"] == "Orphaned"
    assert api("GET", f"/api/studies/{sid}/kpis") == []


def test_deleting_a_kpi_removes_its_pins_but_keeps_other_kpis():
    c = make_class(kpis=(("Max Stress", "Max_Mises", "MPa"), ("Mass", "Mass_kg", "kg")))
    k1, k2 = c["kpis"]
    sid = study("Kpi delete", TMP)["id"]
    jid = job(sid, "j1")["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": c["id"]})
    api("PUT", f"/api/studies/{sid}/kpis/{k1['id']}", json={"job_id": jid})
    api("DELETE", f"/api/study-classes/{c['id']}/kpis/{k1['id']}", expect=204)
    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert [k["name"] for k in kpis] == ["Mass"]


def test_editing_a_kpi_definition():
    c = make_class(kpis=(("Max Stress", "Max_Mises", "MPa"),))
    kid = c["kpis"][0]["id"]
    updated = api("PATCH", f"/api/study-classes/{c['id']}/kpis/{kid}", json={"unit": "ksi"})
    assert updated["kpis"][0]["unit"] == "ksi" and updated["kpis"][0]["name"] == "Max Stress"


def test_pinning_a_job_from_another_study_is_refused():
    c = make_class()
    sid1 = study("Study A", TMP)["id"]
    sid2 = study("Study B", TMP)["id"]
    api("POST", f"/api/studies/{sid1}/class", json={"class_id": c["id"]})
    jid_other_study = job(sid2, "elsewhere")["id"]
    api("PUT", f"/api/studies/{sid1}/kpis/{c['kpis'][0]['id']}", json={"job_id": jid_other_study}, expect=422)


def test_child_class_inherits_parents_kpis():
    parent = make_class("Section of the Plane", kpis=(("Mass", "Mass_kg", "kg"),))
    child = api("POST", "/api/study-classes", json={"name": "Crash Model", "parent_id": parent["id"]})
    child = api("POST", f"/api/study-classes/{child['id']}/kpis",
               json={"name": "Peak G", "metric_key": "Peak_G", "unit": "g"})
    assert child["parent_name"] == "Section of the Plane"
    names = {k["name"]: k for k in child["effective_kpis"]}
    assert set(names) == {"Mass", "Peak G"}
    assert names["Mass"]["inherited"] is True and names["Mass"]["source_class_name"] == "Section of the Plane"
    assert names["Peak G"]["inherited"] is False and names["Peak G"]["source_class_name"] == "Crash Model"
    parent_full = api("GET", f"/api/study-classes/{parent['id']}")
    assert [c["name"] for c in parent_full["children"]] == ["Crash Model"]


def test_child_kpi_overrides_parent_kpi_with_the_same_metric_key():
    parent = make_class("Base", kpis=(("Max Stress", "Max_Mises", "MPa"),))
    child = api("POST", "/api/study-classes", json={"name": "Specific", "parent_id": parent["id"]})
    child = api("POST", f"/api/study-classes/{child['id']}/kpis",
               json={"name": "Peak Stress", "metric_key": "Max_Mises", "unit": "ksi"})
    effective = child["effective_kpis"]
    assert len(effective) == 1  # the child's definition shadows the parent's, not both
    assert effective[0]["name"] == "Peak Stress" and effective[0]["unit"] == "ksi"
    assert effective[0]["inherited"] is False


def test_three_level_inheritance_chain_closest_wins():
    a = make_class("A", kpis=(("KPI A", "key_a", ""),))
    b = api("POST", "/api/study-classes", json={"name": "B", "parent_id": a["id"]})
    b = api("POST", f"/api/study-classes/{b['id']}/kpis", json={"name": "KPI B", "metric_key": "key_b"})
    c = api("POST", "/api/study-classes", json={"name": "C", "parent_id": b["id"]})
    c = api("POST", f"/api/study-classes/{c['id']}/kpis", json={"name": "KPI C", "metric_key": "key_c"})
    effective = api("GET", f"/api/study-classes/{c['id']}")["effective_kpis"]
    assert [k["name"] for k in effective] == ["KPI C", "KPI B", "KPI A"]  # own first, then closest ancestor first
    assert [k["source_class_name"] for k in effective] == ["C", "B", "A"]


def test_self_parent_and_cycles_are_rejected():
    a = make_class("A")
    api("POST", f"/api/study-classes/{a['id']}/parent", json={"parent_id": a["id"]}, expect=422)
    b = api("POST", "/api/study-classes", json={"name": "B", "parent_id": a["id"]})
    # A can't become a child of its own descendant B
    api("POST", f"/api/study-classes/{a['id']}/parent", json={"parent_id": b["id"]}, expect=422)


def test_deleting_a_parent_detaches_children_without_deleting_their_own_kpis():
    parent = make_class("Parent", kpis=(("Mass", "Mass_kg", "kg"),))
    child = api("POST", "/api/study-classes", json={"name": "Child", "parent_id": parent["id"]})
    child = api("POST", f"/api/study-classes/{child['id']}/kpis", json={"name": "Peak G", "metric_key": "Peak_G"})

    api("DELETE", f"/api/study-classes/{parent['id']}", expect=204)
    child = api("GET", f"/api/study-classes/{child['id']}")
    assert child["parent_id"] is None
    assert [k["name"] for k in child["effective_kpis"]] == ["Peak G"]  # kept its own, lost the inherited one


def test_study_scorecard_resolves_an_inherited_kpi_to_a_pinned_jobs_live_value():
    parent = make_class("Section of the Plane", kpis=(("Mass", "Mass_kg", "kg"),))
    child = api("POST", "/api/study-classes", json={"name": "Crash Model", "parent_id": parent["id"]})
    inherited_kpi_id = child["effective_kpis"][0]["id"]  # the parent's own row id, inherited by the child
    assert inherited_kpi_id == parent["kpis"][0]["id"]

    sid = study("Crash case 1", TMP)["id"]
    jid = job(sid, "j1")["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": child["id"]})
    api("PUT", f"/api/studies/{sid}/kpis/{inherited_kpi_id}", json={"job_id": jid})
    api("POST", f"/api/jobs/{jid}/results", data={"name": "r", "description": "", "metrics": '{"Mass_kg": 42.0}'})

    kpis = api("GET", f"/api/studies/{sid}/kpis")
    assert kpis == [{"id": inherited_kpi_id, "name": "Mass", "metric_key": "Mass_kg", "unit": "kg",
                     "source_class_name": "Section of the Plane", "inherited": True,
                     "job_id": jid, "job_name": "j1", "value": 42.0}]


def test_export_includes_kpis_json_with_resolved_values(tmp_path):
    from femtracker import export, settings

    c = make_class(kpis=(("Max Stress", "Max_Mises", "MPa"),))
    sid = study("Export KPI", TMP)["id"]
    jid = job(sid, "j1")["id"]
    api("POST", f"/api/studies/{sid}/class", json={"class_id": c["id"]})
    api("PUT", f"/api/studies/{sid}/kpis/{c['kpis'][0]['id']}", json={"job_id": jid})
    api("POST", f"/api/jobs/{jid}/results", data={"name": "r", "description": "", "metrics": '{"Max_Mises": 99.5}'})

    import json
    from pathlib import Path
    target, _ = export.write_folder(settings.DB_PATH, sid, str(tmp_path))
    data = json.loads((Path(target) / "kpis.json").read_text())
    assert data["class"] == "Static Strength"
    assert data["kpis"] == [{"name": "Max Stress", "metric_key": "Max_Mises", "unit": "MPa",
                             "job_id": jid, "job_name": "j1", "value": 99.5}]
    study_json = json.loads((Path(target) / "study.json").read_text())
    assert study_json["class"] == "Static Strength"
