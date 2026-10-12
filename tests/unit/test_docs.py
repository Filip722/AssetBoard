from __future__ import annotations

import ast
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


def _source_files() -> list[Path]:
    return sorted((DEPLOYMENT.parents[1] / "src/assetboard").rglob("*.py"))


def test_no_source_reads_forwarded_headers() -> None:
    """Only request.client.host is used; forwarded headers are uvicorn's job (TRUSTED_PROXIES)."""
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
                text = node.value.decode() if isinstance(node.value, bytes) else node.value
                lowered = text.lower()
                if "x-forwarded" in lowered or lowered.strip() in {"forwarded", "x-real-ip"}:
                    offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.keyword) and node.arg == "forwarded_allow_ips":
                assert path.name == "cli.py", "only serve configures forwarded headers"
    assert offenders == []


def test_no_stale_authentication_statements() -> None:
    root = DEPLOYMENT.parents[1]
    for document in ("README.md", "SECURITY.md", "docs/deployment.md", "compose.example.yaml"):
        text = (root / document).read_text(encoding="utf-8").lower()
        assert "until m1" not in text, document
        assert "no authentication" not in text, document


def test_deployment_guide_covers_proxy_rate_limits_and_trusted_proxies() -> None:
    text = DEPLOYMENT.read_text(encoding="utf-8")
    section = text.split("## Reverse proxies", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    first_paragraphs = section.split("\n\n", maxsplit=3)[:3]
    assert any("ASSETBOARD_TRUSTED_PROXIES" in part for part in first_paragraphs)
    assert "/login" in section and "/register" in section and "rate limit" in section.lower()
    assert "client_max_body_size" in section  # body-size cap at the proxy
    assert "## Upgrading" in text and "TRUSTED_PROXIES" in text.split("## Upgrading", 1)[1]
    assert "https" in text and "BASE_URL" in text


def test_adr_0012_exists_and_is_indexed() -> None:
    adr_dir = DEPLOYMENT.parents[0] / "adr"
    assert (adr_dir / "0012-local-authentication.md").is_file()
    assert "0012-local-authentication.md" in (adr_dir / "README.md").read_text(encoding="utf-8")


def test_architecture_doc_has_new_model_section() -> None:
    docs = DEPLOYMENT.parent
    architecture = (docs / "architecture.md").read_text(encoding="utf-8")
    section = architecture.split("## How to add a new workspace-owned model\n", maxsplit=1)[1]
    for required in (
        "WorkspaceScopedMixin",
        "ScopedRepository",
        "TENANT_CASES",
        "EXTRA_METHOD_COVERAGE",
        "composite",
        "test_user_b_cannot_read_update_or_delete_user_a_rows",
    ):
        assert required in section
    adr = (docs / "adr" / "0003-workspaces-and-tenant-isolation.md").read_text(encoding="utf-8")
    assert "## Amendment (2026-10-12)" in adr
    assert "with_loader_criteria" in adr and "deferred" in adr
