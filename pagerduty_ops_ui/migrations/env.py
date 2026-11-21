"""Alembic environment: migrate the database `pagerduty_ops_ui.migrate` hands over."""

from alembic import context
from sqlalchemy import create_engine

from pagerduty_ops_ui.store import Base

config = context.config


def run_migrations_online() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # SQLite can't ALTER much
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) migrations aren't supported; run pd-ui-migrate.")
run_migrations_online()
