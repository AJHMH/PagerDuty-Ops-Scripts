"""pd-reorg: CSV + YAML compilation, conflict detection, prune, service assertions, apply."""

import json

import pytest
import responses
import yaml

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import reorg
from pagerduty_ops.pdstate import PDState
from pagerduty_ops.plan import new_plan, write_plan


def account():
    pd = PDState("t")
    pd.teams = {"PA": {"id": "PA", "name": "Alpha"}, "PB": {"id": "PB", "name": "Beta"}}
    pd._members = {"PA": {"PU1": "manager", "PU2": "responder"}, "PB": {"PU3": "manager"}}
    pd.users = {u: {"id": u, "name": u.lower(), "role": "user"} for u in ("PU1", "PU2", "PU3")}
    pd.users["PU0"] = {"id": "PU0", "name": "boss", "role": "owner"}
    pd.escalation_policies = {
        "PEA": {"id": "PEA", "name": "Alpha EP", "teams": [{"id": "PA"}]},
        "PEB": {"id": "PEB", "name": "Beta EP", "teams": [{"id": "PB"}]}}
    pd.schedules = {"PSA": {"id": "PSA", "name": "Alpha Sched", "teams": [{"id": "PA"}],
                            "users": [{"id": "PU2"}]}}
    pd.services = {"PSV": {"id": "PSV", "escalation_policy": {"id": "PEA"},
                           "teams": [{"id": "PA"}]}}
    return pd


def compile_inputs(tmp_path, csv_text=None, teams=None, prune=False):
    pd, intents, errors = account(), reorg.Intents(), []
    if csv_text is not None:
        p = tmp_path / "moves.csv"
        p.write_text("action,kind,id,from_team,to_team,role\n" + csv_text)
        reorg.load_moves(str(p), intents, pd, errors)
    if teams is not None:
        p = tmp_path / "teams.yaml"
        p.write_text(yaml.safe_dump({"teams": teams}))
        reorg.load_teams_yaml(str(p), intents, pd, errors, prune)
    items = [] if errors or intents.conflicts else reorg.compile_plan(pd, intents, errors)
    return items, intents, errors


def ops(items):
    return [(i["op"], i.get("team_id") or i.get("to_team"), i.get("user_id") or i.get("id"),
             i.get("role", "")) for i in items]


def test_moves_csv_compiles_and_skips_noops(tmp_path):
    items, _, errors = compile_inputs(tmp_path, (
        "move-member,user,PU2,PA,PB,\n"          # keeps current role (responder)
        "add-member,user,PU1,,PA,manager\n"      # already manager: no-op
        "set-account-role,user,PU3,,,observer\n"
        "move-resource,escalation_policy,PEA,PA,PB,\n"))
    assert errors == []
    assert ops(items) == [
        ("add_member", "PB", "PU2", "responder"),
        ("set_account_role", None, "PU3", "observer"),
        ("move_resource", "PB", "PEA", ""),
        ("remove_member", "PA", "PU2", ""),
    ]
    assert items[2]["from_team"] == "PA"
    assert "still on team schedule(s): Alpha Sched" in items[3]["detail"]


def test_csv_validation_errors(tmp_path):
    _, _, errors = compile_inputs(tmp_path, (
        "teleport,user,PU1,,PA,\n"
        "add-member,user,PNOPE,,PA,\n"
        "add-member,user,PU1,,PZ,\n"
        "move-resource,service,PSV,,PB,\n"
        "set-account-role,user,PU1,,,owner\n"))
    joined = "\n".join(errors)
    assert "unknown action" in joined and "unknown user PNOPE" in joined
    assert "unknown team PZ" in joined and "move EP PEA instead" in joined
    assert "account role must be one of" in joined


def test_yaml_prune_and_resources(tmp_path):
    items, _, errors = compile_inputs(tmp_path, teams={
        "PA": {"members": {"PU1": "manager", "PU3": {"team_role": "responder",
                                                      "account_role": "limited_user"}},
               "schedules": []}}, prune=True)
    assert errors == []
    assert set(ops(items)) == {
        ("add_member", "PA", "PU3", "responder"),
        ("set_account_role", None, "PU3", "limited_user"),
        ("remove_resource_team", "PA", "PSA", ""),
        ("remove_member", "PA", "PU2", ""),
    }


def test_yaml_without_prune_never_removes(tmp_path):
    items, _, _ = compile_inputs(tmp_path, teams={"PA": {"members": {"PU1": "manager"},
                                                         "schedules": []}})
    assert items == []


def test_csv_and_yaml_conflict_is_refused(tmp_path):
    _, intents, _ = compile_inputs(
        tmp_path, "move-member,user,PU2,PA,PB,\n",
        teams={"PA": {"members": {"PU1": "manager", "PU2": "responder"}}})
    assert len(intents.conflicts) == 1 and "team PA / user PU2" in intents.conflicts[0]


def test_service_assertion_requires_ep_ownership(tmp_path):
    _, _, errors = compile_inputs(tmp_path, teams={"PB": {"services": ["PSV"]}})
    assert any("follows escalation policy PEA" in e for e in errors)
    items, _, errors = compile_inputs(tmp_path, teams={
        "PB": {"services": ["PSV"], "escalation_policies": ["PEA", "PEB"]}})
    assert errors == [] and ops(items) == [("move_resource", "PB", "PEA", "")]


def test_owner_role_never_changed(tmp_path):
    _, _, errors = compile_inputs(tmp_path, "set-account-role,user,PU0,,,user\n")
    assert any("account owner" in e for e in errors)


def test_export_roundtrip_is_a_noop_plan(tmp_path):
    pd = account()
    doc = reorg.export_state(pd)
    assert doc["teams"]["PA"]["members"]["PU1"]["team_role"] == "manager"
    assert doc["teams"]["PA"]["services"] == ["PSV"]
    items, _, errors = compile_inputs(tmp_path, teams=doc["teams"], prune=True)
    assert errors == [] and items == []


def _plan_file(tmp_path, items):
    p = tmp_path / "plan.json"
    write_plan(new_plan("pd-reorg", items, generated_by="test"), str(p))
    return str(p)


@responses.activate
def test_apply_executes_ops_and_counts_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    path = _plan_file(tmp_path, [
        {"op": "remove_member", "team_id": "PA", "user_id": "PU2"},
        {"op": "add_member", "team_id": "PB", "user_id": "PU2", "role": "responder"},
        {"op": "move_resource", "kind": "escalation_policy", "id": "PEA", "from_team": "PA",
         "to_team": "PB"},
        {"op": "move_resource", "kind": "schedule", "id": "PSA", "from_team": "PA",
         "to_team": "PB"},
        {"op": "set_account_role", "user_id": "PU3", "role": "observer"},
    ])
    responses.put(f"{PD_API_BASE}/teams/PB/users/PU2", json={})
    responses.put(f"{PD_API_BASE}/teams/PB/escalation_policies/PEA", json={})
    responses.delete(f"{PD_API_BASE}/teams/PA/escalation_policies/PEA", status=404,
                     json={"error": {"message": "gone"}})  # already removed: ok
    responses.get(f"{PD_API_BASE}/schedules/PSA", json={"schedule": {
        "name": "Alpha Sched", "time_zone": "America/Chicago", "schedule_layers": [],
        "teams": [{"id": "PA"}]}})
    responses.put(f"{PD_API_BASE}/schedules/PSA", json={})
    responses.put(f"{PD_API_BASE}/users/PU3", status=400, json={"error": {"message": "bad"}})
    responses.delete(f"{PD_API_BASE}/teams/PA/users/PU2", status=204)
    assert reorg.main(["apply", path, "-y"]) == 1  # one failure
    sched_put = next(c for c in responses.calls if c.request.method == "PUT"
                     and c.request.url.endswith("/schedules/PSA"))
    assert json.loads(sched_put.request.body)["schedule"]["teams"] == [
        {"id": "PB", "type": "team_reference"}]
    methods = [(c.request.method, c.request.url.split(PD_API_BASE)[1]) for c in responses.calls]
    assert methods[0] == ("PUT", "/teams/PB/users/PU2")      # adds before removals
    assert methods[-1] == ("DELETE", "/teams/PA/users/PU2")


@responses.activate
def test_apply_dry_run_and_invalid_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    path = _plan_file(tmp_path, [{"op": "add_member", "team_id": "PB", "user_id": "PU2",
                                  "role": "responder"}])
    assert reorg.main(["apply", path, "--dry-run"]) == 0
    assert len(responses.calls) == 0
    bad = _plan_file(tmp_path, [{"op": "add_member", "team_id": "PB", "user_id": "PU2",
                                 "role": "boss"}])
    assert reorg.main(["apply", bad, "-y"]) == 2


def test_plan_main_refuses_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")
    monkeypatch.setattr(reorg, "PDState", lambda token: account())
    moves = tmp_path / "m.csv"
    moves.write_text("action,kind,id,from_team,to_team,role\nadd-member,user,PU3,,PA,manager\n")
    teams = tmp_path / "t.yaml"
    teams.write_text(yaml.safe_dump({"teams": {"PA": {"members": {"PU3": "observer"}}}}))
    out = tmp_path / "p.json"
    assert reorg.main(["plan", "--moves", str(moves), "--teams-yaml", str(teams),
                       "--plan-out", str(out)]) == 2
    assert not out.exists()
    assert reorg.main(["plan", "--moves", str(moves), "--plan-out", str(out)]) == 0
    assert json.loads(out.read_text())["items"][0]["op"] == "add_member"
    with pytest.raises(SystemExit):
        reorg.main(["plan", "--plan-out", str(out), "--bogus"])
