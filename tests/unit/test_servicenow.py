"""ServiceNow client/auth/config, plan hashing, rehearsal receipts, and pd-sn-sync."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import responses

from pagerduty_ops import plan as planmod
from pagerduty_ops import receipts
from pagerduty_ops.commands import sn_sync
from pagerduty_ops.pdstate import PDState
from pagerduty_ops.servicenow import (
    DEFAULT_FIELD_MAP,
    ServiceNowClient,
    SNApiError,
    build_auth,
    get_instance_config,
    load_field_map,
)

F = DEFAULT_FIELD_MAP
SYS = {n: f"{n:0>32}" for n in ("1", "2", "3", "4", "5", "9")}


@pytest.fixture(autouse=True)
def _sn_env(monkeypatch, tmp_path):
    for k in ("SN_INSTANCE", "SN_PROD_INSTANCES", "SN_REHEARSAL_INSTANCES", "SN_FIELD_MAP",
              "SN_AUTH_METHOD", "OKTA_SCOPE", "PD_OPS_RECEIPTS_DIR", "SN_RECEIPT_MAX_AGE_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SN_PROD_INSTANCES", "dallastx")
    monkeypatch.setenv("SN_REHEARSAL_INSTANCES", "dallastx-test")
    monkeypatch.setenv("SN_AUTH_METHOD", "basic")
    monkeypatch.setenv("SN_USER", "svc_pd_sync")
    monkeypatch.setenv("SN_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("PD_API_TOKEN", "test-token-not-real")
    monkeypatch.chdir(tmp_path)


# ---------------- config ----------------

def test_instance_classification():
    assert get_instance_config("dallastx").is_prod
    test = get_instance_config("https://dallastx-test.service-now.com")
    assert test.name == "dallastx-test" and test.is_rehearsal and not test.is_prod
    dev = get_instance_config("dallastx-dev")
    assert not dev.is_prod and not dev.is_rehearsal


def test_writes_fail_closed_without_prod_list(monkeypatch):
    monkeypatch.delenv("SN_PROD_INSTANCES")
    get_instance_config("dallastx-dev")  # reads are fine
    with pytest.raises(SystemExit) as exc:
        get_instance_config("dallastx-dev", for_write=True)
    assert exc.value.code == 2


def test_invalid_instance_name_rejected():
    with pytest.raises(SystemExit):
        get_instance_config("evil.example.com/x")


def test_field_map_override_and_unknown_key(tmp_path):
    p = tmp_path / "fm.json"
    p.write_text(json.dumps({"group_team": "u_pd_team"}))
    fm = load_field_map(str(p))
    assert fm["group_team"] == "u_pd_team" and fm["group_service"] == F["group_service"]
    p.write_text(json.dumps({"bogus": "x"}))
    with pytest.raises(SystemExit):
        load_field_map(str(p))


# ---------------- auth + client ----------------

@responses.activate
def test_okta_token_is_fetched_once_and_sent_as_bearer(monkeypatch):
    monkeypatch.setenv("SN_AUTH_METHOD", "okta")
    monkeypatch.setenv("OKTA_DOMAIN", "https://example.okta.com/")
    monkeypatch.setenv("OKTA_AUTH_SERVER_ID", "aus123")
    monkeypatch.setenv("OKTA_CLIENT_ID", "cid")
    monkeypatch.setenv("OKTA_CLIENT_SECRET", "csecret")
    responses.post("https://example.okta.com/oauth2/aus123/v1/token",
                   json={"access_token": "tok-1", "expires_in": 3600})
    url = "https://dallastx-dev.service-now.com/api/now/table/sys_user_group"
    responses.get(url, json={"result": [{"sys_id": SYS["1"]}]})
    responses.get(url, json={"result": []})
    inst = get_instance_config("dallastx-dev")
    client = ServiceNowClient(inst, build_auth(inst))
    client.get_records("sys_user_group", query="x")
    client.get_records("sys_user_group", query="x")
    token_calls = [c for c in responses.calls if "okta" in c.request.url]
    assert len(token_calls) == 1
    assert responses.calls[1].request.headers["Authorization"] == "Bearer tok-1"


@responses.activate
def test_get_records_paginates_and_patch_validates_sys_id():
    url = "https://dallastx-dev.service-now.com/api/now/table/sys_user"
    responses.get(url, json={"result": [{"sys_id": "a"}, {"sys_id": "b"}]})
    responses.get(url, json={"result": [{"sys_id": "c"}]})
    inst = get_instance_config("dallastx-dev")
    client = ServiceNowClient(inst, build_auth(inst))
    assert [r["sys_id"] for r in client.get_records("sys_user", page_size=2)] == ["a", "b", "c"]
    with pytest.raises(SNApiError):
        client.patch_record("sys_user", "not-a-sys-id", {"x": "y"})


@responses.activate
def test_sn_auth_error_is_auth_error():
    responses.get("https://dallastx-dev.service-now.com/api/now/table/sys_user",
                  json={"error": {"message": "User Not Authenticated"}}, status=401)
    inst = get_instance_config("dallastx-dev")
    client = ServiceNowClient(inst, build_auth(inst))
    with pytest.raises(SNApiError) as exc:
        client.get_records("sys_user")
    assert exc.value.is_auth_error


# ---------------- plan + receipts ----------------

def _plan(value="PEP2", sys_id=SYS["1"]):
    return planmod.new_plan("sn-sync", [sn_sync._item(
        "sys_user_group", "name", "Platform", sys_id, F["group_escalation"], value, "PEP1",
        "detail")], generated_by="test")


def test_plan_hash_ignores_instance_specific_keys_but_not_changes():
    assert planmod.plan_hash(_plan()) == planmod.plan_hash(_plan(sys_id=SYS["2"]))
    assert planmod.plan_hash(_plan()) != planmod.plan_hash(_plan(value="PEP3"))


def test_load_plan_rejects_wrong_kind(tmp_path):
    p = tmp_path / "p.json"
    planmod.write_plan(planmod.new_plan("pd-reorg", [], generated_by="t"), str(p))
    with pytest.raises(SystemExit) as exc:
        planmod.load_plan(str(p), expected_kind="sn-sync")
    assert exc.value.code == 2


def test_receipt_required_valid_and_expiry(tmp_path):
    d = str(tmp_path / "r")
    with pytest.raises(SystemExit):
        receipts.require_receipt(d, sha="abc", rehearsal_instances={"dallastx-test"})
    receipts.write_receipt(d, sha="abc", kind="sn-sync", instance="dallastx-test",
                           succeeded=1, skipped=0)
    assert receipts.require_receipt(d, sha="abc", rehearsal_instances={"dallastx-test"})
    later = datetime.now(timezone.utc) + timedelta(days=8)
    with pytest.raises(SystemExit):
        receipts.require_receipt(d, sha="abc", rehearsal_instances={"dallastx-test"}, now=later)
    with pytest.raises(SystemExit):  # dev receipts never unlock production
        receipts.require_receipt(d, sha="abc", rehearsal_instances={"other"})


# ---------------- analysis ----------------

def fake_pd(**overrides):
    pd = PDState("t")
    pd.services = {
        "PSVC1": {"id": "PSVC1", "escalation_policy": {"id": "PEP1"}, "teams": [{"id": "PT1"}]},
        "PSVC2": {"id": "PSVC2", "escalation_policy": {"id": "PEP2"}, "teams": []},
    }
    pd.escalation_policies = {
        "PEP1": {"id": "PEP1", "teams": [{"id": "PT1"}], "escalation_rules": [
            {"targets": [{"id": "PS1", "type": "schedule_reference"}]}]},
        "PEP2": {"id": "PEP2", "teams": [], "escalation_rules": [
            {"targets": [{"id": "PS1", "type": "schedule_reference"},
                         {"id": "PS2", "type": "schedule_reference"}]}]},
    }
    pd.teams = {"PT1": {"id": "PT1"}}
    pd.schedules = {"PS1": {"id": "PS1"}, "PS2": {"id": "PS2"}}
    pd.webhooks = [{"id": "PW1", "filter": {"type": "service_reference", "id": "PSVC1"}}]
    pd.users = {"PU1": {"id": "PU1"}, "PU2": {"id": "PU2"}}
    pd._members = {"PT1": {"PU1": "manager", "PU2": "responder"}}
    for k, v in overrides.items():
        setattr(pd, k, v)
    return pd


def group(sys_id, name, **ids):
    g = {"sys_id": sys_id, "name": name, F["group_error"]: ids.pop("error", "")}
    for key in sn_sync.GROUP_ID_KEYS:
        g[F[key]] = ids.get(key.removeprefix("group_"), "")
    return g


def state(groups, members=None, users=None):
    return {"groups": groups, "members": members or {}, "users": users or {}}


def statuses(rows):
    return {(r["key"], r["field"]): r["status"] for r in rows}


def test_analyze_derives_and_updates_from_service_anchor():
    g = group(SYS["1"], "Platform", service="PSVC1", escalation="PEPX", team="", schedule="PS1",
              webhook="PW1")
    rows, items = sn_sync.analyze(fake_pd(), state([g]), F, today="2026-11-20")
    s = statuses(rows)
    assert s[("Platform", F["group_escalation"])] == "update"
    assert s[("Platform", F["group_team"])] == "update"
    assert s[("Platform", F["group_schedule"])] == "ok"
    changes = {i["field"]: i["value"] for i in items}
    assert changes == {F["group_escalation"]: "PEP1", F["group_team"]: "PT1"}


def test_analyze_ambiguous_is_never_written_and_recorded_in_error_field():
    g = group(SYS["1"], "Ops", service="PSVC2", escalation="PEP2", schedule="")
    rows, items = sn_sync.analyze(fake_pd(), state([g]), F, today="2026-11-20")
    s = statuses(rows)
    assert s[("Ops", F["group_team"])] == "ok"  # no team on either side: nothing to write
    assert s[("Ops", F["group_schedule"])] == "ambiguous"
    assert {i["field"] for i in items} == {F["group_error"]}
    assert items[0]["value"].startswith("[pd-sn-sync 2026-11-20] ")


def test_analyze_multi_team_service_is_ambiguous():
    pd = fake_pd()
    pd.services["PSVC1"]["teams"] = [{"id": "PT1"}, {"id": "PT2"}]
    g = group(SYS["1"], "Platform", service="PSVC1", escalation="PEP1", schedule="PS1",
              webhook="PW1")
    rows, items = sn_sync.analyze(pd, state([g]), F)
    assert statuses(rows)[("Platform", F["group_team"])] == "ambiguous"
    assert F["group_team"] not in {i["field"] for i in items}


def test_analyze_stale_service_cleared_only_with_prune():
    g = group(SYS["1"], "Gone", service="PDELETED", team="PT1")
    rows, items = sn_sync.analyze(fake_pd(), state([g]), F)
    assert statuses(rows)[("Gone", F["group_service"])] == "stale"
    assert all(i["field"] == F["group_error"] for i in items)
    _rows, items = sn_sync.analyze(fake_pd(), state([g]), F, prune=True)
    assert {(i["field"], i["value"]) for i in items} == {(F["group_service"], "")}


def test_analyze_wrong_type_detected():
    g = group(SYS["1"], "Mixed", service="PEP1")
    rows, _ = sn_sync.analyze(fake_pd(), state([g]), F)
    row = next(r for r in rows if r["field"] == F["group_service"])
    assert row["status"] == "stale" and "escalation_policy" in row["detail"]


def test_analyze_duplicate_and_unanchored_write_only_messages():
    groups = [group(SYS["1"], "A", service="PSVC1"), group(SYS["2"], "B", service="PSVC1"),
              group(SYS["3"], "C", team="PT1")]
    rows, items = sn_sync.analyze(fake_pd(), state(groups), F)
    s = statuses(rows)
    assert s[("A", F["group_service"])] == "duplicate_service"
    assert s[("C", F["group_service"])] == "unanchored"
    assert {i["field"] for i in items} == {F["group_error"]}


def test_analyze_clears_only_own_resolved_message():
    ok = dict(service="PSVC1", escalation="PEP1", team="PT1", schedule="PS1", webhook="PW1")
    mine = group(SYS["1"], "Mine", error="[pd-sn-sync 2026-01-01] old", **ok)
    theirs = group(SYS["2"], "Theirs", error="PagerDuty app error", **ok)
    groups = [mine, theirs]
    pd = fake_pd(services={**fake_pd().services, "PSVC3": {
        "id": "PSVC3", "escalation_policy": {"id": "PEP1"}, "teams": [{"id": "PT1"}]}},
        webhooks=fake_pd().webhooks + [
            {"id": "PW3", "filter": {"type": "service_reference", "id": "PSVC3"}}])
    theirs[F["group_service"]] = "PSVC3"
    theirs[F["group_webhook"]] = "PW3"
    _rows, items = sn_sync.analyze(pd, state(groups), F)
    assert [(i["key"], i["value"]) for i in items] == [("Mine", "")]


def test_analyze_membership_and_unmapped_users_report_only():
    g = group(SYS["1"], "Platform", service="PSVC1", escalation="PEP1", team="PT1",
              schedule="PS1", webhook="PW1")
    users = {SYS["4"]: {"sys_id": SYS["4"], "user_name": "jdoe", F["user_pd_id"]: "PU1"},
             SYS["5"]: {"sys_id": SYS["5"], "user_name": "nopd", F["user_pd_id"]: ""},
             SYS["9"]: {"sys_id": SYS["9"], "user_name": "ghost", F["user_pd_id"]: "PGONE"}}
    members = {SYS["1"]: {SYS["4"], SYS["5"]}}
    rows, items = sn_sync.analyze(fake_pd(), state([g], members, users), F)
    got = {(r["status"], r["current"] or r["expected"] or r["key"]) for r in rows
           if r["status"] != "ok"}
    assert ("membership_only_in_pd", "PU2") in got
    assert ("unmapped_user", "nopd") in got
    assert ("stale", "PGONE") in got
    assert items == []  # membership and stale users are not written without --prune


# ---------------- apply end-to-end ----------------

GROUPS_URL = "https://{inst}.service-now.com/api/now/table/sys_user_group"


def _write_plan(path, value="PEP1"):
    p = planmod.new_plan("sn-sync", [sn_sync._item(
        "sys_user_group", "name", "Platform", SYS["9"], F["group_escalation"], value, "PEPX",
        "update")], generated_by="test")
    planmod.write_plan(p, str(path))
    return planmod.plan_hash(p)


def _mock_target(inst, live="PEPX", sys_id=SYS["1"]):
    responses.get(GROUPS_URL.format(inst=inst), json={"result": [
        {"sys_id": sys_id, "name": "Platform", F["group_escalation"]: live}]})
    responses.patch(f"{GROUPS_URL.format(inst=inst)}/{sys_id}", json={"result": {}})


@responses.activate
def test_apply_rehearsal_writes_receipt_then_prod_succeeds(tmp_path):
    plan_file = tmp_path / "plan.json"
    sha = _write_plan(plan_file)
    _mock_target("dallastx-test")
    assert sn_sync.main(["apply", str(plan_file), "--instance", "dallastx-test", "-y"]) == 0
    assert (tmp_path / ".pd_ops_receipts" / f"{sha}.json").exists()

    _mock_target("dallastx", sys_id=SYS["2"])  # prod sys_id differs: matched by name
    assert sn_sync.main(["apply", str(plan_file), "--instance", "dallastx", "-y"]) == 0
    patch = [c for c in responses.calls if c.request.method == "PATCH"][-1]
    assert patch.request.url.split("?")[0].endswith(SYS["2"])
    assert json.loads(patch.request.body) == {F["group_escalation"]: "PEP1"}


@responses.activate
def test_apply_prod_without_receipt_refused(tmp_path):
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file)
    with pytest.raises(SystemExit) as exc:
        sn_sync.main(["apply", str(plan_file), "--instance", "dallastx", "-y"])
    assert exc.value.code == 2
    assert not [c for c in responses.calls if c.request.method == "PATCH"]


@responses.activate
def test_apply_dev_dry_run_writes_nothing_and_no_receipt(tmp_path):
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file)
    _mock_target("dallastx-dev")
    assert sn_sync.main(["apply", str(plan_file), "--instance", "dallastx-dev",
                         "--dry-run"]) == 0
    assert not [c for c in responses.calls if c.request.method == "PATCH"]
    assert not (tmp_path / ".pd_ops_receipts").exists()


@responses.activate
def test_apply_prod_conflict_on_drifted_live_value(tmp_path):
    plan_file = tmp_path / "plan.json"
    sha = _write_plan(plan_file)
    receipts.write_receipt(str(tmp_path / ".pd_ops_receipts"), sha=sha, kind="sn-sync",
                           instance="dallastx-test", succeeded=1, skipped=0)
    _mock_target("dallastx", live="PSOMEONE_ELSE")
    assert sn_sync.main(["apply", str(plan_file), "--instance", "dallastx", "-y"]) == 1
    assert not [c for c in responses.calls if c.request.method == "PATCH"]


@responses.activate
def test_apply_max_writes_cap(tmp_path):
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file)
    _mock_target("dallastx-dev")
    assert sn_sync.main(["apply", str(plan_file), "--instance", "dallastx-dev", "-y",
                         "--max-writes", "0"]) == 2


@responses.activate
def test_apply_rejects_plans_touching_other_fields(tmp_path):
    p = planmod.new_plan("sn-sync", [sn_sync._item(
        "sys_user_group", "name", "Platform", SYS["1"], "manager", "x", "", "")],
        generated_by="test")
    path = tmp_path / "plan.json"
    planmod.write_plan(p, str(path))
    assert sn_sync.main(["apply", str(path), "--instance", "dallastx-dev", "-y"]) == 2


@responses.activate
def test_report_end_to_end_with_fail_on_drift(capsys):
    from pagerduty_ops.api import PD_API_BASE

    sn = "https://dallastx-dev.service-now.com/api/now/table"
    responses.get(f"{sn}/sys_user_group", json={"result": [
        group(SYS["1"], "Platform", service="PSVC1", escalation="PEPX")]})
    responses.get(f"{sn}/sys_user_grmember", json={"result": []})
    responses.get(f"{sn}/sys_user", json={"result": []})
    pd = fake_pd()
    for key, coll in (("services", pd.services), ("escalation_policies",
                      pd.escalation_policies), ("teams", pd.teams),
                      ("schedules", pd.schedules), ("users", pd.users)):
        responses.get(f"{PD_API_BASE}/{key}", json={key: list(coll.values()), "more": False})
    responses.get(f"{PD_API_BASE}/webhook_subscriptions",
                  json={"webhook_subscriptions": pd.webhooks, "more": False})
    responses.get(f"{PD_API_BASE}/teams/PT1/members", json={"members": [], "more": False})
    code = sn_sync.main(["report", "--instance", "dallastx-dev", "-f", "csv",
                         "--fail-on-drift"])
    out = capsys.readouterr().out
    assert code == 1
    assert "x_pd_integration_pagerduty_escalation,PEPX,PEP1,update" in out
