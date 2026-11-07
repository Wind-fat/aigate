"""Redis 后端：连接参数本身就是兼容性契约。

这条测试来自一个真实故障：`redis-py 8` 默认用 RESP3，连接时先发 `HELLO 3`；
Windows 上能拿到的 Redis 5.0.x 不认识这个命令，于是**每个请求 500**
（`unknown command 'HELLO'`）。而 Windows 上又装不了更新的 Redis。

我们并不需要 RESP3 的推送特性，所以固定 `protocol=2` —— 5/6/7 都成立。
没有 redis-server 也能测这一条：把 `from_url` 换成一个记录参数的假函数即可。
"""

from __future__ import annotations

import pytest
import redis.asyncio

from aigate.core import circuit, quota


@pytest.fixture
def captured(monkeypatch):
    seen: list[dict] = []

    class _Stub:
        """只满足构造期调用的最小接口（register_script），不做任何 IO。"""

        def register_script(self, _lua):
            return lambda *a, **kw: None

    def fake_from_url(url: str, **kwargs):
        seen.append({"url": url, **kwargs})
        return _Stub()

    monkeypatch.setattr(
        redis.asyncio.Redis,
        "from_url",
        classmethod(lambda cls, url, **kw: fake_from_url(url, **kw)),
    )
    return seen


def test_limiter_connects_with_resp2(captured):
    quota.RedisTokenBucket("redis://127.0.0.1:6379/0")
    assert captured, "应该用 from_url 建连接"
    assert captured[0]["protocol"] == 2, "RESP3 会让 Redis 5 直接拒绝所有命令"
    assert captured[0]["decode_responses"] is True


def test_breaker_connects_with_resp2(captured):
    circuit.RedisCircuitBreaker("redis://127.0.0.1:6379/0", threshold=3, cooldown_s=30)
    assert captured[0]["protocol"] == 2


def test_breaker_backend_follows_explicit_setting():
    """main.py 用 circuit_backend 或 rate_limiter 决定后端；这里验证工厂本身。"""
    memory = circuit.build_breaker("memory", "", threshold=2, cooldown_s=5)
    assert memory.name == "memory"
    assert memory.threshold == 2

    shared = circuit.build_breaker("redis", "redis://x", threshold=4, cooldown_s=9, client=object())
    assert shared.name == "redis"
    assert shared.cooldown_s == 9