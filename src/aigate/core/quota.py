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

from aigate.models import Project, UsageRecord


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
