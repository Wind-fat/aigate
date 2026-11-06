"""provider configs and per-user project scope

Revision ID: c4a7e5b90f22
Revises: 8c1f2d4a9b31
Create Date: 2026-11-06

Two additions that belong together: the console can now manage upstream vendors
(`provider_configs`), and a developer account can be limited to specific projects
(`console_users.project_scope`).

`project_scope` is JSON with an empty-list default: empty means "everything" for
admin/owner/finance and "nothing" for viewer, decided in code. A nullable column would
have made those two meanings indistinguishable.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4a7e5b90f22"
down_revision: str | Sequence[str] | None = "8c1f2d4a9b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_configs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        # name of the environment variable holding the key — never the key itself
        sa.Column("api_key_env", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("provider_configs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_provider_configs_name"), ["name"], unique=True)

    with op.batch_alter_table("console_users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("project_scope", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("console_users", schema=None) as batch_op:
        batch_op.drop_column("project_scope")

    with op.batch_alter_table("provider_configs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_configs_name"))
    op.drop_table("provider_configs")