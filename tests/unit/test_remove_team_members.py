"""pd-remove-team-members non-interactive mode (#65, ADR 0009): named users are removed from the
team, and on request from the team's schedules and escalation policies, under the full
write-safety contract."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE, PDApiError
from pagerduty_ops.commands import remove_team_members as rtm

B = PD_API_BASE


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    monkeypatch.delenv("PD_TEAM_ID", raising=False)


def _user(uid, name):
    return {"id": uid, "summary": name, "type": "user_reference"}


def mock_account(*, members=("PU1", "PU2"), layer_users=("PU1", "PU3"),
                 rule_targets=(("user", "PU1"), ("schedule", "PS1")), v3=()):
    """Team PT1; schedule PS1 (one layer); escalation policy PE1 (one rule)."""
    names = {"PU1": "Ann", "PU2": "Bob", "PU3": "Cy"}
    for uid, name in names.items():
        responses.get(f"{B}/users/{uid}", json={"user": {"id": uid, "name": name}})
    responses.get(f"{B}/v3/schedules", json={
        "schedules": [{"id": i, "name": n} for i, n in v3], "more": False})
    responses.get(f"{B}/teams/PT1/members", json={
        "members": [{"user": _user(u, names[u]), "role": "responder"} for u in members],
        "more": False})
    responses.get(f"{B}/schedules", json={"schedules": [{"id": "PS1", "summary": "Primary"}],
                                         "more": False})
    responses.get(f"{B}/schedules/PS1", json={"schedule": {
        "id": "PS1", "type": "schedule", "name": "Primary", "summary": "Primary",
        "self": f"{B}/schedules/PS1", "html_url": "https://x.pagerduty.com/schedules/PS1",
        "time_zone": "America/Chicago", "description": "Tier 1",
        "teams": [{"id": "PT1", "type": "team_reference"}],
        "users": [_user(u, names[u]) for u in layer_users],
        "final_schedule": {"name": "Final Schedule", "rendered_schedule_entries": []},
        "schedule_layers": [{"id": "L1", "name": "Layer 1", "start": "2026-01-01T00:00:00Z",
                             "rotation_virtual_start": "2026-01-01T00:00:00Z",
                             "rotation_turn_length_seconds": 604800,
                             "rendered_schedule_entries": [],
                             "rendered_coverage_percentage": 100.0,
                             "users": [{"user": _user(u, names[u])} for u in layer_users]}]}})
    responses.get(f"{B}/escalation_policies", json={
        "escalation_policies": [{"id": "PE1", "summary": "Ops EP"}], "more": False})
    responses.get(f"{B}/escalation_policies/PE1", json={"escalation_policy": {
        "id": "PE1", "type": "escalation_policy", "name": "Ops EP", "summary": "Ops EP",
        "description": "Ops", "num_loops": 2, "on_call_handoff_notifications": "always",
        "teams": [{"id": "PT1", "type": "team_reference"}], "services": [],
        "escalation_rules": [{"id": "R1", "escalation_delay_in_minutes": 30,
                              "targets": [{"type": f"{t}_reference", "id": i}
                                          for t, i in rule_targets]}]}})


def writes():
    return [c.request for c in responses.calls if c.request.method in ("PUT", "DELETE", "POST")]


ALL = ["--team-id", "PT1", "--user-ids", "PU1", "--from-schedules",
       "--from-escalation-policies"]


@responses.activate
def test_dry_run_previews_every_change_and_writes_nothing(capsys):
    mock_account()
    assert rtm.main([*ALL, "--dry-run"]) == 0
    assert writes() == []
    err = capsys.readouterr().err
    assert "schedule 'Primary'" in err
    assert "policy 'Ops EP'" in err
    assert "DELETE /teams/PT1/members/PU1" in err


@responses.activate
def test_apply_removes_from_schedules_policies_then_the_team():
    mock_account()
    responses.put(f"{B}/schedules/PS1", json={"schedule": {}})
    responses.put(f"{B}/escalation_policies/PE1", json={"escalation_policy": {}})
    responses.delete(f"{B}/teams/PT1/members/PU1", status=204)
    assert rtm.main([*ALL, "-y"]) == 0
    sent = writes()
    assert [(r.method, r.url.replace(B, "")) for r in sent] == [
        ("PUT", "/schedules/PS1"), ("PUT", "/escalation_policies/PE1"),
        ("DELETE", "/teams/PT1/members/PU1")]
    schedule = json.loads(sent[0].body)["schedule"]
    assert {k: schedule[k] for k in ("type", "name", "time_zone", "description", "teams")} == {
        "type": "schedule", "name": "Primary", "time_zone": "America/Chicago",
        "description": "Tier 1", "teams": [{"id": "PT1", "type": "team_reference"}]}
    assert not {"self", "html_url", "users", "final_schedule", "summary"} & set(schedule)
    layer = schedule["schedule_layers"][0]
    assert [u["user"]["id"] for u in layer["users"]] == ["PU3"]
    assert layer["rotation_turn_length_seconds"] == 604800
    assert "rendered_schedule_entries" not in layer
    policy = json.loads(sent[1].body)["escalation_policy"]
    assert (policy["num_loops"], policy["on_call_handoff_notifications"]) == (2, "always")
    assert policy["teams"] == [{"id": "PT1", "type": "team_reference"}]
    assert "services" not in policy
    assert policy["escalation_rules"][0]["targets"] == [
        {"type": "schedule_reference", "id": "PS1"}]


@responses.activate
def test_team_only_removal_does_not_touch_schedules_or_policies():
    mock_account()
    responses.delete(f"{B}/teams/PT1/members/PU1", status=204)
    assert rtm.main(["--team-id", "PT1", "--user-ids", "PU1", "-y"]) == 0
    assert [(r.method, r.url.replace(B, "")) for r in writes()] == [
        ("DELETE", "/teams/PT1/members/PU1")]
    assert not any("/schedules" in c.request.url for c in responses.calls)


@responses.activate
def test_rerun_after_everything_is_done_writes_nothing():
    mock_account(members=("PU2",), layer_users=("PU3",), rule_targets=(("schedule", "PS1"),))
    assert rtm.main([*ALL, "-y"]) == 0
    assert writes() == []


@pytest.mark.parametrize("argv", [
    ["--team-id", "PT1", "--user-ids", " , "],
    ["--team-id", "PT1", "--from-schedules"],
    ["--team-id", "PT1", "-y"],
])
def test_usage_errors_exit_2(argv):
    assert rtm.main(argv) == 2


@responses.activate
def test_unknown_user_exits_2_before_any_write():
    mock_account()
    responses.get(f"{B}/users/PNOPE", status=404, json={"error": {"message": "Not Found"}})
    assert rtm.main([*ALL, "--user-ids", "PU1,PNOPE", "-y"]) == 2
    assert writes() == []


@responses.activate
@pytest.mark.parametrize("account, flag", [
    ({"layer_users": ("PU1",)}, "--from-schedules"),
    ({"rule_targets": (("user", "PU1"),)}, "--from-escalation-policies"),
])
def test_removal_that_would_empty_a_layer_or_rule_exits_2(account, flag):
    mock_account(**account)
    assert rtm.main(["--team-id", "PT1", "--user-ids", "PU1", flag, "-y"]) == 2
    assert writes() == []


@responses.activate
def test_non_interactive_stdin_without_yes_is_refused(monkeypatch):
    mock_account()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        rtm.main(ALL)
    assert exc.value.code == 2
    assert writes() == []


def test_missing_team_id_on_non_interactive_stdin_exits_2(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        rtm.main(["--user-ids", "PU1", "-y"])
    assert exc.value.code == 2


@responses.activate
def test_failed_schedule_update_keeps_the_user_on_the_team_and_exits_1():
    mock_account()
    responses.put(f"{B}/schedules/PS1", status=400, json={"error": {"message": "Bad layer"}})
    responses.put(f"{B}/escalation_policies/PE1", json={"escalation_policy": {}})
    assert rtm.main([*ALL, "-y"]) == 1
    assert ("DELETE", "/teams/PT1/members/PU1") not in [
        (r.method, r.url.replace(B, "")) for r in writes()]


@responses.activate
def test_failed_team_removal_of_one_user_does_not_stop_the_others():
    mock_account()
    responses.delete(f"{B}/teams/PT1/members/PU1", status=500, json={"error": {"message": "x"}})
    responses.delete(f"{B}/teams/PT1/members/PU2", status=204)
    assert rtm.main(["--team-id", "PT1", "--user-ids", "PU1,PU2", "-y"]) == 1
    assert ("DELETE", f"{B}/teams/PT1/members/PU2") in [(r.method, r.url) for r in writes()]


@responses.activate
def test_auth_error_aborts_the_run():
    mock_account()
    responses.delete(f"{B}/teams/PT1/members/PU1", status=403, json={"error": {"message": "no"}})
    with pytest.raises(PDApiError) as exc:
        rtm.main(["--team-id", "PT1", "--user-ids", "PU1,PU2", "-y"])
    assert exc.value.is_auth_error
    assert len(writes()) == 1  # PU2 was never attempted


def test_interactive_mode_still_needs_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert rtm.main(["--team-id", "PT1"]) == 2


@responses.activate
def test_user_on_a_shift_based_v3_schedule_blocks_from_schedules():
    mock_account(v3=(("V1", "Night shift"),))
    assert rtm.main([*ALL, "-y"]) == 2
    assert writes() == []


@responses.activate
def test_v3_membership_that_cannot_be_checked_fails_closed():
    mock_account()
    responses.replace(responses.GET, f"{B}/v3/schedules", status=500,
                      json={"error": {"message": "down"}})
    assert rtm.main([*ALL, "-y"]) == 2
    assert writes() == []


@responses.activate
def test_unknown_team_is_a_usage_error():
    mock_account()
    responses.replace(responses.GET, f"{B}/teams/PT1/members", status=404,
                      json={"error": {"message": "Not Found"}})
    assert rtm.main(["--team-id", "PT1", "--user-ids", "PU1", "-y"]) == 2


@responses.activate
def test_interactive_assignment_map_finds_user_reference_policy_targets():
    mock_account()
    responses.get(f"{B}/schedules/PS1/users", json={"users": [], "more": False})
    _, user_policies, _ = rtm.build_assignment_maps("t", "PT1")
    assert user_policies["PU1"] == [{"id": "PE1", "name": "Ops EP"}]


@responses.activate
def test_v3_membership_check_sends_the_early_access_header():
    mock_account()
    assert rtm.main([*ALL, "--dry-run"]) == 0
    v3_calls = [c.request for c in responses.calls if "/v3/schedules" in c.request.url]
    assert v3_calls
    assert all(r.headers.get("X-EARLY-ACCESS") == "flexible-schedules-early-access"
               for r in v3_calls)
