"""key ownership and lifecycle

Revision ID: e1a2b3c4d5e6
Revises: d9b3c1f47e08
Create Date: 2026-11-06

Why these columns exist, in the order the questions get asked at 02:00:

  created_by / created_by_email  -> "who issued this key"
  description                    -> "what is it for, which service"
  environment                    -> "is this running in prod"
  expires_at                     -> "when does it stop working"
  revoked_reason / revoked_by     -> "why was it killed, and by whom"

All of them nullable/defaulted: existing rows keep working, and a key with no expiry is
still a valid key — it is just one nobody will ever rotate.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "d9b3c1f47e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("created_by", sa.String(length=32), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("created_by_email", sa.String(length=160), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("description", sa.String(length=200), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("environment", sa.String(length=12), nullable=False, server_default="dev")
        )
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("revoked_reason", sa.String(length=200), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("revoked_by", sa.String(length=160), nullable=False, server_default="")
        )
        batch_op.create_index(batch_op.f("ix_api_keys_expires_at"), ["expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_expires_at"))
        batch_op.drop_column("revoked_by")
        batch_op.drop_column("revoked_reason")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("environment")
        batch_op.drop_column("description")
        batch_op.drop_column("created_by_email")
        batch_op.drop_column("created_by")