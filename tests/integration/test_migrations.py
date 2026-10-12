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
        "external_identities",
        "invitations",
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


def test_0003_round_trip(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f12.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0003_f12")
    assert revision is not None and revision.down_revision == "0002_f11"
    # Reachable from every head, so later migrations keep it in the chain.
    for head in script.get_heads():
        assert "0003_f12" in {rev.revision for rev in script.iterate_revisions(head, "base")}
    engine = create_database_engine(settings.database_url or "")
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("user_sessions")}
        assert "current_workspace_id" in columns
        command.downgrade(alembic_config(settings), "0002_f11")
        columns = {column["name"] for column in inspect(engine).get_columns("user_sessions")}
        assert "current_workspace_id" not in columns
        assert "ix_user_sessions_user_expires" in {
            index["name"] for index in inspect(engine).get_indexes("user_sessions")
        }
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_session_workspace_is_nulled_on_workspace_delete(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'fk.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    workspace, user = "a" * 32, "b" * 32
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES ('{workspace}', 'W', 'EUR', 'en', 'UTC', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                f"is_instance_admin, created_at, updated_at) VALUES ('{user}', 'u@example.com', "
                "'U', 'en', 'system', 1, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                f"expires_at, current_workspace_id) VALUES ('{'c' * 64}', '{user}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-02-01 00:00:00', "
                f"'{workspace}')"
            )
            connection.exec_driver_sql(f"DELETE FROM workspaces WHERE id = '{workspace}'")
            selected = connection.exec_driver_sql(
                "SELECT current_workspace_id FROM user_sessions"
            ).scalar_one()
        assert selected is None
    finally:
        engine.dispose()


def test_0004_round_trip(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f12b.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0004_f12")
    assert revision is not None and revision.down_revision == "0003_f12"
    for head in script.get_heads():
        assert "0004_f12" in {rev.revision for rev in script.iterate_revisions(head, "base")}
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert "invitations" in inspector.get_table_names()
        unique = {item["name"] for item in inspector.get_unique_constraints("invitations")}
        assert "uq_invitations_token_hash" in unique
        command.downgrade(alembic_config(settings), "0003_f12")
        assert "invitations" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0005_round_trip_keeps_sessions_as_password_logins(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f13.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0005_f13")
    assert revision is not None and revision.down_revision == "0004_f12"
    engine = create_database_engine(settings.database_url or "")
    try:
        unique = {
            item["name"] for item in inspect(engine).get_unique_constraints("external_identities")
        }
        assert "uq_external_identities_provider_subject" in unique
        command.downgrade(alembic_config(settings), "0004_f12")
        inspector = inspect(engine)
        assert "external_identities" not in inspector.get_table_names()
        # OIDC-only users need no schema change: the column was nullable from the start.
        password = {c["name"]: c for c in inspector.get_columns("users")}["password_hash"]
        assert password["nullable"] is True
        with engine.begin() as connection:
            user_id = "00000000000000000000000000000001"
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                "is_instance_admin, created_at, updated_at) VALUES (?, 'a@example.com', 'A', "
                "'en', 'system', 1, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (user_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                "expires_at) VALUES (?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
                "'2026-02-01 00:00:00')",
                ("a" * 64, user_id),
            )
        upgrade_database(settings)
        with engine.connect() as connection:
            methods = connection.exec_driver_sql("SELECT auth_method FROM user_sessions")
            assert methods.scalars().all() == ["password"]
        check_database_migrations(settings)
    finally:
        engine.dispose()
