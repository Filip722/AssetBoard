from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

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
