"""影子流量：把真实请求同时打给一个「候选模型」，只记录、不返回。

为什么值得做：换模型这件事，唯一的证据是「用**真实**的 prompt 跑一遍看差多少」。
评测集能证明功能没坏，但证明不了「新模型回答同样的客户问题会跑偏多少」。

三条纪律：

1. **绝不影响主响应**。影子调用在响应返回之后才发起（`asyncio.create_task`），
   超时/报错只写一行记录，调用方永远看不到。
2. **影子花的钱单独记账**（`shadow_runs`），不进 `usage_records`。
   混在一起的话，所有成本报表都会因为影子比例而系统性偏高。
3. **相似度不是评分**。这里只算字符级相似度作为烟雾信号：接近 1 说明两模型说法类似，
   明显偏低说明该人读原文了。真正的质量判断要靠人看或 LLM judge，别把 0.62 当结论。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aigate import provider_config
from aigate.metering import compute_cost
from aigate.models import ModelPrice, ShadowRun
from aigate.providers import ProviderError, get_configured_provider, get_provider

log = logging.getLogger("aigate.shadow")


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


async def run_shadow(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request_id: str,
    project_id: str,
    route: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int | None,
    primary_text: str,
    candidates: list[dict],
    settings,
) -> None:
    """One task per candidate. Never raises."""
    for cand in candidates[:2]:  # two shadows is already 2x the spend of the primary
        provider_name = cand.get("provider", "")
        model_name = cand.get("model", "")
        try:
            async with session_factory() as session:
                cfg = await provider_config.get(session, settings, provider_name)
                provider = (
                    get_provider(provider_name, settings)
                    if cfg is None or cfg.origin == "default"
                    else get_configured_provider(
                        cfg.name, cfg.base_url, cfg.api_key, settings.request_timeout_s
                    )
                )
                price = (
                    await session.execute(
                        select(ModelPrice).where(
                            ModelPrice.provider == provider_name,
                            ModelPrice.model == model_name,
                        )
                    )
                ).scalar_one_or_none()

            started = time.perf_counter()
            result = await provider.chat(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            latency = int((time.perf_counter() - started) * 1000)
            text = (result.content or "")[:2000]
            cost = (
                compute_cost(
                    input_tokens=getattr(result.usage, "input_tokens", 0),
                    output_tokens=getattr(result.usage, "output_tokens", 0),
                    input_price_per_1k=price.input_price_per_1k,
                    output_price_per_1k=price.output_price_per_1k,
                )
                if price is not None
                else Decimal("0")
            )
            record = ShadowRun(
                request_id=request_id,
                project_id=project_id,
                route=route,
                provider=provider_name,
                model=model_name,
                ok=True,
                latency_ms=latency,
                cost_cny=cost or Decimal("0"),
                output_chars=len(result.content or ""),
                output_preview=text,
                similarity=_similarity(primary_text, result.content or ""),
            )
        except ProviderError as exc:
            record = ShadowRun(
                request_id=request_id,
                project_id=project_id,
                route=route,
                provider=provider_name,
                model=model_name,
                ok=False,
                error_code=exc.code,
            )
        except Exception as exc:  # noqa: BLE001 - shadow must never crash the process
            log.warning("shadow call failed for %s/%s: %s", provider_name, model_name, exc)
            continue

        try:
            async with session_factory() as session:
                session.add(record)
                await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("failed to store shadow run for %s", request_id)


def schedule(
    session_factory: async_sessionmaker[AsyncSession],
    **kwargs,
) -> asyncio.Task | None:
    """Fire and forget, with a strong reference so the loop does not collect it early."""
    if not kwargs.get("candidates"):
        return None
    task = asyncio.create_task(run_shadow(session_factory, **kwargs))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


_PENDING: set[asyncio.Task] = set()
