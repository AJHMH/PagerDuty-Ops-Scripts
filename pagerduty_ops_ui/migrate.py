"""`pd-ui-migrate`: bring the Run store's schema up to date (ADR 0007).

Run by whoever deploys (or a DBA), with an account that may change the schema. The service
itself only checks the schema at startup and refuses to start when it is out of date, so its
own account needs no rights beyond reading and writing the tables.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import ArgumentError, DBAPIError

MIGRATIONS = Path(__file__).with_name("migrations")


class SchemaOutOfDate(RuntimeError):
    pass


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def head_revision() -> str:
    return ScriptDirectory(str(MIGRATIONS)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def describe(revision: str | None) -> str:
    """How a schema revision reads in messages: "at 0001", or "empty"."""
    return f"at {revision}" if revision else "empty"


def check_schema(engine: Engine) -> None:
    """Refuse to run on a schema other than the one this version of the service expects."""
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaOutOfDate(
            f"The Run store's schema is {describe(current)}, but this version of the "
            f"service needs {head}. Run pd-ui-migrate first.")


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="pd-ui-migrate",
        description="Bring the UI service's database schema (the Run store) up to date. "
                    "Connects to PD_UI_DATABASE_URL, which must be set, and applies any "
                    "migrations it hasn't had. Safe to run again.",
        epilog="Exit codes: 0 up to date, 1 a migration failed, 2 configuration or "
               "connection error, 3 login refused.")


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)  # --help and unknown options stop here, untouched
    url = os.environ.get("PD_UI_DATABASE_URL", "").strip()
    if not url:
        print("PD_UI_DATABASE_URL is not set.", file=sys.stderr)
        return 2
    try:
        engine = create_engine(url)
        before = current_revision(engine)
    except ArgumentError as e:
        print(f"PD_UI_DATABASE_URL isn't a database URL: {e}", file=sys.stderr)
        return 2
    except DBAPIError as e:
        message = str(e.orig)
        refused = "28000" in message or "Login failed" in message
        print(f"{'Login refused' if refused else 'Could not connect'}: {message}",
              file=sys.stderr)
        return 3 if refused else 2
    try:
        upgrade(url)
        after = current_revision(engine)
    except Exception as e:  # noqa: BLE001 - report it plainly; the transaction rolled back
        print(f"Migration failed (schema still {describe(before)}): {e}", file=sys.stderr)
        return 1
    print(f"Schema at {after} (was {describe(before)}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
