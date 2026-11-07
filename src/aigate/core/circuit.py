"""Per-provider circuit breaker — with a shared backend.

Why a breaker on top of fallback: fallback alone retries a dead vendor on every single
request. The breaker stops calling it after N consecutive failures and probes again after
a cooldown, so recovery is automatic but cheap.

**Why the backend matters.** State used to live in the process. With N replicas each one
had its own opinion about whether DashScope was down, so:

  - the effective failure threshold became `N × threshold`;
  - a vendor one replica had already written off kept getting hammered by the others —
    the exact traffic the breaker exists to stop;
  - "is it open right now?" had N different answers, which makes the console lie.

Two backends, one interface:

  memory  -- single process (dev, CI, a one-replica install)
  redis   -- shared: what you use as soon as there are two replicas

All methods are async because the Redis path is; the memory path never suspends.

Trade-off, written down on purpose: the Redis path uses plain HGETALL/HSET instead of a Lua
script, so two replicas recording a failure in the same millisecond can lose one increment.
That is fine here — the breaker is a heuristic that fires at 3 consecutive failures, not a
ledger. (The rate limiter *is* a ledger, which is why it uses Lua and stays atomic.)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probing: bool = False
    total_opened: int = 0


class CircuitBreaker(ABC):
    """(allowed, reason) where reason is 'closed' | 'circuit_open' | 'half_open_probe'."""

    name = "base"

    def __init__(self, threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s

    @abstractmethod
    async def allow(self, provider: str) -> tuple[bool, str]: ...

    @abstractmethod
    async def record_success(self, provider: str) -> None: ...

    @abstractmethod
    async def record_failure(self, provider: str) -> None: ...

    @abstractmethod
    async def snapshot(self) -> dict[str, dict]: ...

    @abstractmethod
    async def reset(self) -> None: ...

    async def is_open(self, provider: str) -> bool:  # noqa: D401 - convenience predicate
        allowed, reason = await self.allow(provider)
        return not allowed and reason == "circuit_open"


class MemoryCircuitBreaker(CircuitBreaker):
    name = "memory"

    def __init__(self, threshold: int = 3, cooldown_s: float = 30.0) -> None:
        super().__init__(threshold, cooldown_s)
        self._states: dict[str, _State] = {}

    def _state(self, provider: str) -> _State:
        return self._states.setdefault(provider, _State())

    async def allow(self, provider: str) -> tuple[bool, str]:
        st = self._state(provider)
        if st.opened_at is None:
            return True, "closed"
        if time.monotonic() - st.opened_at >= self.cooldown_s:
            if not st.probing:
                st.probing = True
                return True, "half_open_probe"
            return False, "circuit_open"
        return False, "circuit_open"

    async def record_success(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures = 0
        st.opened_at = None
        st.probing = False

    async def record_failure(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures += 1
        st.probing = False
        if st.consecutive_failures >= self.threshold and st.opened_at is None:
            st.opened_at = time.monotonic()
            st.total_opened += 1

    async def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "open": st.opened_at is not None,
                "consecutive_failures": st.consecutive_failures,
                "opened_times": st.total_opened,
            }
            for name, st in self._states.items()
        }

    async def reset(self) -> None:
        self._states.clear()


class RedisCircuitBreaker(CircuitBreaker):
    """Shared state: one opinion about a dead vendor, not N.

    Keys: `aigate:cb:{provider}`        hash {failures, opened_at, opens}
          `aigate:cb:probe:{provider}`  short-lived lock for the half-open probe
    """

    name = "redis"
    PREFIX = "aigate:cb:"

    def __init__(
        self, url: str, threshold: int = 3, cooldown_s: float = 30.0, client: Any = None
    ) -> None:
        super().__init__(threshold, cooldown_s)
        if client is not None:  # injected (tests)
            self._redis = client
            return
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - deployment error, not logic
            raise RuntimeError("redis backend requires `uv sync --extra redis`") from exc
        # 同 quota：RESP2，见那边的注释（Redis 5 不认识 HELLO 3）。
        self._redis = Redis.from_url(url, decode_responses=True, protocol=2)

    def _key(self, provider: str) -> str:
        return f"{self.PREFIX}{provider}"

    async def allow(self, provider: str) -> tuple[bool, str]:
        data = await self._redis.hgetall(self._key(provider))
        opened_at = data.get("opened_at")
        if not opened_at:
            return True, "closed"
        if time.time() - float(opened_at) < self.cooldown_s:
            return False, "circuit_open"
        # Cooldown elapsed: let exactly one caller through as the probe. SET NX is the
        # cheapest cross-replica mutex there is, and it expires by itself.
        got = await self._redis.set(
            f"{self.PREFIX}probe:{provider}", "1", nx=True, ex=max(1, int(self.cooldown_s))
        )
        return (True, "half_open_probe") if got else (False, "circuit_open")

    async def record_success(self, provider: str) -> None:
        await self._redis.delete(self._key(provider), f"{self.PREFIX}probe:{provider}")

    async def record_failure(self, provider: str) -> None:
        key = self._key(provider)
        failures = await self._redis.hincrby(key, "failures", 1)
        if failures >= self.threshold and not await self._redis.hexists(key, "opened_at"):
            await self._redis.hset(key, "opened_at", time.time())
            await self._redis.hincrby(key, "opens", 1)
        # State must outlive a cooldown, or a dead vendor looks healthy again simply
        # because nobody called it for a minute.
        await self._redis.expire(key, int(self.cooldown_s * 10))

    async def snapshot(self) -> dict[str, dict]:
        out: dict[str, dict[str, Any]] = {}
        async for key in self._redis.scan_iter(match=f"{self.PREFIX}*"):
            if ":probe:" in key:
                continue
            data = await self._redis.hgetall(key)
            out[key.removeprefix(self.PREFIX)] = {
                "open": bool(data.get("opened_at")),
                "consecutive_failures": int(data.get("failures", 0) or 0),
                "opened_times": int(data.get("opens", 0) or 0),
            }
        return out

    async def reset(self) -> None:
        keys = [k async for k in self._redis.scan_iter(match=f"{self.PREFIX}*")]
        if keys:
            await self._redis.delete(*keys)


_breaker: CircuitBreaker = MemoryCircuitBreaker()


def build_breaker(
    backend: str = "memory",
    redis_url: str = "",
    threshold: int = 3,
    cooldown_s: float = 30.0,
    client: Any = None,
) -> CircuitBreaker:
    if backend == "redis":
        return RedisCircuitBreaker(redis_url, threshold, cooldown_s, client=client)
    return MemoryCircuitBreaker(threshold, cooldown_s)


def get_breaker() -> CircuitBreaker:
    return _breaker


def configure_breaker(
    threshold: int = 3,
    cooldown_s: float = 30.0,
    backend: str = "memory",
    redis_url: str = "",
    client: Any = None,
) -> CircuitBreaker:
    global _breaker
    _breaker = build_breaker(backend, redis_url, threshold, cooldown_s, client=client)
    return _breaker


def reset_to_memory() -> CircuitBreaker:
    """Fallback when Redis is unreachable at startup: better a local breaker than none."""
    global _breaker
    _breaker = MemoryCircuitBreaker()
    return _breaker