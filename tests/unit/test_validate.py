"""pd-validate: each check, severity overrides, fail-on, and the remediation plan."""

import json

import pytest

from pagerduty_ops.commands import validate
from pagerduty_ops.pdstate import PDState


def user(uid, name, role="user", contact=True, high=True):
    return {"id": uid, "name": name, "role": role,
            "contact_methods": [{"id": "C"}] if contact else [],
            "notification_rules": [{"urgency": "high"}] if high else [{"urgency": "low"}]}


def account(**over):
    pd = PDState("t")
    pd.teams = {"PT1": {"id": "PT1", "name": "Platform"}, "PT2": {"id": "PT2", "name": "Solo"}}
    pd._members = {"PT1": {"PU1": "manager", "PU2": "responder"}, "PT2": {"PU3": "responder"}}
    pd.users = {"PU1": user("PU1", "Ann"), "PU2": user("PU2", "Bo"),
                "PU3": user("PU3", "Cy", contact=False), "PU4": user("PU4", "Di", high=False),
                "PU5": user("PU5", "Ed", role="observer")}
    pd.schedules = {
        "PS1": {"id": "PS1", "name": "Platform Primary", "teams": [{"id": "PT1"}],
                "users": [{"id": "PU1"}, {"id": "PU4"}]},
        "PS2": {"id": "PS2", "name": "Unused", "teams": [], "users": [{"id": "PU2"}]},
    }
    pd.escalation_policies = {
        "PEP1": {"id": "PEP1", "name": "Platform EP", "teams": [{"id": "PT1"}],
                 "escalation_rules": [{"targets": [{"id": "PS1", "type": "schedule_reference"}]},
                                      {"targets": []}]},
        "PEP2": {"id": "PEP2", "name": "Loose EP", "teams": [],
                 "escalation_rules": [{"targets": [{"id": "PS1", "type": "schedule_reference"},
                                                   {"id": "PU3", "type": "user_reference"}]}]},
    }
    pd.services = {
        "PSVC1": {"id": "PSVC1", "name": "Billing", "escalation_policy": {"id": "PEP1"},
                  "teams": [{"id": "PT1"}]},
        "PSVC2": {"id": "PSVC2", "name": "legacy-thing", "escalation_policy": {"id": "PEP2"},
                  "teams": []},
    }
    pd.extensions = [{"name": "SN", "extension_schema": {"summary": "ServiceNow (v7)"},
                      "extension_objects": [{"id": "PSVC1"}]}]
    pd.webhooks = []
    pd._schedule_details = {"PS1": {"final_schedule": {"rendered_coverage_percentage": 87.5}}}
    for k, v in over.items():
        setattr(pd, k, v)
    return pd


def run(check, pd=None, cfg=None):
    c = validate.load_config(None)
    c.update(cfg or {})
    v = validate.Validator(pd or account(), c)
    return v.run([check]), v


def msgs(findings):
    return {(f["resource_id"], f["message"].split(" (")[0]) for f in findings}


def test_user_not_on_owning_team_plans_add_member():
    findings, v = run("user_not_on_owning_team")
    assert any(f["resource_id"] == "PS1" and "PU4" in f["message"] for f in findings)
    assert v.plan_items == [{"op": "add_member", "team_id": "PT1", "user_id": "PU4",
                             "role": "responder", "detail": "Di is on schedule 'Platform Primary'"}]


def test_service_ep_team_mismatch_plans_ep_move_when_schedules_agree():
    findings, v = run("service_ep_team_mismatch")
    assert any(f["resource_id"] == "PSVC2" for f in findings)
    assert v.plan_items[0]["op"] == "move_resource"
    assert v.plan_items[0]["to_team"] == "PT1" and v.plan_items[0]["id"] == "PEP2"


def test_ep_coverage_gaps():
    findings, _ = run("ep_coverage_gaps")
    got = msgs(findings)
    assert ("PEP1", "rule 2 has no targets") in got
    assert any(f["resource_id"] == "PS1" and "87.5%" in f["message"] for f in findings)


def test_unreachable_responders():
    findings, _ = run("unreachable_responders")
    got = msgs(findings)
    assert ("PU3", "no contact methods") in got
    assert ("PU4", "no high-urgency notification rule") in got
    assert all(f["severity"] == "error" for f in findings)


def test_team_staffing():
    findings, _ = run("team_staffing")
    got = msgs(findings)
    assert ("PT2", "team has no manager") in got
    assert ("PT2", "single member") in got
    assert not any(f["resource_id"] == "PT1" for f in findings)


def test_user_utilization():
    findings, _ = run("user_utilization")
    ids = {f["resource_id"] for f in findings}
    assert "PU4" in ids and "PU5" in ids  # no team
    assert not any(f["resource_id"] == "PU5" and "role" in f["message"] for f in findings)


def test_orphaned_resources():
    findings, _ = run("orphaned_resources")
    got = msgs(findings)
    assert ("PS2", "owned by no team") in got
    assert ("PS2", "not used by any escalation policy") in got
    assert ("PSVC2", "owned by no team") in got


def test_naming_conventions_and_severity_override():
    cfg = {"naming": {"services": r"^[A-Z]"},
           "checks": {"naming_conventions": {"severity": "error"}}}
    findings, _ = run("naming_conventions", cfg=cfg)
    assert [(f["resource_id"], f["severity"]) for f in findings] == [("PSVC2", "error")]


def test_servicenow_integration_extension_and_group_mapping():
    findings, _ = run("servicenow_integration")
    assert [f["resource_id"] for f in findings] == ["PSVC2"]
    from pagerduty_ops.servicenow import DEFAULT_FIELD_MAP as F

    v = validate.Validator(account(), validate.load_config(None))
    v._current = "servicenow_integration"
    v.servicenow_integration([{F["group_service"]: "PSVC2"}], F)
    assert {(f["resource_id"], f["message"]) for f in v.findings} == {
        ("PSVC2", "no ServiceNow extension attached"),
        ("PSVC1", "no ServiceNow assignment group references this service")}


def test_config_validation(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"checks": {"nope": {}}}))
    with pytest.raises(SystemExit) as exc:
        validate.load_config(str(p))
    assert exc.value.code == 2
    p.write_text(json.dumps({"naming": {"teams": "("}}))
    with pytest.raises(SystemExit):
        validate.load_config(str(p))


def test_select_checks_disables_and_skips_servicenow():
    cfg = validate.load_config(None)
    cfg["checks"] = {"team_staffing": {"enabled": False}}
    chosen = validate.select_checks(cfg, None, servicenow=False)
    assert "team_staffing" not in chosen and "servicenow_drift" not in chosen
    assert "servicenow_integration" in chosen
    with pytest.raises(SystemExit):
        validate.select_checks(cfg, "bogus", servicenow=False)


def test_main_fail_on_and_plan_out(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    monkeypatch.setattr(validate, "PDState", lambda token: account())
    plan_path = tmp_path / "fix.json"
    code = validate.main(["--checks", "user_not_on_owning_team", "-f", "json",
                          "--plan-out", str(plan_path)])
    assert code == 0  # warnings only; default --fail-on error
    plan = json.loads(plan_path.read_text())
    assert plan["kind"] == "pd-reorg" and plan["items"][0]["op"] == "add_member"
    assert json.loads(capsys.readouterr().out)[0]["check"] == "user_not_on_owning_team"
    assert validate.main(["--checks", "user_not_on_owning_team", "--fail-on", "warning"]) == 1
    assert validate.main(["--checks", "unreachable_responders", "--fail-on", "never"]) == 0


def test_coverage_retrieval_failure_is_an_error_finding():
    pd = account()
    pd._schedule_details = {"PS1": None}
    findings, _ = run("ep_coverage_gaps", pd)
    assert any(f["resource_id"] == "PS1" and f["severity"] == "error"
               and "could not be retrieved" in f["message"] for f in findings)


def test_ep_move_not_planned_when_a_schedule_is_unowned_or_missing():
    pd = account()
    pd.escalation_policies["PEP2"]["escalation_rules"][0]["targets"].append(
        {"id": "PS2", "type": "schedule_reference"})          # PS2 has no team
    findings, v = run("service_ep_team_mismatch", pd)
    assert v.plan_items == [] and any(f["resource_id"] == "PSVC2" for f in findings)
    pd.escalation_policies["PEP2"]["escalation_rules"][0]["targets"][-1]["id"] = "PSGONE"
    _, v = run("service_ep_team_mismatch", pd)
    assert v.plan_items == []


def test_drift_is_declared_as_the_exit_code_and_summary_line_the_command_produces(
        monkeypatch, caplog):
    # The UI reads these to tell Teams about Drift found by a Scheduled run (#80).
    import logging
    monkeypatch.setenv("PD_API_TOKEN", "t")
    monkeypatch.setattr(validate, "PDState", lambda token: account())
    caplog.set_level(logging.INFO)
    code = validate.main(["--checks", "user_not_on_owning_team", "--fail-on", "warning"])
    assert code == validate.DRIFT_EXIT_CODE
    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith(validate.DRIFT_SUMMARY)]
    assert summaries == ["Findings: info=0, warning=1, error=0"]
