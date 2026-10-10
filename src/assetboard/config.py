from __future__ import annotations

import logging
import os
import secrets
import tempfile
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from assetboard.branding import ENV_PREFIX


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

    @model_validator(mode="after")
    def finalize_settings(self) -> Settings:
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
