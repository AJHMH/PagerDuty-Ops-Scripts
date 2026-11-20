"""pd-service-deps: export dedupe, CSV validation, idempotent batched apply."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import service_deps

HDR = "action,supporting_kind,supporting_id,dependent_kind,dependent_id\n"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def rel(sup, dep, st="service", dt="business_service_reference"):
    return {"supporting_service": {"id": sup, "type": st}, "dependent_service": {"id": dep,
                                                                                   "type": dt}}


@responses.activate
def test_export_dedupes_relationships(capsys):
    responses.get(f"{PD_API_BASE}/services", json={"services": [{"id": "PS1"}], "more": False})
    responses.get(f"{PD_API_BASE}/business_services",
                  json={"business_services": [{"id": "PB1"}], "more": False})
    both = {"relationships": [rel("PS1", "PB1")]}
    responses.get(f"{PD_API_BASE}/service_dependencies/technical_services/PS1", json=both)
    responses.get(f"{PD_API_BASE}/service_dependencies/business_services/PB1", json=both)
    assert service_deps.main(["export"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[1:] == ["associate,service,PS1,business_service,PB1"]


def test_csv_validation(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(HDR + "link,service,PS1,service,PS2\nassociate,service,PS1,service,PS1\n")
    with pytest.raises(SystemExit) as exc:
        service_deps.load_rows(str(p))
    assert exc.value.code == 2


@responses.activate
def test_apply_skips_existing_and_batches(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(HDR + "associate,service,PS1,business_service,PB1\n"   # exists: skip
                 "associate,service,PS2,business_service,PB1\n"
                 "associate,service,PS3,business_service,PB1\n"
                 "disassociate,service,PS9,business_service,PB1\n")  # absent: skip
    responses.get(f"{PD_API_BASE}/service_dependencies/business_services/PB1",
                  json={"relationships": [rel("PS1", "PB1")]})
    responses.post(f"{PD_API_BASE}/service_dependencies/associate", json={})
    assert service_deps.main(["apply", str(p), "-y", "--batch-size", "1"]) == 0
    posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(posts) == 2
    assert json.loads(posts[0].request.body)["relationships"][0]["supporting_service"] == {
        "id": "PS2", "type": "service"}


@responses.activate
def test_apply_dry_run_and_contradiction(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(HDR + "associate,service,PS2,business_service,PB1\n")
    responses.get(f"{PD_API_BASE}/service_dependencies/business_services/PB1",
                  json={"relationships": []})
    assert service_deps.main(["apply", str(p), "--dry-run"]) == 0
    assert not [c for c in responses.calls if c.request.method == "POST"]
    p.write_text(HDR + "associate,service,PS2,service,PS3\ndisassociate,service,PS2,service,PS3\n")
    assert service_deps.main(["apply", str(p), "-y"]) == 2
