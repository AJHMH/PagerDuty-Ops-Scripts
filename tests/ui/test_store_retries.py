"""A read the database picks as a deadlock victim is tried again (SQL Server error 1205).

On SQL Server, a read of the Runs table can deadlock with the purge's locked deletes (#76):
`GET /api/queue` lost one in CI while the start-up purge ran. The victim's transaction is
rolled back, so a read-only store method can simply run again. SQLite never deadlocks this
way, so these tests make a statement fail the way pyodbc reports it."""

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from pagerduty_ops_ui import store as store_module
from pagerduty_ops_ui.store import RunStore, is_deadlock

READS = ("get", "record", "files", "next_queued", "queue", "search", "awaiting_approval",
         "run_ids", "ledger", "get_file")


class OdbcError(Exception):
    """Stands in for pyodbc.Error: args are (SQLSTATE, message)."""


def deadlock():
    return DBAPIError("SELECT ...", {}, OdbcError(
        "40001", "[40001] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Transaction "
        "(Process ID 350) was deadlocked on lock resources with another process and has been "
        "chosen as the deadlock victim. Rerun the transaction. (1205) (SQLExecDirectW)"))


def lost_connection():
    return DBAPIError("SELECT ...", {}, OdbcError(
        "08S01", "[08S01] [Microsoft][ODBC Driver 18 for SQL Server]Communication link failure"))


@pytest.fixture
def store(database_url, monkeypatch):
    monkeypatch.setattr(store_module, "READ_RETRY_SECONDS", 0)
    return RunStore(database_url)


@pytest.fixture
def failing_scalars(monkeypatch):
    """Make the next `failures` Session.scalars calls raise `error`, then behave normally."""
    real = Session.scalars
    calls = {"n": 0}

    def arm(error, failures):
        def scalars(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise error()
            return real(self, *args, **kwargs)
        monkeypatch.setattr(Session, "scalars", scalars)
        return calls
    return arm


def test_a_deadlock_is_recognised_and_other_database_errors_are_not():
    assert is_deadlock(deadlock())
    assert not is_deadlock(lost_connection())


def test_a_read_that_loses_a_deadlock_is_tried_again(store, failing_scalars):
    calls = failing_scalars(deadlock, failures=store_module.READ_ATTEMPTS - 1)
    assert store.queue() == {"executing": None, "pending": []}
    assert calls["n"] == store_module.READ_ATTEMPTS


def test_a_read_gives_up_after_its_last_attempt(store, failing_scalars):
    calls = failing_scalars(deadlock, failures=store_module.READ_ATTEMPTS)
    with pytest.raises(DBAPIError):
        store.queue()
    assert calls["n"] == store_module.READ_ATTEMPTS


def test_any_other_database_error_is_not_retried(store, failing_scalars):
    calls = failing_scalars(lost_connection, failures=1)
    with pytest.raises(DBAPIError):
        store.queue()
    assert calls["n"] == 1


def test_every_read_only_store_method_retries_deadlocks():
    # Only reads: a write's effects may not be safe to repeat, so writes aren't wrapped.
    for name in READS:
        assert getattr(RunStore, name).__wrapped__, name
