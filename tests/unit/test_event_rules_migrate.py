"""pd-event-rules-migrate: inventory states, irreversible-convert guards, safe activation."""

import json

import pytest
import responses

from pagerduty_ops.api import PD_API_BASE
from pagerduty_ops.commands import event_rules_migrate as erm

# service_id: (event_rule_count, converted, eo_active)
ACCOUNT = {"PS1": (3, False, False),   # needs_convert
           "PS2": (2, True, False),    # needs_activate
           "PS3": (1, True, True),     # migrated
           "PS4": (0, False, False)}   # no_event_rules


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PD_API_TOKEN", "t")


def mock_account(account=None):
    account = account or ACCOUNT
    responses.get(f"{PD_API_BASE}/services", json={"services": [
        {"id": sid, "name": f"svc-{sid}"} for sid in account], "more": False})
    for sid, (count, converted, active) in account.items():
        body = {"rules": [{"id": "r"}] if count else [], "total": count}
        if converted:
            body["migrated_status"] = "completed"
        responses.get(f"{PD_API_BASE}/services/{sid}/rules", json=body)
        responses.get(f"{PD_API_BASE}/event_orchestrations/services/{sid}/active",
                      json={"active": active})


def writes():
    return [(c.request.method, c.request.url.split(PD_API_BASE)[1]) for c in responses.calls
            if c.request.method != "GET"]


@responses.activate
def test_inventory_states_and_fail_on_unmigrated(capsys):
    mock_account()
    assert erm.main(["inventory", "-f", "csv"]) == 0
    out = capsys.readouterr().out
    for line in ("PS1,svc-PS1,3,False,False,needs_convert",
                 "PS2,svc-PS2,2,True,False,needs_activate",
                 "PS3,svc-PS3,1,True,True,migrated",
                 "PS4,svc-PS4,0,False,False,no_event_rules"):
        assert line in out
    assert erm.main(["inventory", "--fail-on-unmigrated", "-q"]) == 1
    assert "include%5B%5D=migrated_metadata" in responses.calls[1].request.url


@responses.activate
def test_convert_all_targets_only_unconverted_and_dry_run_writes_nothing():
    mock_account()
    assert erm.main(["convert", "--all", "--dry-run"]) == 0
    assert writes() == []
    responses.post(f"{PD_API_BASE}/services/PS1/rules/convert",
                   json={"convert_status": "success", "converted_to": "https://x"})
    assert erm.main(["convert", "--all", "-y"]) == 0
    assert writes() == [("POST", "/services/PS1/rules/convert")]


@responses.activate
def test_convert_respects_max_services_cap():
    mock_account({f"P{i}": (1, False, False) for i in range(3)})
    assert erm.main(["convert", "--all", "-y", "--max-services", "2"]) == 2
    assert writes() == []


@responses.activate
def test_activate_refuses_services_with_unconverted_rules():
    mock_account()
    assert erm.main(["activate", "--service-ids", "PS1,PS2", "-y"]) == 2
    assert writes() == []


@responses.activate
def test_activate_all_skips_unconverted_and_already_active():
    mock_account()
    for sid in ("PS2", "PS4"):
        responses.put(f"{PD_API_BASE}/event_orchestrations/services/{sid}/active",
                      json={"active": True})
    assert erm.main(["activate", "--all", "-y"]) == 0
    assert writes() == [("PUT", "/event_orchestrations/services/PS2/active"),
                        ("PUT", "/event_orchestrations/services/PS4/active")]
    put = next(c for c in responses.calls if c.request.method == "PUT")
    assert json.loads(put.request.body) == {"active": True}


@responses.activate
def test_deactivate_and_failure_exit_code():
    mock_account()
    responses.put(f"{PD_API_BASE}/event_orchestrations/services/PS3/active", status=500,
                  json={"error": {"message": "boom"}})
    assert erm.main(["deactivate", "--service-ids", "PS3", "-y"]) == 1


@responses.activate
def test_unknown_service_id_is_a_usage_error():
    mock_account()
    with pytest.raises(SystemExit) as exc:
        erm.main(["convert", "--service-ids", "PNOPE", "-y"])
    assert exc.value.code == 2


@responses.activate
def test_unreadable_state_refuses_changes():
    mock_account({"PS1": (1, False, False)})
    responses.replace(responses.GET, f"{PD_API_BASE}/services/PS1/rules", status=404,
                      json={"error": {"message": "nope"}})
    assert erm.main(["convert", "--all", "-y"]) == 2
    assert writes() == []


@pytest.mark.parametrize("value", ["", " ", ",", " , "])
@pytest.mark.parametrize("command", ["convert", "activate", "deactivate"])
@responses.activate
def test_empty_service_ids_never_widen_to_all(command, value):
    mock_account()
    responses.post(f"{PD_API_BASE}/services/PS1/rules/convert", json={})
    for sid in ACCOUNT:
        responses.put(f"{PD_API_BASE}/event_orchestrations/services/{sid}/active", json={})
    assert erm.main([command, "--service-ids", value, "-y"]) == 2
    assert writes() == []
    assert not [c for c in responses.calls if "/rules" in c.request.url]  # no state loaded
