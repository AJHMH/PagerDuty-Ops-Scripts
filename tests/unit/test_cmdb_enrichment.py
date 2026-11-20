"""pd-cmdb-enrichment (Early Access): flag gate, idempotent applies, CSV upload, associations."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import cmdb_enrichment as ce

SN = f"{PD_API_BASE}/enrichment/integrations/servicenow"
EA = "--early-access"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mappings(n=2):
    return [{"servicenow_field": f"f{i}", "event_field": f"e{i}"} for i in range(n)]


def test_requires_early_access_flag():
    with pytest.raises(SystemExit) as exc:
        ce.main(["list"])
    assert exc.value.code == 2


def test_table_validation():
    problems = ce.validate_tables([{"display_name": "CI", "ci_table_name": "cmdb_ci",
                                    "field_mappings": mappings(1)},
                                   {"display_name": "CI", "ci_table_name": "x",
                                    "field_mappings": mappings()}])
    assert any("2-20" in p for p in problems) and any("duplicate" in p for p in problems)


@responses.activate
def test_tables_apply_creates_updates_and_enables(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"cmdb_tables": [
        {"display_name": "Servers", "ci_table_name": "cmdb_ci_server",
         "field_mappings": mappings()},
        {"display_name": "Apps", "ci_table_name": "cmdb_ci_appl", "field_mappings": mappings()},
        {"display_name": "Same", "ci_table_name": "cmdb_ci", "field_mappings": mappings()}]}))
    responses.get(f"{SN}/I1", json={"integration": {"id": "I1", "cmdb_tables": [
        {"id": "T2", "display_name": "Apps", "ci_table_name": "cmdb_ci_appl_old",
         "field_mappings": mappings()},
        {"id": "T3", "display_name": "Same", "ci_table_name": "cmdb_ci",
         "field_mappings": mappings()}]}})
    responses.post(f"{SN}/I1/tables", json={"cmdb_table": {"id": "T1"}})
    responses.put(f"{SN}/I1/tables/T2", json={})
    responses.post(f"{SN}/I1/tables/T1/enable", json={})
    responses.post(f"{SN}/I1/tables/T2/enable", json={})
    assert ce.main(["tables-apply", str(f), "--integration-id", "I1", "--enable", "-y",
                    EA]) == 0
    writes = [(c.request.method, c.request.url.split(SN)[1]) for c in responses.calls
              if c.request.method != "GET"]
    assert writes == [("POST", "/I1/tables"), ("POST", "/I1/tables/T1/enable"),
                      ("PUT", "/I1/tables/T2"), ("POST", "/I1/tables/T2/enable")]


@responses.activate
def test_schemas_apply_idempotent(tmp_path):
    f = tmp_path / "s.json"
    fields = [{"name": "host", "type": "query"}, {"name": "owner", "type": "enriched"}]
    f.write_text(json.dumps({"schemas": [{"name": "hosts", "fields": fields}]}))
    responses.get(f"{PD_API_BASE}/enrichment/schemas",
                  json={"schemas": [{"id": "S1", "name": "hosts", "fields": fields}]})
    assert ce.main(["schemas-apply", str(f), "-y", EA]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
    f.write_text(json.dumps({"schemas": [{"name": "x", "fields": [
        {"name": "a", "type": "enriched"}, {"name": "b", "type": "enriched"}]}]}))
    assert ce.main(["schemas-apply", str(f), "-y", EA]) == 2   # no query field


@responses.activate
def test_records_upload_sends_csv_and_checks_query_columns(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("host,owner,extra\nweb01,ops,x\nweb02,dba,y\n")
    responses.get(f"{PD_API_BASE}/enrichment/schemas/S1", json={"schema": {
        "name": "hosts", "fields": [{"name": "host", "type": "query"},
                                    {"name": "owner", "type": "enriched"}]}})
    responses.post(f"{PD_API_BASE}/enrichment/schemas/S1/records", status=202, body="")
    assert ce.main(["records-upload", str(f), "--schema-id", "S1", "-y", EA]) == 0
    post = responses.calls[-1].request
    assert post.headers["Content-Type"] == "text/csv" and b"web02,dba" in post.body
    assert "filename=r.csv" in post.url
    f.write_text("owner\nops\n")
    assert ce.main(["records-upload", str(f), "--schema-id", "S1", "-y", EA]) == 2


@responses.activate
def test_associations_idempotent_with_delete_body(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("action,type,id\nadd,service,PS1\nadd,service,PS2\n"
                 "remove,event_orchestration,PO1\nremove,service,PS9\n")
    base = f"{PD_API_BASE}/enrichment/event_enrichments/E1/associations"
    responses.get(base, json={"associations": [
        {"type": "service_reference", "id": "PS1"},
        {"type": "event_orchestration_reference", "id": "PO1"}]})
    responses.post(base, json={})
    responses.delete(base, json={})
    assert ce.main(["associations", str(f), "--enrichment-id", "E1", "-y", EA]) == 0
    post = next(c for c in responses.calls if c.request.method == "POST")
    delete = next(c for c in responses.calls if c.request.method == "DELETE")
    assert json.loads(post.request.body) == {"associations": [
        {"type": "service_reference", "id": "PS2"}]}
    assert json.loads(delete.request.body) == {"associations": [
        {"type": "event_orchestration_reference", "id": "PO1"}]}


@responses.activate
def test_tables_apply_ignores_response_only_mapping_fields(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"cmdb_tables": [
        {"display_name": "Servers", "ci_table_name": "cmdb_ci_server",
         "field_mappings": mappings()}]}))
    live_maps = [{**m, "type": "query"} for m in mappings()]
    responses.get(f"{SN}/I1", json={"integration": {"id": "I1", "cmdb_tables": [
        {"id": "T1", "display_name": "Servers", "ci_table_name": "cmdb_ci_server",
         "query_filter": None, "field_mappings": live_maps}]}})
    assert ce.main(["tables-apply", str(f), "--integration-id", "I1", "-y", EA]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_schema_field_change_is_unsupported_and_never_put(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"schemas": [{"name": "hosts", "fields": [
        {"name": "host", "type": "query"}, {"name": "team", "type": "enriched"}]}]}))
    responses.get(f"{PD_API_BASE}/enrichment/schemas", json={"schemas": [
        {"id": "S1", "name": "hosts", "fields": [{"name": "host", "type": "query"},
                                                {"name": "owner", "type": "enriched"}]}]})
    assert ce.main(["schemas-apply", str(f), "--dry-run", EA]) == 1
    assert ce.main(["schemas-apply", str(f), "-y", EA]) == 1
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_associations_paginate_and_count_207_failures(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("action,type,id\nadd,service,PS2\nadd,service,PS3\nremove,service,PS9\n")
    base = f"{PD_API_BASE}/enrichment/event_enrichments/E1/associations"
    responses.get(base, json={"associations": [{"type": "service_reference", "id": "PS1"}],
                              "more": True})
    responses.get(base, json={"associations": [{"type": "service_reference", "id": "PS9"}],
                              "more": False})
    responses.post(base, status=207, json={"associations": [
        {"status": 201, "association": {"type": "service_reference", "id": "PS2"}},
        {"status": 404, "errors": ["service not found"],
         "association": {"type": "service_reference", "id": "PS3"}}]})
    responses.delete(base, json={"status": "ok"})
    assert ce.main(["associations", str(f), "--enrichment-id", "E1", "-y", EA]) == 1
    assert [c.request.method for c in responses.calls].count("DELETE") == 1   # PS9 on page 2
