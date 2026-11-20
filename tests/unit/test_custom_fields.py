"""pd-custom-fields: export by name, diff semantics, prune, immutables, apply."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import custom_fields as cf

SVC = f"{PD_API_BASE}/services/custom_fields"
TYPES = f"{PD_API_BASE}/incidents/types"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock_live(svc_fields=None, types=None, type_fields=None):
    responses.get(SVC, json={"fields": svc_fields if svc_fields is not None else [
        {"id": "F1", "name": "tier", "display_name": "Tier", "data_type": "string",
         "field_type": "single_value_fixed", "enabled": True},
        {"id": "F2", "name": "owner_email", "display_name": "Owner", "data_type": "string",
         "field_type": "single_value", "enabled": True}], "more": False})
    responses.get(f"{SVC}/F1/field_options", json={"field_options": [
        {"id": "O1", "data": {"data_type": "string", "value": "gold"}},
        {"id": "O2", "data": {"data_type": "string", "value": "bronze"}}]})
    responses.get(TYPES, json={"incident_types": types if types is not None else [
        {"id": "T1", "name": "incident_default", "display_name": "Base", "enabled": True,
         "parent_type": None}]})
    responses.get(f"{TYPES}/incident_default/custom_fields",
                  json={"fields": type_fields or [], "more": False})


@responses.activate
def test_export_is_keyed_by_name_without_ids(tmp_path):
    mock_live()
    assert cf.main(["export", "-o", str(tmp_path)]) == 0
    svc = json.loads((tmp_path / cf.SERVICE_FILE).read_text())["fields"]
    assert svc[1] == {"name": "tier", "display_name": "Tier", "data_type": "string",
                      "field_type": "single_value_fixed", "enabled": True,
                      "options": ["bronze", "gold"]}
    assert "id" not in json.dumps(svc)


def test_diff_fields_create_update_options_and_prune():
    live = [{"id": "F1", "name": "tier", "display_name": "Tier", "data_type": "string",
             "field_type": "single_value_fixed", "enabled": True,
             "_options": {"gold": "O1", "bronze": "O2"}, "options": ["bronze", "gold"]},
            {"id": "F2", "name": "old", "display_name": "Old", "data_type": "string",
             "field_type": "single_value"}]
    desired = [{"name": "tier", "display_name": "Service Tier", "data_type": "string",
                "field_type": "single_value_fixed", "options": ["gold", "silver"]},
               {"name": "region", "display_name": "Region", "data_type": "string",
                "field_type": "single_value"}]
    changes, notes, errors = cf.diff_fields("services/custom_fields", desired, live, prune=False)
    assert [c["action"] for c in changes] == ["update_field", "create_option", "create_field"]
    assert len(notes) == 2 and errors == []           # bronze option + old field kept
    changes, _, _ = cf.diff_fields("services/custom_fields", desired, live, prune=True)
    assert {c["action"] for c in changes} >= {"delete_option", "delete_field"}


def test_immutable_change_is_an_error():
    live = [{"id": "F1", "name": "n", "data_type": "string", "field_type": "single_value"}]
    _, _, errors = cf.diff_fields("b", [{"name": "n", "data_type": "integer"}], live, False)
    assert errors and "cannot change in place" in errors[0]


@responses.activate
def test_apply_diff_only_then_apply(tmp_path, capsys):
    (tmp_path / cf.SERVICE_FILE).write_text(json.dumps({"fields": [
        {"name": "tier", "display_name": "Tier", "data_type": "string",
         "field_type": "single_value_fixed", "enabled": True, "options": ["bronze", "gold",
                                                                          "silver"]},
        {"name": "owner_email", "display_name": "Owner", "data_type": "string",
         "field_type": "single_value", "enabled": True}]}))
    (tmp_path / cf.TYPES_FILE).write_text(json.dumps({"incident_types": [
        {"name": "incident_default", "display_name": "Base", "enabled": True},
        {"name": "security", "display_name": "Security", "parent_type": "incident_default",
         "fields": [{"name": "cve", "display_name": "CVE", "data_type": "string",
                     "field_type": "single_value"}]}]}))
    mock_live()
    assert cf.main(["apply", "-i", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "+ services/custom_fields tier option 'silver'" in out
    assert "+ incident type security" in out and "incidents/types/security/custom_fields cve" in out
    assert not [c for c in responses.calls if c.request.method != "GET"]

    responses.post(f"{SVC}/F1/field_options", json={})
    responses.post(TYPES, json={})
    responses.post(f"{TYPES}/security/custom_fields", json={})
    assert cf.main(["apply", "-i", str(tmp_path), "--apply"]) == 2      # needs -y
    assert cf.main(["apply", "-i", str(tmp_path), "--apply", "-y"]) == 0
    writes = [c.request.url.split(PD_API_BASE)[1] for c in responses.calls
              if c.request.method == "POST"]
    assert writes == ["/services/custom_fields/F1/field_options", "/incidents/types",
                      "/incidents/types/security/custom_fields"]


def test_invalid_file_rejected(tmp_path):
    (tmp_path / cf.SERVICE_FILE).write_text(json.dumps({"fields": [{"name": "a"},
                                                                   {"name": "a"}]}))
    with pytest.raises(SystemExit) as exc:
        cf.main(["apply", "-i", str(tmp_path)])
    assert exc.value.code == 2


@responses.activate
def test_live_types_include_disabled_and_resolve_parent_names():
    responses.get(TYPES, json={"incident_types": [
        {"id": "T1", "name": "incident_default", "display_name": "Base", "enabled": True},
        {"id": "T2", "name": "security", "display_name": "Security", "enabled": False,
         "parent": {"id": "T1", "type": "incident_type_reference"}}]})
    types = {t["name"]: t for t in cf.live_types("t")}
    assert "filter=all" in responses.calls[0].request.url
    assert types["security"]["parent_type"] == "incident_default"
    assert types["security"]["enabled"] is False
