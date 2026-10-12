from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from assetboard import cli
from assetboard.branding import ENV_PREFIX
from assetboard.cli import app as cli_app
from assetboard.clock import FixedClock
from assetboard.db.models.identity import Membership, User, Workspace


def test_assetboard_console_script_help_runs() -> None:
    executable = Path(sys.executable).parent / "assetboard"
    assert executable.is_file()

    result = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "migrate" in result.stdout
    assert "create-admin" in result.stdout
    assert "reset-password" in result.stdout
    assert "revision" not in result.stdout


def test_assetboard_module_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "assetboard", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "migrate" in result.stdout
    assert "create-admin" in result.stdout
    assert "reset-password" in result.stdout
    assert "revision" not in result.stdout


def test_migrate_runs_upgrade_in_configured_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "migrations"
    result = CliRunner().invoke(
        cli_app,
        ["migrate"],
        env={f"{ENV_PREFIX}_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    assert "Database migrations applied" in result.output
    assert (data_dir / "assetboard.db").exists()


def _auth_env(data_dir: Path, **extra: str) -> dict[str, str]:
    env = {
        f"{ENV_PREFIX}_DATA_DIR": str(data_dir),
    }
    env.update({f"{ENV_PREFIX}_{name}": value for name, value in extra.items()})
    return env


def _users(data_dir: Path) -> list[tuple[str, str | None]]:
    connection = sqlite3.connect(data_dir / "assetboard.db")
    try:
        return connection.execute(
            "SELECT email, password_hash FROM users ORDER BY email"
        ).fetchall()
    finally:
        connection.close()


def _create_admin(
    env: dict[str, str], email: str, *extra: str, password: str = "CLI Admin Strong Password"
):
    return CliRunner().invoke(
        cli_app,
        [
            "create-admin",
            "--email",
            email,
            "--display-name",
            "CLI Admin",
            *extra,
            "--password-stdin",
        ],
        input=f"{password}\n",
        env=env,
    )


def test_create_admin_and_reset_password_accept_stdin_without_setup_token(tmp_path: Path) -> None:
    env = _auth_env(tmp_path / "auth-data")
    created = _create_admin(env, "cli@example.com", "--workspace-name", "Home")
    assert created.exit_code == 0, created.output
    assert "Created administrator cli@example.com and workspace Home." in created.output
    assert "CLI Admin Strong Password" not in created.output

    reset = CliRunner().invoke(
        cli_app,
        ["reset-password", "CLI@example.com", "--password-stdin"],
        input="New CLI Password Strong\n",
        env=env,
    )
    assert reset.exit_code == 0, reset.output
    assert "Password reset for cli@example.com" in reset.output
    assert "New CLI Password Strong" not in reset.output


def test_create_admin_prompts_with_hidden_confirmed_password(tmp_path: Path) -> None:
    env = _auth_env(tmp_path / "auth-data")
    result = CliRunner().invoke(
        cli_app,
        ["create-admin"],
        input=(
            "prompt@example.com\nPrompt Admin\n\n"
            "Prompted Strong Password\nPrompted Strong Password\n"
        ),
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "Created administrator prompt@example.com and workspace Prompt Admin." in result.output
    assert "Repeat for confirmation" in result.output
    assert "Prompted Strong Password" not in result.output


def test_reset_password_prompts_with_hidden_password(tmp_path: Path) -> None:
    data_dir = tmp_path / "auth-data"
    env = _auth_env(data_dir)
    assert _create_admin(env, "cli@example.com").exit_code == 0
    before = _users(data_dir)
    result = CliRunner().invoke(
        cli_app,
        ["reset-password", "cli@example.com"],
        input="Interactive New Password\nInteractive New Password\n",
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "Interactive New Password" not in result.output
    assert _users(data_dir) != before


def test_create_admin_refuses_existing_email_without_modifying_user(tmp_path: Path) -> None:
    data_dir = tmp_path / "auth-data"
    env = _auth_env(data_dir)
    assert _create_admin(env, "cli@example.com").exit_code == 0
    before = _users(data_dir)

    duplicate = _create_admin(env, " CLI@Example.com ", password="Replacement Strong Password")
    assert duplicate.exit_code != 0
    assert "already exists" in duplicate.output
    assert _users(data_dir) == before


def test_create_admin_works_with_existing_users_and_can_skip_workspace(tmp_path: Path) -> None:
    data_dir = tmp_path / "auth-data"
    env = _auth_env(data_dir)
    assert _create_admin(env, "first@example.com").exit_code == 0
    second = _create_admin(env, "second@example.com", "--no-workspace")
    assert second.exit_code == 0, second.output
    assert second.output.strip().endswith("Created administrator second@example.com.")
    connection = sqlite3.connect(data_dir / "assetboard.db")
    try:
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM users WHERE is_instance_admin"
        ).fetchone() == (2,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("arguments", "input_text", "message"),
    [
        (
            ["reset-password", "nobody@example.com", "--password-stdin"],
            "Strong New Password\n",
            "No user",
        ),
        (["reset-password", "cli@example.com", "--password-stdin"], "short\n", "at least 10"),
        (["reset-password", "cli@example.com", "--password-stdin"], "\n", "must not be empty"),
    ],
)
def test_reset_password_errors_exit_non_zero(
    tmp_path: Path, arguments: list[str], input_text: str, message: str
) -> None:
    data_dir = tmp_path / "auth-data"
    env = _auth_env(data_dir)
    assert _create_admin(env, "cli@example.com").exit_code == 0
    before = _users(data_dir)
    result = CliRunner().invoke(cli_app, arguments, input=input_text, env=env)
    assert result.exit_code != 0
    assert message in result.output
    assert _users(data_dir) == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["create-admin", "--email", "cli@example.com", "--display-name", "A", "--password-stdin"],
        ["reset-password", "cli@example.com", "--password-stdin"],
    ],
)
def test_auth_commands_refuse_unmigrated_database_when_auto_migrate_is_off(
    tmp_path: Path, arguments: list[str]
) -> None:
    data_dir = tmp_path / "auth-data"
    env = _auth_env(data_dir, AUTO_MIGRATE="false")
    result = CliRunner().invoke(cli_app, arguments, input="CLI Admin Strong Password\n", env=env)
    assert result.exit_code != 0
    assert "database is not at the latest schema; run `assetboard migrate` first" in result.output
    assert not (data_dir / "assetboard.db").exists()

    data_dir.mkdir()
    sqlite3.connect(data_dir / "assetboard.db").close()
    result = CliRunner().invoke(cli_app, arguments, input="CLI Admin Strong Password\n", env=env)
    assert result.exit_code != 0
    assert "run `assetboard migrate` first" in result.output
    connection = sqlite3.connect(data_dir / "assetboard.db")
    try:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_auth_commands_work_with_auto_migrate_off_when_database_is_at_head(tmp_path: Path) -> None:
    data_dir = tmp_path / "auth-data"
    migrated = CliRunner().invoke(cli_app, ["migrate"], env=_auth_env(data_dir))
    assert migrated.exit_code == 0, migrated.output
    env = _auth_env(data_dir, AUTO_MIGRATE="false")
    created = _create_admin(env, "cli@example.com")
    assert created.exit_code == 0, created.output
    assert [email for email, _ in _users(data_dir)] == ["cli@example.com"]


def test_serve_passes_trusted_proxies_to_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "create_app", lambda settings: "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(kwargs))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli_app,
        ["serve"],
        env={
            f"{ENV_PREFIX}_DATA_DIR": str(tmp_path / "data"),
            f"{ENV_PREFIX}_TRUSTED_PROXIES": "127.0.0.1,10.20.0.0/16",
        },
    )

    assert result.exit_code == 0, result.output
    assert calls["forwarded_allow_ips"] == "127.0.0.1,10.20.0.0/16"
    assert calls["proxy_headers"] is True


def test_serve_trusts_no_proxy_when_trusted_proxies_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "create_app", lambda settings: "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(kwargs))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli_app, ["serve"], env={f"{ENV_PREFIX}_DATA_DIR": str(tmp_path / "data")}
    )

    assert result.exit_code == 0, result.output
    assert calls["proxy_headers"] is False
    assert calls["forwarded_allow_ips"] == ""
    assert calls["access_log"] is False  # the app's own request log redacts tokens (ADR-0013)


def test_serve_reports_create_app_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_create_app(_settings: object) -> str:
        raise RuntimeError("DATA_DIR is not writable; chown 10001:10001")

    monkeypatch.setattr(cli, "create_app", fail_create_app)

    result = CliRunner().invoke(
        cli_app,
        ["serve"],
        env={f"{ENV_PREFIX}_DATA_DIR": str(tmp_path / "data")},
    )

    assert result.exit_code == 1
    assert "DATA_DIR is not writable" in result.output
    assert "chown 10001:10001" in result.output


def test_serve_reports_settings_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_settings() -> object:
        raise ValueError("Invalid configuration")

    monkeypatch.setattr(cli, "Settings", fail_settings)
    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 1
    assert "Invalid configuration" in result.output
    assert "Traceback" not in result.output


def test_create_admin_with_password_stdin_requires_identity_options(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli_app,
        ["create-admin", "--email", "cli@example.com", "--password-stdin"],
        input="CLI Admin Strong Password\n",
        env=_auth_env(tmp_path / "auth-data"),
    )
    assert result.exit_code != 0
    assert "--display-name are required" in result.output


def test_only_the_users_email_unique_violation_counts_as_duplicate_email(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user = User(email="dup@example.com", display_name="Dup")
    workspace = Workspace(name="Home")
    db_session.add_all([user, workspace])
    db_session.flush()
    with pytest.raises(IntegrityError) as duplicate_email, db_session.begin_nested():
        db_session.add(User(email="dup@example.com", display_name="Again"))
    assert str(duplicate_email.value.orig) == "UNIQUE constraint failed: users.email"
    assert cli.is_email_conflict(duplicate_email.value)
    for message in (
        "UNIQUE constraint failed: users.email_backup",
        "NOT NULL constraint failed: users.email",
        "CHECK constraint failed: users.email",
    ):
        lookalike = IntegrityError("INSERT", {}, Exception(message))
        assert not cli.is_email_conflict(lookalike)

    db_session.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db_session.flush()
    with pytest.raises(IntegrityError) as duplicate_membership, db_session.begin_nested():
        db_session.add(Membership(workspace_id=workspace.id, user_id=user.id, role="viewer"))
    assert not cli.is_email_conflict(duplicate_membership.value)


def test_create_admin_does_not_mask_other_integrity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(cli, "create_admin_cli", fail)
    result = _create_admin(_auth_env(tmp_path / "auth-data"), "cli@example.com")
    assert result.exit_code != 0
    assert "already exists" not in result.output
    assert isinstance(result.exception, IntegrityError)
