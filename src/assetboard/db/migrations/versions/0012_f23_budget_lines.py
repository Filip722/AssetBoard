"""budget lines

Revision ID: 0012_f23
Revises: 0011_f22
Create Date: 2026-10-18 12:00:00.000000

``budget_lines`` (F2.3): the planned value per (workspace, month, category) in positive minor
units. The composite foreign key ``(workspace_id, category_id) -> categories(workspace_id, id)``
keeps every line in its category's workspace. A zero plan has no row (lines are removed).

``workspaces.auto_copy_plan`` (default false) copies the previous month's plan when a month is
first opened; ``month_states.plan_started_at`` records the month's first planned-value write, so
that copy happens at most once per month.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0012_f23"
down_revision = "0011_f22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_lines",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("planned_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "planned_minor > 0 AND planned_minor <= 999999999999999",
            name=op.f("ck_budget_lines_planned_valid"),
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_budget_lines_currency_length")),
        sa.CheckConstraint(
            "month >= '1900-01-01' AND month <= '2199-12-01'",
            name=op.f("ck_budget_lines_month_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_budget_lines_workspace_category",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_budget_lines_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_lines")),
        sa.UniqueConstraint(
            "workspace_id",
            "month",
            "category_id",
            name="uq_budget_lines_workspace_month_category",
        ),
    )
    with op.batch_alter_table("budget_lines", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_budget_lines_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_budget_lines_workspace_category", ["workspace_id", "category_id"], unique=False
        )

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_copy_plan", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    with op.batch_alter_table("month_states", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_started_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("month_states", schema=None) as batch_op:
        batch_op.drop_column("plan_started_at")

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("auto_copy_plan")

    with op.batch_alter_table("budget_lines", schema=None) as batch_op:
        batch_op.drop_index("ix_budget_lines_workspace_category")
        batch_op.drop_index(batch_op.f("ix_budget_lines_workspace_id"))

    op.drop_table("budget_lines")
