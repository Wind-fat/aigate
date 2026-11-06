"""Audit trail for the control plane.

What gets recorded: every mutating call under `/admin/v1` and `/console/v1` — key issued,
key revoked, user created, password changed, route edited. Reads are not recorded; a UI
that polls the overview every 10 seconds would otherwise bury the signal.

The actor is resolved here rather than taken from the request handler because middleware
runs outside the dependency graph. It duplicates a few lines of auth logic on purpose:
an audit row that cannot name the actor is worthless, and re-deriving the name from the
same cookie/token keeps the two paths honest.
"""

from __future__ import annotations

import logging
import re

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aigate.config import Settings
from aigate.console_auth import COOKIE_NAME, verify_session
from aigate.models import AuditLog

log = logging.getLogger("aigate.audit")

AUDITED_PREFIXES = ("/admin/v1", "/console/v1")
AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# login/logout are audited too, but a failed login is not a config change — keep it
# in the same table so "who tried to sign in" is answerable.
SKIP_PATHS = {"/console/v1/me"}

# (method, path template, action). `{...}` matches one path segment, so a route that
# takes an id and a route that takes a model name both resolve to a stable action name.
_ACTION_TEMPLATES: list[tuple[str, str, str]] = [
    ("POST", "/admin/v1/keys", "key.create"),
    ("POST", "/admin/v1/keys/{any}/revoke", "key.revoke"),
    ("PUT", "/admin/v1/projects/{any}", "project.update"),
    ("PUT", "/admin/v1/teams/{any}", "team.update"),
    ("POST", "/admin/v1/alerts/{any}/ack", "alert.ack"),
    ("PUT", "/admin/v1/routes/{any}", "route.update"),
    ("POST", "/admin/v1/routes", "route.create"),
    ("PUT", "/admin/v1/providers/{any}", "provider.update"),
    ("POST", "/admin/v1/providers", "provider.create"),
    ("POST", "/admin/v1/providers/{any}/test", "provider.test"),
    ("POST", "/console/v1/login", "auth.login"),
    ("POST", "/console/v1/logout", "auth.logout"),
    ("POST", "/console/v1/users", "user.create"),
    ("POST", "/console/v1/users/{any}/disable", "user.disable"),
    ("POST", "/console/v1/me/password", "user.password"),
]


def _compile(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{[^}]+\}", template)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


_ACTIONS = [(method, _compile(tpl), action) for method, tpl, action in _ACTION_TEMPLATES]


def should_audit(request: Request) -> bool:
    path = request.url.path
    if request.method not in AUDITED_METHODS:
        return False
    if not path.startswith(AUDITED_PREFIXES):
        return False
    return path not in SKIP_PATHS


def resolve_action(method: str, path: str) -> str:
    for action_method, pattern, action in _ACTIONS:
        if action_method == method and pattern.match(path):
            return action
    # fall back to something greppable rather than dropping the event
    return f"{method.lower()} {path}"


def resolve_actor(request: Request, settings: Settings) -> tuple[str, str]:
    """Best-effort (name, role) without touching the database."""
    token = request.headers.get("x-admin-token")
    if token and token == settings.admin_token:
        return ("token:admin", "owner")

    cookie = request.cookies.get(COOKIE_NAME)
    user_id = verify_session(cookie, settings.session_secret) if cookie else None
    if user_id:
        # The email is filled in by the writer (it has a session); until then the id is
        # still enough to correlate. Handlers set `request.state.actor_name` when known.
        return (f"user:{user_id}", "user")

    return ("anonymous", "")


async def write_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    role: str,
    action: str,
    request: Request,
    status_code: int,
) -> None:
    request_id = getattr(request.state, "request_id", "")
    client_ip = request.client.host if request.client else ""
    try:
        async with session_factory() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    actor_role=role,
                    action=action,
                    target=request.url.path,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    ip=client_ip,
                    request_id=request_id,
                    detail={"query": dict(request.query_params)},
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - auditing must never break the request
        log.exception("failed to write audit row for %s %s", request.method, request.url.path)
