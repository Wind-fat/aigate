"""quotas per level, daily budgets, alerts, prompt versions, shadow traffic

Revision ID: d9b3c1f47e08
Revises: c4a7e5b90f22
Create Date: 2026-11-06

One migration for the whole "production hardening" batch, because they ship together:

  teams           + rpm_limit, monthly_budget_cny      (department-level caps)
  projects        + daily_budget_cny                   (the loop-ran-overnight guard)
  api_keys        + rpm_limit                          (a leaked key must not eat the project)
  usage_records   + prompt_version                     (cost and cache are per version)
  request_traces  + prompt_raw, prompt_version         (raw prompt is opt-in only)
  route_policies  + shadow                             (A/B against the live model)
  alerts          new table, unique per (scope, metric, period)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9b3c1f47e08"
down_revision: str | Sequence[str] | None = "c4a7e5b90f22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rpm_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monthly_budget_cny", sa.Numeric(14, 6), nullable=True))

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("daily_budget_cny", sa.Numeric(14, 6), nullable=True))

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rpm_limit", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("prompt_version", sa.String(length=40), nullable=False, server_default="")
        )
        batch_op.create_index(batch_op.f("ix_usage_records_prompt_version"), ["prompt_version"])

    with op.batch_alter_table("request_traces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prompt_raw", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("prompt_version", sa.String(length=40), nullable=False, server_default="")
        )

    with op.batch_alter_table("route_policies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("shadow", sa.JSON(), nullable=True))

    op.create_table(
        "alerts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("scope_type", sa.String(length=10), nullable=False),
        sa.Column("scope_id", sa.String(length=32), nullable=False),
        sa.Column("scope_slug", sa.String(length=80), nullable=False),
        sa.Column("metric", sa.String(length=30), nullable=False),
        sa.Column("period_key", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Numeric(14, 6), nullable=False),
        sa.Column("limit_value", sa.Numeric(14, 6), nullable=False),
        sa.Column("message", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "shadow_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("route", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.Numeric(14, 6), nullable=False),
        sa.Column("output_chars", sa.Integer(), nullable=False),
        sa.Column("output_preview", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("shadow_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_shadow_runs_request_id"), ["request_id"])
        batch_op.create_index(batch_op.f("ix_shadow_runs_project_id"), ["project_id"])
        batch_op.create_index(batch_op.f("ix_shadow_runs_created_at"), ["created_at"])

    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_alerts_scope_id"), ["scope_id"])
        batch_op.create_index(batch_op.f("ix_alerts_created_at"), ["created_at"])
        batch_op.create_index(
            "ix_alerts_dedupe", ["scope_type", "scope_id", "metric", "period_key"], unique=True
        )


def downgrade() -> None:
    # Note: this used to start by re-creating `shadow_runs` (a copy-paste of the upgrade
    # block), which made `alembic downgrade base` fail with "table shadow_runs already
    # exists" — caught by tests/test_migrations.py, not by any runtime test.
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_index("ix_alerts_dedupe")
        batch_op.drop_index(batch_op.f("ix_alerts_created_at"))
        batch_op.drop_index(batch_op.f("ix_alerts_scope_id"))
    op.drop_table("alerts")

    with op.batch_alter_table("shadow_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shadow_runs_created_at"))
        batch_op.drop_index(batch_op.f("ix_shadow_runs_project_id"))
        batch_op.drop_index(batch_op.f("ix_shadow_runs_request_id"))
    op.drop_table("shadow_runs")

    with op.batch_alter_table("route_policies", schema=None) as batch_op:
        batch_op.drop_column("shadow")

    with op.batch_alter_table("request_traces", schema=None) as batch_op:
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("prompt_raw")

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_records_prompt_version"))
        batch_op.drop_column("prompt_version")

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_column("rpm_limit")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("daily_budget_cny")

    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.drop_column("monthly_budget_cny")
        batch_op.drop_column("rpm_limit")
