"""Console authentication: human accounts, signed session cookies, coarse roles.

Three deliberate choices:

1. **scrypt from the standard library**, not argon2/bcrypt. One less dependency to
   compile on Windows, and scrypt with sane parameters is a fine password KDF.
2. **Stateless signed cookie**, not a session table. The cookie carries `uid:exp` plus an
   HMAC; revocation happens by checking `status` on each request. No session GC, no
   Redis, and a restart does not log everyone out.
3. **Two ways in**: a human session cookie, or the machine `X-Admin-Token` for scripts and
   CI. Both resolve to an `Actor`, so audit rows can name whoever did the thing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

COOKIE_NAME = "aigate_session"
# Four roles, ordered. `finance` sits between viewer and admin: it may read money and
# budgets but must never see prompts, keys or routes.
ROLE_ORDER = {"viewer": 1, "finance": 2, "admin": 3, "owner": 4}

# What each role may reach. Kept as data so the console nav and the API agree, instead of
# both hardcoding a chain of `if role == ...`.
CAPABILITIES: dict[str, set[str]] = {
    "viewer": {"dev", "usage:own", "trace:own", "keys:own", "models:read"},
    "finance": {"costs", "usage:all", "export", "projects:read", "models:read"},
    "admin": {
        "costs",
        "usage:all",
        "trace:all",
        "export",
        "projects:read",
        "projects:write",
        "keys:read",
        "keys:write",
        "routes:read",
        "routes:write",
        "providers:read",
        "providers:write",
        "members:read",
        "members:write",
        "audit:read",
        "models:read",
        "dev",
    },
    "owner": {
        "costs",
        "usage:all",
        "trace:all",
        "export",
        "projects:read",
        "projects:write",
        "keys:read",
        "keys:write",
        "routes:read",
        "routes:write",
        "providers:read",
        "providers:write",
        "members:read",
        "members:write",
        "members:owner",
        "audit:read",
        "models:read",
        "dev",
    },
}

# scrypt parameters: ~50ms on a laptop. Enough to make offline cracking expensive,
# cheap enough that a login form does not feel slow.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    parts = [
        "scrypt",
        str(_SCRYPT_N),
        str(_SCRYPT_R),
        str(_SCRYPT_P),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ]
    return "$".join(parts)


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------- sessions
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def sign_session(user_id: str, secret: str, ttl_s: int = 12 * 3600) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + ttl_s}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def verify_session(token: str | None, secret: str) -> str | None:
    """Return the user id, or None when the cookie is missing/forged/expired."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return str(payload.get("uid") or "") or None


# ------------------------------------------------------------------------------ actor
@dataclass(slots=True)
class Actor:
    """Whoever is making the request. Humans and machine tokens share this shape."""

    name: str
    role: str
    kind: str  # "user" | "token"
    user_id: str | None = None
    # Projects this person may see. Empty means "everything", which only admin/owner get:
    # a developer account is supposed to see their own project and nothing else.
    project_scope: list[str] = field(default_factory=list)

    @property
    def can_write(self) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER["admin"]

    def can(self, capability: str) -> bool:
        return capability in CAPABILITIES.get(self.role, set())

    @property
    def scoped_projects(self) -> list[str] | None:
        """`None` = unscoped (sees every project); a list = only these projects.

        Finance is unscoped on purpose: a cost report that silently omits some projects is
        worse than no report. What finance cannot do is reach prompts, keys or routes —
        those are separate capabilities, and failing one of them is a 403, not a filter.
        """
        if self.role in ("owner", "admin", "finance"):
            return None
        return list(self.project_scope or [])

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "user_id": self.user_id,
            "project_scope": list(self.project_scope or []),
            "capabilities": sorted(CAPABILITIES.get(self.role, set())),
        }
