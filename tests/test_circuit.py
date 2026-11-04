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


def test_opens_after_threshold():
    breaker = circuit.CircuitBreaker(threshold=2, cooldown_s=10)
    assert breaker.allow("p") == (True, "closed")

    breaker.record_failure("p")
    assert breaker.allow("p") == (True, "closed")

    breaker.record_failure("p")
    allowed, reason = breaker.allow("p")
    assert allowed is False
    assert reason == "circuit_open"


def test_half_open_probe_then_success_closes():
    breaker = circuit.CircuitBreaker(threshold=1, cooldown_s=0)
    breaker.record_failure("p")

    allowed, reason = breaker.allow("p")
    assert allowed is True
    assert reason == "half_open_probe"
    # only one probe at a time
    assert breaker.allow("p")[0] is False

    breaker.record_success("p")
    assert breaker.allow("p") == (True, "closed")


def test_success_resets_failure_count():
    breaker = circuit.CircuitBreaker(threshold=3, cooldown_s=10)
    breaker.record_failure("p")
    breaker.record_failure("p")
    breaker.record_success("p")
    breaker.record_failure("p")
    assert breaker.allow("p")[0] is True
    assert breaker.is_open("p") is False


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
