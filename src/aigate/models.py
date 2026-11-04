"""ORM models — the tables that carry the whole product story.

teams / projects / api_keys      -> tenancy + credential
model_prices / route_policies    -> routing + billing config
usage_records                    -> the bill (cannot be wrong)
request_traces                   -> the diagnostic trail
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list[Project]] = relationship(back_populates="team")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="active")
    # NULL = unlimited.  Hard limit: exhausted budget returns 429, never a silent 200.
    monthly_budget_cny: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    # Requests per minute for this project's keys.
    rpm_limit: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    team: Mapped[Team] = relationship(back_populates="projects")

    __table_args__ = (Index("ix_projects_team_slug", "team_id", "slug", unique=True),)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(20), index=True)  # displayable: sk-ag-ab12cd
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Empty list = inherit project policy (all enabled models allowed).
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship()


class ModelPrice(Base):
    """Price table is configuration, not code — changing it must not rewrite history."""

    __tablename__ = "model_prices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(80), index=True)
    input_price_per_1k: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    output_price_per_1k: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    context_window: Mapped[int] = mapped_column(Integer, default=8192)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_model_prices_provider_model", "provider", "model", unique=True),)


class RoutePolicy(Base):
    """match_model is the virtual model name business code asks for."""

    __tablename__ = "route_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80))
    match_model: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    strategy: Mapped[str] = mapped_column(String(20), default="static")  # static|cost_first|latency_first
    candidates: Mapped[list] = mapped_column(JSON, default=list)  # [{"provider","model"}]
    fallback: Mapped[list] = mapped_column(JSON, default=list)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=20000)
    max_retries: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class UsageRecord(Base):
    """One row per upstream call. The accounting source of truth."""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(40), index=True)
    team_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    api_key_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model_requested: Mapped[str] = mapped_column(String(80))
    model_used: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|fallback|error
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cache_hit: Mapped[str | None] = mapped_column(String(8), nullable=True)  # None|L1|L2
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RequestTrace(Base):
    """Diagnostic record. Prompt is masked by default — safety first, not convenience."""

    __tablename__ = "request_traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    model_requested: Mapped[str] = mapped_column(String(80))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    pii_masked: Mapped[int] = mapped_column(Integer, default=0)
    stage_timings: Mapped[dict] = mapped_column(JSON, default=dict)
    route_path: Mapped[list] = mapped_column(JSON, default=list)
    http_status: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
