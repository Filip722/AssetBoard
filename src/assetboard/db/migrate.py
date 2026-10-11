from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from alembic import command
from alembic.autogenerate.api import AutogenContext
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from assetboard.config import Settings
from assetboard.db.types import DecimalType, UtcDateTime


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("path_separator", "os")
    config.attributes["settings"] = settings
    return config


def configured_database_url(config: Config) -> str:
    settings = config.attributes.get("settings")
    if settings is None:
        settings = Settings()
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    return database_url


def upgrade_database(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")


def database_is_at_head(settings: Settings) -> bool:
    """Report whether the database is at the Alembic head without creating or changing it."""
    database_url = configured_database_url(alembic_config(settings))
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:":
            return False
        if not database.startswith("file:") and not Path(database).expanduser().is_file():
            return False
    heads = set(ScriptDirectory.from_config(alembic_config(settings)).get_heads())
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
    return current == heads


def check_database_migrations(settings: Settings) -> None:
    command.check(alembic_config(settings))


def create_revision(settings: Settings, message: str) -> None:
    with TemporaryDirectory(prefix="assetboard-revision-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        scratch_settings = settings.model_copy(
            update={
                "data_dir": temporary_path,
                "database_url": f"sqlite:///{temporary_path / 'revision.db'}",
            }
        )
        upgrade_database(scratch_settings)
        command.revision(alembic_config(scratch_settings), message=message, autogenerate=True)


def render_item(
    item_type: str,
    item: Any,
    autogen_context: AutogenContext,
) -> str | Literal[False]:
    if item_type == "type" and isinstance(item, (DecimalType, UtcDateTime)):
        type_name = type(item).__name__
        autogen_context.imports.add(f"from assetboard.db.types import {type_name}")
        return f"{type_name}()"
    return False
