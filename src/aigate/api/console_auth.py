"""Console account endpoints: login, logout, whoami, user management.

Login is rate-limited through the same token bucket the gateway uses for traffic — a
password form without a throttle is the cheapest way to get brute-forced, and reusing the
limiter means one mental model instead of two.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.api.deps import Actor, get_actor, get_session
from aigate.config import Settings, get_settings
from aigate.console_auth import (
    CAPABILITIES,
    COOKIE_NAME,
    ROLE_ORDER,
    hash_password,
    sign_session,
    verify_password,
)
from aigate.core import quota
from aigate.models import ConsoleUser, utcnow

router = APIRouter(prefix="/console/v1", tags=["console-auth"])

MIN_PASSWORD = 10


def _clean_email(value: str) -> str:
    """Deliberately not pydantic's EmailStr.

    EmailStr rejects special-use domains (`.local`, `.internal`, `.test`), which is
    exactly what internal tools use. A local team tool should not require a public TLD
    to let a colleague sign in; it should require something that looks like an address
    and is unique.
    """
    email = (value or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 190:
        raise ValueError("enter a valid email address")
    return email


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=190)
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return _clean_email(value)


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=190)
    name: str = ""
    password: str = Field(min_length=MIN_PASSWORD)
    role: str = "admin"
    # Which projects this person may see. Meaningful for viewer/finance; empty means
    # "everything" for admin/owner and "nothing" for viewer (fail closed).
    project_scope: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return _clean_email(value)


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=MIN_PASSWORD)


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    project_scope: list[str] = []
    capabilities: list[str] = []
    role: str
    status: str


def _out(user: ConsoleUser) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
        project_scope=list(user.project_scope or []),
        capabilities=sorted(CAPABILITIES.get(user.role, set())),
    )


@router.post("/login")
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    limiter = quota.get_limiter(settings.rate_limiter, settings.redis_url)
    attempt = await limiter.hit(f"login:{client_ip}:{payload.email}", 10, 300)
    if not attempt.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "gateway.too_many_login_attempts", "message": "try again later"},
            headers={"Retry-After": str(attempt.retry_after_s)},
        )

    user = (
        await session.execute(select(ConsoleUser).where(ConsoleUser.email == payload.email))
    ).scalar_one_or_none()

    # Same code path and message whether the user exists or the password is wrong:
    # a distinct "no such user" reply is a free account-enumeration oracle.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.invalid_credentials", "message": "email or password is wrong"},
        )
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.user_disabled", "message": "account disabled"},
        )

    token = sign_session(user.id, settings.session_secret, settings.session_ttl_hours * 3600)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        # secure=True would be right behind HTTPS; a local http:// install would then
        # never be able to sign in, so it is opt-in via AIGATE_COOKIE_SECURE.
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )

    user.last_login_at = utcnow()
    await session.commit()
    return {"user": _out(user).model_dump(), "expires_in": settings.session_ttl_hours * 3600}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(actor: Actor = Depends(get_actor)) -> dict:
    return actor.as_dict()


@router.get("/users")
async def list_users(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Who else is in the org is not a developer's business.
    if not actor.can("members:read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.forbidden", "message": f"role '{actor.role}' cannot list members"},
        )
    rows = (await session.execute(select(ConsoleUser).order_by(ConsoleUser.created_at))).scalars().all()
    return {"items": [_out(u).model_dump() for u in rows]}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserIn,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if payload.role not in ROLE_ORDER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "gateway.bad_role", "message": f"role must be one of {list(ROLE_ORDER)}"},
        )
    # Only an owner may mint another owner — otherwise an admin can silently escalate.
    if payload.role == "owner" and actor.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.forbidden", "message": "only an owner can create owners"},
        )

    exists = (
        await session.execute(select(ConsoleUser).where(ConsoleUser.email == payload.email))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "gateway.user_exists", "message": payload.email},
        )

    if payload.role == "viewer" and not payload.project_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway.scope_required",
                "message": "a viewer account needs at least one project (otherwise it sees nothing)",
            },
        )

    user = ConsoleUser(
        email=payload.email,
        name=payload.name or payload.email.split("@")[0],
        password_hash=hash_password(payload.password),
        role=payload.role,
        project_scope=list(payload.project_scope),
    )
    session.add(user)
    await session.commit()
    return _out(user).model_dump()


@router.post("/me/password")
async def change_password(
    payload: PasswordIn,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if actor.kind != "user" or not actor.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "gateway.not_a_user", "message": "token actors cannot change passwords"},
        )
    user = (
        await session.execute(select(ConsoleUser).where(ConsoleUser.id == actor.user_id))
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.current, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "gateway.invalid_credentials", "message": "current password is wrong"},
        )
    user.password_hash = hash_password(payload.new)
    await session.commit()
    return {"ok": True}


@router.post("/users/{user_id}/disable")
async def disable_user(
    user_id: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if actor.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "gateway.forbidden", "message": "owner role required"},
        )
    user = (await session.execute(select(ConsoleUser).where(ConsoleUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.user_not_found", "message": user_id},
        )
    user.status = "disabled"
    await session.commit()
    return {"id": user.id, "status": user.status}
