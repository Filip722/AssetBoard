from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from assetboard import cli
from assetboard.branding import ENV_PREFIX
from assetboard.cli import app as cli_app


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


def test_serve_reports_unwritable_data_directory(
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
