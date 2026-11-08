"""上游故障告警：把「某家供应商挂了」变成一条可处置的行，而不是一个计数器。

**为什么有这个东西。** 真调 DashScope 时抓到的：供应商整夜 502，网关上只留下
`aigate_upstream_errors_total` 在涨、熔断器打开了，`alerts` 表里一条都没有 —— 因为
`alerts` 的唯一写入者是预算检查。夜里挂了没人会被叫醒，控制台上也没有任何东西可点。

**写进库的语义**（别的地方都按这个读，改之前先看 `models.Alert` 的 docstring）::

    scope_type="provider"  scope_id=<provider>  scope_slug=<provider>
    metric="upstream_failure"  level="critical"
    period_key="open"            # 未恢复；唯一索引的一部分，因此天然去重
    value=<连续失败数>  limit_value=<阈值>
    detail={provider, consecutive_failures, threshold, first_failure_at,
            last_error_code, last_failure_at, resolved_at, duration_s}

* **触发**：连续失败数*第一次*达到阈值（`AIGATE_UPSTREAM_ALERT_THRESHOLD`，默认 3）。
  一次成功就把计数清零 —— 「连续」是字面意思，中间成功过一次就不算同一段故障。
* **聚合，不刷屏**：同一段故障里继续失败只更新同一行的 `value`/`message`/`detail`，
  绝不新增行。DB 侧还有唯一索引 `(scope_type, scope_id, metric, period_key)` 兜底，
  所以多副本同时越阈值也只会有**一条** `period_key="open"` 的行。
* **恢复**：第一次成功时把那一行 `resolved_at` 填上、`period_key` 改成 `closed:<row id>`
  （≤20 字符，且天然唯一：同一秒里关了再开也不会撞唯一索引）、`message` 追加
  `recovered after Ns`、`detail.duration_s` 给出持续秒数。历史留得住，下一次故障还能
  再开一条新的。
* **不自动 ack**：ack 是「人看过了」，resolved 是「上游自己好了」。夜里三点自己恢复的
  故障必须仍然被人看见一次，否则「自动恢复」就等于「没人知道发生过」。
* **只对 retryable 失败计数**：厂商 400 说明它活着、在正常拒绝我们的请求，那是我们的
  bug，不是它挂了。调用方在 `v1_chat` 里按 `exc.retryable` 分流。

**已知边界（写清楚，不装作没有）**：聚合状态在进程内存里。多副本部署时，如果开这条
告警的副本先挂了，那一行不会被自动 resolved —— 用
`POST /admin/v1/alerts/{id}/resolve` 手动关，或者等那个副本重启后再次失败/成功。
DB 唯一索引保证的只是「不刷屏」，不保证「一定有人来关」。

**告警写入绝不抛异常**：一次已经失败的请求不能再因为告警挂掉第二次。所有落库都包在
try/except 里，失败只留一条 WARNING。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aigate.models import Alert, as_aware, utcnow

log = logging.getLogger("aigate.upstream")

SCOPE = "provider"
METRIC = "upstream_failure"
LEVEL = "critical"
OPEN = "open"


@dataclass
class _Episode:
    """一段连续失败。内存里的聚合窗口，不是账本。"""

    consecutive: int = 0
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str = ""
    # True once this process knows an unresolved alert row exists for the provider
    # (either it inserted it, or it found one opened by another replica).
    alert_open: bool = False


_episodes: dict[str, _Episode] = {}
_lock = asyncio.Lock()


def reset_state() -> None:
    """Drop the in-process aggregation window (tests, and a clean restart)."""
    _episodes.clear()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _message(provider: str, failures: int, threshold: int, first: datetime | None, error_code: str) -> str:
    return (
        f"provider {provider} down: {failures} consecutive failures (threshold {threshold}), "
        f"first at {_iso(first)}, last error {error_code}"
    )[:255]


def _detail(
    provider: str,
    failures: int,
    threshold: int,
    first: datetime | None,
    error_code: str,
    last: datetime | None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "consecutive_failures": failures,
        "threshold": threshold,
        "first_failure_at": _iso(first),
        "last_failure_at": _iso(last),
        "last_error_code": error_code,
        "resolved_at": None,
        "duration_s": None,
    }


def _open_row_stmt(provider: str):
    return select(Alert).where(
        Alert.scope_type == SCOPE,
        Alert.scope_id == provider,
        Alert.metric == METRIC,
        Alert.period_key == OPEN,
    )


def _session_factory():
    # Imported lazily: `aigate.db` pulls in the models, and this module is imported from
    # the request path where an import cycle would be a startup crash.
    from aigate.db import get_session_factory

    return get_session_factory()


async def note_failure(provider: str, error_code: str, *, threshold: int = 3) -> None:
    """Record one upstream failure and raise/refresh the provider alert if needed.

    Never raises — see the module docstring.
    """
    provider = (provider or "").strip()
    if not provider:
        return
    threshold = max(1, int(threshold or 1))
    now = utcnow()

    async with _lock:
        ep = _episodes.setdefault(provider, _Episode())
        ep.consecutive += 1
        if ep.first_failure_at is None:
            ep.first_failure_at = now
        ep.last_failure_at = now
        ep.last_error_code = error_code or "unknown"
        failures = ep.consecutive
        first = ep.first_failure_at
        code = ep.last_error_code

    # 结构化字段（provider / consecutive_failures / error_code）走 `extra`：值班的人用
    # grep 找 provider，SIEM 按字段聚合，两边都能用。`alerting` 标出这一次是否已经越过
    # 阈值，否则日志里分不清「第 1 次失败」和「第 30 次失败」。
    log.warning(
        "upstream failure: provider=%s consecutive_failures=%d threshold=%d error_code=%s "
        "first_failure_at=%s alerting=%s",
        provider,
        failures,
        threshold,
        code,
        _iso(first),
        failures >= threshold,
        extra={
            "provider": provider,
            "consecutive_failures": failures,
            "threshold": threshold,
            "error_code": code,
            "first_failure_at": _iso(first),
            "alerting": failures >= threshold,
        },
    )

    if failures < threshold:
        return

    try:
        await _upsert_open_alert(provider, failures, threshold, first, code, now)
    except Exception:  # noqa: BLE001 - alerting must never break the request
        log.warning("provider alert write failed: provider=%s", provider, exc_info=True)


async def note_success(provider: str) -> None:
    """Record one upstream success; close the provider alert if this process opened one.

    Cheap on the happy path **by design**: with no episode in memory this returns before
    touching the database, so a healthy gateway pays zero extra queries.
    """
    provider = (provider or "").strip()
    if not provider:
        return

    async with _lock:
        ep = _episodes.get(provider)
        if ep is None:
            return
        failures = ep.consecutive
        was_open = ep.alert_open
        ep.consecutive = 0
        ep.first_failure_at = None
        ep.last_failure_at = None
        ep.last_error_code = ""
        ep.alert_open = False

    if failures:
        log.warning(
            "upstream recovered: provider=%s after_failures=%d",
            provider,
            failures,
            extra={"provider": provider, "consecutive_failures": failures, "recovered": True},
        )
    if not was_open:
        return

    try:
        await _close_alert(provider, failures)
    except Exception:  # noqa: BLE001 - alerting must never break the request
        log.warning("provider alert close failed: provider=%s", provider, exc_info=True)


async def _upsert_open_alert(
    provider: str,
    failures: int,
    threshold: int,
    first: datetime | None,
    error_code: str,
    last: datetime,
) -> None:
    message = _message(provider, failures, threshold, first, error_code)
    detail = _detail(provider, failures, threshold, first, error_code, last)

    async with _session_factory()() as session:
        row = (await session.execute(_open_row_stmt(provider))).scalar_one_or_none()
        inserted = False
        if row is None:
            session.add(
                Alert(
                    level=LEVEL,
                    scope_type=SCOPE,
                    scope_id=provider,
                    scope_slug=provider,
                    metric=METRIC,
                    period_key=OPEN,
                    value=Decimal(failures),
                    limit_value=Decimal(threshold),
                    message=message,
                    detail=detail,
                )
            )
            try:
                await session.commit()
                inserted = True
            except IntegrityError:
                # Another replica opened it between our SELECT and INSERT. The unique
                # index is the dedupe; adopt that row instead of failing.
                await session.rollback()
                row = (await session.execute(_open_row_stmt(provider))).scalar_one_or_none()
                if row is None:
                    raise

        if not inserted and row is not None:
            # Same incident, more evidence. Update in place: no new row, no new
            # notification, and a human "ack" on it is left exactly as it was.
            row.value = Decimal(failures)
            row.limit_value = Decimal(threshold)
            row.message = message
            row.detail = detail
            await session.commit()

    ep = _episodes.get(provider)
    if ep is not None:
        ep.alert_open = True


async def _close_alert(provider: str, failures: int) -> None:
    now = utcnow()
    async with _session_factory()() as session:
        row = (await session.execute(_open_row_stmt(provider))).scalar_one_or_none()
        if row is None:
            return
        started = as_aware(row.created_at) or now
        detail = dict(row.detail or {})
        duration = max(0, int((now - started).total_seconds()))
        detail.update(
            {
                "resolved_at": now.isoformat(),
                "duration_s": duration,
                "consecutive_failures": max(int(row.value or 0), failures),
                "last_error_code": detail.get("last_error_code", ""),
            }
        )
        row.resolved_at = now
        # 19 chars: fits period_key String(20), unique per row, and frees "open" for the
        # next outage. Time ordering lives in resolved_at, not in this key.
        row.period_key = f"closed:{row.id[:12]}"
        row.message = (f"{row.message} | recovered after {duration}s")[:255]
        row.detail = detail
        await session.commit()


async def resolve_alert(session: Any, alert: Alert, *, note: str = "") -> Alert:
    """Close an alert by hand — the escape hatch for a stale incident row.

    Used by `POST /admin/v1/alerts/{id}/resolve`. Runs on the caller's session (an
    admin endpoint already has one); the incident writer uses its own.
    """
    now = utcnow()
    started = as_aware(alert.created_at) or now
    detail = dict(alert.detail or {})
    detail.update({"resolved_at": now.isoformat(), "duration_s": int((now - started).total_seconds())})
    if note:
        detail["resolved_by_note"] = note[:200]
    alert.resolved_at = now
    if alert.scope_type == SCOPE:
        alert.period_key = f"closed:{alert.id[:12]}"
    alert.detail = detail
    await session.commit()

    # The in-process window would otherwise keep thinking the incident is open and
    # re-open it on the very next failure — or, worse, skip the alert entirely.
    ep = _episodes.get(alert.scope_id)
    if ep is not None and alert.scope_type == SCOPE:
        ep.alert_open = False
        # 手动关掉 = 人判断这段故障结束了。计数一起清零，否则下一个失败会直接
        # 越过阈值再开一条（人刚关掉就又弹一条，等于没关）。
        ep.consecutive = 0
        ep.first_failure_at = None
        ep.last_failure_at = None
        ep.last_error_code = ""
    return alert