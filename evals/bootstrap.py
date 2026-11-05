"""Install the routing fixtures the eval datasets need but seed.py does not ship.

``bad-request`` (a non-retryable 4xx candidate -> 502 ``upstream_4xx``) exists
only in ``tests/conftest.py`` today. Evals run against a real server, so the
route has to live in the database; this script inserts it idempotently. It never
touches ``src/`` or ``scripts/`` — it is test-fixture setup, kept under evals/.

    AIGATE_DATABASE_URL="sqlite+aiosqlite:///./eval_tmp.db" \
        uv run python evals/bootstrap.py

Safe to run repeatedly and against a fresh DB (it calls init_db first).
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import dispose_engine, get_session_factory, init_db, set_engine
from aigate.models import RoutePolicy

FIXTURES = [
    {
        "name": "eval bad request",
        "match_model": "bad-request",
        "strategy": "static",
        "candidates": [{"provider": "mock", "model": "badrequest-model"}],
        "fallback": [{"provider": "mock-alt", "model": "mock-ok"}],
    },
]


async def install(database_url: str) -> None:
    set_engine(database_url)
    await init_db()
    created: list[str] = []
    existing: list[str] = []

    async with get_session_factory()() as session:
        for fixture in FIXTURES:
            found = (
                await session.execute(
                    select(RoutePolicy).where(RoutePolicy.match_model == fixture["match_model"])
                )
            ).scalar_one_or_none()
            if found is not None:
                existing.append(fixture["match_model"])
                continue
            session.add(RoutePolicy(**fixture))
            created.append(fixture["match_model"])
        await session.commit()

    print(f"database: {database_url}")
    print(f"created : {created or '-'}")
    print(f"existing: {existing or '-'}")


async def _run(database_url: str) -> None:
    try:
        await install(database_url)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Install idempotent eval routing fixtures.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="defaults to AIGATE_DATABASE_URL / settings.database_url",
    )
    args = parser.parse_args()
    database_url = args.database_url or get_settings().database_url
    asyncio.run(_run(database_url))


if __name__ == "__main__":
    main()