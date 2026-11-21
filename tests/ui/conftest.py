"""Seam B fixtures: the UI service's HTTP API in-process (TestClient), a test identity
provider, SQLite through the production database layer, and real subprocess Runs."""

import json
import os
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pagerduty_ops_ui.app import create_app  # noqa: E402
from pagerduty_ops_ui.catalog import CommandEntry, engine_catalog  # noqa: E402
from pagerduty_ops_ui.identity import Admin  # noqa: E402
from pagerduty_ops_ui.settings import Settings  # noqa: E402

HERE = Path(__file__).parent
SECRET = "pd-secret-token-value-123"


class HeaderIdentity:
    """Test identity provider: the signed-in admin comes from an X-Test-Admin header."""

    def install(self, app):
        pass

    def current(self, request):
        raw = request.headers.get("X-Test-Admin")
        return Admin(**json.loads(raw)) if raw else None


def admin_header(*groups, email="ann@dallas.gov", name="Ann"):
    return {"X-Test-Admin": json.dumps(
        {"subject": email, "email": email, "name": name, "groups": list(groups)})}


VIEWER = admin_header("PD-Ops-Viewer")
OPERATOR = admin_header("PD-Ops-Operator", "Everyone")
APPROVER = admin_header("PD-Ops-Approver")
NO_ROLE = admin_header("Everyone")


@pytest.fixture
def catalog():
    entries = engine_catalog()
    entries["ui-fixture"] = CommandEntry("ui-fixture", "fixture_command", "main",
                                         "build_parser")
    entries["ui-subcommands"] = CommandEntry("ui-subcommands", "fixture_subcommands", "main",
                                             "build_parser")
    entries["ui-writer"] = CommandEntry("ui-writer", "fixture_writer", "main", "build_parser")
    entries["ui-differ"] = CommandEntry("ui-differ", "fixture_differ", "main", "build_parser")
    return entries


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'runs.db'}",
        run_root=tmp_path / "runs",
        secret_env={"PD_API_TOKEN": SECRET},
        extra_env={"PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])},
        session_secret="test-session-secret",
    )


@pytest.fixture
def client(settings, catalog, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))  # so assess() can import the fixture module too
    app = create_app(settings, identity=HeaderIdentity(), catalog=catalog)
    # The front end sends this on every request; see test_csrf in test_apply.py.
    with TestClient(app, headers={"X-PD-Ops-Request": "1"}) as c:
        yield c


def wait(client, run_id, headers=None, timeout=30.0):
    """Runs execute in the background (#74): poll the Run until it has finished."""
    deadline = time.monotonic() + timeout
    while True:
        run = client.get(f"/api/runs/{run_id}", headers=headers or APPROVER).json()
        if run["status"] == "finished":
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"Run {run_id} still {run['status']} after {timeout}s")
        time.sleep(0.05)


def finished(client, response, headers=None):
    """The Run a successful POST started, once it has finished in the background."""
    assert response.status_code == 201, response.text
    return wait(client, response.json()["id"], headers)


def wait_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)
