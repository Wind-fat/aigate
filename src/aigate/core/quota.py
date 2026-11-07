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

from aigate import timeutil
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
        # protocol=2（RESP2）是刻意的：redis-py 8 默认用 RESP3，连接时先发 `HELLO 3`，
        # 而 Windows 上能拿到的 Redis 5.0.x 不认识这个命令，表现为每个请求 500
        # 「unknown command HELLO」。RESP2 在所有版本（5/6/7）上都成立，我们也没用到
        # RESP3 的推送特性。
        self._redis = Redis.from_url(url, decode_responses=True, protocol=2)
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


def month_start(now: datetime | None = None, tz_name: str | None = None) -> datetime:
    """本地自然月起点（UTC 瞬时）。默认 Asia/Shanghai：按 UTC 切会让"每月 1 号重置"
    实际发生在北京时间 8 点，对账时永远差一截。"""
    from aigate import timeutil

    return timeutil.month_start(tz_name, now=now)


async def month_spend(session: AsyncSession, project_id: str, tz_name: str | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(UsageRecord.cost_cny), 0)).where(
        UsageRecord.project_id == project_id,
        UsageRecord.created_at >= month_start(tz_name=tz_name),
    )
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def check_budget(
    session: AsyncSession, project: Project, tz_name: str | None = None
) -> tuple[bool, Decimal, Decimal | None]:
    """Returns (within_budget, spent, limit). No limit configured -> always within."""
    if project.monthly_budget_cny is None:
        return True, Decimal("0"), None
    spent = await month_spend(session, project.id, tz_name=tz_name)
    return spent < Decimal(str(project.monthly_budget_cny)), spent, Decimal(str(project.monthly_budget_cny))


def day_start(now: datetime | None = None, tz_name: str | None = None) -> datetime:
    from aigate import timeutil

    return timeutil.day_start(tz_name, now=now)


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


async def check_budgets(
    session: AsyncSession, project: Project, team: Team, tz_name: str | None = None
) -> BudgetCheck:
    """Check daily then monthly, project then team. First breach wins.

    Order matters for the message the caller sees: "your project's daily cap" is actionable
    by the developer; "the department's monthly cap" requires going to whoever owns the
    budget. Cheapest-to-act-on first.
    """
    now = datetime.now(UTC)
    # 账期键与花费窗口必须是同一把尺子：都用 AIGATE_TIMEZONE 的本地自然日/月。
    # 用 UTC 日期做 period_key 会让北京时间 00:00-08:00 的「新一天超支」撞上前一天的
    # 唯一索引，被当成重复告警静默吞掉（Q1 D1）。
    checks: list[tuple[str, str, str, str, Decimal | None, str]] = [
        # (scope_type, scope_id, scope_slug, metric, limit, period_key)
        (
            "project",
            project.id,
            project.slug,
            "budget_daily",
            Decimal(str(project.daily_budget_cny)) if project.daily_budget_cny is not None else None,
            timeutil.local_date(now, tz_name),
        ),
        (
            "project",
            project.id,
            project.slug,
            "budget_monthly",
            Decimal(str(project.monthly_budget_cny)) if project.monthly_budget_cny is not None else None,
            timeutil.local_date(month_start(now, tz_name), tz_name)[:7],
        ),
        (
            "team",
            team.id,
            team.slug,
            "budget_monthly",
            Decimal(str(team.monthly_budget_cny)) if team.monthly_budget_cny is not None else None,
            timeutil.local_date(month_start(now, tz_name), tz_name)[:7],
        ),
    ]

    for scope_type, scope_id, slug, metric, limit, period_key in checks:
        if limit is None:
            continue
        since = day_start(now, tz_name) if metric == "budget_daily" else month_start(now, tz_name)
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

    The insert runs inside a SAVEPOINT, and that is load-bearing: a unique-key conflict
    here is the *normal* path (every request in the warn band after the first one), so
    settling it with `session.rollback()` is not allowed. `rollback()` ignores
    `expire_on_commit=False` and expires every persistent object in the session — the
    caller's `api_key` / `project` / `team` (loaded by deps.py on this same session)
    included. The endpoint then reads `principal.project.id` synchronously
    (v1_chat.py cache.lookup / `_persist`), which becomes sync IO inside an AsyncSession:
    MissingGreenlet. Non-streaming -> 500; streaming -> client gets the full stream and
    `[DONE]` while the usage row never lands (silent unbilled traffic). Rolling back to
    the savepoint discards only the row added inside it; the outer transaction and the
    identity map stay intact.

    Deduplication semantics are unchanged: still the unique index, still not a
    read-then-write (that duplicates under concurrency).
    """
    if not limit or limit <= 0:
        return None
    ratio = spent / limit
    if ratio < Decimal("0.8"):
        return None
    level = "critical" if ratio >= Decimal("1") else "warn"
    period = period_key
    message = (
        f"{scope_type} {scope_slug} {metric} at {ratio * 100:.0f}% "
        f"({_money(spent)} / {_money(limit)})"
    )
    try:
        from sqlalchemy.exc import IntegrityError

        from aigate.models import Alert

        try:
            async with session.begin_nested():
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
                        message=message,
                    )
                )
            await session.commit()
            return level
        except IntegrityError:
            # Already warned this period. The savepoint is already rolled back, so the
            # session is usable and nothing was expired: do NOT roll the request session
            # back. If the ratio has now crossed 100%, escalate the existing row instead
            # of silently dropping the critical signal (and un-ack it, because a
            # "we handled it at 80%" ack must not swallow "we are over budget").
            pass

        # Close the outer transaction, with commit rather than rollback: on SQLite the
        # SAVEPOINT opened a real transaction, and "rollback to savepoint" leaves it — and
        # its write lock — open, which stalls the metering writer's own session
        # (`INSERT INTO usage_records` -> "database is locked", i.e. still lost billing).
        # commit releases the lock and, thanks to expire_on_commit=False, expires nothing.
        await session.commit()

        existing = (
            await session.execute(
                select(Alert).where(
                    Alert.scope_type == scope_type,
                    Alert.scope_id == scope_id,
                    Alert.metric == metric,
                    Alert.period_key == period,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.level == "warn" and level == "critical":
            existing.level = "critical"
            existing.value = spent
            existing.limit_value = limit
            existing.message = message
            existing.acknowledged_at = None
            await session.commit()
            return "critical"
        return None
    except Exception:  # noqa: BLE001 - alerts are best-effort
        # Only a session whose transaction actually failed needs the full rollback; a
        # healthy one must be left alone, otherwise we re-introduce the expiring bug
        # this function exists to avoid.
        if not session.is_active:
            await session.rollback()
        return None
