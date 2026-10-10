from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_github_issues.py"
SPEC = importlib.util.spec_from_file_location("create_github_issues", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def test_find_features_reads_acceptance_and_milestone(tmp_path: Path) -> None:
    (tmp_path / "M1-identity.md").write_text(
        "## F1.2 (stretch) Workspaces\n\n**Acceptance**\n- Scoped reads\n- Role checks\n\n"
        "**Agent prompt**\nIgnore this.\n",
        encoding="utf-8",
    )
    (tmp_path / "M0-foundations.md").write_text("## F0.1 Scaffold\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("## F9.9 Ignored\n", encoding="utf-8")

    features = generator.find_features(tmp_path)

    assert [(item.spec_id, item.title) for item in features] == [
        ("F0.1", "Scaffold"),
        ("F1.2", "(stretch) Workspaces"),
    ]
    assert features[0].source.name == "M0-foundations.md"
    assert features[1].milestone == "M1"
    assert features[1].acceptance == ("Scoped reads", "Role checks")
    assert features[1].stretch


def test_command_for_includes_milestone_acceptance_and_stretch_label() -> None:
    feature = generator.Feature(
        "F1.2",
        "(stretch) Workspace roles",
        Path("docs/specs/M1-identity.md"),
        "M1",
        ("Every read is scoped", "Roles enforced in services"),
        True,
    )

    title, command = generator.command_for(feature)

    assert title == "F1.2 (stretch) Workspace roles"
    assert command[:8] == [
        "gh",
        "issue",
        "create",
        "--title",
        "F1.2 (stretch) Workspace roles",
        "--milestone",
        "M1",
        "--body",
    ]
    assert "docs/specs/M1-identity.md" in command[8]
    assert "- [ ] Every read is scoped" in command[8]
    assert "- [ ] Roles enforced in services" in command[8]
    assert command[-2:] == ["--label", "stretch"]


def test_existing_issue_titles_are_loaded_from_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, **kwargs):
        assert command == [
            "gh",
            "issue",
            "list",
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "title",
        ]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return CompletedProcess(command, 0, json.dumps([{"title": "F0.1 Scaffold"}]), "")

    monkeypatch.setattr(generator.subprocess, "run", fake_run)

    assert generator.existing_issue_titles() == {"F0.1 Scaffold"}


def test_main_skips_existing_title_and_dry_runs_new_titles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "M0-foundations.md").write_text(
        "## F0.1 Existing\n\n## F0.2 New\n\n**Acceptance**\n- Works\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--spec-dir", str(tmp_path)])
    monkeypatch.setattr(generator, "existing_issue_titles", lambda: {"F0.1 Existing"})

    assert generator.main() == 0
    output = capsys.readouterr().out
    assert "F0.1 Existing" not in output
    assert "F0.2 New" in output
    assert "--milestone M0" in output
    assert "- [ ] Works" in output


def test_inline_acceptance_becomes_one_checklist_item(tmp_path: Path) -> None:
    (tmp_path / "M1-identity.md").write_text(
        "## F1.6 (stretch) TOTP\n\n**Acceptance:** optional per-user TOTP with recovery codes.\n",
        encoding="utf-8",
    )

    feature = generator.find_features(tmp_path)[0]
    title, command = generator.command_for(feature)

    assert title == "F1.6 (stretch) TOTP"
    assert feature.acceptance == ("optional per-user TOTP with recovery codes.",)
    assert "- [ ] optional per-user TOTP with recovery codes." in command[8]


def test_dry_run_without_gh_warns_and_prints_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("gh not installed")),
    )
    feature = generator.Feature(
        "F0.1", "Scaffold", Path("docs/specs/M0-foundations.md"), "M0", (), False
    )

    assert generator.print_or_create([feature], execute=False) == 0
    captured = capsys.readouterr()
    assert "Warning:" in captured.err
    assert "gh not installed" in captured.err
    assert "F0.1 Scaffold" in captured.out


def test_execute_without_gh_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("gh not installed")),
    )
    feature = generator.Feature(
        "F0.1", "Scaffold", Path("docs/specs/M0-foundations.md"), "M0", (), False
    )

    assert generator.print_or_create([feature], execute=True) == 1
    assert "refusing to create issues" in capsys.readouterr().err
