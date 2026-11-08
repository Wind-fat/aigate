"""Test harness: fresh SQLite file per test, real ASGI app, mock providers only.

`mock` is the primary vendor, `mock-alt` the fallback vendor — two names so the
circuit breaker (keyed by provider) can be exercised honestly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aigate import metrics
from aigate.console_auth import hash_password
from aigate.core import cache as cache_mod
from aigate.core import circuit, incidents, quota
from aigate.db import dispose_engine, get_engine, get_session_factory, init_db, set_engine
from aigate.main import create_app
from aigate.metering_queue import configure_writer, reset_writer
from aigate.models import (
    ApiKey,
    Base,
    ConsoleUser,
    ModelPrice,
    Project,
    RoutePolicy,
    Team,
)
from aigate.providers import get_provider, reset_providers
from aigate.security import hash_api_key

TEST_KEY_PLAIN = "sk-ag-test-000000000000000000000000"
RESTRICTED_KEY_PLAIN = "sk-ag-restricted-000000000000000000"
OWNER_PASSWORD = "correct-horse-battery"
VIEWER_PASSWORD = "read-only-password"
FINANCE_PASSWORD = "finance-password-1"
ADMIN_PASSWORD = "plain-admin-password"


@pytest.fixture(autouse=True)
async def _reset_singletons():
    """Limiter and breaker are process singletons; stale state across tests = flaky CI."""
    reset_providers()
    quota.reset_limiter()
    circuit.configure_breaker(threshold=3, cooldown_s=30.0, backend="memory")
    # 上游故障的聚合窗口也是进程级单例：不重置就会带着上一个用例的连续失败数进来。
    incidents.reset_state()
    cache_mod.reset_cache()
    cache_mod.get_cache(cache_mod.build_cache(enabled=True))
    reset_writer()
    yield
    quota.reset_limiter()
    await circuit.get_breaker().reset()
    cache_mod.reset_cache()
    reset_providers()


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    """SQLite by default; `AIGATE_TEST_DB_URL` points the same suite at a real server.

    PostgreSQL is a different animal (real ALTER, real concurrency, no `PRAGMA`), so the
    only honest way to claim "works on PG" is to run the same tests against one. When the
    env var is set the schema is dropped and recreated per test, because rows would
    otherwise leak across tests through the shared database.
    """
    override = os.environ.get("AIGATE_TEST_DB_URL", "").strip()
    if override:
        set_engine(override)
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    else:
        set_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await init_db()
    metrics.reset()
    # Not started on purpose: submit() then writes inline, so a test can assert on the
    # database immediately after the response. The queue path has its own test file.
    configure_writer(get_session_factory())

    async with get_session_factory()() as session:
        team = Team(name="Test Team", slug="test-team")
        session.add(team)
        await session.flush()

        project = Project(
            team_id=team.id,
            name="Test Project",
            slug="test-project",
            monthly_budget_cny=Decimal("100"),
            rpm_limit=60,
        )
        session.add(project)
        await session.flush()

        session.add_all(
            [
                ApiKey(
                    project_id=project.id,
                    name="full-access",
                    prefix=TEST_KEY_PLAIN[:14],
                    key_hash=hash_api_key(TEST_KEY_PLAIN),
                    allowed_models=[],
                ),
                ApiKey(
                    project_id=project.id,
                    name="restricted",
                    prefix="sk-ag-restrict",
                    key_hash=hash_api_key(RESTRICTED_KEY_PLAIN),
                    allowed_models=["chat-default"],
                ),
            ]
        )

        session.add_all(
            [
                ConsoleUser(
                    email="owner@example.com",
                    name="Owner",
                    password_hash=hash_password(OWNER_PASSWORD),
                    role="owner",
                ),
                ConsoleUser(
                    email="admin@example.com",
                    name="Admin",
                    password_hash=hash_password(ADMIN_PASSWORD),
                    role="admin",
                ),
                ConsoleUser(
                    email="viewer@example.com",
                    name="Viewer",
                    password_hash=hash_password(VIEWER_PASSWORD),
                    role="viewer",
                    project_scope=[project.id],
                ),
                ConsoleUser(
                    email="finance@example.com",
                    name="Finance",
                    password_hash=hash_password(FINANCE_PASSWORD),
                    role="finance",
                ),
                ModelPrice(
                    provider="mock",
                    model="mock-ok",
                    input_price_per_1k=Decimal("0.001"),
                    output_price_per_1k=Decimal("0.002"),
                ),
                ModelPrice(
                    provider="dashscope",
                    model="qwen-plus",
                    input_price_per_1k=Decimal("0.0008"),
                    output_price_per_1k=Decimal("0.002"),
                ),
                ModelPrice(
                    provider="mock-alt",
                    model="mock-ok",
                    input_price_per_1k=Decimal("0.001"),
                    output_price_per_1k=Decimal("0.002"),
                ),
            ]
        )
        session.add_all(
            [
                RoutePolicy(
                    name="default",
                    match_model="chat-default",
                    candidates=[{"provider": "mock", "model": "mock-ok"}],
                    fallback=[],
                ),
                RoutePolicy(
                    name="stream",
                    match_model="chat-stream",
                    candidates=[{"provider": "mock", "model": "mock-ok"}],
                    fallback=[],
                ),
                RoutePolicy(
                    name="failover",
                    match_model="failover-demo",
                    candidates=[{"provider": "mock", "model": "fail500-primary"}],
                    fallback=[{"provider": "mock-alt", "model": "mock-ok"}],
                ),
                RoutePolicy(
                    name="bad-request",
                    match_model="bad-request",
                    candidates=[{"provider": "mock", "model": "badrequest-model"}],
                    fallback=[{"provider": "mock-alt", "model": "mock-ok"}],
                ),
                RoutePolicy(
                    name="stream-fail",
                    match_model="stream-fail",
                    candidates=[{"provider": "mock", "model": "fail500-stream"}],
                    fallback=[{"provider": "mock-alt", "model": "mock-ok"}],
                ),
            ]
        )
        await session.commit()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac

    reset_writer()
    await dispose_engine()
    reset_providers()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_KEY_PLAIN}"}


@pytest.fixture
def restricted_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RESTRICTED_KEY_PLAIN}"}


@pytest.fixture
def chat_payload() -> dict:
    return {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "你好，请介绍一下你自己"}],
        "temperature": 0,
    }


@pytest.fixture
def mock_provider():
    return get_provider("mock")


@pytest.fixture
def mock_alt_provider():
    return get_provider("mock-alt")
