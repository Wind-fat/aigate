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
from aigate.models import ProviderConfig

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
    ("vllm", "自建 vLLM", "http://127.0.0.1:8000/v1", "AIGATE_VLLM_API_KEY"),
    ("mock", "离线 Mock（不发网络请求）", "", ""),
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


def _from_row(row: ProviderConfig, settings: Settings) -> Resolved:
    base_url = row.base_url or str(getattr(settings, f"{row.name}_base_url", "") or "")
    return Resolved(
        name=row.name,
        label=row.label or row.name,
        base_url=base_url,
        api_key=_env_key(row.name, row.api_key_env, settings),
        enabled=bool(row.enabled),
        api_key_env=row.api_key_env,
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
    return Resolved(
        name=name,
        label=label,
        base_url=str(getattr(settings, f"{name}_base_url", "") or (seed[2] if seed else "")),
        api_key=_env_key(name, env, settings),
        enabled=True,
        api_key_env=env,
        origin="default",
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
        "models": models or [],
    }
