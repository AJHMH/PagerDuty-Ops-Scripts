"""PD_UI_MAX_PHASE (ADR 0016): how far the service has rolled out. 1 refuses every apply, 2
refuses Irreversible operations and Approvals, 3 allows everything. Whether an apply is
irreversible is the engine's verdict. Seam B, with real subprocess Runs of fixture commands."""

import pytest
from conftest import APPROVER, OPERATOR, admin_header, wait, wait_until
from fastapi.testclient import TestClient

from pagerduty_ops_ui import phase
from pagerduty_ops_ui.app import create_app
from pagerduty_ops_ui.settings import Settings

OTHER_APPROVER = admin_header("PD-Ops-Approver", email="cat@dallas.gov", name="Cat")


@pytest.fixture
def at_phase(settings, catalog, clock, monkeypatch):
    """A client for the service set to the given phase."""
    from conftest import HERE, HeaderIdentity

    monkeypatch.syspath_prepend(str(HERE))
    clients = []

    def make(max_phase):
        settings.max_phase = max_phase
        app = create_app(settings, identity=HeaderIdentity(), catalog=catalog, clock=clock)
        client = TestClient(app, headers={"X-PD-Ops-Request": "1"})
        clients.append(client.__enter__())
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def reviewed(client, values=None, headers=OPERATOR):
    response = client.post("/api/runs", json={"command": "ui-writer", "values": values or {}},
                           headers=headers)
    assert response.status_code == 201, response.text
    dry = wait(client, response.json()["id"])
    assert dry["outcome"] == "succeeded", dry["stderr"]
    assert client.post(f"/api/runs/{dry['id']}/review", headers=headers).status_code == 200
    return dry


def apply(client, run_id):
    return client.post(f"/api/runs/{run_id}/apply", headers=OPERATOR)


def ask(client, run_id):
    return client.post(f"/api/runs/{run_id}/approval", headers=OPERATOR)


# ---------- the setting ----------

def test_the_default_is_read_only():
    assert phase.DEFAULT == phase.READ_ONLY == 1
    assert Settings(database_url="sqlite://", run_root=".", session_secret="s").max_phase == 1


@pytest.mark.parametrize("raw, expected", [(None, 1), ("", 1), ("  ", 1), ("1", 1),
                                           ("2", 2), (" 3 ", 3)])
def test_the_phase_is_read_from_the_environment(raw, expected):
    assert phase.parse(raw) == expected


@pytest.mark.parametrize("raw", ["0", "4", "two", "2.0", "-1", "phase 2"])
def test_a_bad_value_stops_the_service_at_startup(raw):
    with pytest.raises(SystemExit, match="PD_UI_MAX_PHASE must be 1, 2 or 3"):
        phase.parse(raw)


def test_from_env_reads_the_phase(monkeypatch):
    for name, value in {"PD_UI_DATABASE_URL": "sqlite://", "PD_UI_RUN_ROOT": "runs",
                        "PD_UI_SESSION_SECRET": "s", "PD_UI_OKTA_ISSUER": "https://okta",
                        "PD_UI_OKTA_CLIENT_ID": "c", "PD_UI_OKTA_CLIENT_SECRET": "x",
                        "PD_UI_OKTA_REDIRECT_URI": "https://ui/cb"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PD_UI_MAX_PHASE", raising=False)
    assert Settings.from_env().max_phase == 1
    monkeypatch.setenv("PD_UI_MAX_PHASE", "3")
    assert Settings.from_env().max_phase == 3
    monkeypatch.setenv("PD_UI_MAX_PHASE", "9")
    with pytest.raises(SystemExit):
        Settings.from_env()


@pytest.mark.parametrize("max_phase", [1, 2, 3])
def test_the_front_end_is_told_the_phase(at_phase, max_phase):
    assert at_phase(max_phase).get("/api/me", headers=OPERATOR).json()["max_phase"] == max_phase


# ---------- Phase 1: read only ----------

def test_phase_1_refuses_every_apply(at_phase):
    client = at_phase(1)
    dry = reviewed(client)  # dry runs and reviews still work
    response = apply(client, dry["id"])
    assert response.status_code == 403
    assert "Apply is turned off" in response.json()["detail"]
    assert "Phase 1" in response.json()["detail"]
    assert client.get(f"/api/runs/{dry['id']}", headers=OPERATOR).json()["applied_run_id"] is None


def test_phase_1_refuses_an_irreversible_apply_too(at_phase):
    client = at_phase(1)
    dry = reviewed(client, values={"purge": True})
    assert apply(client, dry["id"]).status_code == 403


def test_phase_1_still_runs_reports(at_phase):
    client = at_phase(1)
    response = client.post("/api/runs", json={"command": "ui-fixture", "argv": []},
                           headers=OPERATOR)
    assert response.status_code == 201, response.text
    assert wait(client, response.json()["id"])["outcome"] == "succeeded"


# ---------- Phase 2: applies that can be undone ----------

def test_phase_2_applies_what_isnt_irreversible(at_phase):
    client = at_phase(2)
    dry = reviewed(client)
    response = apply(client, dry["id"])
    assert response.status_code == 201, response.text
    assert wait(client, response.json()["id"])["outcome"] == "succeeded"


def test_phase_2_refuses_an_irreversible_apply(at_phase):
    client = at_phase(2)
    dry = reviewed(client, values={"purge": True})
    response = apply(client, dry["id"])
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "Irreversible operation (--purge given)" in detail and "Phase 3" in detail
    # Refused before anything was claimed: the dry run can still be applied at Phase 3.
    assert client.get(f"/api/runs/{dry['id']}", headers=OPERATOR).json()["applied_run_id"] is None


def test_phase_2_refuses_approvals(at_phase):
    client = at_phase(2)
    dry = reviewed(client, values={"purge": True})
    response = ask(client, dry["id"])
    assert response.status_code == 403
    assert "Phase 3" in response.json()["detail"]
    decided = client.post(f"/api/runs/{dry['id']}/approval/approve",
                          json={"comment": "ok"}, headers=OTHER_APPROVER)
    assert decided.status_code == 403
    assert "Phase 3" in decided.json()["detail"]


# ---------- Phase 3: everything ----------

def test_phase_3_applies_an_approved_irreversible_operation(at_phase):
    client = at_phase(3)
    dry = reviewed(client, values={"purge": True})
    assert ask(client, dry["id"]).status_code == 200
    decided = client.post(f"/api/runs/{dry['id']}/approval/approve",
                          json={"comment": "Checked the diff."}, headers=OTHER_APPROVER)
    assert decided.status_code == 200, decided.text
    response = apply(client, dry["id"])
    assert response.status_code == 201, response.text
    assert wait(client, response.json()["id"])["outcome"] == "succeeded"


# ---------- the writer checks again ----------

def test_an_apply_queued_before_a_lower_phase_is_not_applied(at_phase, settings):
    """Applies wait in the queue across a restart; one queued at Phase 3 mustn't run after the
    service is set back to Phase 1."""
    settings.extra_env["UI_FIXTURE_APPLY_SLEEP"] = "2"  # the first apply holds the slot
    client = at_phase(3)
    first, second = reviewed(client), reviewed(client, values={"note": "second"})
    holding = apply(client, first["id"]).json()
    waiting = apply(client, second["id"]).json()
    wait_until(lambda: client.get(f"/api/runs/{holding['id']}",
                                  headers=APPROVER).json()["status"] == "running")
    assert client.get(f"/api/runs/{waiting['id']}", headers=APPROVER).json()["status"] == "queued"
    settings.max_phase = 1  # as after a restart with PD_UI_MAX_PHASE=1
    done = wait(client, waiting["id"])
    assert done["outcome"] == "failed"
    assert done["fresh_dry_run_id"] is None and "APPLIED" not in (done["stdout"] or "")
    assert "Apply is turned off" in done["message"] and "Nothing was applied" in done["message"]
