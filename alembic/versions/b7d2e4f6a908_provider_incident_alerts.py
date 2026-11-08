"""alerts: provider incidents get a lifecycle (resolved_at) and structured detail

Revision ID: b7d2e4f6a908
Revises: a7d4e5f60b12
Create Date: 2026-11-08

Until now the only thing that ever wrote to `alerts` was the budget check, and a budget
crossing never un-crosses: the row is warn -> critical and stays there. An upstream
outage is the opposite kind of event — it *ends* — so the table needs somewhere to say
so:

* `resolved_at` — when the condition cleared (vendor answered again, or an operator
  closed it by hand). NULL means "still true right now".
* `detail` — the machine-readable evidence (provider, consecutive failures, first
  failure time, last error code, duration) so nobody has to regex `message` later.

`scope_id` widens 32 -> 80 because it now also carries provider names (`ProviderConfig
.name` allows 40 chars). On PostgreSQL the narrow column would have made every long
provider name fail the INSERT — silently, since alert writes are best-effort.

Written with `batch_alter_table` so the same revision works on SQLite (which cannot
ALTER a column in place) and PostgreSQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d2e4f6a908"
down_revision: str | Sequence[str] | None = "a7d4e5f60b12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("detail", sa.JSON(), nullable=True))
        batch_op.alter_column(
            "scope_id",
            existing_type=sa.String(length=32),
            type_=sa.String(length=80),
            existing_nullable=False,
        )


def downgrade() -> None:
    # A scope_id longer than 32 chars cannot survive the narrow column, and truncating it
    # would leave an alert pointing at a provider that does not exist. Refusing is the
    # only honest option; delete those rows by hand if you really need to go back.
    op.execute("DELETE FROM alerts WHERE length(scope_id) > 32")
    op.execute("DELETE FROM alerts WHERE detail IS NOT NULL AND scope_type = 'provider'")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.alter_column(
            "scope_id",
            existing_type=sa.String(length=80),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.drop_column("detail")
        batch_op.drop_column("resolved_at")