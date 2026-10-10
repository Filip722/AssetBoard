from __future__ import annotations

import re
from pathlib import Path

from assetboard.branding import ENV_PREFIX
from assetboard.config import Settings

DEPLOYMENT = Path(__file__).resolve().parents[2] / "docs" / "deployment.md"
TABLE_VARIABLE = re.compile(r"^\| `(?P<name>ASSETBOARD_[A-Z_]+)` \|", re.MULTILINE)
README_TABLE_ROW = re.compile(
    r"^\| `(?P<name>ASSETBOARD_[A-Z_]+)` \| `?(?P<default>[^|`]+?)`? \|",
    re.MULTILINE,
)
COMPOSE_ONLY = {
    "ASSETBOARD_BIND_ADDRESS",
    "ASSETBOARD_HOST_PORT",
    "ASSETBOARD_IMAGE",
}


def deployment_table_variables() -> set[str]:
    document = DEPLOYMENT.read_text(encoding="utf-8")
    section = document.split("## Environment variables\n", maxsplit=1)[1]
    section = section.split("\n## ", maxsplit=1)[0]
    return set(TABLE_VARIABLE.findall(section))


def test_every_settings_field_is_documented() -> None:
    documented = deployment_table_variables()
    settings_names = {f"{ENV_PREFIX}_{field.upper()}" for field in Settings.model_fields}

    assert settings_names <= documented


def test_deployment_table_has_no_unknown_environment_variables() -> None:
    settings_names = {f"{ENV_PREFIX}_{field.upper()}" for field in Settings.model_fields}

    assert deployment_table_variables() <= settings_names | COMPOSE_ONLY


def test_readme_configuration_table_has_known_variables_and_settings_defaults() -> None:
    readme = (DEPLOYMENT.parents[1] / "README.md").read_text(encoding="utf-8")
    rows = {match["name"]: match["default"].strip() for match in README_TABLE_ROW.finditer(readme)}
    settings_names = {f"{ENV_PREFIX}_{field.upper()}" for field in Settings.model_fields}

    assert rows
    assert set(rows) <= settings_names | COMPOSE_ONLY

    for name in ("ASSETBOARD_BASE_URL", "ASSETBOARD_DEFAULT_LOCALE", "ASSETBOARD_DEFAULT_CURRENCY"):
        field_name = name.removeprefix(f"{ENV_PREFIX}_").lower()
        assert rows[name] == str(Settings.model_fields[field_name].default)

    assert rows["ASSETBOARD_TRUSTED_PROXIES"] == "empty"
    assert Settings.model_fields["trusted_proxies"].default_factory is list


def test_every_numbered_adr_is_listed_in_the_index() -> None:
    adr_dir = DEPLOYMENT.parents[0] / "adr"
    index = (adr_dir / "README.md").read_text(encoding="utf-8")
    linked_files = set(re.findall(r"\]\((\d{4}-[^)]+\.md)\)", index))
    adr_files = {path.name for path in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")}

    assert adr_files <= linked_files
