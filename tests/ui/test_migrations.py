"""#70 the Run store's schema comes from migrations (ADR 0007), applied by `pd-ui-migrate`,
not by the service. Runs on SQLite, and on SQL Server in CI (PD_UI_TEST_DATABASE_URL)."""

import pytest
from conftest import HeaderIdentity
from sqlalchemy import create_engine, inspect


def test_migrations_build_an_empty_database_that_matches_the_models(empty_database):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from pagerduty_ops_ui.migrate import downgrade, upgrade
    from pagerduty_ops_ui.store import Base

    upgrade(empty_database)
    engine = create_engine(empty_database)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    assert {"runs", "run_files"} <= set(inspect(engine).get_table_names())

    downgrade(empty_database, "base")  # and the migrations undo cleanly
    assert not {"runs", "run_files"} & set(inspect(engine).get_table_names())
    engine.dispose()


def test_the_service_refuses_to_start_on_a_schema_that_is_out_of_date(empty_database,
                                                                       catalog, tmp_path):
    from pagerduty_ops_ui.app import create_app
    from pagerduty_ops_ui.migrate import SchemaOutOfDate
    from pagerduty_ops_ui.settings import Settings

    settings = Settings(database_url=empty_database, run_root=tmp_path / "runs",
                        session_secret="test")
    with pytest.raises(SchemaOutOfDate, match="pd-ui-migrate"):
        create_app(settings, identity=HeaderIdentity(), catalog=catalog)


def test_pd_ui_migrate_brings_the_schema_up_to_date_and_can_run_again(empty_database,
                                                                      monkeypatch, capsys):
    from pagerduty_ops_ui.migrate import current_revision, head_revision, main

    monkeypatch.setenv("PD_UI_DATABASE_URL", empty_database)
    assert main([]) == 0
    assert main([]) == 0  # nothing to do the second time
    engine = create_engine(empty_database)
    assert current_revision(engine) == head_revision()
    engine.dispose()
    assert f"Schema at {head_revision()}" in capsys.readouterr().err


def test_pd_ui_migrate_needs_the_database_url(monkeypatch):
    from pagerduty_ops_ui.migrate import main

    monkeypatch.delenv("PD_UI_DATABASE_URL", raising=False)
    assert main([]) == 2


def test_pd_ui_migrate_help_changes_nothing(tmp_path, monkeypatch, capsys):
    from pagerduty_ops_ui.migrate import main

    database = tmp_path / "untouched.db"
    monkeypatch.setenv("PD_UI_DATABASE_URL", f"sqlite:///{database}")
    with pytest.raises(SystemExit) as done:
        main(["--help"])
    assert done.value.code == 0
    assert "PD_UI_DATABASE_URL" in capsys.readouterr().out
    assert not database.exists()  # never connected, let alone migrated
    with pytest.raises(SystemExit) as done:
        main(["--no-such-option"])
    assert done.value.code == 2
    assert not database.exists()


def test_pd_ui_migrate_reports_a_bad_url_as_a_configuration_error(monkeypatch, capsys):
    from pagerduty_ops_ui.migrate import main

    monkeypatch.setenv("PD_UI_DATABASE_URL", "not a url")
    assert main([]) == 2
    assert "PD_UI_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize("message, code", [
    ("[28000] Login failed for user 'deploy'. (18456)", 3),
    ("[08001] TCP Provider: Error code 0x2749 (10057)", 2),
])
def test_pd_ui_migrate_tells_a_refused_login_from_an_unreachable_server(
        monkeypatch, capsys, message, code):
    from sqlalchemy.exc import DBAPIError

    import pagerduty_ops_ui.migrate as migrate

    def refuse(engine):
        raise DBAPIError("connect", {}, Exception(message))

    monkeypatch.setenv("PD_UI_DATABASE_URL", "sqlite://")
    monkeypatch.setattr(migrate, "current_revision", refuse)
    assert migrate.main([]) == code
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err


def test_pd_ui_migrate_reports_a_failed_migration_without_a_traceback(empty_database,
                                                                      monkeypatch, capsys):
    import pagerduty_ops_ui.migrate as migrate

    def broken(url, revision="head"):
        raise RuntimeError("duplicate column")

    monkeypatch.setenv("PD_UI_DATABASE_URL", empty_database)
    monkeypatch.setattr(migrate, "upgrade", broken)
    assert migrate.main([]) == 1
    assert "duplicate column" in capsys.readouterr().err
