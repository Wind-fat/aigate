"""供应商配置（Providers）：把「来源」变成可管理的数据，而不是藏在 env 里的字符串。

三条设计约束，都是为了「不出事」：

1. **密钥不落库**。`provider_configs` 存 base_url / 显示名 / 模型 / 启用开关，以及
   「密钥在哪个环境变量里」（`api_key_env`）。SQLite 文件里放厂商 key，等于每个备份
   都是一次凭据泄露；轮换密钥也不该是数据库写操作。
2. **运行时读库，改完即生效**。网关原本在启动时从 settings 读死 base_url，运营改不了。
   这里做一层 5 秒 TTL 的缓存，写操作立即失效。
3. **测试连通是真的打一次**。`POST /admin/v1/providers/{name}/test` 会用当前配置发
   一条 1 token 的请求，把 200 / 401 / timeout 原样带回来——"配置好了"必须能被证明。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.config import Settings
from aigate.models import ProviderConfig, utcnow
from aigate.secrets import SecretBox, SecretError, hint

TTL_S = 5.0

# (name, label, base_url, api_key_env) — the vendors this gateway ships knowing about.
# A row here is only a *starting point*; the operator edits it in the console.
SEEDS: list[tuple[str, str, str, str]] = [
    (
        "dashscope",
        "通义千问（DashScope）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "AIGATE_DASHSCOPE_API_KEY",
    ),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "AIGATE_DEEPSEEK_API_KEY"),
    ("openai", "OpenAI", "https://api.openai.com/v1", "AIGATE_OPENAI_API_KEY"),
    ("vllm", "自建 vLLM", "http://127.0.0.1:8000/v1", "AIGATE_VLLM_API_KEY"),
    ("mock", "离线 Mock（不发网络请求）", "", ""),
    # 第二个离线实例：熔断与降级按 provider 名计数，主备同名就演示不出"换一家"。
    ("mock-alt", "离线 Mock · 备用实例", "", ""),
]

_cache: dict[str, Resolved] = {}
_cache_at: float = 0.0


@dataclass
class Resolved:
    name: str
    label: str
    base_url: str
    api_key: str
    enabled: bool
    api_key_env: str
    # "db" = the operator configured this row; "default" = built-in/env fallback.
    # The gateway keeps using its shared provider instance in the default case, which is
    # what tests (and the mock's call counter) rely on.
    origin: str = "db"
    # "db" = 控制台录入（密文）；"env" = 环境变量；"none" = 都没有
    key_source: str = "none"
    key_hint: str = ""

    @property
    def configured(self) -> bool:
        """Offline providers need no key (`mock` and `mock-alt` are two instances of the
        same offline vendor, which is how failover is demoed without a network)."""
        return self.name.startswith("mock") or bool(self.api_key)


def invalidate() -> None:
    global _cache_at
    _cache.clear()
    _cache_at = 0.0


def _env_key(name: str, api_key_env: str, settings: Settings) -> str:
    """Resolve the secret. An explicit variable wins and is *not* silently backfilled
    from the settings default — otherwise pointing a provider at `MY_EMPTY_VAR` would
    still look configured because settings has a placeholder like "not-needed"."""
    if api_key_env:
        return os.environ.get(api_key_env, "")
    return str(getattr(settings, f"{name}_api_key", "") or "")


def master_secret(settings: Settings) -> str:
    """主密钥：优先 AIGATE_SECRET_KEY，没配就退化为 session_secret（并告警）。"""
    if settings.secret_key:
        return settings.secret_key
    if not getattr(settings, "_warned_secret_fallback", False):
        settings._warned_secret_fallback = True  # type: ignore[attr-defined]
        import logging

        logging.getLogger("aigate").warning(
            "AIGATE_SECRET_KEY is not set; provider secrets are encrypted with "
            "session_secret. Rotating the session secret will make saved provider keys "
            "unreadable — set AIGATE_SECRET_KEY before storing real keys."
        )
    return settings.session_secret


def decrypt_secret(settings: Settings, cipher: str) -> tuple[str, str]:
    """返回 (明文, 错误说明)。解不开时明文为空但错误要能显示在控制台上。"""
    if not cipher:
        return "", ""
    try:
        return SecretBox(master_secret(settings)).decrypt(cipher), ""
    except SecretError as exc:
        return "", str(exc)


def _resolve_key(row: ProviderConfig, settings: Settings) -> tuple[str, str, str]:
    """(key, source, hint) —— 库里的密文优先，其次环境变量。"""
    plain, _err = decrypt_secret(settings, row.api_key_cipher or "")
    if plain:
        return plain, "db", row.api_key_hint or hint(plain)
    env_value = _env_key(row.name, row.api_key_env, settings)
    if env_value:
        return env_value, "env", hint(env_value)
    return "", "none", ""


def _from_row(row: ProviderConfig, settings: Settings) -> Resolved:
    base_url = row.base_url or str(getattr(settings, f"{row.name}_base_url", "") or "")
    key, source, key_hint = _resolve_key(row, settings)
    return Resolved(
        name=row.name,
        label=row.label or row.name,
        base_url=base_url,
        api_key=key,
        enabled=bool(row.enabled),
        api_key_env=row.api_key_env,
        key_source=source,
        key_hint=key_hint,
    )


async def load(session: AsyncSession, settings: Settings, force: bool = False) -> dict[str, Resolved]:
    """Cached view of provider settings. Five seconds is short enough that an operator who
    clicks save and immediately re-tests sees the new value, long enough to keep the hot
    path off the database."""
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache and (now - _cache_at) < TTL_S:
        return _cache

    # No seeding here on purpose: a request path must not write. Rows are created by the
    # startup hook or by the console, and anything missing falls back to the built-in
    # defaults (origin="default"), which keeps the shared provider instances in play.
    rows = (await session.execute(select(ProviderConfig))).scalars().all()
    resolved = {row.name: _from_row(row, settings) for row in rows}
    _cache = resolved
    _cache_at = now
    return resolved


async def get(session: AsyncSession, settings: Settings, name: str) -> Resolved | None:
    table = await load(session, settings)
    found = table.get(name)
    if found is not None:
        return found
    # Not configured in the DB yet: fall back to what the process actually knows about,
    # so a fresh install with only env vars routes correctly — and `mock-alt` (a second
    # offline instance used to demo failover) keeps working without a DB row.
    from aigate.providers import known_providers

    if name not in known_providers():
        return None
    seed = next((s for s in SEEDS if s[0] == name), None)
    label = seed[1] if seed else name
    env = seed[3] if seed else f"AIGATE_{name.upper()}_API_KEY"
    env_value = _env_key(name, env, settings)
    return Resolved(
        name=name,
        label=label,
        base_url=str(getattr(settings, f"{name}_base_url", "") or (seed[2] if seed else "")),
        api_key=env_value,
        enabled=True,
        api_key_env=env,
        origin="default",
        key_source="env" if env_value else "none",
        key_hint=hint(env_value) if env_value else "",
    )


async def ensure_seeds(session: AsyncSession) -> int:
    """Insert the built-in vendors once. Never overwrites an edited row."""
    existing = {r.name for r in (await session.execute(select(ProviderConfig))).scalars()}
    added = 0
    for name, label, base_url, env in SEEDS:
        if name in existing:
            continue
        session.add(ProviderConfig(name=name, label=label, base_url=base_url, api_key_env=env, enabled=True))
        added += 1
    if added:
        await session.commit()
        invalidate()
    return added


def dump(item: Resolved | ProviderConfig, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if isinstance(item, ProviderConfig):
        return {
            "name": item.name,
            "label": item.label or item.name,
            "base_url": item.base_url,
            "api_key_env": item.api_key_env,
            "enabled": bool(item.enabled),
            "configured": None,
            "models": models or [],
        }
    return {
        "name": item.name,
        "label": item.label,
        "base_url": item.base_url,
        "api_key_env": item.api_key_env,
        "enabled": item.enabled,
        "configured": item.configured,
        # 只给来源与末四位：明文永远不出现在任何读接口里
        "key_source": item.key_source,
        "key_hint": item.key_hint,
        "models": models or [],
    }


async def set_secret(
    session: AsyncSession, settings: Settings, name: str, plaintext: str, actor: str
) -> ProviderConfig:
    """把一把真实密钥加密写进 provider_configs。"""
    row = (
        await session.execute(select(ProviderConfig).where(ProviderConfig.name == name))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(name)
    clean = plaintext.strip()
    row.api_key_cipher = SecretBox(master_secret(settings)).encrypt(clean)
    row.api_key_hint = hint(clean)
    row.api_key_updated_at = utcnow()
    row.api_key_updated_by = actor
    await session.commit()
    invalidate()
    return row


async def clear_secret(session: AsyncSession, name: str) -> ProviderConfig:
    row = (
        await session.execute(select(ProviderConfig).where(ProviderConfig.name == name))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(name)
    row.api_key_cipher = ""
    row.api_key_hint = ""
    row.api_key_updated_at = None
    row.api_key_updated_by = ""
    await session.commit()
    invalidate()
    return row
