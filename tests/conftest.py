"""Test harness: fresh SQLite file per test, real ASGI app, mock provider only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aigate import metrics
from aigate.db import dispose_engine, get_session_factory, init_db, set_engine
from aigate.main import create_app
from aigate.models import ApiKey, ModelPrice, Project, RoutePolicy, Team
from aigate.providers import reset_providers
from aigate.security import hash_api_key

TEST_KEY_PLAIN = "sk-ag-test-000000000000000000000000"
RESTRICTED_KEY_PLAIN = "sk-ag-restricted-000000000000000000"


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    set_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await init_db()
    metrics.reset()
    reset_providers()

    async with get_session_factory()() as session:
        team = Team(name="Test Team", slug="test-team")
        session.add(team)
        await session.flush()

        project = Project(
            team_id=team.id,
            name="Test Project",
            slug="test-project",
            monthly_budget_cny=Decimal("100"),
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

        session.add(
            ModelPrice(
                provider="mock",
                model="mock-ok",
                input_price_per_1k=Decimal("0.001"),
                output_price_per_1k=Decimal("0.002"),
            )
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
                    name="failover",
                    match_model="failover-demo",
                    candidates=[{"provider": "mock", "model": "fail500-primary"}],
                    fallback=[{"provider": "mock", "model": "mock-ok"}],
                ),
                RoutePolicy(
                    name="bad-request",
                    match_model="bad-request",
                    candidates=[{"provider": "mock", "model": "badrequest-model"}],
                    fallback=[{"provider": "mock", "model": "mock-ok"}],
                ),
            ]
        )
        await session.commit()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac

    await dispose_engine()


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