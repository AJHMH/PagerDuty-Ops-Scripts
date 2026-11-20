"""pd-service-field-values: export round-trip, typed validation, idempotent atomic apply."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import service_field_values as sfv

FIELDS = {"fields": [
    {"id": "F1", "name": "tier", "data_type": "string", "field_type": "single_value_fixed",
     "field_options": [{"data": {"data_type": "string", "value": v}} for v in ("gold", "bronze")]},
    {"id": "F2", "name": "envs", "data_type": "string", "field_type": "multi_value"},
    {"id": "F3", "name": "pci", "data_type": "boolean", "field_type": "single_value"},
    {"id": "F4", "name": "runbook", "data_type": "url", "field_type": "single_value"},
], "more": False}
SERVICES = {"services": [{"id": "PS1", "name": "Billing"}, {"id": "PS2", "name": "Web"}],
            "more": False}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock_base(values=None):
    responses.get(f"{PD_API_BASE}/services/custom_fields", json=FIELDS)
    responses.get(f"{PD_API_BASE}/services", json=SERVICES)
    values = values or {}
    for sid in ("PS1", "PS2"):
        responses.get(f"{PD_API_BASE}/services/{sid}/custom_fields/values", json={
            "custom_fields": [{"name": n, "value": v} for n, v in values.get(sid, {}).items()]})


def defs():
    return {f["name"]: {"id": f["id"], "data_type": f["data_type"],
                        "field_type": f["field_type"],
                        "options": {o["data"]["value"] for o in f.get("field_options", [])}
                        if f["field_type"].endswith("_fixed") else None}
            for f in FIELDS["fields"]}


@responses.activate
def test_export_wide_csv(capsys):
    mock_base({"PS1": {"tier": "gold", "envs": ["prod", "dr"], "pci": True}})
    assert sfv.main(["export"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "service_id,service_name,envs,pci,runbook,tier"
    assert lines[1] == "PS1,Billing,prod|dr,true,,gold"
    assert lines[2] == "PS2,Web,,,,"


def test_parse_cell_types_and_options():
    d = defs()
    assert sfv.parse_cell("yes", d["pci"]) is True
    assert sfv.parse_cell("prod | dr", d["envs"]) == ["prod", "dr"]
    for field, bad in (("tier", "platinum"), ("pci", "maybe"), ("runbook", "ftp://x"),
                       ("envs", "a|a")):
        with pytest.raises(ValueError):
            sfv.parse_cell(bad, d[field])


def test_same_value_multi_is_order_insensitive():
    d = defs()
    assert sfv.same_value(["a", "b"], ["b", "a"], d["envs"])
    assert not sfv.same_value(None, "gold", d["tier"])


@responses.activate
def test_apply_validates_everything_before_writing(tmp_path):
    mock_base()
    p = tmp_path / "v.csv"
    p.write_text("service_id,service_name,tier,bogus\nPS1,Billing,platinum,x\nPS9,,gold,\n")
    with pytest.raises(SystemExit) as exc:
        sfv.main(["apply", str(p), "-y"])
    assert exc.value.code == 2
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_apply_sends_only_changed_fields_and_skips_matching_services(tmp_path):
    mock_base({"PS1": {"tier": "gold", "envs": ["dr", "prod"]}, "PS2": {"tier": "bronze"}})
    responses.put(f"{PD_API_BASE}/services/PS1/custom_fields/values", json={})
    p = tmp_path / "v.csv"
    p.write_text("service_id,service_name,tier,envs,pci\n"
                 "PS1,Billing,gold,prod|dr,true\n"      # only pci changes
                 "PS2,Web,bronze,,\n")                  # nothing changes; empties ignored
    assert sfv.main(["apply", str(p), "-y"]) == 0
    puts = [c for c in responses.calls if c.request.method == "PUT"]
    assert len(puts) == 1
    assert json.loads(puts[0].request.body) == {"custom_fields": [{"name": "pci", "value": True}]}


@responses.activate
def test_clear_empty_resets_and_dry_run_writes_nothing(tmp_path):
    mock_base({"PS2": {"tier": "bronze"}})
    p = tmp_path / "v.csv"
    p.write_text("service_id,tier\nPS2,\n")
    assert sfv.main(["apply", str(p), "-y"]) == 0           # empty = unchanged
    assert sfv.main(["apply", str(p), "--clear-empty", "--dry-run"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
    responses.put(f"{PD_API_BASE}/services/PS2/custom_fields/values", json={})
    assert sfv.main(["apply", str(p), "--clear-empty", "-y"]) == 0
    put = next(c for c in responses.calls if c.request.method == "PUT")
    assert json.loads(put.request.body) == {"custom_fields": [{"name": "tier", "value": None}]}


@responses.activate
def test_failed_put_exits_one(tmp_path):
    mock_base()
    responses.put(f"{PD_API_BASE}/services/PS1/custom_fields/values", status=400,
                  json={"error": {"message": "bad"}})
    p = tmp_path / "v.csv"
    p.write_text("service_id,tier\nPS1,gold\n")
    assert sfv.main(["apply", str(p), "-y"]) == 1


def test_multi_value_escaping_round_trips():
    d = defs()
    values = ["us|east", "back\\slash", "prod"]
    cell = sfv.format_value(values, d["envs"])
    assert cell == "us\\|east|back\\\\slash|prod"
    assert sfv.parse_cell(cell, d["envs"]) == values
    with pytest.raises(ValueError):
        sfv.parse_cell("bad\\escape", d["envs"])


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "1e999"])
def test_non_finite_floats_rejected(text):
    defn = {"data_type": "float", "field_type": "single_value", "options": None}
    with pytest.raises(ValueError):
        sfv.parse_cell(text, defn)


@responses.activate
def test_unchanged_export_with_pipe_in_value_applies_as_noop(tmp_path, capsys):
    mock_base({"PS1": {"envs": ["us|east", "prod"]}})
    assert sfv.main(["export", "--fields", "envs"]) == 0
    p = tmp_path / "v.csv"
    p.write_text(capsys.readouterr().out)
    assert sfv.main(["apply", str(p), "-y"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
