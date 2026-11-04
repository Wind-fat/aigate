"""Shared dependencies: DB session, API-key principal, admin principal."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aigate.config import Settings, get_settings
from aigate.db import get_session
from aigate.models import ApiKey, Project, Team
from aigate.security import hash_api_key

__all__ = ["Principal", "get_session", "get_settings", "get_principal", "require_admin"]


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


async def require_admin(
    x_admin_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.invalid_admin_token", "message": "X-Admin-Token required"},
        )
    return "admin"