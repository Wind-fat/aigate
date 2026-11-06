"""Token-bucket rate limiting + monthly budget guard.

Two implementations behind one interface, same as the database story:
  memory -- single process, zero infra (dev, tests, CI)
  redis  -- shared state across replicas (production)

The Lua script exists because read-modify-write across two round trips lets two
concurrent requests both see "1 token left" and both spend it.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.models import Project, Team, UsageRecord


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int
    backend: str


class RateLimiter(ABC):
    name: str = "base"

    @abstractmethod
    async def hit(self, key: str, limit: int, window_s: int = 60) -> RateLimitResult: ...

    async def aclose(self) -> None:  # pragma: no cover
        return None


class InMemoryTokenBucket(RateLimiter):
    name = "memory"

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_s: int = 60) -> RateLimitResult:
        now = time.monotonic()
        async with self._lock:
            tokens, ts = self._buckets.get(key, (float(limit), now))
            elapsed = max(0.0, now - ts)
            tokens = min(float(limit), tokens + elapsed * limit / window_s)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            retry_after = 0 if allowed else max(1, int((1.0 - tokens) * window_s / limit))
            return RateLimitResult(allowed, limit, int(tokens), retry_after, self.name)


_LUA = """
local capacity = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * capacity / window)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(window * 2))
return {allowed, math.floor(tokens), math.ceil((capacity - tokens) * window / capacity)}
"""


class RedisTokenBucket(RateLimiter):
    name = "redis"

    def __init__(self, url: str) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("redis backend requires `uv sync --extra redis`") from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(_LUA)

    async def hit(self, key: str, limit: int, window_s: int = 60) -> RateLimitResult:
        allowed, remaining, retry_after = await self._script(
            keys=[f"aigate:rl:{key}"], args=[limit, window_s, 1]
        )
        return RateLimitResult(bool(allowed), limit, int(remaining), max(0, int(retry_after)), self.name)

    async def aclose(self) -> None:
        await self._redis.aclose()


_limiter: RateLimiter | None = None


def build_limiter(backend: str, redis_url: str = "") -> RateLimiter:
    if backend == "redis":
        return RedisTokenBucket(redis_url)
    return InMemoryTokenBucket()


def get_limiter(backend: str = "memory", redis_url: str = "") -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = build_limiter(backend, redis_url)
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_spend(session: AsyncSession, project_id: str) -> Decimal:
    stmt = select(func.coalesce(func.sum(UsageRecord.cost_cny), 0)).where(
        UsageRecord.project_id == project_id,
        UsageRecord.created_at >= month_start(),
    )
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def check_budget(session: AsyncSession, project: Project) -> tuple[bool, Decimal, Decimal | None]:
    """Returns (within_budget, spent, limit). No limit configured -> always within."""
    if project.monthly_budget_cny is None:
        return True, Decimal("0"), None
    spent = await month_spend(session, project.id)
    return spent < Decimal(str(project.monthly_budget_cny)), spent, Decimal(str(project.monthly_budget_cny))


def day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def spend_since(session: AsyncSession, since: datetime, **filters: str) -> Decimal:
    stmt = select(func.coalesce(func.sum(UsageRecord.cost_cny), 0)).where(UsageRecord.created_at >= since)
    if "project_id" in filters:
        stmt = stmt.where(UsageRecord.project_id == filters["project_id"])
    if "team_id" in filters:
        stmt = stmt.where(UsageRecord.team_id == filters["team_id"])
    return Decimal(str((await session.execute(stmt)).scalar_one()))


def _money(value: Decimal) -> str:
    """Two decimals for real money, six for the sub-yuan caps used in tests and trials.

    `¥0.0000` next to a 103537% warning is how a dashboard loses trust.
    """
    return f"¥{value:.2f}" if abs(value) >= Decimal("1") else f"¥{value:.6f}"


@dataclass
class BudgetCheck:
    """The outcome of walking the budget ladder (key -> project -> team, day -> month)."""

    allowed: bool = True
    scope: str = ""
    scope_id: str = ""
    scope_slug: str = ""
    metric: str = ""  # budget_daily | budget_monthly
    spent: Decimal = Decimal("0")
    limit: Decimal | None = None
    period_key: str = ""

    @property
    def ratio(self) -> float:
        if not self.limit:
            return 0.0
        return float(self.spent / self.limit)


async def check_budgets(session: AsyncSession, project: Project, team: Team) -> BudgetCheck:
    """Check daily then monthly, project then team. First breach wins.

    Order matters for the message the caller sees: "your project's daily cap" is actionable
    by the developer; "the department's monthly cap" requires going to whoever owns the
    budget. Cheapest-to-act-on first.
    """
    now = datetime.now(UTC)
    checks: list[tuple[str, str, str, str, Decimal | None, str]] = [
        # (scope_type, scope_id, scope_slug, metric, limit, period_key)
        (
            "project",
            project.id,
            project.slug,
            "budget_daily",
            Decimal(str(project.daily_budget_cny)) if project.daily_budget_cny is not None else None,
            now.date().isoformat(),
        ),
        (
            "project",
            project.id,
            project.slug,
            "budget_monthly",
            Decimal(str(project.monthly_budget_cny)) if project.monthly_budget_cny is not None else None,
            month_start(now).date().isoformat()[:7],
        ),
        (
            "team",
            team.id,
            team.slug,
            "budget_monthly",
            Decimal(str(team.monthly_budget_cny)) if team.monthly_budget_cny is not None else None,
            month_start(now).date().isoformat()[:7],
        ),
    ]

    for scope_type, scope_id, slug, metric, limit, period_key in checks:
        if limit is None:
            continue
        since = day_start(now) if metric == "budget_daily" else month_start(now)
        kwargs = {"project_id": project.id} if scope_type == "project" else {"team_id": team.id}
        spent = await spend_since(session, since, **kwargs)
        if spent >= limit:
            return BudgetCheck(
                allowed=False,
                scope=scope_type,
                scope_id=scope_id,
                scope_slug=slug,
                metric=metric,
                spent=spent,
                limit=limit,
                period_key=period_key,
            )
    return BudgetCheck()


async def raise_alert_if_needed(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
    scope_slug: str,
    metric: str,
    spent: Decimal,
    limit: Decimal,
    period_key: str,
) -> str | None:
    """Write a warn (>=80%) or critical (>=100%) row, once per period.

    The unique index does the deduplication; a conflict means "already warned", which is
    exactly the case we want to ignore. Never raises: an alert failure must not fail the
    request that triggered it.
    """
    if not limit or limit <= 0:
        return None
    ratio = spent / limit
    if ratio < Decimal("0.8"):
        return None
    level = "critical" if ratio >= Decimal("1") else "warn"
    period = period_key
    try:
        from sqlalchemy.exc import IntegrityError

        from aigate.models import Alert

        session.add(
            Alert(
                level=level,
                scope_type=scope_type,
                scope_id=scope_id,
                scope_slug=scope_slug,
                metric=metric,
                period_key=period,
                value=spent,
                limit_value=limit,
                message=(
                    f"{scope_type} {scope_slug} {metric} at {ratio * 100:.0f}% "
                    f"({_money(spent)} / {_money(limit)})"
                ),
            )
        )
        await session.commit()
        return level
    except IntegrityError:
        # Already warned this period. If it has now crossed 100%, escalate the existing
        # row instead of silently dropping the critical signal (and un-ack it, because a
        # "we handled it at 80%" ack must not swallow "we are over budget").
        await session.rollback()
        try:
            from sqlalchemy import select as _select

            from aigate.models import Alert as _Alert

            existing = (
                await session.execute(
                    _select(_Alert).where(
                        _Alert.scope_type == scope_type,
                        _Alert.scope_id == scope_id,
                        _Alert.metric == metric,
                        _Alert.period_key == period,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and existing.level == "warn" and level == "critical":
                existing.level = "critical"
                existing.value = spent
                existing.limit_value = limit
                existing.message = (
                    f"{scope_type} {scope_slug} {metric} at {ratio * 100:.0f}% "
                    f"({_money(spent)} / {_money(limit)})"
                )
                existing.acknowledged_at = None
                await session.commit()
                return "critical"
        except Exception:  # noqa: BLE001
            await session.rollback()
        return None
    except Exception:  # noqa: BLE001 - alerts are best-effort
        await session.rollback()
        return None
