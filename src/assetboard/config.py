from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from assetboard.branding import ENV_PREFIX

# Proxy authentication (F1.4, ADR-0015): the narrowest TRUSTED_PROXIES entries allowed.
PROXY_AUTH_MIN_PREFIX = {4: 24, 6: 64}
_IPV4_MAPPED = ip_network("::ffff:0:0/96")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9a-z]{1,64}")
# Headers that carry something else (the connection, forwarding, cookies, the browser's own
# requests); an identity header must never share a name with one of them.
_RESERVED_PROXY_AUTH_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "forwarded",
        "host",
        "origin",
        "proxy-authorization",
        "referer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-real-ip",
        "x-request-id",
    }
)
_RESERVED_PROXY_AUTH_PREFIXES = ("hx-", "sec-")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="dev")
    data_dir: Path = Field(default=Path("./data"))
    database_url: str | None = None
    secret_key: str | None = None
    base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    log_format: str = "text"
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    default_locale: str = "en"
    default_currency: str = "EUR"
    auto_migrate: bool = True
    setup_token: SecretStr | None = None
    allow_registration: bool = False
    recurrence_horizon_months: int = Field(default=13, ge=1)
    session_idle_timeout_days: int = Field(default=14, ge=1, le=365)
    session_absolute_timeout_days: int = Field(default=60, ge=1, le=365)
    auth_account_free_failures: int = Field(default=5, ge=1)
    auth_ip_free_failures: int = Field(default=20, ge=1)
    auth_ip_window_minutes: int = Field(default=60, ge=1, le=1440)
    auth_throttle_base_seconds: int = Field(default=60, ge=1)
    auth_throttle_max_seconds: int = Field(default=900, ge=1)
    setup_throttle_free_failures: int = Field(default=5, ge=1)
    registration_ip_limit: int = Field(default=5, ge=1)
    registration_window_minutes: int = Field(default=60, ge=1, le=1440)
    auth_max_concurrent_hashes: int = Field(default=2, ge=1, le=8)
    # OIDC sign-in (F1.3, ADR-0014): on when ``oidc_issuer`` is set.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = "openid email profile"
    oidc_provider_name: str = "SSO"
    oidc_link_by_email: bool = False
    oidc_auto_create: bool = False
    oidc_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    oidc_groups_claim: str = "groups"
    oidc_admin_group: str | None = None
    oidc_only: bool = False
    allow_local_admin_login: bool = False
    oidc_rp_logout: bool = False
    # Reverse-proxy header authentication (F1.4, ADR-0015): off unless explicitly enabled.
    proxy_auth_enabled: bool = False
    proxy_auth_user_header: str | None = None
    proxy_auth_email_header: str | None = None
    proxy_auth_name_header: str | None = None
    proxy_auth_auto_create: bool = False
    proxy_auth_link_by_email: bool = False

    def __init__(self, *, _env_file: str | Path | None = ".env", **values: Any) -> None:
        super().__init__(_env_file=_env_file, **values)

    @field_validator("data_dir", mode="before")
    @classmethod
    def validate_data_dir(cls, value: str | Path) -> Path:
        if not str(value).strip():
            raise ValueError("DATA_DIR must not be empty")
        path = Path(value).expanduser()
        if path.exists() and not path.is_dir():
            raise ValueError("DATA_DIR must be a directory")
        return path

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("BASE_URL must be an absolute HTTP or HTTPS URL without credentials")
        return value.rstrip("/")

    @field_validator("secret_key", mode="before")
    @classmethod
    def validate_secret_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("SECRET_KEY must not be empty")
        return value.strip()

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        if value not in {"dev", "prod"}:
            raise ValueError("ENV must be 'dev' or 'prod'")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if value.upper() not in valid:
            raise ValueError("LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return value.upper()

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        if value not in {"text", "json"}:
            raise ValueError("LOG_FORMAT must be 'text' or 'json'")
        return value

    @field_validator("default_locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if len(value) != 2 or not value.islower():
            raise ValueError("DEFAULT_LOCALE must be a two-letter lowercase language code")
        return value

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("DEFAULT_CURRENCY must be a three-letter ISO currency code")
        return normalized

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def split_trusted_proxies(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            value = [proxy.strip() for proxy in value.split(",") if proxy.strip()]
        if not isinstance(value, list):
            raise ValueError("TRUSTED_PROXIES must be a comma-separated list")
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        for proxy in value:
            try:
                ip_network(proxy, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "TRUSTED_PROXIES must contain valid IPv4 or IPv6 CIDR networks"
                ) from exc
        return value

    @field_validator(
        "proxy_auth_user_header",
        "proxy_auth_email_header",
        "proxy_auth_name_header",
        mode="before",
    )
    @classmethod
    def validate_proxy_auth_header(cls, value: str | None) -> str | None:
        # Stored lowercase: ASGI servers deliver header names lowercased.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("PROXY_AUTH_*_HEADER must be a header name")
        name = value.strip().lower()
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError("PROXY_AUTH_*_HEADER must be an HTTP header name such as Remote-User")
        if name in _RESERVED_PROXY_AUTH_HEADERS or name.startswith(_RESERVED_PROXY_AUTH_PREFIXES):
            raise ValueError(f"PROXY_AUTH_*_HEADER cannot use the reserved header {value.strip()}")
        return name

    @field_validator("oidc_issuer", "oidc_client_id", "oidc_admin_group", mode="before")
    @classmethod
    def blank_is_unset(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("oidc_client_secret", mode="before")
    @classmethod
    def blank_secret_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("oidc_issuer")
    @classmethod
    def validate_oidc_issuer(cls, value: str | None) -> str | None:
        # Kept exactly as given (trailing slash included): discovery must return the same string.
        if value is None:
            return None
        if len(value) > 512 or not value.isascii():
            raise ValueError(
                "OIDC_ISSUER must be at most 512 ASCII characters (use punycode for IDN hosts)"
            )
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("OIDC_ISSUER must be an absolute URL without query or fragment")
        return value

    @field_validator("oidc_scopes")
    @classmethod
    def validate_oidc_scopes(cls, value: str) -> str:
        scopes = value.split()
        if "openid" not in scopes:
            raise ValueError("OIDC_SCOPES must contain 'openid'")
        return " ".join(scopes)

    @field_validator("oidc_provider_name")
    @classmethod
    def validate_oidc_provider_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 40:
            raise ValueError("OIDC_PROVIDER_NAME must contain 1 to 40 characters")
        return cleaned

    @field_validator("oidc_groups_claim")
    @classmethod
    def validate_oidc_groups_claim(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("OIDC_GROUPS_CLAIM must not be empty")
        return cleaned

    @field_validator("oidc_allowed_domains", mode="before")
    @classmethod
    def split_oidc_allowed_domains(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            raise ValueError("OIDC_ALLOWED_DOMAINS must be a comma-separated list")
        domains = [domain.strip().lower().rstrip(".") for domain in value if domain.strip()]
        for domain in domains:
            labels = domain.split(".")
            # ASCII labels only (str.isalnum also accepts other scripts); IDNs go in as punycode.
            if len(labels) < 2 or not all(
                label
                and len(label) <= 63
                and label.isascii()
                and label.replace("-", "").isalnum()
                and not label.startswith("-")
                and not label.endswith("-")
                for label in labels
            ):
                raise ValueError(
                    "OIDC_ALLOWED_DOMAINS must contain ASCII domain names like example.com "
                    "(punycode for internationalized names)"
                )
        return domains

    @property
    def oidc_enabled(self) -> bool:
        return self.oidc_issuer is not None

    @property
    def oidc_redirect_uri(self) -> str:
        """Always built from BASE_URL, never from request headers."""
        return f"{self.base_url}/auth/oidc/callback"

    @property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        return tuple(ip_network(proxy, strict=False) for proxy in self.trusted_proxies)

    def wide_trusted_proxies(self) -> list[str]:
        """Entries that cover more than one address (logged at startup with proxy auth on)."""
        return [
            str(network)
            for network in self.trusted_proxy_networks
            if network.prefixlen < network.max_prefixlen
        ]

    def _validate_proxy_auth(self) -> None:
        if not self.proxy_auth_enabled:
            return
        if self.proxy_auth_user_header is None or self.proxy_auth_email_header is None:
            raise ValueError(
                "PROXY_AUTH_USER_HEADER and PROXY_AUTH_EMAIL_HEADER are required when "
                "PROXY_AUTH_ENABLED is true"
            )
        names = [
            name
            for name in (
                self.proxy_auth_user_header,
                self.proxy_auth_email_header,
                self.proxy_auth_name_header,
            )
            if name is not None
        ]
        if len(set(names)) != len(names):
            raise ValueError("PROXY_AUTH_*_HEADER settings must name different headers")
        if not self.trusted_proxies:
            raise ValueError(
                "TRUSTED_PROXIES is required when PROXY_AUTH_ENABLED is true: list the "
                "addresses of the reverse proxy that sets the identity headers"
            )
        for network in self.trusted_proxy_networks:
            if network.prefixlen < PROXY_AUTH_MIN_PREFIX[network.version]:
                raise ValueError(
                    f"TRUSTED_PROXIES entry {network} is too wide for proxy authentication: "
                    "use at most /24 for IPv4 and /64 for IPv6, ideally the proxy's own address"
                )
            # IPv4-mapped peers (::ffff:a.b.c.d) are compared as IPv4, so such an entry would
            # never match what it seems to say.
            if isinstance(network, IPv6Network) and network.overlaps(_IPV4_MAPPED):
                raise ValueError(
                    f"TRUSTED_PROXIES entry {network} covers IPv4-mapped IPv6 addresses "
                    "(::ffff:0:0/96); write IPv4 proxies as IPv4 networks"
                )

    def _validate_oidc(self) -> None:
        if not self.oidc_enabled:
            if self.oidc_only:
                raise ValueError("OIDC_ONLY needs OIDC_ISSUER")
            return
        insecure_allowed = self.env == "dev"
        if self.oidc_client_id is None:
            raise ValueError("OIDC_CLIENT_ID is required when OIDC_ISSUER is set")
        if not insecure_allowed and urlsplit(self.oidc_issuer or "").scheme != "https":
            raise ValueError("OIDC_ISSUER must use https (http is allowed only with ENV=dev)")
        if "base_url" not in self.model_fields_set:
            raise ValueError("BASE_URL must be set when OIDC is enabled")
        if not insecure_allowed and urlsplit(self.base_url).scheme != "https":
            raise ValueError(
                "BASE_URL must use https when OIDC is enabled (http only with ENV=dev)"
            )
        if self.oidc_only and self.allow_registration:
            raise ValueError("OIDC_ONLY cannot be combined with ALLOW_REGISTRATION")

    @model_validator(mode="after")
    def finalize_settings(self) -> Settings:
        if self.setup_token is not None:
            token = self.setup_token.get_secret_value()
            if len(token) < 22:
                raise ValueError(
                    "SETUP_TOKEN must be at least 22 characters (128 bits when generated randomly)"
                )
        if self.session_idle_timeout_days > self.session_absolute_timeout_days:
            raise ValueError("SESSION_IDLE_TIMEOUT_DAYS must be <= SESSION_ABSOLUTE_TIMEOUT_DAYS")
        if self.auth_throttle_max_seconds < self.auth_throttle_base_seconds:
            raise ValueError("AUTH_THROTTLE_MAX_SECONDS must be >= AUTH_THROTTLE_BASE_SECONDS")
        self._validate_oidc()
        self._validate_proxy_auth()
        if self.database_url is None:
            database_filename = self.data_dir / "assetboard.db"
            self.database_url = f"sqlite:///{database_filename}"
        elif not self.database_url.startswith("sqlite://"):
            raise ValueError("DATABASE_URL must use the SQLite scheme for this foundation")

        return self

    def ensure_data_dir_writable(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self.data_dir):
                pass
        except OSError as exc:
            raise RuntimeError(
                f"DATA_DIR {self.data_dir} is not writable. For a host bind mount, run "
                "'chown 10001:10001 <host-data-directory>' or use a named Docker volume."
            ) from exc

    def ensure_secret_key(self) -> str:
        if self.secret_key is not None:
            return self.secret_key

        secret_file = self.data_dir / "secret_key"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary_file: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=".secret_key.",
                delete=False,
            ) as secret_stream:
                temporary_file = Path(secret_stream.name)
                os.chmod(temporary_file, 0o600)
                secret = secrets.token_urlsafe(32)
                secret_stream.write(secret)
                secret_stream.flush()
                os.fsync(secret_stream.fileno())

            try:
                os.link(temporary_file, secret_file)
            except FileExistsError:
                existing_secret = secret_file.read_text(encoding="utf-8").strip()
                if not existing_secret:
                    raise ValueError(
                        f"SECRET_KEY file {secret_file} is empty; delete it to regenerate."
                    ) from None
                self.secret_key = existing_secret
                return existing_secret

            self.secret_key = secret
            logging.getLogger(__name__).warning(
                "Generated a new secret key and saved it to %s",
                secret_file,
            )
            return secret
        finally:
            if temporary_file is not None:
                temporary_file.unlink(missing_ok=True)
