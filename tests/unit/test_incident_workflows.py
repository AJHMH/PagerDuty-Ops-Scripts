"""pd-incident-workflows: export/diff stability, trigger-link export and idempotent apply."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import incident_workflows as iw

WF = f"{PD_API_BASE}/incident_workflows"
TRIG = f"{PD_API_BASE}/incident_workflows/triggers"


def workflow(name="Major Incident", steps=None, updated="2026-11-01T00:00:00Z"):
    return {"id": "W1", "name": name, "is_enabled": True, "updated_at": updated,
            "self": "https://api/x", "steps": steps or [{"id": "S1", "name": "Page leads"}]}


def triggers():
    return [{"id": "T1", "trigger_type": "conditional", "condition": "incident.priority=P1",
             "workflow": {"id": "W1", "name": "Major Incident"},
             "services": [{"id": "PS1"}], "is_subscribed_to_all_services": False},
            {"id": "T2", "trigger_type": "manual", "workflow": {"id": "W1",
                                                                "name": "Major Incident"},
             "services": [], "is_subscribed_to_all_services": True}]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock(wf=None):
    responses.get(WF, json={"incident_workflows": [wf or workflow()], "more": False})
    responses.get(TRIG, json={"triggers": triggers(), "next_cursor": None})


@responses.activate
def test_export_writes_stable_docs_without_server_noise(tmp_path):
    mock()
    assert iw.main(["export", "-o", str(tmp_path)]) == 0
    (path,) = tmp_path.glob("*.json")
    assert path.name == "W1__major-incident.json"
    doc = json.loads(path.read_text())
    assert "updated_at" not in doc["workflow"] and "self" not in doc["workflow"]
    assert [t["id"] for t in doc["triggers"]] == ["T1", "T2"]
    assert "include%5B%5D=steps" in responses.calls[0].request.url


@responses.activate
def test_diff_ignores_volatile_fields_but_reports_real_changes(tmp_path, capsys):
    mock()
    assert iw.main(["export", "-o", str(tmp_path)]) == 0
    responses.reset()
    mock(workflow(updated="2026-11-20T00:00:00Z"))          # only volatile change
    assert iw.main(["diff", "-i", str(tmp_path), "--fail-on-drift"]) == 0
    responses.reset()
    mock(workflow(steps=[{"id": "S1", "name": "Page EVERYONE"}]))
    assert iw.main(["diff", "-i", str(tmp_path), "--fail-on-drift"]) == 1
    assert "Page EVERYONE" in capsys.readouterr().out


@responses.activate
def test_triggers_export_omits_all_services_triggers(capsys):
    mock()
    assert iw.main(["triggers-export"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[1:] == ["attach,T1,PS1,Major Incident,conditional"]


HDR = "action,trigger_id,service_id\n"


@responses.activate
def test_triggers_apply_validates_before_writing(tmp_path):
    mock()
    responses.get(f"{PD_API_BASE}/services", json={"services": [{"id": "PS1"}, {"id": "PS2"}],
                                                   "more": False})
    p = tmp_path / "t.csv"
    p.write_text(HDR + "link,T1,PS2\nattach,TNOPE,PS2\nattach,T1,PNOPE\nattach,T2,PS2\n")
    assert iw.main(["triggers-apply", str(p), "-y"]) == 2
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_triggers_apply_idempotent_attach_and_detach(tmp_path):
    mock()
    responses.get(f"{PD_API_BASE}/services", json={"services": [{"id": "PS1"}, {"id": "PS2"}],
                                                   "more": False})
    responses.post(f"{TRIG}/T1/services", json={"trigger": {}})
    responses.delete(f"{TRIG}/T1/services/PS1", json={"trigger": {}})
    p = tmp_path / "t.csv"
    p.write_text(HDR + "attach,T1,PS1\nattach,T1,PS2\n")      # PS1 already attached
    assert iw.main(["triggers-apply", str(p), "-y"]) == 0
    writes = [(c.request.method, c.request.url.split(PD_API_BASE)[1]) for c in responses.calls
              if c.request.method != "GET"]
    assert writes == [("POST", "/incident_workflows/triggers/T1/services")]
    assert json.loads(responses.calls[-1].request.body) == {"service": {"id": "PS2"}}
    p.write_text(HDR + "detach,T1,PS1\ndetach,T1,PS2\n")      # PS2 is not linked (live)
    assert iw.main(["triggers-apply", str(p), "--dry-run"]) == 0
    assert iw.main(["triggers-apply", str(p), "-y"]) == 0
    assert [c.request.method for c in responses.calls].count("DELETE") == 1


@responses.activate
def test_reexport_after_rename_removes_stale_file_and_diff_is_clean(tmp_path):
    mock(workflow(name="Zulu"))
    assert iw.main(["export", "-o", str(tmp_path)]) == 0
    (tmp_path / "notes.json").write_text("{}")                  # not ours: kept
    responses.reset()
    mock(workflow(name="Alpha"))
    assert iw.main(["export", "-o", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["W1__alpha.json", "notes.json"]
    (tmp_path / "notes.json").unlink()
    assert iw.main(["diff", "-i", str(tmp_path), "--fail-on-drift"]) == 0


@responses.activate
def test_diff_rejects_duplicate_workflow_ids(tmp_path):
    mock()
    assert iw.main(["export", "-o", str(tmp_path)]) == 0
    (path,) = tmp_path.glob("*.json")
    (tmp_path / "W1__old-name.json").write_text(path.read_text())
    assert iw.main(["diff", "-i", str(tmp_path)]) == 2


@responses.activate
def test_triggers_apply_rejects_incomplete_rows_before_writing(tmp_path):
    mock()
    responses.get(f"{PD_API_BASE}/services", json={"services": [{"id": "PS1"}, {"id": "PS2"}],
                                                   "more": False})
    responses.post(f"{TRIG}/T1/services", json={"trigger": {}})
    p = tmp_path / "t.csv"
    p.write_text(HDR + "attach,T1,PS2\ndetach,T1,\n")
    assert iw.main(["triggers-apply", str(p), "-y"]) == 2
    assert all(c.request.method == "GET" for c in responses.calls)
