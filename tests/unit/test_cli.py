from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

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


def test_migrate_reports_not_implemented_until_f03() -> None:
    result = CliRunner().invoke(cli_app, ["migrate"])

    assert result.exit_code == 1
    assert "not implemented until F0.3" in result.output
