"""#76 the permanent Ledger and the 90-day purge (ADR 0013). Seam B: the HTTP API with real
subprocess Runs of the test-only writer, and the controllable clock to age Runs."""

import json

import pytest
from conftest import APPROVER, OPERATOR, SECRET, VIEWER, admin_header, finished, wait, wait_until

OTHER_APPROVER = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")
LEDGER_FIELDS = {"run_id", "command", "requested_by", "requester_name", "requester_role",
                 "approved_by", "queued_at", "started_at", "finished_at", "outcome"}


def reviewed(client, values=None):
    dry = finished(client, client.post("/api/runs", json={"command": "ui-writer",
                                                          "values": values or {}},
                                       headers=OPERATOR))
    assert client.post(f"/api/runs/{dry['id']}/review", headers=OPERATOR).status_code == 200
    return dry


def applied(client, values=None):
    dry = reviewed(client, values)
    return wait(client, client.post(f"/api/runs/{dry['id']}/apply",
                                    headers=OPERATOR).json()["id"])


def ledger(client, headers=VIEWER, **params):
    response = client.get("/api/ledger", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def row_for(client, run_id):
    rows = [r for r in ledger(client)["rows"] if r["run_id"] == run_id]
    assert len(rows) == 1, rows
    return rows[0]


# ---------- every finished Writing run leaves one row ----------

def test_a_succeeded_apply_leaves_a_ledger_row(client):
    run = applied(client)
    row = row_for(client, run["id"])
    assert set(row) == LEDGER_FIELDS
    assert (row["command"], row["requested_by"], row["outcome"], row["approved_by"]) == (
        "ui-writer", "ann@dallas.gov", "succeeded", None)
    assert row["finished_at"] == run["finished_at"]


@pytest.mark.parametrize("env, outcome", [({"UI_FIXTURE_APPLY_EXIT": "1"}, "failed"),
                                          ({"UI_FIXTURE_DRIFT": "1"}, "changed")])
def test_every_outcome_leaves_a_row(client, settings, env, outcome):
    dry = reviewed(client)
    settings.extra_env.update(env)
    run = wait(client, client.post(f"/api/runs/{dry['id']}/apply", headers=OPERATOR).json()["id"])
    assert run["outcome"] == outcome
    assert row_for(client, run["id"])["outcome"] == outcome


def test_a_stopped_apply_leaves_a_row(client, settings):
    dry = reviewed(client)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "10"
    apply = client.post(f"/api/runs/{dry['id']}/apply", headers=OPERATOR).json()
    wait_until(lambda: client.get(f"/api/runs/{apply['id']}", headers=OPERATOR)
               .json()["fresh_dry_run_id"] is not None)
    import time
    time.sleep(1)
    client.post(f"/api/runs/{apply['id']}/cancel", headers=OPERATOR)
    assert wait(client, apply["id"])["outcome"] == "stopped"
    assert row_for(client, apply["id"])["outcome"] == "stopped"


def test_an_apply_cancelled_before_it_started_leaves_a_row(client, settings):
    first, second = reviewed(client), reviewed(client)
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "3"
    a = client.post(f"/api/runs/{first['id']}/apply", headers=OPERATOR).json()
    b = client.post(f"/api/runs/{second['id']}/apply", headers=OPERATOR).json()
    client.post(f"/api/runs/{b['id']}/cancel", headers=OPERATOR)
    row = row_for(client, b["id"])
    assert (row["outcome"], row["started_at"]) == ("stopped", None)
    wait(client, a["id"])


def test_reports_and_dry_runs_leave_no_rows(client):
    report = finished(client, client.post("/api/runs", json={"command": "ui-fixture",
                                                             "argv": []}, headers=VIEWER))
    dry = reviewed(client)
    run_ids = {r["run_id"] for r in ledger(client)["rows"]}
    assert report["id"] not in run_ids and dry["id"] not in run_ids


def test_the_approver_is_recorded(client):
    dry = reviewed(client, {"purge": True})  # irreversible: needs an Approval
    client.post(f"/api/runs/{dry['id']}/approval", headers=OPERATOR)
    client.post(f"/api/runs/{dry['id']}/approval/approve", json={"comment": "ok"},
                headers=OTHER_APPROVER)
    run = wait(client, client.post(f"/api/runs/{dry['id']}/apply", headers=OPERATOR).json()["id"])
    assert row_for(client, run["id"])["approved_by"] == "cat@dallas.gov"


def test_ledger_rows_hold_nothing_sensitive(client, settings):
    upload = [("file.input", ("services.csv", b"service_id\nPSECRETSVC\n", "text/csv"))]
    body = {"command": "ui-writer", "values": {"note": "do not keep me"}}
    dry = finished(client, client.post("/api/runs", data={"run": json.dumps(body)},
                                       files=upload, headers=OPERATOR))
    client.post(f"/api/runs/{dry['id']}/review", headers=OPERATOR)
    run = wait(client, client.post(f"/api/runs/{dry['id']}/apply", headers=OPERATOR).json()["id"])
    text = json.dumps(row_for(client, run["id"]))
    for sensitive in (SECRET, "do not keep me", "PSECRETSVC", "services.csv", "APPLIED",
                      "--note", "new-service"):
        assert sensitive not in text, sensitive


# ---------- the 90-day purge ----------

def test_the_purge_removes_runs_older_than_90_days_but_never_the_ledger(client, settings,
                                                                         clock):
    run = applied(client)
    dry_id = run["applied_from"]
    clock.advance(days=89)
    assert client.app.state.purge_now() == 0
    assert client.get(f"/api/runs/{run['id']}", headers=VIEWER).status_code == 200
    clock.advance(days=1, seconds=1)
    assert client.app.state.purge_now() >= 3  # the dry run, its fresh dry run and the apply
    for run_id in (run["id"], dry_id, run["fresh_dry_run_id"]):
        assert client.get(f"/api/runs/{run_id}", headers=VIEWER).status_code == 404
        assert not (settings.run_root / run_id).exists()  # its stored files are gone too
    assert row_for(client, run["id"])["outcome"] == "succeeded"  # the Ledger survives


def test_the_purge_leaves_younger_and_unfinished_runs(client, settings, clock):
    old = finished(client, client.post("/api/runs", json={"command": "ui-fixture",
                                                          "argv": []}, headers=VIEWER))
    clock.advance(days=91)
    running = client.post("/api/runs", json={"command": "ui-fixture", "argv": ["--sleep", "5"]},
                          headers=VIEWER).json()
    client.app.state.purge_now()
    assert client.get(f"/api/runs/{old['id']}", headers=VIEWER).status_code == 404
    assert client.get(f"/api/runs/{running['id']}", headers=VIEWER).status_code == 200


def test_the_service_purges_when_it_starts(settings, catalog, clock, monkeypatch):
    from conftest import HERE, HeaderIdentity
    from fastapi.testclient import TestClient

    from pagerduty_ops_ui.app import create_app

    monkeypatch.syspath_prepend(str(HERE))

    def service():
        return TestClient(create_app(settings, identity=HeaderIdentity(), catalog=catalog,
                                     clock=clock), headers={"X-PD-Ops-Request": "1"})

    with service() as client:
        old = finished(client, client.post("/api/runs", json={"command": "ui-fixture",
                                                              "argv": []}, headers=VIEWER))
    clock.advance(days=91)
    with service() as client:
        assert client.get(f"/api/runs/{old['id']}", headers=VIEWER).status_code == 404


# ---------- the switch, while Records Management decides ----------

def test_with_the_ledger_switched_off_nothing_is_kept(settings, catalog, clock, monkeypatch):
    from conftest import HERE, HeaderIdentity
    from fastapi.testclient import TestClient

    from pagerduty_ops_ui.app import create_app

    monkeypatch.syspath_prepend(str(HERE))
    settings.ledger_enabled = False
    app = create_app(settings, identity=HeaderIdentity(), catalog=catalog, clock=clock)
    with TestClient(app, headers={"X-PD-Ops-Request": "1"}) as client:
        run = applied(client)
        page = ledger(client)
        assert (page["enabled"], page["rows"]) == (False, [])
        clock.advance(days=91)
        client.app.state.purge_now()
        assert client.get(f"/api/runs/{run['id']}", headers=VIEWER).status_code == 404


# ---------- viewing and searching ----------

@pytest.mark.parametrize("headers", [VIEWER, OPERATOR, APPROVER])
def test_every_role_can_see_the_ledger(client, headers):
    run = applied(client)
    assert [r["run_id"] for r in ledger(client, headers=headers)["rows"]] == [run["id"]]


def test_the_ledger_can_be_searched(client, settings):
    ok = applied(client)
    settings.extra_env["UI_FIXTURE_APPLY_EXIT"] = "1"
    bad = applied(client)
    assert [r["run_id"] for r in ledger(client)["rows"]] == [bad["id"], ok["id"]]  # newest first
    assert [r["run_id"] for r in ledger(client, outcome="failed")["rows"]] == [bad["id"]]
    assert ledger(client, command="ui-writer", requester="ANN")["total"] == 2
    assert ledger(client, requester="nobody")["total"] == 0
    page = ledger(client, per_page=1, page=2)
    assert ([r["run_id"] for r in page["rows"]], page["total"]) == ([ok["id"]], 2)
    assert client.get("/api/ledger", params={"outcome": "exploded"},
                      headers=VIEWER).status_code == 422
