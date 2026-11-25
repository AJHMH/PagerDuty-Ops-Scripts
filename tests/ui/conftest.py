"""Seam B fixtures: the UI service's HTTP API in-process (TestClient), a test identity
provider, SQLite through the production database layer, and real subprocess Runs."""

import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    # Real engine parsers and risk declarations with harmless mains (#77).
    for name, module in (("ui-reorg", "reorg"), ("ui-business-services", "business_services"),
                         ("ui-scim", "scim_user_sync"), ("ui-event-rules", "event_rules_migrate"),
                         ("ui-sn-sync", "sn_sync"), ("ui-templates", "templates")):
        entries[name] = CommandEntry(name, f"fixture_real_{module}", "main", "build_parser")
    return entries


# The Run store's tests run on SQLite by default. CI also runs them on SQL Server (ADR 0007,
# #70) by setting this to a SQLAlchemy URL for an empty database the tests may own.
TEST_DATABASE_URL = os.environ.get("PD_UI_TEST_DATABASE_URL", "").strip() or None
# CI sets this to the database it means to test ("mssql"), so the job fails rather than
# quietly passing on SQLite if the URL ever goes missing.
EXPECTED_BACKEND = os.environ.get("PD_UI_TEST_DATABASE_BACKEND", "").strip() or None


def pytest_sessionstart(session):
    if EXPECTED_BACKEND is None:
        return
    from sqlalchemy.engine import make_url

    actual = make_url(TEST_DATABASE_URL).get_backend_name() if TEST_DATABASE_URL else "sqlite"
    if actual != EXPECTED_BACKEND:
        pytest.exit(f"PD_UI_TEST_DATABASE_BACKEND is {EXPECTED_BACKEND!r}, but the tests would "
                    f"run on {actual!r}: is PD_UI_TEST_DATABASE_URL set?", returncode=1)


class SharedDatabase:
    """One database the tests own (SQL Server in CI): migrated once, rows cleared per test."""

    def __init__(self, url):
        self.url = url

    def migrate(self):
        from pagerduty_ops_ui.migrate import upgrade

        upgrade(self.url)

    def fresh(self, tmp_path):
        from sqlalchemy import create_engine

        from pagerduty_ops_ui.store import Base

        engine = create_engine(self.url)
        with engine.begin() as db:  # every table, children first, so none is missed
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(table.delete())
        engine.dispose()
        return self.url

    @contextmanager
    def empty(self, tmp_path):
        from pagerduty_ops_ui.migrate import downgrade, upgrade

        downgrade(self.url, "base")
        try:
            yield self.url
        finally:
            upgrade(self.url)


class SqliteFiles:
    """A SQLite file per test, copied from one migrated template."""

    def __init__(self, directory):
        self.template = directory / "template.db"

    def migrate(self):
        from pagerduty_ops_ui.migrate import upgrade

        upgrade(f"sqlite:///{self.template}")

    def fresh(self, tmp_path):
        path = tmp_path / "runs.db"
        shutil.copyfile(self.template, path)
        return f"sqlite:///{path}"

    @contextmanager
    def empty(self, tmp_path):
        yield f"sqlite:///{tmp_path / 'empty.db'}"


@pytest.fixture(scope="session")
def test_database(tmp_path_factory):
    database = (SharedDatabase(TEST_DATABASE_URL) if TEST_DATABASE_URL
                else SqliteFiles(tmp_path_factory.mktemp("schema")))
    database.migrate()
    return database


@pytest.fixture
def database_url(tmp_path, test_database):
    """A migrated, empty Run store for one test."""
    return test_database.fresh(tmp_path)


@pytest.fixture
def empty_database(tmp_path, test_database):
    """A database with no schema at all, as before pd-ui-migrate first runs."""
    with test_database.empty(tmp_path) as url:
        yield url


@pytest.fixture
def settings(tmp_path, database_url):
    return Settings(
        database_url=database_url,
        run_root=tmp_path / "runs",
        secret_env={"PD_API_TOKEN": SECRET},
        extra_env={"PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])},
        session_secret="test-session-secret",
        # Most tests exercise every feature; test_phase.py covers the read-only default.
        max_phase=3,
    )


@pytest.fixture
def sn_instances(settings, monkeypatch):
    """ServiceNow production and rehearsal instances, as a server would configure them: in
    the service's environment (assess() judges a Run in-process) and passed to its Runs."""
    instances = {"SN_PROD_INSTANCES": "dallastx", "SN_REHEARSAL_INSTANCES": "dallastx-test"}
    for name, value in instances.items():
        monkeypatch.setenv(name, value)
    settings.extra_env.update(instances)
    return instances


class FakeClock:
    """The service's clock in tests: real time, plus however far a test moves it on."""

    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, **kwargs) -> None:
        self.offset += timedelta(**kwargs)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def client(settings, catalog, clock, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))  # so assess() can import the fixture module too
    app = create_app(settings, identity=HeaderIdentity(), catalog=catalog, clock=clock)
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
