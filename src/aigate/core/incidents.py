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

**第二种告警：配置类（`metric="provider_config"`，`level="warn"`）**

「密钥没配」「供应商被人手动停用」「路由指向一个不存在的 provider」这三件事和上游挂了
是**两种事故**：上游挂了要等厂商恢复并切流量，配置坏了要有人去改配置，而且它不会自己
好。以前它们只在 `/providers` 与 `/ops` 上可见，`retryable=False` 也不计入告警，所以
夜里没人会被叫醒 —— 可「主供应商的密钥滚失效了」恰恰是最该在十分钟内知道的事。

    scope_type="provider"  scope_id=<provider>  scope_slug=<provider>
    metric="provider_config"  level="warn"  period_key="open"
    value=<这段配置故障被撞到的次数>  limit_value=0（没有阈值：确定性故障，第一次就叫）
    detail={provider, reasons[], reason, error_code, error_message, fix,
            occurrences, first_seen_at, last_seen_at, resolved_at, duration_s}

* **级别是 warn，不是 critical**：critical 留给「上游在挂、流量正在丢」。配置类不会自己
  恶化，但也**不会自己好** —— 所以它照样进「未确认」列表，等人去改。
* **去重**：同一 provider 同时只有一条 `provider_config` 行（唯一索引兜底），
  `detail.reasons` 记录这段故障里出现过的原因；换原因（先缺密钥、后又被停用）只更新
  同一行，不新增。
* **恢复**：下一次该 provider 的配置**解析成功**就关掉（不是等调用成功 —— 「密钥配好了
  但上游 500」是两件事）。语义与上游告警一致：`resolved_at` + `period_key="closed:<id>"`。
* **写库节流**：配置故障会在**每一个**打到这条路由的请求上重复。所以同一行最多每
  `CONFIG_REFRESH_S`（60s）刷新一次；否则一条坏路由在压测里会变成「每个请求一次 UPDATE」，
  SQLite 上这就是自己给自己造事故。`detail.fix` 是给人看的下一步动作，`message` 也带上它。

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
from dataclasses import dataclass, field
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

# 配置类告警：同一张表、同一套去重/恢复语义，但**不是**上游故障。
# 缺密钥 / 被人停用 / 路由指向不存在的 provider，都是「有人要去改配置」，不是「等厂商恢复」。
CONFIG_METRIC = "provider_config"
CONFIG_LEVEL = "warn"
# 一条坏配置会在每个请求上重复撞到；同一行最多每分钟刷一次库（见 note_config_failure）。
CONFIG_REFRESH_S = 60.0

# ProviderError.code -> 告警里的 reason。`provider_unknown` 是数据面的历史拼法
# （`_provider_for` 抛的），对外统一写成 unknown_provider。
CONFIG_REASONS: dict[str, str] = {
    "provider_unconfigured": "provider_unconfigured",
    "provider_disabled": "provider_disabled",
    "provider_unknown": "unknown_provider",
    "unknown_provider": "unknown_provider",
}


def config_reason(code: str) -> str | None:
    """`ProviderError.code` -> 配置问题的 reason；不是配置问题（上游/客户端错）返回 None。"""
    return CONFIG_REASONS.get((code or "").strip())


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


@dataclass
class _ConfigEpisode:
    """一段配置故障。同一 provider 换原因不另开一条，只记进 reasons。"""

    reasons: set[str] = field(default_factory=set)
    occurrences: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_error_code: str = ""
    last_error_message: str = ""
    alert_open: bool = False
    # 上一次成功刷库的时间；节流用（CONFIG_REFRESH_S）。
    last_write_at: datetime | None = None



_episodes: dict[str, _Episode] = {}
_config_episodes: dict[str, _ConfigEpisode] = {}
_lock = asyncio.Lock()


def reset_state() -> None:
    """Drop the in-process aggregation window (tests, and a clean restart)."""
    _episodes.clear()
    _config_episodes.clear()


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


def _open_row_stmt(provider: str, metric: str = METRIC):
    return select(Alert).where(
        Alert.scope_type == SCOPE,
        Alert.scope_id == provider,
        Alert.metric == metric,
        Alert.period_key == OPEN,
    )


def _config_fix(reason: str, provider: str, error_message: str) -> str:
    """「该修什么」——一句话 + 一个能直接执行的动作，别只写 `unconfigured`。"""
    if reason == "provider_unconfigured":
        env = ""
        # `_provider_for` 抛的是 "provider vllm has no api key (AIGATE_VLLM_API_KEY)"
        if error_message.endswith(")") and "(" in error_message:
            env = error_message[error_message.rindex("(") + 1 : -1].strip()
        head = f"set {env}" if env and env != "env not set" else "save an API key"
        return (
            f"{head} for {provider} (console 供应商 → 密钥, or "
            f"PUT /admin/v1/providers/{provider}/secret), then POST "
            f"/admin/v1/providers/{provider}/test"
        )
    if reason == "provider_disabled":
        return (
            f"re-enable {provider} (console 供应商 → 启用, or "
            f'PUT /admin/v1/providers/{provider} {{"enabled": true}}) if it should still serve traffic'
        )
    return (
        f"point the route at a provider that exists (PUT /admin/v1/routes/<model>) or create "
        f"'{provider}' (POST /admin/v1/providers); it is not in the provider registry"
    )


def _config_message(provider: str, reasons: list[str], fix: str) -> str:
    # 不在这里写命中次数：写库是节流的（CONFIG_REFRESH_S），这条 message 会一段时间不刷新，
    # 写一个「1x」在它其实已经被撞了 30 次时就是在撒谎。次数在 detail.occurrences，
    # resolve 时落最终值。
    return f"provider {provider} needs a config fix ({' + '.join(reasons)}): {fix}"[:255]


def _config_detail(
    provider: str,
    reasons: list[str],
    occurrences: int,
    error_code: str,
    error_message: str,
    fix: str,
    first: datetime | None,
    last: datetime | None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "scope": "config",
        "reasons": reasons,
        "reason": reasons[0] if len(reasons) == 1 else "mixed",
        "error_code": error_code,
        "error_message": error_message[:200],
        "fix": fix,
        "occurrences": occurrences,
        "first_seen_at": _iso(first),
        "last_seen_at": _iso(last),
        "resolved_at": None,
        "duration_s": None,
    }


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


async def note_config_failure(provider: str, code: str, *, error_message: str = "") -> None:
    """记一次**配置侧**失败，必要时写/刷新一条 `provider_config` 的 warn 告警。

    与 `note_failure` 的区别，按重要性排：

    * **没有阈值**：缺密钥是确定性的，第一次打就坏，等三次只是让调用方白挨两次。
    * **不是「连续失败」账本**：中间插一次成功也不会把「配置坏了」洗掉 —— 配置的好坏只看
      它下一次能不能被解析（`note_config_ok`）。
    * **写库节流**：同一个坏配置会在每个请求上重复撞到，所以同一行最多每
      `CONFIG_REFRESH_S` 秒刷新一次。第一次、以及原因变化时立即写。

    和上游告警一样**绝不抛异常**：这次请求已经失败了，告警不能让它再挂第二次。
    """
    provider = (provider or "").strip()
    reason = config_reason(code)
    if not provider or reason is None:
        return
    now = utcnow()

    async with _lock:
        ep = _config_episodes.get(provider)
        if ep is None:
            ep = _config_episodes[provider] = _ConfigEpisode(first_seen_at=now)
        reason_changed = reason not in ep.reasons
        ep.reasons.add(reason)
        ep.occurrences += 1
        ep.last_seen_at = now
        ep.last_error_code = code
        ep.last_error_message = (error_message or "")[:200]
        reasons = sorted(ep.reasons)
        occurrences = ep.occurrences
        first = ep.first_seen_at
        message = ep.last_error_message
        due = (
            not ep.alert_open
            or reason_changed
            or ep.last_write_at is None
            or (now - ep.last_write_at).total_seconds() >= CONFIG_REFRESH_S
        )

    fix = _config_fix(reason, provider, message)
    log.warning(
        "provider config problem: provider=%s reason=%s error_code=%s occurrences=%d fix=%s",
        provider,
        reason,
        code,
        occurrences,
        fix,
        extra={
            "provider": provider,
            "reason": reason,
            "error_code": code,
            "occurrences": occurrences,
            "first_seen_at": _iso(first),
            "alerting": True,
        },
    )
    if not due:
        return

    try:
        await _upsert_open_alert(
            provider,
            occurrences,
            0,
            first,
            code,
            now,
            metric=CONFIG_METRIC,
            level=CONFIG_LEVEL,
            message=_config_message(provider, reasons, fix),
            detail=_config_detail(provider, reasons, occurrences, code, message, fix, first, now),
        )
    except Exception:  # noqa: BLE001 - alerting must never break the request
        log.warning("provider config alert write failed: provider=%s", provider, exc_info=True)
        return

    async with _lock:
        ep = _config_episodes.get(provider)
        if ep is not None:
            ep.last_write_at = now


async def note_config_ok(provider: str) -> None:
    """Provider 的配置**解析成功**了 → 关掉这条配置告警（如果本进程开过）。

    在 `_provider_for` 返回后立刻调用，而不是等调用成功：「密钥配好了但上游 500」是两件事，
    配置那条该关。空字典命中就返回，健康网关零额外查询。
    """
    provider = (provider or "").strip()
    if not provider:
        return

    async with _lock:
        ep = _config_episodes.pop(provider, None)
        if ep is None:
            return
        was_open = ep.alert_open
        occurrences = ep.occurrences
        reasons = sorted(ep.reasons)

    if occurrences:
        log.info(
            "provider config ok: provider=%s reasons=%s after_occurrences=%d",
            provider,
            ",".join(reasons),
            occurrences,
            extra={"provider": provider, "reasons": reasons, "recovered": True},
        )
    if not was_open:
        return

    try:
        await _close_alert(provider, occurrences, metric=CONFIG_METRIC, suffix="config fixed")
    except Exception:  # noqa: BLE001 - alerting must never break the request
        log.warning("provider config alert close failed: provider=%s", provider, exc_info=True)


async def _upsert_open_alert(
    provider: str,
    failures: int,
    threshold: int,
    first: datetime | None,
    error_code: str,
    last: datetime,
    *,
    metric: str = METRIC,
    level: str = LEVEL,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert-or-update the one open row for (provider, metric).

    上游告警与配置告警共用这一段：去重靠 DB 唯一索引 + `period_key="open"`，
    「同一次事故里继续发生」只更新同一行（`ack` 时间不动）。差别只有 level / metric /
    文案，由调用方给。
    """
    message = message if message is not None else _message(provider, failures, threshold, first, error_code)
    detail = detail if detail is not None else _detail(provider, failures, threshold, first, error_code, last)

    async with _session_factory()() as session:
        row = (await session.execute(_open_row_stmt(provider, metric))).scalar_one_or_none()
        inserted = False
        if row is None:
            session.add(
                Alert(
                    level=level,
                    scope_type=SCOPE,
                    scope_id=provider,
                    scope_slug=provider,
                    metric=metric,
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
                row = (await session.execute(_open_row_stmt(provider, metric))).scalar_one_or_none()
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

    ep = _episodes.get(provider) if metric == METRIC else _config_episodes.get(provider)
    if ep is not None:
        ep.alert_open = True


async def _close_alert(
    provider: str,
    failures: int,
    *,
    metric: str = METRIC,
    suffix: str = "recovered",
) -> None:
    now = utcnow()
    async with _session_factory()() as session:
        row = (await session.execute(_open_row_stmt(provider, metric))).scalar_one_or_none()
        if row is None:
            return
        started = as_aware(row.created_at) or now
        detail = dict(row.detail or {})
        duration = max(0, int((now - started).total_seconds()))
        detail.update(
            {
                "resolved_at": now.isoformat(),
                "duration_s": duration,
            }
        )
        if metric == CONFIG_METRIC:
            # 配置故障的「次数」是撞到的请求数，不是连续失败；键名跟着语义走。
            detail["occurrences"] = max(int(row.value or 0), failures)
        else:
            detail["consecutive_failures"] = max(int(row.value or 0), failures)
            detail["last_error_code"] = detail.get("last_error_code", "")
        row.resolved_at = now
        # 19 chars: fits period_key String(20), unique per row, and frees "open" for the
        # next outage. Time ordering lives in resolved_at, not in this key.
        row.period_key = f"closed:{row.id[:12]}"
        row.message = (f"{row.message} | {suffix} after {duration}s")[:255]
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
    if alert.scope_type != SCOPE:
        return alert
    if alert.metric == CONFIG_METRIC:
        # 人手工关掉配置告警 = 这段配置故障到此为止。丢掉聚合窗口，下次再坏重新开一条，
        # 而不是接着往里聚合（否则「刚关掉又弹同一条」，等于没关）。
        _config_episodes.pop(alert.scope_id, None)
        return alert

    ep = _episodes.get(alert.scope_id)
    if ep is not None:
        ep.alert_open = False
        # 手动关掉 = 人判断这段故障结束了。计数一起清零，否则下一个失败会直接
        # 越过阈值再开一条（人刚关掉就又弹一条，等于没关）。
        ep.consecutive = 0
        ep.first_failure_at = None
        ep.last_failure_at = None
        ep.last_error_code = ""
    return alert