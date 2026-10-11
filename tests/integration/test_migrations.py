from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import (
    alembic_config,
    check_database_migrations,
    create_revision,
    database_is_at_head,
    upgrade_database,
)


def test_alembic_upgrade_and_check_on_empty_sqlite_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url=database_url)

    upgrade_database(settings)
    check_database_migrations(settings)

    engine = create_database_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            current_heads = MigrationContext.configure(connection).get_current_heads()
            setup_rows = connection.exec_driver_sql("SELECT id FROM setup_state").scalars().all()
    finally:
        engine.dispose()

    assert tables == {
        "alembic_version",
        "audit_events",
        "auth_throttles",
        "memberships",
        "setup_state",
        "user_sessions",
        "users",
        "workspaces",
    }
    assert setup_rows == [1]
    assert current_heads == tuple(ScriptDirectory.from_config(alembic_config(settings)).get_heads())
    config = alembic_config(settings)
    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).is_dir()
    assert config.get_main_option("sqlalchemy.url") is None


def test_create_revision_migrates_a_temporary_database_first(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_upgrade(scratch_settings: Settings) -> None:
        assert scratch_settings.database_url is not None
        calls.append(("upgrade", scratch_settings.database_url))

    def fake_revision(config, *, message: str, autogenerate: bool) -> None:
        scratch_settings = config.attributes["settings"]
        assert scratch_settings.database_url is not None
        assert autogenerate
        calls.append(("revision", scratch_settings.database_url))

    monkeypatch.setattr("assetboard.db.migrate.upgrade_database", fake_upgrade)
    monkeypatch.setattr(command, "revision", fake_revision)
    original_database_url = settings.database_url

    create_revision(settings, "scratch revision")

    assert [call[0] for call in calls] == ["upgrade", "revision"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != original_database_url
    assert not Path(calls[0][1].removeprefix("sqlite:///")).parent.exists()


def test_identity_migration_is_reversible_and_head_detection_is_read_only(tmp_path: Path) -> None:
    database_path = tmp_path / "reversible.db"
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{database_path}"
    )
    assert not database_is_at_head(settings)
    assert not database_path.exists()

    upgrade_database(settings)
    assert database_is_at_head(settings)
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        unique = inspect(engine).get_unique_constraints("users")
        assert {"name": "uq_users_email_normalized", "column_names": ["email"]} in [
            {"name": item["name"], "column_names": item["column_names"]} for item in unique
        ]

        command.downgrade(alembic_config(settings), "0001_f03")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        assert not database_is_at_head(settings)

        upgrade_database(settings)
        assert database_is_at_head(settings)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM setup_state").scalar_one() == 1
    finally:
        engine.dispose()
