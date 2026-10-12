"""proxy session auth method

Revision ID: 0006_f14
Revises: 0005_f13
Create Date: 2026-10-12 20:00:00.000000

Only the CHECK constraint on ``user_sessions.auth_method`` changes. Proxy identities use the
existing ``external_identities`` table with ``provider = 'proxy'``. The downgrade deletes proxy
sessions first, since the old constraint does not allow them.
"""

from __future__ import annotations

from alembic import op

revision = "0006_f14"
down_revision = "0005_f13"
branch_labels = None
depends_on = None


def _replace_check(expression: str) -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_user_sessions_auth_method_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_user_sessions_auth_method_valid"), expression)


def upgrade() -> None:
    _replace_check("auth_method IN ('password', 'oidc', 'proxy')")


def downgrade() -> None:
    op.execute("DELETE FROM user_sessions WHERE auth_method = 'proxy'")
    _replace_check("auth_method IN ('password', 'oidc')")
