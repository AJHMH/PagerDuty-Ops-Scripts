"""pd-business-services: name-keyed export, validation, diff-first apply, capped prune."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import business_services as bsvc

BS = f"{PD_API_BASE}/business_services"
LIVE = [{"id": "B1", "name": "Water Billing", "description": "Pay bills",
         "point_of_contact": "ops@dallas.gov", "team": {"id": "PT1"}},
        {"id": "B2", "name": "311 Intake", "description": "", "point_of_contact": "",
         "team": None}]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock(live=None):
    responses.get(f"{PD_API_BASE}/teams", json={"teams": [{"id": "PT1", "name": "Utilities"},
                                                          {"id": "PT2", "name": "311"}],
                                                "more": False})
    responses.get(BS, json={"business_services": live if live is not None else LIVE,
                            "more": False})


def write(tmp_path, entries):
    p = tmp_path / "bs.json"
    p.write_text(json.dumps({"business_services": entries}))
    return str(p)


@responses.activate
def test_export_is_name_keyed_without_ids(tmp_path):
    mock()
    out = tmp_path / "bs.json"
    assert bsvc.main(["export", "-o", str(out)]) == 0
    doc = json.loads(out.read_text())["business_services"]
    assert [b["name"] for b in doc] == ["311 Intake", "Water Billing"]
    assert doc[1] == {"name": "Water Billing", "description": "Pay bills",
                      "point_of_contact": "ops@dallas.gov", "team_id": "PT1",
                      "team_name": "Utilities"}
    assert "id" not in json.dumps(doc).replace('"team_id"', "")


@responses.activate
def test_export_then_apply_is_a_noop(tmp_path, capsys):
    mock()
    out = tmp_path / "bs.json"
    bsvc.main(["export", "-o", str(out)])
    assert bsvc.main(["apply", "-i", str(out), "--apply", "-y", "--prune"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_validation_errors_exit_2_before_writes(tmp_path):
    mock()
    for entries in ([{"name": "A"}, {"name": "a"}],                 # duplicate (case-insensitive)
                    [{"name": "A", "team_id": "PNOPE"}],             # unknown team
                    [{"description": "no name"}],                    # missing name
                    [{"name": "A", "owner": "x"}]):                  # unknown key
        with pytest.raises(SystemExit) as exc:
            bsvc.main(["apply", "-i", write(tmp_path, entries), "--apply", "-y"])
        assert exc.value.code == 2
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_diff_only_then_apply_create_update_prune(tmp_path, capsys):
    mock()
    path = write(tmp_path, [
        {"name": "Water Billing", "description": "Pay bills", "point_of_contact": "ops@dallas.gov",
         "team_id": "PT2"},                                            # team moves PT1 -> PT2
        {"name": "Streets", "description": "Potholes", "team_id": "PT2"}])  # new
    assert bsvc.main(["apply", "-i", path, "--prune"]) == 0          # diff only
    out = capsys.readouterr().out
    assert "~ Water Billing: team_id 'PT1'->'PT2'" in out
    assert "+ Streets" in out and "- 311 Intake (B2)" in out
    assert all(c.request.method == "GET" for c in responses.calls)
    responses.put(f"{BS}/B1", json={})
    responses.post(BS, json={})
    responses.delete(f"{BS}/B2", status=204)
    assert bsvc.main(["apply", "-i", path, "--prune", "--apply", "-y"]) == 0
    put = next(c for c in responses.calls if c.request.method == "PUT")
    assert json.loads(put.request.body)["business_service"]["team"] == {
        "id": "PT2", "type": "team_reference"}
    assert [c.request.method for c in responses.calls if c.request.method != "GET"] == [
        "PUT", "POST", "DELETE"]


@responses.activate
def test_without_prune_nothing_is_deleted_and_cap_applies(tmp_path):
    mock()
    path = write(tmp_path, [])
    assert bsvc.main(["apply", "-i", path, "--apply", "-y"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
    assert bsvc.main(["apply", "-i", path, "--prune", "--apply", "-y",
                      "--max-deletes", "1"]) == 2
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_ambiguous_live_names_and_apply_requires_yes(tmp_path):
    mock(LIVE + [{"id": "B3", "name": "water billing", "team": None}])
    path = write(tmp_path, [{"name": "Water Billing"}])
    assert bsvc.main(["apply", "-i", path]) == 2
    assert bsvc.main(["apply", "-i", path, "--apply"]) == 2
