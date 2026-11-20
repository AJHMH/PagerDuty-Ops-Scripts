"""pd-subscriptions: list, validation, idempotent grouped apply."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import subscriptions as subs

HDR = "action,subscriber_kind,subscriber_id,subscribable_kind,subscribable_id\n"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def sub(kind, oid, bs):
    return {"subscription": {"subscriber_type": kind, "subscriber_id": oid,
                             "subscribable_type": "business_service", "subscribable_id": bs}}


@responses.activate
def test_list_by_business_service(capsys):
    responses.get(f"{PD_API_BASE}/business_services",
                  json={"business_services": [{"id": "PB1"}], "more": False})
    responses.get(f"{PD_API_BASE}/business_services/PB1/subscribers", json={
        "subscribers": [{"subscriber_type": "team", "subscriber_id": "PT1"}], "more": False})
    assert subs.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "subscribe,team,PT1,business_service,PB1"


def test_validation_rejects_service_subscribable(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(HDR + "subscribe,user,PU1,service,PS1\n")
    with pytest.raises(SystemExit) as exc:
        subs.load_rows(str(p))
    assert exc.value.code == 2


@responses.activate
def test_apply_groups_per_subscriber_and_skips_existing(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(HDR + "subscribe,team,PT1,business_service,PB1\n"     # exists
                 "subscribe,team,PT1,business_service,PB2\n"
                 "subscribe,team,PT1,business_service,PB3\n"
                 "unsubscribe,user,PU1,business_service,PB1\n")
    responses.get(f"{PD_API_BASE}/teams/PT1/notification_subscriptions",
                  json={"subscriptions": [sub("team", "PT1", "PB1")], "more": False})
    responses.get(f"{PD_API_BASE}/users/PU1/notification_subscriptions",
                  json={"subscriptions": [sub("user", "PU1", "PB1")], "more": False})
    responses.post(f"{PD_API_BASE}/teams/PT1/notification_subscriptions", json={})
    responses.post(f"{PD_API_BASE}/users/PU1/notification_subscriptions/unsubscribe", json={})
    assert subs.main(["apply", str(p), "-y"]) == 0
    posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(posts) == 2
    team_body = json.loads(posts[0].request.body)["subscribables"]
    assert [b["subscribable_id"] for b in team_body] == ["PB2", "PB3"]


@responses.activate
def test_apply_dry_run(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(HDR + "subscribe,user,PU1,business_service,PB9\n")
    responses.get(f"{PD_API_BASE}/users/PU1/notification_subscriptions",
                  json={"subscriptions": [], "more": False})
    assert subs.main(["apply", str(p), "--dry-run"]) == 0
    assert all(c.request.method == "GET" for c in responses.calls)


@responses.activate
def test_rejected_items_in_200_responses_count_as_failures(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(HDR + "subscribe,user,PU1,business_service,PB1\n"
                 "subscribe,user,PU1,business_service,PB2\n"
                 "unsubscribe,team,PT1,business_service,PB3\n")
    responses.get(f"{PD_API_BASE}/users/PU1/notification_subscriptions",
                  json={"subscriptions": [], "more": False})
    responses.get(f"{PD_API_BASE}/teams/PT1/notification_subscriptions",
                  json={"subscriptions": [sub("team", "PT1", "PB3")], "more": False})
    responses.post(f"{PD_API_BASE}/users/PU1/notification_subscriptions", json={
        "subscriptions": [{"subscribable_id": "PB1", "result": "success"},
                          {"subscribable_id": "PB2", "result": "unauthorized"}]})
    responses.post(f"{PD_API_BASE}/teams/PT1/notification_subscriptions/unsubscribe", json={
        "deleted_count": 0, "unauthorized_count": 1, "non_existent_count": 0})
    assert subs.main(["apply", str(p), "-y"]) == 1


def test_rejected_count_treats_duplicates_and_missing_as_ok():
    assert subs.rejected_count("subscribe", {"subscriptions": [{"result": "duplicate"}]}, 1) == 0
    assert subs.rejected_count("unsubscribe", {"deleted_count": 0, "unauthorized_count": 0,
                                               "non_existent_count": 1}, 1) == 0
