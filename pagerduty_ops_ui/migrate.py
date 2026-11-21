"""`pd-ui-migrate`: bring the Run store's schema up to date (ADR 0007).

Run by whoever deploys (or a DBA), with an account that may change the schema. The service
itself only checks the schema at startup and refuses to start when it is out of date, so its
own account needs no rights beyond reading and writing the tables.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine

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


def check_schema(engine: Engine) -> None:
    """Refuse to run on a schema other than the one this version of the service expects."""
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaOutOfDate(
            f"The Run store's schema is at {current or 'nothing (an empty database)'}, but "
            f"this version of the service needs {head}. Run pd-ui-migrate first.")


def main(argv: list[str] | None = None) -> int:
    url = os.environ.get("PD_UI_DATABASE_URL", "").strip()
    if not url:
        print("PD_UI_DATABASE_URL is not set.", file=sys.stderr)
        return 2
    engine = create_engine(url)
    before = current_revision(engine)
    upgrade(url)
    after = current_revision(engine)
    print(f"Schema at {after} (was {before or 'empty'}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
