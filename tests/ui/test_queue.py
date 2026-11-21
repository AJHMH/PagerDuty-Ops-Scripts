"""#74 one Writing run at a time, timeouts and cancellation (ADR 0012). Seam B: the HTTP API
with real subprocess Runs of test-only fixture commands that sleep."""

import time

import pytest
from conftest import APPROVER, OPERATOR, VIEWER, admin_header, wait, wait_until

OTHER_OPERATOR = admin_header("PD-Ops-Operator", email="bob@dallas.gov", name="Bob")
OTHER_APPROVER = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")


def report(client, argv, headers=VIEWER):
    response = client.post("/api/runs", json={"command": "ui-fixture", "argv": argv},
                           headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def reviewed_dry_run(client, values=None, files=None, headers=OPERATOR):
    import json
    body = {"command": "ui-writer", "values": values or {}}
    if files:
        response = client.post("/api/runs", data={"run": json.dumps(body)}, files=files,
                               headers=headers)
    else:
        response = client.post("/api/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    dry = wait(client, response.json()["id"])
    assert dry["outcome"] == "succeeded", dry
    assert client.post(f"/api/runs/{dry['id']}/review", headers=headers).status_code == 200
    return dry


def apply(client, dry_id, headers=OPERATOR):
    response = client.post(f"/api/runs/{dry_id}/apply", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def get(client, run_id):
    return client.get(f"/api/runs/{run_id}", headers=APPROVER).json()


def applying_now(client, apply_id):
    """The fresh dry run inside the queue slot has finished, so the apply itself runs."""
    fresh_id = get(client, apply_id)["fresh_dry_run_id"]
    return fresh_id is not None and get(client, fresh_id)["status"] == "finished"


def cancel(client, run_id, headers):
    return client.post(f"/api/runs/{run_id}/cancel", headers=headers)


# ---------- Runs execute in the background ----------

def test_a_run_is_returned_at_once_and_finishes_in_the_background(client):
    created = report(client, ["--sleep", "1", "--print", "done"])
    assert (created["status"], created["outcome"]) == ("running", None)
    finished = wait(client, created["id"])
    assert (finished["status"], finished["outcome"], finished["stdout"]) == (
        "finished", "succeeded", "done\n")


# ---------- one Writing run at a time ----------

def test_a_second_writing_run_waits_until_the_first_ends(client, settings):
    first, second = reviewed_dry_run(client), reviewed_dry_run(client)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "2"
    a = apply(client, first["id"])
    b = apply(client, second["id"])
    assert b["status"] == "queued"
    wait_until(lambda: get(client, a["id"])["status"] == "running")
    queue = client.get("/api/queue", headers=VIEWER).json()
    assert queue["executing"]["id"] == a["id"]
    assert [r["id"] for r in queue["pending"]] == [b["id"]]
    a, b = wait(client, a["id"]), wait(client, b["id"])
    assert (a["outcome"], b["outcome"]) == ("succeeded", "succeeded")
    assert b["started_at"] >= a["finished_at"]
    assert client.get("/api/queue", headers=VIEWER).json() == {"executing": None,
                                                               "pending": []}


def test_reports_and_dry_runs_never_wait_in_the_queue(client, settings):
    dry = reviewed_dry_run(client)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "5"
    applying = apply(client, dry["id"])
    wait_until(lambda: get(client, applying["id"])["status"] == "running")
    quick = wait(client, report(client, ["--print", "hi"])["id"])
    dry_run = client.post("/api/runs", json={"command": "ui-writer", "values": {}},
                          headers=OPERATOR).json()
    assert wait(client, dry_run["id"])["outcome"] == "succeeded"
    assert quick["outcome"] == "succeeded"
    assert get(client, applying["id"])["status"] == "running"  # still applying meanwhile


def test_the_queue_needs_a_role(client):
    from conftest import NO_ROLE
    assert client.get("/api/queue", headers=NO_ROLE).status_code == 403


# ---------- timeouts ----------

def test_a_timeout_kills_the_whole_process_tree(client, settings):
    settings.run_timeout_seconds = 1
    created = report(client, ["--spawn-child", "alive.txt", "--sleep", "10"])
    assert wait(client, created["id"])["outcome"] == "stopped"
    time.sleep(3)  # the grandchild writes its file after 2 seconds unless it was killed
    assert not (settings.run_root / created["id"] / "alive.txt").exists()


def test_a_finished_command_leaves_no_process_behind(client, settings):
    created = report(client, ["--spawn-child", "alive.txt"])  # exits at once
    assert wait(client, created["id"])["outcome"] == "succeeded"
    time.sleep(3)
    assert not (settings.run_root / created["id"] / "alive.txt").exists()


def test_a_fresh_dry_run_that_times_out_stops_the_apply(client, settings):
    dry = reviewed_dry_run(client)
    settings.run_timeout_seconds = 1
    settings.extra_env["UI_FIXTURE_DRY_SLEEP"] = "10"
    stopped = wait(client, apply(client, dry["id"])["id"])
    assert (stopped["outcome"], stopped["stdout"]) == ("stopped", "")
    assert "Nothing was applied" in stopped["message"]


def test_an_apply_that_times_out_is_stopped(client, settings):
    dry = reviewed_dry_run(client)
    settings.run_timeout_seconds = 2
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "10"
    stopped = wait(client, apply(client, dry["id"])["id"])
    assert (stopped["outcome"], stopped["exit_code"]) == ("stopped", None)
    assert "no result after" in stopped["message"]


# ---------- cancellation ----------

def test_the_requester_can_cancel_their_own_run(client):
    created = report(client, ["--sleep", "10"])
    response = cancel(client, created["id"], VIEWER)
    assert response.status_code == 200
    stopped = wait(client, created["id"])
    assert (stopped["outcome"], stopped["cancelled_by"]) == ("stopped", "ann@dallas.gov")
    assert "cancelled by ann@dallas.gov" in stopped["message"]


@pytest.mark.parametrize("who, allowed", [(OTHER_OPERATOR, False), (OTHER_APPROVER, True)])
def test_only_approvers_can_cancel_someone_elses_run(client, who, allowed):
    created = report(client, ["--sleep", "10"], headers=OPERATOR)
    response = cancel(client, created["id"], who)
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        assert get(client, created["id"])["status"] == "running"
        cancel(client, created["id"], OPERATOR)
    assert wait(client, created["id"])["outcome"] == "stopped"


def test_a_finished_run_cannot_be_cancelled(client):
    done = wait(client, report(client, [])["id"])
    assert cancel(client, done["id"], VIEWER).status_code == 409
    assert cancel(client, "nope", VIEWER).status_code == 404


def test_a_cancelled_apply_is_stopped_and_offers_a_fresh_dry_run(client, settings):
    upload = [("file.input", ("services.csv", b"service_id\n", "text/csv"))]
    dry = reviewed_dry_run(client, files=upload)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "10"
    applying = apply(client, dry["id"])
    wait_until(lambda: applying_now(client, applying["id"]))
    assert cancel(client, applying["id"], OPERATOR).status_code == 200
    stopped = wait(client, applying["id"])
    assert stopped["outcome"] == "stopped"

    response = client.post(f"/api/runs/{stopped['id']}/fresh-dry-run", headers=OPERATOR)
    assert response.status_code == 201, response.text
    fresh = wait(client, response.json()["id"])
    assert (fresh["kind"], fresh["outcome"]) == ("dry-run", "succeeded")
    assert fresh["argv"] == dry["argv"]
    assert fresh["input_hash"] == dry["input_hash"]  # same command, arguments and inputs
    assert client.post(f"/api/runs/{stopped['id']}/fresh-dry-run",
                       headers=OTHER_OPERATOR).status_code == 403


def test_cancelling_the_fresh_dry_run_stops_the_apply(client, settings):
    dry = reviewed_dry_run(client)
    settings.extra_env["UI_FIXTURE_DRY_SLEEP"] = "10"
    applying = apply(client, dry["id"])
    wait_until(lambda: get(client, applying["id"])["fresh_dry_run_id"] is not None)
    fresh_id = get(client, applying["id"])["fresh_dry_run_id"]
    assert cancel(client, fresh_id, OPERATOR).status_code == 200
    stopped = wait(client, applying["id"])
    assert (stopped["outcome"], stopped["stdout"]) == ("stopped", "")  # never applied
    assert stopped["cancelled_by"] == "ann@dallas.gov"
    assert wait(client, fresh_id)["outcome"] == "stopped"


def test_a_run_cancelled_before_it_starts_never_starts_its_process(tmp_path, catalog,
                                                                   monkeypatch):
    import threading

    from conftest import HERE

    from pagerduty_ops_ui.runner import run_command

    monkeypatch.syspath_prepend(str(HERE))
    cancelled = threading.Event()
    cancelled.set()
    result = run_command(catalog["ui-fixture"], ["--output", "out.txt"], cwd=tmp_path,
                         extra_env={"PYTHONPATH": str(HERE)}, secret_env={}, cancel=cancelled)
    assert (result.outcome, result.stop_reason) == ("stopped", "cancelled")
    assert not (tmp_path / "out.txt").exists()


def test_a_cancel_is_not_recorded_on_a_run_that_has_finished(settings):
    from pagerduty_ops_ui.store import RunRecord, RunStore, now

    store = RunStore(settings.database_url)
    store.save(RunRecord(id="r1", command="ui-fixture", argv=[], requested_by="a@x",
                         requester_name="A", requester_role="viewer", queued_at=now(),
                         started_at=now(), timeout_seconds=60, status="running"))
    store.finish("r1", outcome="succeeded")
    assert store.request_cancel("r1", "b@x") is False
    assert store.get("r1")["cancelled_by"] is None
    store.finish("r1", outcome="failed")  # finishing twice changes nothing
    assert store.get("r1")["outcome"] == "succeeded"


def test_a_queued_apply_that_is_cancelled_never_runs(client, settings):
    first, second = reviewed_dry_run(client), reviewed_dry_run(client)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "3"
    a = apply(client, first["id"])
    b = apply(client, second["id"])
    assert cancel(client, b["id"], OPERATOR).status_code == 200
    b = get(client, b["id"])
    assert (b["status"], b["outcome"], b["started_at"]) == ("finished", "stopped", None)
    assert wait(client, a["id"])["outcome"] == "succeeded"
    assert get(client, b["id"])["stdout"] == ""


# ---------- a service restart ----------

def test_a_restart_stops_what_was_executing_and_resumes_the_queue(settings, catalog,
                                                                  monkeypatch):
    from conftest import HERE, HeaderIdentity
    from fastapi.testclient import TestClient
    from sqlalchemy import update

    from pagerduty_ops_ui.app import create_app
    from pagerduty_ops_ui.store import RunRecord, RunStore

    monkeypatch.syspath_prepend(str(HERE))

    def service():
        app = create_app(settings, identity=HeaderIdentity(), catalog=catalog)
        return TestClient(app, headers={"X-PD-Ops-Request": "1"})

    with service() as client:
        first, second = reviewed_dry_run(client), reviewed_dry_run(client)
        settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "30"
        a = apply(client, first["id"])
        wait_until(lambda: applying_now(client, a["id"]))
        b = apply(client, second["id"])
    # A clean shutdown records A as stopped. A crash leaves no time to, so put A back as
    # executing, as the database would be after the host lost power.
    with RunStore(settings.database_url).engine.begin() as db:
        db.execute(update(RunRecord).where(RunRecord.id == a["id"])
                   .values(status="running", outcome=None, finished_at=None, stderr=""))

        db.execute(update(RunRecord).where(RunRecord.id == first["id"])
                   .values(status="running", outcome=None, finished_at=None))

    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "0"
    with service() as client:
        a = get(client, a["id"])
        assert (a["status"], a["outcome"]) == ("finished", "stopped")
        assert "restarted" in a["message"] and "partly applied" in a["message"]
        cut_off_dry_run = get(client, first["id"])
        assert "restarted" in cut_off_dry_run["message"]
        assert "applied" not in cut_off_dry_run["message"]  # a dry run changes nothing
        assert wait(client, b["id"])["outcome"] == "succeeded"  # the queue resumed


def test_a_shutdown_during_the_fresh_check_leaves_the_apply_queued(settings, catalog,
                                                                    monkeypatch):
    from conftest import HERE, HeaderIdentity
    from fastapi.testclient import TestClient

    from pagerduty_ops_ui.app import create_app

    monkeypatch.syspath_prepend(str(HERE))

    def service():
        app = create_app(settings, identity=HeaderIdentity(), catalog=catalog)
        return TestClient(app, headers={"X-PD-Ops-Request": "1"})

    with service() as client:
        dry = reviewed_dry_run(client)
        settings.extra_env["UI_FIXTURE_DRY_SLEEP"] = "30"
        a = apply(client, dry["id"])
        wait_until(lambda: get(client, a["id"])["fresh_dry_run_id"] is not None)
    # Nothing was applied yet, so the apply goes back to the queue rather than Stopped.
    settings.extra_env["UI_FIXTURE_DRY_SLEEP"] = "0"
    with service() as client:
        resumed = wait(client, a["id"])
        assert (resumed["outcome"], resumed["stdout"]) == ("succeeded", "APPLIED\n")
