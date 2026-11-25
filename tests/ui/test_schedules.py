"""#80 Scheduled read-only Runs with Drift notifications (ADR 0014). Seam B: the HTTP API with
real subprocess Runs of test-only commands (a pd-validate stand-in with the real parser and
declarations), the controllable clock, ticks driven directly, and Teams mocked with
`responses`."""

import dataclasses
import json

import pytest
import responses
from conftest import (
    APPROVER,
    HERE,
    OPERATOR,
    VIEWER,
    HeaderIdentity,
    admin_header,
    wait,
)
from fastapi.testclient import TestClient

from pagerduty_ops_ui.app import create_app

WEBHOOK = "https://prod-00.westus.logic.azure.com/workflows/x/triggers/manual/paths/invoke?sig=S"
BASE = "https://pd-ops.dallas.example"
BOB = admin_header("PD-Ops-Operator", email="bob@dallas.gov", name="Bob")
NIGHTLY = {"kind": "daily", "at": "02:30"}


def service(settings, catalog, clock):
    """The service as a context manager, so a test can stop it and start it again."""
    app = create_app(settings, identity=HeaderIdentity(), catalog=catalog, clock=clock)
    client = TestClient(app, headers={"X-PD-Ops-Request": "1"})
    client.scheduler = app.state.scheduler
    return client


class Service:
    """The service, with its background scheduler stopped once started: the tests tick it
    themselves, so nothing fires behind their backs."""

    def __init__(self, settings, catalog, clock):
        self.client = service(settings, catalog, clock)

    def __enter__(self):
        self.client.__enter__()
        self.client.scheduler.stop()
        return self.client

    def __exit__(self, *exc):
        return self.client.__exit__(*exc)


@pytest.fixture
def webhook():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(WEBHOOK, status=202)
        yield mock


@pytest.fixture
def teams_settings(settings, webhook):
    return dataclasses.replace(settings, teams_webhook_url=WEBHOOK, public_url=BASE)


@pytest.fixture
def svc(teams_settings, catalog, clock, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    with Service(teams_settings, catalog, clock) as client:
        yield client


def schedule(client, *, name="Nightly drift report", command="ui-validate", values=None,
             timing=NIGHTLY, headers=OPERATOR, subcommand=None):
    return client.post("/api/schedules", headers=headers, json={
        "name": name, "command": command, "subcommand": subcommand,
        "values": values or {}, "timing": timing})


def created(client, **kwargs):
    response = schedule(client, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def fire(client, clock, **advance):
    """Move the clock on and tick: the Runs the scheduler started, finished."""
    clock.advance(**advance)
    return [wait(client, run_id) for run_id in client.scheduler.tick()]


def judge(client):
    return client.scheduler.judge_finished()


def cards(webhook):
    return [json.loads(c.request.body)["attachments"][0]["content"] for c in webhook.calls]


# ---------- what can be scheduled ----------

def test_an_operator_schedules_a_read_only_command(svc):
    made = created(svc, values={"fail_on": "warning"})
    assert (made["name"], made["command"], made["paused"]) == (
        "Nightly drift report", "ui-validate", False)
    assert made["description"] == "Every day at 02:30 Central"
    assert made["created_by"] == "ann@dallas.gov" and made["next_run_at"]
    assert svc.get("/api/schedules", headers=VIEWER).json()["schedules"] == [made]


def test_a_viewer_can_see_schedules_but_not_make_them(svc):
    created(svc)
    assert schedule(svc, headers=VIEWER).status_code == 403
    assert len(svc.get("/api/schedules", headers=VIEWER).json()["schedules"]) == 1


def test_scheduling_a_command_that_writes_is_refused(svc):
    response = schedule(svc, command="ui-writer")
    assert response.status_code == 403
    assert "Only read-only commands can be scheduled" in response.json()["detail"]
    # A command that previews unless told to apply: its Run would be a dry run of a write.
    response = schedule(svc, command="ui-differ")
    assert response.status_code == 403
    assert "a dry run of a command that writes" in response.json()["detail"]
    # The mode flags aren't form values at all: the service sets them itself.
    assert schedule(svc, command="ui-writer", values={"dry_run": True}).status_code == 422
    assert svc.get("/api/schedules", headers=VIEWER).json()["schedules"] == []


def test_a_writing_subcommand_is_refused_and_a_read_only_one_allowed(svc):
    assert schedule(svc, command="ui-subcommands", subcommand="apply",
                    values={"name": "x"}).status_code in (403, 422)
    form = svc.get("/api/commands/ui-subcommands/form", headers=VIEWER).json()
    read_only = [s["name"] for s in form["subcommands"] if not s["writes"]]
    assert read_only, form
    assert schedule(svc, command="ui-subcommands", subcommand=read_only[0]).status_code == 201


def test_quiet_logging_is_refused_where_it_would_hide_drift(svc):
    # pd-validate's Drift is read from its "Findings: ..." INFO line, which --quiet drops.
    response = schedule(svc, values={"quiet": True})
    assert response.status_code == 422
    assert "--quiet would hide" in response.json()["detail"]
    # Commands that declare no Drift may be quiet.
    assert schedule(svc, command="pd-list-teams", values={"quiet": True}).status_code == 201


def test_schedules_take_arguments_not_files(svc):
    response = schedule(svc, values={"config": "checks.json"})
    assert response.status_code == 422
    assert "arguments only" in response.json()["detail"]


@pytest.mark.parametrize("timing", [{"kind": "daily", "at": "25:00"}, {"kind": "monthly"},
                                    {"kind": "weekly", "at": "02:30", "days": []},
                                    {"kind": "hourly", "every_hours": 5}])
def test_unusable_timings_are_refused(svc, timing):
    assert schedule(svc, timing=timing).status_code == 422


# ---------- Scheduled runs ----------

def test_a_scheduled_run_is_in_the_history_marked_and_attributed(svc, clock):
    made = created(svc)
    [run] = fire(svc, clock, days=1)
    assert (run["kind"], run["outcome"], run["schedule_id"]) == ("report", "succeeded",
                                                                 made["id"])
    assert (run["requested_by"], run["requester_name"]) == ("ann@dallas.gov", "Ann")
    assert svc.get(f"/api/runs/{run['id']}", headers=VIEWER).json()["schedule_name"] == \
        "Nightly drift report"
    history = svc.get("/api/runs", headers=VIEWER).json()["runs"]
    assert [r["schedule_id"] for r in history if r["id"] == run["id"]] == [made["id"]]
    again = svc.get(f"/api/schedules/{made['id']}", headers=VIEWER).json()
    assert again["last_run_id"] == run["id"] and again["next_run_at"] > run["queued_at"]


def test_missed_times_run_once_not_once_each(svc, clock):
    created(svc)
    runs = fire(svc, clock, days=4)  # four nightly times missed
    assert len(runs) == 1
    assert svc.scheduler.tick() == []  # and it is next due tomorrow, not now


def test_missed_times_after_a_restart_run_once(teams_settings, catalog, clock, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    with Service(teams_settings, catalog, clock) as before:
        made = created(before)
    clock.advance(days=3)  # the service was down over three nightly times
    with Service(teams_settings, catalog, clock) as after:
        started = after.scheduler.tick()
        assert len(started) == 1 and after.scheduler.tick() == []
        assert wait(after, started[0])["schedule_id"] == made["id"]


def test_a_paused_schedule_doesnt_run_and_resuming_skips_what_it_missed(svc, clock):
    made = created(svc)
    paused = svc.post(f"/api/schedules/{made['id']}/pause", headers=BOB).json()
    assert paused["paused"] and paused["next_run_at"] is None
    assert fire(svc, clock, days=2) == []
    resumed = svc.post(f"/api/schedules/{made['id']}/resume", headers=BOB).json()
    assert not resumed["paused"] and svc.scheduler.tick() == []  # due after now
    assert len(fire(svc, clock, days=1)) == 1


def test_editing_keeps_the_creator_and_is_judged_again(svc, clock):
    made = created(svc)
    edited = svc.put(f"/api/schedules/{made['id']}", headers=BOB, json={
        "name": "Weekday drift report", "command": "ui-validate",
        "values": {"fail_on": "error"},
        "timing": {"kind": "weekly", "at": "06:15", "days": [0, 1, 2, 3, 4]}})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["name"], body["created_by"], body["updated_by"]) == (
        "Weekday drift report", "ann@dallas.gov", "bob@dallas.gov")
    assert body["description"] == "Mon, Tue, Wed, Thu and Fri at 06:15 Central"
    assert svc.put(f"/api/schedules/{made['id']}", headers=BOB, json={
        "name": "x", "command": "ui-writer", "values": {}, "timing": NIGHTLY}).status_code == 403


def test_deleting_a_schedule_keeps_its_runs(svc, clock):
    made = created(svc)
    [run] = fire(svc, clock, days=1)
    assert svc.delete(f"/api/schedules/{made['id']}", headers=VIEWER).status_code == 403
    assert svc.delete(f"/api/schedules/{made['id']}", headers=APPROVER).status_code == 204
    kept = svc.get(f"/api/runs/{run['id']}", headers=VIEWER).json()
    assert (kept["schedule_id"], kept["schedule_name"]) == (made["id"], None)
    assert fire(svc, clock, days=1) == []


def test_a_schedule_the_engine_refuses_later_records_why(svc, clock, catalog):
    created(svc, command="ui-fixture")
    catalog["ui-fixture"] = catalog["ui-writer"]  # the command now writes
    [run] = fire(svc, clock, days=1)
    assert run["outcome"] == "usage-error"
    assert "Not run: the schedule was refused" in run["message"]


# ---------- Drift ----------

def test_drift_found_by_a_scheduled_pd_validate_is_told_to_teams(svc, clock, teams_settings,
                                                                  webhook):
    teams_settings.extra_env["UI_FIXTURE_FINDINGS"] = "info=4, warning=1, error=2"
    created(svc)
    [run] = fire(svc, clock, days=1)
    assert run["exit_code"] == 1
    assert judge(svc) == 1 and judge(svc) == 0  # decided once

    judged = svc.get(f"/api/runs/{run['id']}", headers=VIEWER).json()
    assert judged["drift"] is True
    assert judged["drift_summary"] == "Findings: info=4, warning=1, error=2"
    from conftest import wait_until
    wait_until(lambda: len(webhook.calls) == 1, timeout=10)
    [card] = cards(webhook)
    assert card["body"][0]["text"] == "Drift found: ui-validate"
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["Schedule"] == "Nightly drift report"
    assert facts["Summary"] == "Findings: info=4, warning=1, error=2"
    assert card["actions"][0]["url"] == f"{BASE}/#/runs/{run['id']}"


def test_no_drift_no_message(svc, clock, teams_settings, webhook):
    teams_settings.extra_env["UI_FIXTURE_FINDINGS"] = "info=3, warning=2, error=0"
    created(svc)  # fails on error (the default): warnings aren't Drift here
    [run] = fire(svc, clock, days=1)
    assert (run["exit_code"], judge(svc)) == (0, 0)
    assert svc.get(f"/api/runs/{run['id']}", headers=VIEWER).json()["drift"] is False
    assert len(webhook.calls) == 0


def test_a_failed_run_is_not_drift(svc, clock, teams_settings, webhook):
    teams_settings.extra_env["UI_FIXTURE_API_FAIL"] = "1"  # exits 1, but no summary line
    created(svc)
    [run] = fire(svc, clock, days=1)
    assert (run["exit_code"], judge(svc)) == (1, 0)
    assert svc.get(f"/api/runs/{run['id']}", headers=VIEWER).json()["drift"] is False
    assert len(webhook.calls) == 0


def test_commands_that_declare_no_drift_never_report_it(svc, clock, webhook):
    created(svc, command="ui-fixture", values={"exit_code": 1})
    [run] = fire(svc, clock, days=1)
    assert (run["exit_code"], judge(svc)) == (1, 0)
    assert len(webhook.calls) == 0


def test_drift_is_decided_after_a_restart_too(teams_settings, catalog, clock, monkeypatch,
                                              webhook):
    # A Run that finished while nothing judged it (here: before any tick) is judged later.
    monkeypatch.syspath_prepend(str(HERE))
    teams_settings.extra_env["UI_FIXTURE_FINDINGS"] = "error=1"
    with Service(teams_settings, catalog, clock) as before:
        created(before)
        clock.advance(days=1)
        [due] = before.scheduler.store.due_schedules()
        wait(before, before.scheduler.fire(due))  # started, finished, not yet judged
    with Service(teams_settings, catalog, clock) as after:
        assert judge(after) == 1
