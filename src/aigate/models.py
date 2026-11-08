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

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """Normalise a datetime coming back from the database.

    SQLite (dev/CI) returns naive datetimes even for `DateTime(timezone=True)` columns,
    PostgreSQL returns aware ones. Comparing the two raises `TypeError`, and the failure
    shows up as a 500 on whichever endpoint happened to read the column first — which is
    how this bit us: `api_keys.expires_at` and `created_at` both go through here.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    # 0 = unlimited. A team cap is *shared* by all its projects — that is the point of
    # having it: one noisy project must not eat the whole department's headroom.
    rpm_limit: Mapped[int] = mapped_column(Integer, default=0)
    monthly_budget_cny: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
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
    # A daily cap catches "the loop ran all night" before it becomes a monthly incident.
    daily_budget_cny: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
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
    # 0 = inherit the project limit. A per-key cap exists so one leaked key cannot use
    # the whole project's allowance.
    rpm_limit: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- ownership and lifecycle ------------------------------------------------
    # Who issued it. Stored as email as well as id because "who asked for this key"
    # has to be answerable after the account is deleted — an id alone becomes a dead end.
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_by_email: Mapped[str] = mapped_column(String(160), default="")
    # Free text: which service, which environment, who to call at 02:00.
    description: Mapped[str] = mapped_column(String(200), default="")
    # dev | staging | prod. Not cosmetic: it drives the "no prod key without expiry"
    # check and the default warning level on the console.
    environment: Mapped[str] = mapped_column(String(12), default="dev")
    # NULL = never expires. A key with no expiry is a key nobody will ever rotate.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(200), default="")
    revoked_by: Mapped[str] = mapped_column(String(160), default="")

    project: Mapped[Project] = relationship()


class PromptTemplate(Base):
    """一个 prompt 的版本库。

    网关本身不该管 prompt 的内容，但它必须知道「同一个名字有过哪些版本、现在用的是哪个」——
    否则缓存会被悄悄串味（改了提示词却还命中旧答案），账单也说不清「这次多花的钱是不是
    换了 v2」。所以这里只存文本 + 版本号 + 是否启用，不提供模板渲染能力。
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_name_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 带有归属：同一个名字在不同项目里可以各自演进
    name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(20), default="v1")
    content: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(String(200), default="")
    # 同一 name 下最多一个 active；切换时把兄弟版本置 False（用代码保证，不建部分索引，
    # SQLite 与 PG 的部分索引语法不一致，为了两边都能跑）
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # Shadow candidates: called in the background after the real response is returned,
    # recorded but never shown to the caller. Used to compare a new model against the one
    # that currently serves traffic before switching.
    shadow: Mapped[list] = mapped_column(JSON, default=list)
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
    # Which prompt template produced this call. Cache keys and cost reviews are per version.
    prompt_version: Mapped[str] = mapped_column(String(40), default="", index=True)
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
    # Only written when AIGATE_TRACE_PROMPT_MODE=raw. Default is NULL on purpose: the
    # masked text is what left the building, and it is the only version we keep by default.
    prompt_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    pii_masked: Mapped[int] = mapped_column(Integer, default=0)
    stage_timings: Mapped[dict] = mapped_column(JSON, default=dict)
    route_path: Mapped[list] = mapped_column(JSON, default=list)
    http_status: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SemanticCacheEntry(Base):
    """Semantic cache row.

    `embedding` is JSON instead of a pgvector column on purpose: the demo and CI run on
    SQLite. Brute-force cosine over a few thousand short queries is sub-millisecond, and
    swapping in pgvector later only touches the search function (ADR-0005).
    """

    __tablename__ = "semantic_cache_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(80), index=True)
    prompt_version: Mapped[str] = mapped_column(String(20), default="v1")
    prompt_hash: Mapped[str] = mapped_column(String(64), index=True)
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(40), default="")
    model_used: Mapped[str] = mapped_column(String(80), default="")
    response_content: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_cache_lookup",
            "project_id",
            "model",
            "prompt_version",
            "prompt_hash",
            unique=True,
        ),
    )


class ConsoleUser(Base):
    """A person who can open the console.

    Deliberately separate from API keys: keys are for machines, users are for humans,
    and audit rows need to name a human. `role` is coarse on purpose — a local team
    tool needs owner/admin/viewer, not a permission matrix.
    """

    __tablename__ = "console_users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="admin")  # owner|admin|finance|viewer
    # Data scope for viewer/finance: the project ids this person may see. Empty list means
    # "everything", which only owner/admin may have — enforced when the user is created.
    project_scope: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Who did what, when, from where. Append-only; never updated or deleted by the app."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(190), index=True)  # email or `token:<name>`
    actor_role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(60), index=True)  # e.g. key.create
    target: Mapped[str] = mapped_column(String(190), default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(40), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProviderConfig(Base):
    """One upstream vendor as the operator sees it: 通义千问 / DeepSeek / 自建 vLLM / OpenAI.

    密钥有两个来源，按优先级：`api_key_cipher`（控制台里录入，Fernet 加密）→
    `api_key_env` 指向的环境变量。前者是为了让运维**能自己把 key 填进去**：
    只认环境变量的那版里，刚申请下来的 key 必须改 .env 再重启，控制台上永远显示
    「未配置密钥」。

    落库的只有密文：单看数据库文件（或它的备份）解不开，主密钥在进程环境里。
    读接口只返回末四位，明文只在写入那一次和发往上游的那个 HTTP 头里出现。
    `api_key_env` 保留下来，是因为很多团队本来就是用 K8s Secret 注入环境变量的，
    两条路都得能走。
    """

    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # dashscope, deepseek...
    label: Mapped[str] = mapped_column(String(80), default="")  # 通义千问 (DashScope)
    base_url: Mapped[str] = mapped_column(String(255), default="")
    api_key_env: Mapped[str] = mapped_column(String(80), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Fernet 密文。空 = 没有在控制台录入，回退到 api_key_env。
    api_key_cipher: Mapped[str] = mapped_column(Text, default="")
    # 末四位，用于"这把是哪把"的人工核对；不是密钥本身。
    api_key_hint: Mapped[str] = mapped_column(String(20), default="")
    api_key_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    api_key_updated_by: Mapped[str] = mapped_column(String(160), default="")


class Alert(Base):
    """One row per (scope, metric, period) — the 80% warning and the 100% stop.

    Deduplicated by a unique index rather than by application logic: the check runs on
    every request, and "insert if not exists" in Python races under concurrency.

    Two producers, one table, deliberately:

    * budget (`quota.raise_alert_if_needed`) — `scope_type` is project/team, `period_key`
      is the billing period, the row is never resolved (a budget crossing does not
      un-cross itself);
    * upstream outage (`core.incidents`) — `scope_type="provider"`, `period_key="open"`
      while the vendor is down, `closed:<id>` once it recovers, `resolved_at` set.

    `acknowledged_at` and `resolved_at` are different questions and must stay separate:
    ack means "a human looked at it", resolved means "the condition is gone". Merging
    them makes a 03:00 outage that fixed itself indistinguishable from one nobody read.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_dedupe", "scope_type", "scope_id", "metric", "period_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    level: Mapped[str] = mapped_column(String(10), default="warn")  # warn|critical
    scope_type: Mapped[str] = mapped_column(String(10), default="project")  # team|project|provider
    # 80 no longer only project ids: provider names are up to 40 chars (ProviderConfig.name).
    scope_id: Mapped[str] = mapped_column(String(80), index=True)
    scope_slug: Mapped[str] = mapped_column(String(80), default="")
    metric: Mapped[str] = mapped_column(String(30))  # budget_daily|budget_monthly|rpm|upstream_failure
    period_key: Mapped[str] = mapped_column(String(20))  # 2026-11 | 2026-11-06 | open | closed:<id>
    value: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    limit_value: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    message: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = the condition is still true. Set by the producer when it clears (upstream
    # recovered) or by an operator (`POST /alerts/{id}/resolve`).
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Machine-readable evidence next to the human sentence in `message`: provider name,
    # consecutive failure count, first failure time, last error code, duration. Parsing
    # fields back out of `message` with a regex is a bug waiting for a wording change.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class ShadowRun(Base):
    """A background call to a candidate that is NOT serving traffic.

    The point of shadow traffic is to answer "would the new model have done better?"
    using real production prompts, without the caller ever seeing a different answer.
    Stored separately from `usage_records`: shadow spend is real money but is not the
    customer's bill, and mixing the two makes every cost report wrong by the shadow rate.
    """

    __tablename__ = "shadow_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(40), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    route: Mapped[str] = mapped_column(String(80), default="")  # virtual model under test
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    output_chars: Mapped[int] = mapped_column(Integer, default=0)
    output_preview: Mapped[str] = mapped_column(Text, default="")
    # Cheap agreement proxy: 0..1 character-level similarity against the primary answer.
    # Not a judge — a smoke signal that says "these two answers are wildly different,
    # go read them".
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
