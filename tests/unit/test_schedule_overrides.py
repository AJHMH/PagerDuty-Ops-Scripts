"""pd-schedule-overrides: email lookup, validation, idempotent create/delete, v2 + v3."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import schedule_overrides as so

HDR = "action,schedule_id,user_id,user_email,start,end\n"
USERS = {"users": [{"id": "PU1", "email": "Ann@Dallas.gov"}, {"id": "PU2", "email": "dup@x.gov"},
                   {"id": "PU3", "email": "DUP@x.gov"}], "more": False}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def csv(tmp_path, body, header=HDR):
    p = tmp_path / "o.csv"
    p.write_text(header + body)
    return str(p)


@responses.activate
def test_email_lookup_exact_case_insensitive_and_ambiguous(tmp_path):
    responses.get(f"{PD_API_BASE}/users", json=USERS)
    rows = so.load_rows(csv(tmp_path, "create,PS1,,ann@dallas.gov,2026-12-24T17:00:00-06:00,"
                                      "2026-12-26T08:00:00-06:00\n"), "t", v3=False)
    assert rows[0]["user_id"] == "PU1"
    with pytest.raises(SystemExit) as exc:
        so.load_rows(csv(tmp_path, "create,PS1,,dup@x.gov,2026-12-24T00:00:00Z,"
                                   "2026-12-25T00:00:00Z\n"), "t", v3=False)
    assert exc.value.code == 2


def test_validation_rejects_both_ids_bad_order_and_naive_time(tmp_path):
    with pytest.raises(SystemExit):
        so.load_rows(csv(tmp_path, "create,PS1,PU1,a@b.c,2026-12-24T00:00:00Z,"
                                   "2026-12-25T00:00:00Z\n"), "t", v3=False)
    with pytest.raises(SystemExit):
        so.load_rows(csv(tmp_path, "create,PS1,PU1,,2026-12-25T00:00:00Z,"
                                   "2026-12-24T00:00:00Z\n"), "t", v3=False)
    with pytest.raises(SystemExit):
        so.load_rows(csv(tmp_path, "create,PS1,PU1,,2026-12-24T00:00:00,"
                                   "2026-12-25T00:00:00Z\n"), "t", v3=False)


@responses.activate
def test_v2_create_skips_identical_and_counts_rejections(tmp_path):
    path = csv(tmp_path,
               "create,PS1,PU1,,2026-12-24T00:00:00Z,2026-12-25T00:00:00Z\n"   # exists
               "create,PS1,PU2,,2026-12-25T00:00:00Z,2026-12-26T00:00:00Z\n"
               "create,PS1,PU3,,2026-12-26T00:00:00Z,2026-12-27T00:00:00Z\n")
    responses.get(f"{PD_API_BASE}/schedules/PS1/overrides", json={"overrides": [
        {"id": "PO1", "start": "2026-12-23T18:00:00-06:00", "end": "2026-12-24T18:00:00-06:00",
         "user": {"id": "PU1"}}]})
    responses.post(f"{PD_API_BASE}/schedules/PS1/overrides", status=201,
                   json=[{"status": 201}, {"status": 400, "errors": ["overlaps"]}])
    assert so.main([path, "-y"]) == 1
    body = json.loads(next(c for c in responses.calls if c.request.method == "POST").request.body)
    assert [o["user"]["id"] for o in body["overrides"]] == ["PU2", "PU3"]


@responses.activate
def test_delete_matches_existing_and_dry_run_writes_nothing(tmp_path):
    path = csv(tmp_path, "delete,PS1,PU1,,2026-12-24T00:00:00Z,2026-12-25T00:00:00Z\n"
                         "delete,PS1,PU2,,2026-12-24T00:00:00Z,2026-12-25T00:00:00Z\n")
    responses.get(f"{PD_API_BASE}/schedules/PS1/overrides", json={"overrides": [
        {"id": "PO1", "start": "2026-12-24T00:00:00Z", "end": "2026-12-25T00:00:00Z",
         "user": {"id": "PU1"}}]})
    assert so.main([path, "--dry-run"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)
    responses.delete(f"{PD_API_BASE}/schedules/PS1/overrides/PO1", status=204)
    assert so.main([path, "-y"]) == 0
    assert [c.request.method for c in responses.calls].count("DELETE") == 1


@responses.activate
def test_v3_create_body(tmp_path):
    header = "action,schedule_id,user_id,user_email,overridden_user_id,rotation_id,start,end\n"
    path = csv(tmp_path, "create,PS3,PU2,,PU1,ROT1,2026-12-24T00:00:00Z,2026-12-25T00:00:00Z\n",
               header)
    responses.get(f"{PD_API_BASE}/v3/schedules/PS3/overrides", json={"overrides": []})
    responses.post(f"{PD_API_BASE}/v3/schedules/PS3/overrides", status=201, json={})
    assert so.main([path, "--v3", "-y"]) == 0
    body = json.loads(responses.calls[-1].request.body)["overrides"][0]
    assert body["overridden_member"] == {"type": "user_member", "user_id": "PU1"}
    assert body["overriding_member"] == {"type": "user_member", "user_id": "PU2"}
    assert body["rotation_id"] == "ROT1" and body["start_time"] == "2026-12-24T00:00:00Z"


def test_v3_requires_overridden_user(tmp_path):
    with pytest.raises(SystemExit):
        so.load_rows(csv(tmp_path, "create,PS3,PU2,,2026-12-24T00:00:00Z,"
                                   "2026-12-25T00:00:00Z\n"), "t", v3=True)
