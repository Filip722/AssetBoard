"""nullable user locale

Revision ID: 0007_f15
Revises: 0006_f14
Create Date: 2026-10-13 10:00:00.000000

``users.locale`` NULL means "follow the browser" (F1.5). Until now the stored value was never
applied (the browser's language always won), so the upgrade sets every row to NULL, which keeps
exactly that behaviour. The downgrade writes the configured DEFAULT_LOCALE into NULL rows before
restoring NOT NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.config import Settings

revision = "0007_f15"
down_revision = "0006_f14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("locale", existing_type=sa.String(length=8), nullable=True)
    op.execute("UPDATE users SET locale = NULL")


def _default_locale() -> str:
    # The settings the migration runs with (``alembic_config``); a bare Alembic run reads them
    # from the environment like the app.
    config = op.get_context().config
    settings = config.attributes.get("settings") if config is not None else None
    return (settings or Settings()).default_locale


def downgrade() -> None:
    default_locale = _default_locale()
    op.get_bind().execute(
        sa.text("UPDATE users SET locale = :locale WHERE locale IS NULL"),
        {"locale": default_locale},
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("locale", existing_type=sa.String(length=8), nullable=False)
