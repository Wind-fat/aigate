"""Circuit breaker: stop calling a dead vendor instead of paying for the retry on
every single request."""

from __future__ import annotations

from sqlalchemy import select

from aigate.core import circuit
from aigate.db import get_session_factory
from aigate.models import RequestTrace

FAILOVER = {
    "model": "failover-demo",
    "messages": [{"role": "user", "content": "hi"}],
    "temperature": 0,
}


# --------------------------------------------------------------------------- unit


async def test_opens_after_threshold():
    breaker = circuit.MemoryCircuitBreaker(threshold=2, cooldown_s=10)
    assert await breaker.allow("p") == (True, "closed")

    await breaker.record_failure("p")
    assert await breaker.allow("p") == (True, "closed")

    await breaker.record_failure("p")
    allowed, reason = await breaker.allow("p")
    assert allowed is False
    assert reason == "circuit_open"


async def test_half_open_probe_then_success_closes():
    breaker = circuit.MemoryCircuitBreaker(threshold=1, cooldown_s=0)
    await breaker.record_failure("p")

    allowed, reason = await breaker.allow("p")
    assert allowed is True
    assert reason == "half_open_probe"
    # only one probe at a time
    assert (await breaker.allow("p"))[0] is False

    await breaker.record_success("p")
    assert await breaker.allow("p") == (True, "closed")


async def test_success_resets_failure_count():
    breaker = circuit.MemoryCircuitBreaker(threshold=3, cooldown_s=10)
    await breaker.record_failure("p")
    await breaker.record_failure("p")
    await breaker.record_success("p")
    await breaker.record_failure("p")
    assert (await breaker.allow("p"))[0] is True
    assert await breaker.is_open("p") is False


# --------------------------------------------------------------------- endpoint


async def test_open_circuit_stops_calling_the_dead_vendor(
    client, auth_headers, mock_provider, mock_alt_provider
):
    circuit.configure_breaker(threshold=1, cooldown_s=60)

    first = await client.post("/v1/chat/completions", json=FAILOVER, headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["x_aigate"]["degraded"] is True
    assert mock_provider.calls == 1
    primary_calls = mock_provider.calls
    alt_calls = mock_alt_provider.calls

    second = await client.post("/v1/chat/completions", json=FAILOVER, headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["x_aigate"]["degraded"] is True
    assert mock_provider.calls == primary_calls, "dead vendor must not be called again"
    assert mock_alt_provider.calls == alt_calls + 1

    async with get_session_factory()() as session:
        traces = (await session.execute(select(RequestTrace))).scalars().all()

    skipped = [t for t in traces if t.route_path and t.route_path[0].get("skipped")]
    assert skipped, "second request should record a skipped candidate"
    assert skipped[0].route_path[0]["skipped"] == "circuit_open"


# ------------------------------------------------------------------- shared (redis)


class FakeRedis:
    """只实现熔断器用到的命令。用假实现而不是真 Redis：CI 里没有 redis-server，
    而这段逻辑（阈值、冷却、半开只放一个）本身与 Redis 无关。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttl: dict[str, int] = {}
        self.sets: dict[str, str] = {}

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = str(value)

    async def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + amount)
        return int(bucket[field])

    async def hexists(self, key, field):
        return field in self.hashes.get(key, {})

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)
            self.sets.pop(key, None)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.sets:
            return None
        self.sets[key] = value
        return True

    async def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.hashes) + list(self.sets):
            if key.startswith(prefix):
                yield key


async def test_redis_breaker_shares_the_verdict_between_replicas():
    """两个「副本」共用一个 Redis：A 判定的死亡，B 立刻也看得到。"""
    redis = FakeRedis()
    replica_a = circuit.RedisCircuitBreaker("redis://x", threshold=2, cooldown_s=60, client=redis)
    replica_b = circuit.RedisCircuitBreaker("redis://x", threshold=2, cooldown_s=60, client=redis)

    assert (await replica_a.allow("dashscope"))[0] is True
    await replica_a.record_failure("dashscope")
    await replica_a.record_failure("dashscope")

    assert (await replica_a.allow("dashscope"))[1] == "circuit_open"
    # 关键：B 自己的计数是 0，但它必须立刻同意 A
    assert (await replica_b.allow("dashscope"))[1] == "circuit_open"


async def test_redis_breaker_lets_exactly_one_probe_through():
    redis = FakeRedis()
    a = circuit.RedisCircuitBreaker("redis://x", threshold=1, cooldown_s=0, client=redis)
    b = circuit.RedisCircuitBreaker("redis://x", threshold=1, cooldown_s=0, client=redis)
    await a.record_failure("p")

    first = await a.allow("p")
    second = await b.allow("p")
    assert first[1] == "half_open_probe"
    assert second[0] is False, "冷却结束也只能放一个探测请求进去"


async def test_redis_breaker_success_clears_the_shared_state():
    redis = FakeRedis()
    a = circuit.RedisCircuitBreaker("redis://x", threshold=1, cooldown_s=0, client=redis)
    await a.record_failure("p")
    await a.record_success("p")
    assert redis.hashes.get("aigate:cb:p") is None
    assert (await a.allow("p"))[1] == "closed"


async def test_redis_breaker_snapshot_and_reset():
    redis = FakeRedis()
    breaker = circuit.RedisCircuitBreaker("redis://x", threshold=1, cooldown_s=30, client=redis)
    await breaker.record_failure("deepseek")
    snap = await breaker.snapshot()
    assert snap["deepseek"]["open"] is True
    await breaker.reset()
    assert await breaker.snapshot() == {}
