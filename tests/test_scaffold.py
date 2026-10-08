import subprocess
import tempfile
from importlib import import_module
from pathlib import Path

import yaml


def test_assetboard_branding_exports_required_names() -> None:
    branding = import_module("assetboard.branding")

    assert branding.APP_NAME == "AssetBoard"
    assert branding.ENV_PREFIX.isupper()
    assert branding.CLI_NAME == "assetboard"


def test_all_assetboard_packages_import() -> None:
    packages = [
        "assetboard",
        "assetboard.api",
        "assetboard.db",
        "assetboard.domain",
        "assetboard.i18n",
        "assetboard.jobs",
        "assetboard.services",
        "assetboard.web",
    ]

    for package in packages:
        assert import_module(package)


def test_required_repository_layout_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        root / "src" / "assetboard" / "domain",
        root / "src" / "assetboard" / "services",
        root / "src" / "assetboard" / "db",
        root / "src" / "assetboard" / "web",
        root / "src" / "assetboard" / "api",
        root / "src" / "assetboard" / "jobs",
        root / "src" / "assetboard" / "i18n",
        root / "src" / "assetboard" / "branding.py",
        root / "tests" / "unit",
        root / "tests" / "integration",
        root / "docs" / "adr",
        root / "docker",
        root / "scripts",
    ]

    missing = [path for path in required if not path.exists()]
    assert not missing, f"Missing required paths: {missing}"


def test_project_tooling_configuration_files_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        root / "pyproject.toml",
        root / ".python-version",
        root / "Makefile",
        root / ".pre-commit-config.yaml",
        root / ".github" / "workflows" / "ci.yml",
        root / ".gitignore",
    ]

    missing = [path for path in required if not path.exists()]
    assert not missing, f"Missing toolchain files: {missing}"


def test_pre_commit_blocks_database_and_spreadsheet_files() -> None:
    root = Path(__file__).resolve().parents[1]
    production_config = yaml.safe_load(
        (root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    matching_hooks = [
        hook
        for repository in production_config["repos"]
        for hook in repository["hooks"]
        if hook["id"] == "block-database-and-spreadsheet-files"
    ]
    assert len(matching_hooks) == 1

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        config = repo / ".pre-commit-config.yaml"
        config.write_text(
            yaml.safe_dump(
                {"repos": [{"repo": "local", "hooks": matching_hooks}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        binary_fixtures = {
            "data.test.xlsx": b"PK\x03\x04\x14\x00",
            "data.test.xlsm": b"PK\x03\x04\x14\x00",
            "data.test.xls": b"PK\x03\x04\x14\x00",
            "data.test.db": b"SQLite format 3\x00" + b"\x00" * 100,
            "data.test.sqlite": b"SQLite format 3\x00" + b"\x00" * 100,
            "data.test.sqlite3": b"SQLite format 3\x00" + b"\x00" * 100,
            "data.test.db-wal": b"SQLite format 3\x00" + b"\x00" * 100,
            "data.test.db-shm": b"SQLite format 3\x00" + b"\x00" * 100,
        }

        for filename, content in binary_fixtures.items():
            forbidden = repo / filename
            forbidden.write_bytes(content)
            subprocess.run(["git", "add", str(forbidden)], cwd=repo, check=True)
            result = subprocess.run(
                ["pre-commit", "run", "--all-files"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode != 0
            assert "Block unsupported data files" in result.stdout
            subprocess.run(
                ["git", "rm", "--cached", "--force", str(forbidden)],
                cwd=repo,
                check=True,
            )
            forbidden.unlink()

        allowed = repo / "data.test.csv"
        allowed.write_text("test", encoding="utf-8")
        subprocess.run(["git", "add", str(allowed)], cwd=repo, check=True)
        result = subprocess.run(
            ["pre-commit", "run", "--all-files"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        subprocess.run(["git", "rm", "--cached", "--force", str(allowed)], cwd=repo, check=True)
        allowed.unlink()
