from __future__ import annotations

import stat
from pathlib import Path

import pytest

from assetboard.branding import ENV_PREFIX
from assetboard.config import Settings


def test_settings_are_side_effect_free_and_use_spec_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "not-created-yet"
    monkeypatch.setenv(f"{ENV_PREFIX}_DATA_DIR", str(data_dir))

    settings = Settings(_env_file=None)

    assert settings.env == "dev"
    assert settings.data_dir == data_dir
    assert settings.database_url == f"sqlite:///{data_dir / 'assetboard.db'}"
    assert settings.base_url == "http://localhost:8000"
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"
    assert settings.trusted_proxies == []
    assert settings.default_locale == "en"
    assert settings.default_currency == "EUR"
    assert settings.auto_migrate is True
    assert settings.secret_key is None
    assert settings.setup_token is None
    assert settings.allow_registration is False
    assert settings.session_idle_timeout_days == 14
    assert settings.session_absolute_timeout_days == 60
    assert settings.auth_account_free_failures == 5
    assert settings.auth_ip_free_failures == 20
    assert settings.auth_ip_window_minutes == 60
    assert settings.registration_ip_limit == 5
    assert settings.registration_window_minutes == 60
    assert settings.auth_max_concurrent_hashes == 2
    assert settings.auth_throttle_base_seconds == 60
    assert settings.auth_throttle_max_seconds == 900
    assert settings.setup_throttle_free_failures == 5
    assert not data_dir.exists()


def test_ensure_secret_key_creates_private_persistent_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "secret-data"
    settings = Settings(_env_file=None, data_dir=data_dir)

    secret = settings.ensure_secret_key()
    secret_file = data_dir / "secret_key"

    assert secret
    assert secret_file.read_text(encoding="utf-8") == secret
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert Settings(_env_file=None, data_dir=data_dir).ensure_secret_key() == secret


def test_ensure_secret_key_strips_existing_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "existing-secret"
    data_dir.mkdir()
    (data_dir / "secret_key").write_text("  retained-secret\n", encoding="utf-8")

    settings = Settings(_env_file=None, data_dir=data_dir)

    assert settings.ensure_secret_key() == "retained-secret"


def test_ensure_secret_key_rejects_empty_values(tmp_path: Path) -> None:
    data_dir = tmp_path / "empty-secret"
    data_dir.mkdir()
    secret_file = data_dir / "secret_key"
    secret_file.write_text(" \n", encoding="utf-8")

    with pytest.raises(ValueError, match="SECRET_KEY") as error:
        Settings(_env_file=None, data_dir=data_dir).ensure_secret_key()

    assert str(secret_file) in str(error.value)
    assert "delete it to regenerate" in str(error.value)

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, secret_key=" \n")


def test_settings_validate_base_url_and_data_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BASE_URL"):
        Settings(_env_file=None, base_url="/relative/path")

    with pytest.raises(ValueError, match="BASE_URL"):
        Settings(_env_file=None, base_url="ftp://example.test")

    data_path = tmp_path / "not-a-directory"
    data_path.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="DATA_DIR"):
        Settings(_env_file=None, data_dir=data_path)


def test_data_directory_permission_error_has_container_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "locked-data"
    settings = Settings(_env_file=None, data_dir=data_dir)

    def fail_temporary_file(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only mount")

    monkeypatch.setattr("assetboard.config.tempfile.TemporaryFile", fail_temporary_file)

    with pytest.raises(RuntimeError, match="DATA_DIR") as error:
        settings.ensure_data_dir_writable()

    assert "chown 10001:10001" in str(error.value)
    assert "named Docker volume" in str(error.value)


def test_settings_parse_trusted_proxies_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        f"{ENV_PREFIX}_TRUSTED_PROXIES",
        "127.0.0.1, 10.20.0.0/16",
    )

    settings = Settings(_env_file=None)

    assert settings.trusted_proxies == ["127.0.0.1", "10.20.0.0/16"]


def test_settings_parse_empty_trusted_proxies_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}_TRUSTED_PROXIES", "")

    settings = Settings(_env_file=None)

    assert settings.trusted_proxies == []


def test_settings_validate_trusted_proxy_networks() -> None:
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings(_env_file=None, trusted_proxies=["not-a-cidr"])


def test_settings_reject_invalid_environment_logging_locale_and_currency() -> None:
    with pytest.raises(ValueError, match="ENV"):
        Settings(_env_file=None, env="staging")

    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(_env_file=None, log_level="verbose")

    with pytest.raises(ValueError, match="LOG_FORMAT"):
        Settings(_env_file=None, log_format="yaml")

    with pytest.raises(ValueError, match="DEFAULT_LOCALE"):
        Settings(_env_file=None, default_locale="de_DE")

    with pytest.raises(ValueError, match="DEFAULT_CURRENCY"):
        Settings(_env_file=None, default_currency="US$")


def test_settings_reject_non_sqlite_database_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(_env_file=None, data_dir=tmp_path, database_url="postgresql://example.test/app")


def test_settings_configure_auth_thresholds_timeouts_and_secret_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}_SETUP_TOKEN", "0123456789abcdefghijkl")
    monkeypatch.setenv(f"{ENV_PREFIX}_SESSION_IDLE_TIMEOUT_DAYS", "7")
    monkeypatch.setenv(f"{ENV_PREFIX}_SESSION_ABSOLUTE_TIMEOUT_DAYS", "14")
    monkeypatch.setenv(f"{ENV_PREFIX}_AUTH_IP_FREE_FAILURES", "30")

    settings = Settings(_env_file=None)

    assert settings.setup_token is not None
    assert settings.setup_token.get_secret_value() == "0123456789abcdefghijkl"
    assert settings.session_idle_timeout_days == 7
    assert settings.session_absolute_timeout_days == 14
    assert settings.auth_ip_free_failures == 30


def test_settings_reject_idle_timeout_longer_than_absolute_timeout() -> None:
    with pytest.raises(ValueError, match="SESSION_IDLE_TIMEOUT_DAYS"):
        Settings(_env_file=None, session_idle_timeout_days=30, session_absolute_timeout_days=14)


@pytest.mark.parametrize("value", [0, 9])
def test_settings_bound_concurrent_password_hashes(value: int) -> None:
    with pytest.raises(ValueError, match="auth_max_concurrent_hashes"):
        Settings(_env_file=None, auth_max_concurrent_hashes=value)


# OIDC (F1.3)

OIDC_BASE = {
    "env": "prod",
    "base_url": "https://money.example.test",
    "oidc_issuer": "https://idp.example.test/application/o/assetboard/",
    "oidc_client_id": "assetboard",
}


def test_oidc_is_off_by_default(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert not settings.oidc_enabled
    assert settings.oidc_scopes == "openid email profile"
    assert settings.oidc_provider_name == "SSO"
    assert settings.oidc_groups_claim == "groups"
    assert settings.oidc_allowed_domains == []
    assert not (settings.oidc_link_by_email or settings.oidc_auto_create or settings.oidc_only)
    assert not (settings.allow_local_admin_login or settings.oidc_rp_logout)
    assert settings.oidc_client_secret is None and settings.oidc_admin_group is None


def test_oidc_settings_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "OIDC_ISSUER": OIDC_BASE["oidc_issuer"],
        "OIDC_CLIENT_ID": "assetboard",
        "OIDC_CLIENT_SECRET": "s3cret-value",
        "OIDC_SCOPES": "openid  email profile groups",
        "OIDC_ALLOWED_DOMAINS": " Example.COM, family.example ",
        "OIDC_ADMIN_GROUP": "",
        "BASE_URL": "https://money.example.test/",
        "ENV": "prod",
    }.items():
        monkeypatch.setenv(f"{ENV_PREFIX}_{name}", value)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    # The issuer is kept exactly, trailing slash included.
    assert settings.oidc_issuer == "https://idp.example.test/application/o/assetboard/"
    assert settings.oidc_scopes == "openid email profile groups"
    assert settings.oidc_allowed_domains == ["example.com", "family.example"]
    assert settings.oidc_admin_group is None  # blank is unset
    assert settings.oidc_redirect_uri == "https://money.example.test/auth/oidc/callback"
    assert settings.oidc_client_secret is not None
    assert "s3cret-value" not in repr(settings)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"oidc_client_id": None}, "OIDC_CLIENT_ID is required"),
        ({"oidc_scopes": "email profile"}, "must contain 'openid'"),
        ({"oidc_issuer": "http://idp.example.test"}, "OIDC_ISSUER must use https"),
        ({"oidc_issuer": "https://idp.example.test/?x=1"}, "without query"),
        ({"oidc_issuer": "idp.example.test"}, "absolute URL"),
        ({"oidc_issuer": "https://idp.example.test/" + "a" * 490}, "at most 512"),
        ({"base_url": "http://money.example.test"}, "BASE_URL must use https"),
        ({"base_url": None}, "BASE_URL must be set"),
        ({"oidc_only": True, "allow_registration": True}, "cannot be combined"),
        ({"oidc_issuer": None, "oidc_only": True}, "OIDC_ONLY needs OIDC_ISSUER"),
        ({"oidc_provider_name": " "}, "1 to 40 characters"),
        ({"oidc_provider_name": "x" * 41}, "1 to 40 characters"),
        ({"oidc_allowed_domains": "example"}, "domain names"),
        ({"oidc_allowed_domains": "user@example.com"}, "domain names"),
        ({"oidc_allowed_domains": "bücher.example"}, "ASCII domain names"),
        ({"oidc_allowed_domains": "-bad.example"}, "ASCII domain names"),
        ({"oidc_allowed_domains": "xn--bcher-kva.example"}, None),
        ({"oidc_issuer": "https://idp.bücher.example"}, "ASCII"),
        ({"oidc_groups_claim": " "}, "must not be empty"),
    ],
)
def test_oidc_settings_are_validated(
    tmp_path: Path, overrides: dict[str, object], message: str | None
) -> None:
    values: dict[str, object] = {"data_dir": tmp_path, **OIDC_BASE, **overrides}
    if overrides.get("base_url", "") is None:
        del values["base_url"]
    if message is None:  # a valid value among the invalid ones
        Settings(_env_file=None, **values)  # type: ignore[arg-type]
        return
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_http_is_allowed_for_oidc_in_dev(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        env="dev",
        base_url="http://localhost:8000",
        oidc_issuer="http://localhost:9000/realms/dev",
        oidc_client_id="assetboard",
    )
    assert settings.oidc_enabled
    assert settings.oidc_redirect_uri == "http://localhost:8000/auth/oidc/callback"
