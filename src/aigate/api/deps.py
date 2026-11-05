"""Shared dependencies: DB session, API-key principal, admin principal."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aigate.config import Settings, get_settings
from aigate.console_auth import COOKIE_NAME, Actor, verify_session
from aigate.db import get_session
from aigate.models import ApiKey, ConsoleUser, Project, Team
from aigate.security import hash_api_key

__all__ = [
    "Principal",
    "Actor",
    "get_session",
    "get_settings",
    "get_principal",
    "get_actor",
    "require_admin",
]


@dataclass
class Principal:
    api_key: ApiKey
    project: Project
    team: Team

    @property
    def allowed_models(self) -> list[str]:
        return list(self.api_key.allowed_models or [])


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.missing_credentials", "message": "Authorization header required"},
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.invalid_auth_scheme", "message": "Expected 'Bearer <api-key>'"},
        )
    return parts[1]


async def get_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    raw = _bearer(authorization)
    stmt = (
        select(ApiKey)
        .where(ApiKey.key_hash == hash_api_key(raw), ApiKey.status == "active")
        .options(selectinload(ApiKey.project).selectinload(Project.team))
    )
    api_key = (await session.execute(stmt)).scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.invalid_key", "message": "API key not found or revoked"},
        )
    project = api_key.project
    if project is None or project.team is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.broken_tenant", "message": "Key is not bound to a team"},
        )
    return Principal(api_key=api_key, project=project, team=project.team)


async def get_actor(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Actor:
    """Resolve who is calling: a machine token or a signed-in human.

    Role enforcement lives here rather than on each endpoint: anything that is not a
    read requires at least `admin`, so a new mutating endpoint cannot forget the check.
    """
    actor: Actor | None = None

    if x_admin_token and x_admin_token == settings.admin_token:
        actor = Actor(name="token:admin", role="owner", kind="token")
    elif session_cookie:
        user_id = verify_session(session_cookie, settings.session_secret)
        if user_id:
            user = (
                await session.execute(select(ConsoleUser).where(ConsoleUser.id == user_id))
            ).scalar_one_or_none()
            if user is not None and user.status == "active":
                actor = Actor(name=user.email, role=user.role, kind="user", user_id=user.id)

    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.login_required", "message": "sign in or send X-Admin-Token"},
        )

    # The audit middleware runs outside the dependency graph; handing it the resolved
    # actor here is what lets audit rows name an email instead of an opaque id.
    request.state.actor = actor

    if request.method not in {"GET", "HEAD", "OPTIONS"} and not actor.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.read_only", "message": f"role '{actor.role}' cannot write"},
        )
    return actor


async def require_admin(actor: Actor = Depends(get_actor)) -> Actor:
    """Backwards-compatible alias used by the control-plane router."""
    return actor
