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


def _reserved_header_names(tree: ast.Module) -> set[int]:
    """Constants inside config.py's list of names an identity header may not use."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_RESERVED_PROXY_AUTH_HEADERS"
            for target in node.targets
        ):
            ids |= {id(child) for child in ast.walk(node)}
    return ids


def test_no_source_reads_forwarded_headers() -> None:
    """No app code reads forwarded headers itself.

    ``PeerMiddleware`` (used by ``serve``) applies them for ``TRUSTED_PROXIES`` through uvicorn's
    ``ProxyHeadersMiddleware``, after recording the TCP peer; the app then sees only
    ``request.client``. The one exception here is config.py's list of names that proxy identity
    headers may not use (F1.4).
    """
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = _reserved_header_names(tree) if path.name == "config.py" else set[int]()
        for node in ast.walk(tree):
            if id(node) in exempt:
                continue
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
    assert "/invite/" in section
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


def test_adr_0013_exists_and_upgrading_mentions_the_access_log() -> None:
    adr_dir = DEPLOYMENT.parents[0] / "adr"
    adr = (adr_dir / "0013-invitation-links.md").read_text(encoding="utf-8")
    assert "0013-invitation-links.md" in (adr_dir / "README.md").read_text(encoding="utf-8")
    for required in (
        "SHA-256",
        "same-origin",
        'referrerpolicy="no-referrer"',
        "Origin: null",
        "access log",
        "not proof of ownership",
        "5 wrong",
    ):
        assert required in adr
    assert "burn it with 5 wrong attempts" in adr
    upgrading = DEPLOYMENT.read_text(encoding="utf-8").split("## Upgrading", 1)[1]
    assert "access log is off" in upgrading.lower()
    assert "[redacted]" in upgrading
    # Excluding /invite/ from the proxy log is not enough: /login?next= carries the token too.
    assert "is not enough" in upgrading and "/login?next=" in upgrading


def test_adr_0014_records_the_oidc_decisions_and_risks() -> None:
    adr_dir = DEPLOYMENT.parents[0] / "adr"
    adr = (adr_dir / "0014-oidc-sign-in.md").read_text(encoding="utf-8")
    for required in (
        "PKCE (S256)",
        "that account's password entered",
        "No back-channel logout",
        "orphans identities",
        "`email_verified` false",
        "Referrer-Policy: no-referrer",
        "form-action 'self'",
        "SSL_CERT_FILE",
        "assetboard reset-password <email>",
        "cannot probe for accounts",
    ):
        assert required in adr, required
    for amended in ("0012-local-authentication.md", "0013-invitation-links.md"):
        assert "## Amendment (2026-10-12): " in (adr_dir / amended).read_text(encoding="utf-8")
    upgrading = DEPLOYMENT.read_text(encoding="utf-8").split("### F1.3", 1)[1].split("###")[0]
    assert "callback query" in upgrading and "OIDC_ONLY" in upgrading


def test_oidc_guide_covers_providers_and_operations() -> None:
    guide = (DEPLOYMENT.parent / "auth" / "oidc.md").read_text(encoding="utf-8")
    for section in (
        "## Checklist",
        "### Authentik",
        "### Keycloak",
        "### Authelia",
        "## Administrators from a group",
        "## Network: proxies and private certificate authorities",
        "## When the issuer URL changes",
    ):
        assert section in guide, section
    for term in (
        "/auth/oidc/callback",
        "<BASE_URL>/login",
        "email_verified",
        "groups scope",
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "assetboard reset-password <email>",
        "/login/local",
        "ASSETBOARD_OIDC_ONLY",
    ):
        assert term in guide, term
    # Every OIDC setting in the guide is a real setting.
    names = set(re.findall(r"ASSETBOARD_(OIDC_[A-Z_]+|ALLOW_LOCAL_ADMIN_LOGIN)", guide))
    fields = {field.upper() for field in Settings.model_fields}
    assert names and names <= fields, names - fields
    assert "](auth/oidc.md)" in DEPLOYMENT.read_text(encoding="utf-8")


def test_proxy_auth_guide_opens_with_the_warning_and_links_each_proxy() -> None:
    guide = (DEPLOYMENT.parent / "auth" / "proxy-auth.md").read_text(encoding="utf-8")
    first_section = guide.split("\n## ", 1)[0]
    assert "[!WARNING]" in first_section
    for point in ("ASSETBOARD_TRUSTED_PROXIES", "overwrite or remove", "verified", "logout"):
        assert point in first_section
    sections = {
        "### nginx": "https://nginx.org/",
        "### Caddy": "https://caddyserver.com/docs/",
        "### Traefik": "https://doc.traefik.io/",
        "### Authentik": "https://docs.goauthentik.io/",
    }
    references = dict(re.findall(r"^\[([a-z-]+)\]: (\S+)$", guide, re.MULTILINE))
    for heading, link in sections.items():
        body = guide.split(heading, 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
        # Inline links, or reference links resolved through their definitions.
        targets = re.findall(r"\]\((https?://[^)]+)\)", body) + [
            references[label] for label in re.findall(r"\]\[([a-z-]+)\]", body)
        ]
        assert any(target.startswith(link) for target in targets), heading
    assert "https://www.authelia.com/" in guide
    test_it = guide.split("## Test it", 1)[1].split("\n## ", 1)[0]
    assert "challenged" in test_it and "overwritten" in test_it and "another host" in test_it
    assert "OIDC_ALLOWED_DOMAINS" in guide and "same accounts" in guide
