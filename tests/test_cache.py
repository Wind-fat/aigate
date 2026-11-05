"""Semantic cache: eligibility, L1/L2 behaviour, tenant isolation, and the two
things that must never be cached (non-deterministic requests, degraded answers)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from aigate.core.cache import SemanticCache, build_cache, get_cache
from aigate.core.embedding import HashEmbedder, cosine
from aigate.db import get_session_factory
from aigate.models import Project, SemanticCacheEntry, UsageRecord

QUESTION = "你好，今天天气怎么样"
PARAPHRASE = "你好，今天天气如何"
UNRELATED = "请帮我写一份季度销售报告"


def _payload(**over):
    class P:
        pass

    p = P()
    p.stream = over.get("stream", False)
    p.temperature = over.get("temperature", 0)
    p.tools = over.get("tools", None)

    class M:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    p.messages = over.get("messages", [M("user", QUESTION)])
    return p


async def _ask(client, headers, content, model="chat-default", **extra):
    # temperature defaults to 1.0 in the OpenAI schema, and temperature=1 is explicitly
    # not cacheable — so cache tests must opt in, exactly like a real client would.
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        **extra,
    }
    return await client.post("/v1/chat/completions", json=body, headers=headers)


async def _entries() -> list[SemanticCacheEntry]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(SemanticCacheEntry))).scalars().all())


async def _usage() -> list[UsageRecord]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(UsageRecord))).scalars().all())


# ------------------------------------------------------------------- embedder


async def test_paraphrase_is_far_closer_than_unrelated():
    emb = HashEmbedder()
    q = await emb.embed(QUESTION)
    p = await emb.embed(PARAPHRASE)
    u = await emb.embed(UNRELATED)

    near = cosine(q, p)
    far = cosine(q, u)
    assert near > emb.recommended_threshold > far * 5
    assert far < 0.2


# ---------------------------------------------------------------- eligibility


def test_eligible_only_for_deterministic_single_shot():
    cache = build_cache()
    assert cache.eligible(_payload())
    assert not cache.eligible(_payload(stream=True))
    assert not cache.eligible(_payload(temperature=0.7))
    assert not cache.eligible(_payload(tools=[{"type": "function"}]))

    class M:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    assert not cache.eligible(_payload(messages=[M("user", "hi"), M("assistant", "hello")]))


# ------------------------------------------------------------------ L1 / L2 e2e


async def test_second_identical_call_is_served_from_l1(client, auth_headers, mock_provider):
    first = await _ask(client, auth_headers, QUESTION)
    assert first.status_code == 200
    assert first.json()["x_aigate"]["cache_hit"] is None
    calls = mock_provider.calls

    second = await _ask(client, auth_headers, QUESTION)
    body = second.json()
    assert body["x_aigate"]["cache_hit"] == "L1"
    assert body["x_aigate"]["cost_cny"] == 0.0
    assert body["x_aigate"]["saved_cny"] > 0
    assert body["choices"][0]["message"]["content"] == first.json()["choices"][0]["message"]["content"]

    assert mock_provider.calls == calls, "cache hit must not touch the provider"

    rows = await _usage()
    assert rows[-1].status == "cached"
    assert rows[-1].cache_hit == "L1"
    assert rows[-1].cost_cny == Decimal("0")


async def test_paraphrase_is_served_from_l2(client, auth_headers, mock_provider):
    await _ask(client, auth_headers, QUESTION)
    calls = mock_provider.calls

    resp = await _ask(client, auth_headers, PARAPHRASE)
    meta = resp.json()["x_aigate"]
    assert meta["cache_hit"] == "L2"
    assert 0.65 <= meta["cache_similarity"] <= 1.0
    assert meta["cost_cny"] == 0.0
    assert mock_provider.calls == calls


async def test_unrelated_question_is_not_served_from_cache(client, auth_headers, mock_provider):
    await _ask(client, auth_headers, QUESTION)
    calls = mock_provider.calls

    resp = await _ask(client, auth_headers, UNRELATED)
    assert resp.json()["x_aigate"]["cache_hit"] is None
    assert mock_provider.calls == calls + 1


async def test_temperature_nonzero_bypasses_cache(client, auth_headers, mock_provider):
    await _ask(client, auth_headers, QUESTION, temperature=0)
    calls = mock_provider.calls
    resp = await _ask(client, auth_headers, QUESTION, temperature=0.7)
    assert resp.json()["x_aigate"]["cache_hit"] is None
    assert mock_provider.calls == calls + 1


async def test_streaming_bypasses_cache(client, auth_headers):
    payload = {
        "model": "chat-stream",
        "stream": True,
        "temperature": 0,
        "messages": [{"role": "user", "content": QUESTION}],
    }
    async with client.stream("POST", "/v1/chat/completions", json=payload, headers=auth_headers) as resp:
        await resp.aread()
    assert await _entries() == []


async def test_degraded_answer_is_not_cached(client, auth_headers):
    body = {"model": "failover-demo", "messages": [{"role": "user", "content": "hi"}]}
    resp = await client.post("/v1/chat/completions", json=body, headers=auth_headers)
    assert resp.json()["x_aigate"]["degraded"] is True
    assert await _entries() == [], "a fallback answer must not be pinned for the whole TTL"


# ------------------------------------------------------------------- isolation


async def test_cache_is_isolated_per_tenant(client, auth_headers):
    await _ask(client, auth_headers, QUESTION)
    entries = await _entries()
    assert len(entries) == 1

    async with get_session_factory()() as session:
        other = Project(team_id=entries[0].project_id, name="Other", slug="other-project")
        session.add(other)
        await session.commit()
        other_id = other.id

    cache = get_cache()
    async with get_session_factory()() as session:
        hit = await cache.lookup(session, other_id, "chat-default", _payload().messages)
    assert hit is None, "one tenant's cache must never serve another tenant"


def test_threshold_follows_embedder():
    cache = SemanticCache(embedder=HashEmbedder())
    assert cache.threshold == HashEmbedder.recommended_threshold
    explicit = SemanticCache(threshold=0.99, embedder=HashEmbedder())
    assert explicit.threshold == 0.99
