"""prompt version library

Revision ID: a7d4e5f60b12
Revises: f1c2d3e4a5b6
Create Date: 2026-11-07

网关不渲染 prompt，但必须知道「同一个名字有哪些版本、现在用哪个」：
否则改了提示词还命中旧缓存（答非所问），账单也说不清成本变化是不是换了版本。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d4e5f60b12"
down_revision: str | Sequence[str] | None = "f1c2d3e4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_prompt_name_version"),
    )
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_prompt_templates_name"), ["name"])


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_templates_name"))
    op.drop_table("prompt_templates")