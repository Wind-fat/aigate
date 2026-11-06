"""provider secrets: ciphertext at rest, hint for humans

Revision ID: f1c2d3e4a5b6
Revises: e1a2b3c4d5e6
Create Date: 2026-11-07

Until now `provider_configs` only named the environment variable that holds a vendor key
(`api_key_env`). That is defensible for a public repo demo and unusable for an internal
team: the person who just got a key has no way to put it in without editing .env and
restarting, so the console permanently shows "未配置密钥".

These four columns let the operator paste a key in the console. The key is stored
encrypted (Fernet, master key from AIGATE_SECRET_KEY); `api_key_hint` keeps the last four
characters so a human can tell *which* key it is without being able to use it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1c2d3e4a5b6"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("provider_configs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("api_key_cipher", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("api_key_hint", sa.String(length=20), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("api_key_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("api_key_updated_by", sa.String(length=160), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("provider_configs", schema=None) as batch_op:
        batch_op.drop_column("api_key_updated_by")
        batch_op.drop_column("api_key_updated_at")
        batch_op.drop_column("api_key_hint")
        batch_op.drop_column("api_key_cipher")