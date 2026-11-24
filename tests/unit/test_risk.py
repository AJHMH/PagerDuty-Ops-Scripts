"""assess(): the risk verdict for one Run of a command (ADR 0010).

Expected verdicts come from the Irreversible operation list in CONTEXT.md and ADR 0003:
SCIM user deletion, Event Rules conversion, any prune, and any ServiceNow production apply.
"""

import json

import pytest

from pagerduty_ops.commands import (
    apply_tags,
    business_services,
    event_orchestration,
    event_rules_migrate,
    list_users,
    reorg,
    scim_user_sync,
    sn_sync,
)
from pagerduty_ops.risk import assess


def _plan(tmp_path, **options):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"kind": "x", "items": [], "options": options}), "utf-8")
    return str(path)


def _plan_with(tmp_path, items, **options):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"kind": "x", "items": items, "options": options}), "utf-8")
    return str(path)


def test_read_only_command_does_not_write():
    verdict = assess(list_users, [])
    assert (verdict.writes, verdict.dry_run, verdict.irreversible) == (False, False, False)


def test_dry_run_flag_selects_a_preview():
    assert assess(apply_tags, ["tags.csv", "--dry-run"]).dry_run is True
    verdict = assess(apply_tags, ["tags.csv"])
    assert (verdict.writes, verdict.dry_run, verdict.irreversible) == (True, False, False)


def test_diff_first_command_previews_unless_apply_is_given():
    assert assess(business_services, ["apply", "-i", "bs.json"]).dry_run is True
    assert assess(business_services, ["apply", "-i", "bs.json", "--apply"]).dry_run is False


def test_read_only_subcommand_of_a_writing_command():
    assert assess(business_services, ["export"]).writes is False
    assert assess(event_rules_migrate, ["inventory"]).writes is False


def test_prune_makes_an_apply_irreversible():
    reversible = assess(business_services, ["apply", "-i", "bs.json", "--apply"])
    pruning = assess(business_services, ["apply", "-i", "bs.json", "--apply", "--prune"])
    assert reversible.irreversible is False
    assert pruning.irreversible is True
    assert pruning.reasons == ("--prune given",)


def test_event_rules_conversion_is_always_irreversible():
    verdict = assess(event_rules_migrate, ["convert", "--all", "--dry-run"])
    assert verdict.irreversible is True
    assert verdict.dry_run is True
    assert assess(event_rules_migrate, ["activate", "--all"]).irreversible is False


def test_reorg_apply_is_reversible_even_for_a_pruning_plan(tmp_path):
    # ADR 0010: membership and resource-team removals can be re-added, so no Approval.
    plan = _plan_with(tmp_path, [{"op": "remove_member", "team_id": "T1", "user_id": "U1"}],
                      prune=True)
    verdict = assess(reorg, ["apply", plan])
    assert (verdict.writes, verdict.irreversible) == (True, False)


def test_unreadable_plan_fails_closed(tmp_path):
    verdict = assess(scim_user_sync, ["apply", str(tmp_path / "missing.json")])
    assert verdict.irreversible is True
    (tmp_path / "bad.json").write_text("not json", "utf-8")
    assert assess(scim_user_sync, ["apply", str(tmp_path / "bad.json")]).irreversible is True


def test_servicenow_production_apply_is_irreversible(tmp_path, monkeypatch):
    monkeypatch.setenv("SN_PROD_INSTANCES", "dallastx")
    monkeypatch.setenv("SN_REHEARSAL_INSTANCES", "dallastx-test,dallastx-dev")
    plan = _plan(tmp_path, prune=False)
    prod = assess(sn_sync, ["apply", plan, "--instance", "dallastx"])
    assert prod.irreversible is True
    assert prod.reasons == ("ServiceNow production instance dallastx",)
    assert assess(sn_sync, ["apply", plan, "--instance", "dallastx-test"]).irreversible is False


def test_servicenow_apply_fails_closed_without_production_instances(tmp_path, monkeypatch):
    monkeypatch.delenv("SN_PROD_INSTANCES", raising=False)
    plan = _plan(tmp_path, prune=False)
    assert assess(sn_sync, ["apply", plan, "--instance", "dallastx-test"]).irreversible is True


def test_caps_are_reported_for_override_gating():
    assert assess(business_services, ["apply", "-i", "bs.json"]).cap_flags == ("--max-deletes",)
    assert assess(list_users, []).cap_flags == ()


def test_second_parser_of_a_two_script_command():
    assert assess(event_orchestration, [], builder="build_export_parser").writes is False
    applying = assess(event_orchestration, ["--apply"], builder="build_apply_parser")
    assert (applying.writes, applying.dry_run) == (True, False)


def test_invalid_arguments_are_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        assess(apply_tags, ["--no-such-flag"])
    assert exc.value.code == 2


def test_scim_plan_that_deletes_users_is_irreversible_whatever_its_options(tmp_path):
    plan = _plan_with(tmp_path, [{"op": "delete_user", "scim_id": "s1"}],
                      delete_orphans=False)
    verdict = assess(scim_user_sync, ["apply", plan])
    assert verdict.irreversible is True
    assert verdict.reasons == ("plan has 1 delete_user item(s)",)


def test_scim_plan_with_only_blocked_deletes_is_reversible(tmp_path):
    plan = _plan_with(tmp_path, [{"op": "blocked_delete", "scim_id": "s1"},
                                 {"op": "set_display_name", "scim_id": "s2"}])
    assert assess(scim_user_sync, ["apply", plan]).irreversible is False


def test_sn_plan_that_clears_ids_is_irreversible_whatever_its_options(tmp_path, monkeypatch):
    monkeypatch.setenv("SN_PROD_INSTANCES", "dallastx")
    clearing = _plan_with(tmp_path, [{"op": "sn_set_field", "field": "f", "value": ""}],
                          prune=False)
    verdict = assess(sn_sync, ["apply", clearing, "--instance", "dallastx-test"])
    assert verdict.reasons == ("plan has 1 sn_set_field item(s) clearing a value",)
    setting = _plan_with(tmp_path, [{"op": "sn_set_field", "field": "f", "value": "PABC"}])
    assert assess(sn_sync, ["apply", setting, "--instance", "dallastx-test"]).irreversible is False


def test_relative_plan_path_resolves_against_the_run_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plan_with(run_dir, [{"op": "delete_user", "scim_id": "s1"}])
    monkeypatch.chdir(tmp_path)
    verdict = assess(scim_user_sync, ["apply", "plan.json"], cwd=str(run_dir))
    assert verdict.reasons == ("plan has 1 delete_user item(s)",)


def test_help_is_refused_rather_than_exiting_zero():
    with pytest.raises(ValueError):
        assess(apply_tags, ["--help"])


def test_risk_rejects_inconsistent_declarations():
    from pagerduty_ops.risk import Risk

    with pytest.raises(ValueError):
        Risk(writes=True)
    with pytest.raises(ValueError):
        Risk(irreversible_flags=("--prune",))


def test_assessment_reports_how_a_dry_run_is_chosen():
    from pagerduty_ops.risk import APPLY_FLAG, DRY_RUN_FLAG

    assert assess(apply_tags, ["tags.csv"]).write_switch == DRY_RUN_FLAG
    assert assess(business_services, ["apply", "-i", "bs.json"]).write_switch == APPLY_FLAG
    assert assess(list_users, []).write_switch is None


def test_a_cap_raised_above_its_default_is_reported():
    # Which flags are caps comes from the risk declaration; their defaults from the parser.
    from pagerduty_ops.commands.business_services import DEFAULT_MAX_DELETES
    from pagerduty_ops.risk import RaisedCap

    raised = assess(business_services, ["apply", "-i", "bs.json", "--max-deletes",
                                        str(DEFAULT_MAX_DELETES + 5)])
    assert raised.raised_caps == (
        RaisedCap("--max-deletes", DEFAULT_MAX_DELETES, DEFAULT_MAX_DELETES + 5),)


def test_a_cap_at_or_below_its_default_is_not_an_override():
    from pagerduty_ops.commands.business_services import DEFAULT_MAX_DELETES

    for value in (DEFAULT_MAX_DELETES, 0):
        verdict = assess(business_services, ["apply", "-i", "bs.json", "--max-deletes",
                                             str(value)])
        assert verdict.raised_caps == ()
    assert assess(business_services, ["apply", "-i", "bs.json"]).raised_caps == ()
    assert assess(list_users, []).raised_caps == ()


def test_raised_caps_cover_each_capped_command():
    from pagerduty_ops.commands.event_rules_migrate import DEFAULT_MAX_SERVICES
    from pagerduty_ops.commands.sn_sync import DEFAULT_MAX_WRITES

    caps = assess(event_rules_migrate, ["activate", "--all", "--max-services",
                                        str(DEFAULT_MAX_SERVICES + 1)]).raised_caps
    assert [c.flag for c in caps] == ["--max-services"]
    caps = assess(sn_sync, ["apply", "plan.json", "--instance", "x", "--max-writes",
                            str(DEFAULT_MAX_WRITES * 2)]).raised_caps
    assert [(c.flag, c.default, c.value) for c in caps] == [
        ("--max-writes", DEFAULT_MAX_WRITES, DEFAULT_MAX_WRITES * 2)]


@pytest.mark.parametrize("argv", [
    ["apply", "-i", "bs.json", "--max-d", "40"],          # an argparse prefix
    ["apply", "-i", "bs.json", "--max-deletes=40"],       # an attached value
    ["apply", "-i", "bs.json", "--max-deletes", "1", "--max-deletes", "40"],  # the last wins
])
def test_raised_caps_read_the_argv_as_the_parser_does(argv):
    assert [(c.flag, c.value) for c in assess(business_services, argv).raised_caps] == [
        ("--max-deletes", 40)]


def test_a_declared_cap_the_parser_lacks_fails_closed(monkeypatch):
    # A typo in cap_flags must not silently switch the check off.
    from pagerduty_ops.risk import writes_with_apply

    declaration = dict(business_services.RISK)
    declaration["apply"] = writes_with_apply(cap_flags=("--max-delete",))
    monkeypatch.setattr(business_services, "RISK", declaration)
    with pytest.raises(LookupError, match="--max-delete"):
        assess(business_services, ["apply", "-i", "bs.json"])
