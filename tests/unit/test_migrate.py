from __future__ import annotations

import pytest
from alembic.autogenerate.api import AutogenContext
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from assetboard.config import Settings
from assetboard.db.migrate import configured_database_url, render_item
from assetboard.db.types import DecimalType, UtcDateTime


@pytest.mark.parametrize(
    ("item", "rendered", "import_statement"),
    [
        (DecimalType(), "DecimalType()", "from assetboard.db.types import DecimalType"),
        (UtcDateTime(), "UtcDateTime()", "from assetboard.db.types import UtcDateTime"),
    ],
)
def test_render_item_adds_import_for_custom_types(
    item: DecimalType | UtcDateTime,
    rendered: str,
    import_statement: str,
) -> None:
    autogen_context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", item, autogen_context) == rendered
    assert import_statement in autogen_context.imports


def test_render_item_leaves_standard_types_to_alembic() -> None:
    autogen_context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", object(), autogen_context) is False
    assert not autogen_context.imports


def test_configured_database_url_uses_default_settings_without_config_attribute() -> None:
    settings = Settings()

    assert configured_database_url(Config()) == settings.database_url
