#!/usr/bin/env python3
"""Print GitHub issue creation commands for feature headings in milestone specs."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

FEATURE_HEADING = re.compile(r"^##\s+(F\d+\.\d+)\s+(.+?)\s*$", re.MULTILINE)
ACCEPTANCE = re.compile(
    r"^\*\*Acceptance\*\*\s*$([\s\S]*?)(?=^\*\*[^*]+\*\*|^## |\Z)", re.MULTILINE
)
INLINE_ACCEPTANCE = re.compile(r"^\*\*Acceptance:\*\*\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Feature:
    spec_id: str
    title: str
    source: Path
    milestone: str
    acceptance: tuple[str, ...]
    stretch: bool


def find_features(spec_dir: Path) -> list[Feature]:
    """Read feature headings and acceptance bullets from M*.md files."""
    repo_root = Path(__file__).resolve().parents[1]
    features: list[Feature] = []
    for source in sorted(spec_dir.glob("M*.md")):
        contents = source.read_text(encoding="utf-8")
        matches = list(FEATURE_HEADING.finditer(contents))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(contents)
            section = contents[match.end() : end]
            acceptance_match = ACCEPTANCE.search(section)
            inline_acceptance = INLINE_ACCEPTANCE.search(section)
            if inline_acceptance:
                bullets = (inline_acceptance.group(1).strip(),)
            elif acceptance_match:
                bullets = tuple(
                    bullet.strip()
                    for bullet in re.findall(r"^\s*-\s+(.+?)\s*$", acceptance_match.group(1), re.M)
                )
            else:
                bullets = ()
            milestone_match = re.match(r"^(M\d+)-", source.name)
            if milestone_match is None:
                continue
            try:
                relative_source = source.resolve().relative_to(repo_root)
            except ValueError:
                relative_source = source
            features.append(
                Feature(
                    spec_id=match.group(1),
                    title=match.group(2),
                    source=relative_source,
                    milestone=milestone_match.group(1),
                    acceptance=bullets,
                    stretch="(stretch)" in match.group(2).lower(),
                )
            )
    return features


def command_for(feature: Feature) -> tuple[str, list[str]]:
    title = f"{feature.spec_id} {feature.title}"
    checklist = feature.acceptance or ("Review acceptance criteria in the feature specification.",)
    body = (
        f"Feature specification: `{feature.source.as_posix()}`\n\n"
        "## Acceptance checklist\n\n" + "\n".join(f"- [ ] {item}" for item in checklist)
    )
    command = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--milestone",
        feature.milestone,
        "--body",
        body,
    ]
    if feature.stretch:
        command.extend(["--label", "stretch"])
    return title, command


def existing_issue_titles() -> set[str]:
    result = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--limit", "1000", "--json", "title"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {item["title"] for item in json.loads(result.stdout)}


def print_or_create(features: list[Feature], *, execute: bool) -> int:
    try:
        existing_titles = existing_issue_titles()
    except Exception as exc:
        if execute:
            print(
                f"Could not list existing GitHub issues; refusing to create issues: {exc}",
                file=sys.stderr,
            )
            return 1
        print(
            f"Warning: could not list existing GitHub issues; printing all commands: {exc}",
            file=sys.stderr,
        )
        existing_titles = set()

    created = False
    for feature in features:
        title, command = command_for(feature)
        if title in existing_titles:
            continue
        if execute:
            if created:
                time.sleep(1)
            subprocess.run(command, check=True)
            created = True
        else:
            print(shlex.join(command))
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec-dir",
        type=Path,
        default=repo_root / "docs" / "specs",
        help="directory containing M*.md specs",
    )
    parser.add_argument(
        "--execute", action="store_true", help="run gh commands (default: print commands only)"
    )
    args = parser.parse_args()

    return print_or_create(find_features(args.spec_dir), execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
