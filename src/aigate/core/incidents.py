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

**聚合状态在进程内存里，所以还有第二道保险：「对账」（`reconcile_open_alerts`）**

多副本部署时，开这条告警的副本先退出，那一行就没人来关（本进程的内存窗口跟着没了）；
重启后进程对旧的 open 行同样没有记忆。所以启动时 + 每
`AIGATE_ALERT_RECONCILE_INTERVAL_S`（默认 300s）扫一遍
`scope_type="provider" and metric in (upstream_failure, provider_config) and period_key="open"`
的残留行，对每个 `scope_id` 做一次**便宜**的判定：

* `provider_config` —— 只看配置本身（不碰网络）：provider 在不在、是否启用、密钥能不能解析；
  `unknown_provider` 还额外看「现在是否仍有路由指向它」。配置好了就是好了。
* `upstream_failure` —— 配置正常之后还要求「上游此刻真的在应答」：对 `base_url + /models`
  发一次 GET（**不打真模型、不花 token**），拿到任何 <500 的响应就算活着。离线 provider
  （`mock*`、没有 base_url）探不了 —— **不探就不关**。

判定原则只有一条：**证据不足就不关**。留一条过期告警的代价是值班多看一行；误关一条真实
故障的代价是没人知道它还在。关掉的行写 `detail.closed_by="reconcile"`、
`detail.reconcile_reason`（探针的结论）与 `resolved_at`/`duration_s`，并落一条 WARNING 日志
—— 自动关不等于没有记录。仍异常的行原样留着，不重写、不刷屏。

剩下的真边界：`mock*` 这类没有网络端点的 provider，上游故障行只能靠人
（`POST /admin/v1/alerts/{id}/resolve`）或下一次真的失败/成功来收口。

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

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aigate.models import Alert, RoutePolicy, as_aware, utcnow

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
    # 撞到这条配置故障的**虚拟模型**（路由的 match_model）。写进 detail 供控制台跳转；
    # 同一个 provider 可能被多条路由共用，记住最近一条就够处置用了。
    model: str = ""



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
    model: str = "",
) -> dict[str, Any]:
    detail: dict[str, Any] = {
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
    if model:
        # 哪条**虚拟模型**的路由坏了。控制台的处置链接要拿它跳到
        # `/routes?model=<model>` —— 没有它，值班只能自己在一堆路由里猜。
        detail["model"] = model
    return detail


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


async def note_config_failure(
    provider: str, code: str, *, error_message: str = "", model: str = ""
) -> None:
    """记一次**配置侧**失败，必要时写/刷新一条 `provider_config` 的 warn 告警。

    与 `note_failure` 的区别，按重要性排：

    * **没有阈值**：缺密钥是确定性的，第一次打就坏，等三次只是让调用方白挨两次。
    * **不是「连续失败」账本**：中间插一次成功也不会把「配置坏了」洗掉 —— 配置的好坏只看
      它下一次能不能被解析（`note_config_ok`）。
    * **写库节流**：同一个坏配置会在每个请求上重复撞到，所以同一行最多每
      `CONFIG_REFRESH_S` 秒刷新一次。第一次、以及原因变化时立即写。

    `model` 是调用方请求的**虚拟模型**（路由的 `match_model`），写进 `detail.model`：
    控制台要靠它把「路由指向不存在的 provider」这类告警直接链到那条路由。

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
        if model:
            ep.model = model
        model = ep.model
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
            detail=_config_detail(
                provider, reasons, occurrences, code, message, fix, first, now, model
            ),
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


# ---------------------------------------------------------------- reconcile
#
# 见模块 docstring：进程内存里的聚合窗口不是账本。这里补一条主动对账 —— 启动时 + 周期性
# 扫一遍残留的 open 行，**只关「现在能证明条件不成立」的**。
#
# 判定原则只有一条：证据不足就不关。留一条过期告警的代价是值班多看一行；误关一条真实
# 故障的代价是没人知道它还在。

RECONCILE_METRICS: tuple[str, ...] = (METRIC, CONFIG_METRIC)


@dataclass
class _Probe:
    """一次对账探测的结论。

    `ok=False` 不等于「有故障」—— 也可能是探不了（离线 provider、没 base_url、探测超时）。
    两者对 reconcile 的处理一样：留着，不动。
    """

    ok: bool
    reason: str
    detail: str = ""


def _config_probe(cfg: Any, referenced: bool) -> _Probe:
    """配置本身是否已经正常 —— 纯本地判定，不打网络。

    `unknown_provider` 的「正常」有两种：provider 现在存在了，或者已经没有路由指向它
    （故障已随路由改动消失）。后者仍算正常，否则一条早就没人用的坏路由会挂着不散。
    """
    if cfg is None:
        if referenced:
            return _Probe(False, "provider_missing", "still referenced by a route")
        return _Probe(True, "route_no_longer_references_provider")
    if not cfg.enabled:
        return _Probe(False, "provider_disabled")
    if not cfg.configured:
        return _Probe(False, "provider_unconfigured", cfg.api_key_env or "")
    return _Probe(True, "config_ok")


async def _probe_upstream(provider: str, cfg: Any, *, timeout_s: float = 5.0) -> _Probe:
    """一次 `GET /models`：不打真模型、不花 token，只问「你还答不答」。

    任何 <500 的响应都算活着 —— 401/403/404 是它在正常拒绝我们（那是配置问题，归
    `provider_config` 管），429 是它在限流；都不是「上游挂了」。5xx / 连接失败 = 证据不足或
    确实还坏着，留着不动。
    """
    if cfg is None:
        return _Probe(False, "provider_unknown")
    if provider.startswith("mock") or not cfg.base_url:
        # 离线 provider 没有可探的端点。**不探就不关**：宁可留一条，也不误关。
        return _Probe(False, "not_probeable")
    url = cfg.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))
        ) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 探不通只是「证据不足」，不是新事故
        return _Probe(False, "unreachable", f"{type(exc).__name__}: {exc}"[:160])
    if resp.status_code >= 500:
        return _Probe(False, f"upstream_{resp.status_code}", url)
    return _Probe(True, f"upstream_answered_{resp.status_code}", url)


async def _referenced_providers(session: Any) -> set[str]:
    """所有路由里出现过的 provider 名（候选 / 降级 / 影子）。"""
    names: set[str] = set()
    for policy in (await session.execute(select(RoutePolicy))).scalars():
        for chain in (policy.candidates or [], policy.fallback or [], policy.shadow or []):
            for cand in chain:
                name = cand.get("provider") if isinstance(cand, dict) else None
                if name:
                    names.add(str(name))
    return names


async def _forget_open_episode(provider: str, metric: str) -> None:
    """行关掉之后，把本进程内存里「这段事故还开着」一起忘掉（同 `resolve_alert`）。"""
    async with _lock:
        if metric == CONFIG_METRIC:
            _config_episodes.pop(provider, None)
            return
        ep = _episodes.get(provider)
        if ep is None:
            return
        ep.alert_open = False
        ep.consecutive = 0
        ep.first_failure_at = None
        ep.last_failure_at = None
        ep.last_error_code = ""


def _mark_reconciled(row: Alert, probe: _Probe, now: datetime) -> None:
    """把一行标成「由对账关闭」，并把探针结论留在 detail 里（这就是记录）。"""
    started = as_aware(row.created_at) or now
    duration = max(0, int((now - started).total_seconds()))
    detail = dict(row.detail or {})
    detail.update(
        {
            "resolved_at": now.isoformat(),
            "duration_s": duration,
            "closed_by": "reconcile",
            "reconcile_reason": probe.reason,
            "reconciled_at": now.isoformat(),
        }
    )
    if probe.detail:
        detail["reconcile_probe"] = probe.detail
    if row.metric == CONFIG_METRIC:
        detail["occurrences"] = max(int(detail.get("occurrences") or 0), int(row.value or 0))
    else:
        detail["consecutive_failures"] = max(
            int(detail.get("consecutive_failures") or 0), int(row.value or 0)
        )
    row.resolved_at = now
    row.period_key = f"closed:{row.id[:12]}"
    row.message = f"{row.message} | reconciled ({probe.reason}) after {duration}s"[:255]
    row.detail = detail


async def reconcile_open_alerts(
    *,
    upstream_probe: Any = None,
    settings: Any = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """扫一遍残留的 provider 告警，关掉「现在能证明已经正常」的那些。

    返回一份可直接打印/贴报告的摘要：`scanned` / `closed` / `kept` / `errors`。
    `upstream_probe` 是给测试用的注入点（签名 `(provider, cfg) -> _Probe`）；默认实现只发
    一次 `GET /models`。

    关一条行的两个前提：探测说「正常」，且**探测期间这一行没有被改写**（`value` 变了说明
    还有新的失败在往上写，那是活的故障，不是残留）。数据库异常往外抛（后台循环自己兜住），
    单行探测异常只记进 `errors`。
    """
    from aigate import provider_config as provider_config_mod
    from aigate.config import get_settings

    settings = settings or get_settings()
    if timeout_s is None:
        timeout_s = settings.alert_probe_timeout_s
    probe_upstream = upstream_probe or (
        lambda provider, cfg: _probe_upstream(provider, cfg, timeout_s=timeout_s)
    )

    summary: dict[str, Any] = {
        "scanned": 0,
        "closed": [],
        "kept": [],
        "errors": [],
        "at": utcnow().isoformat(),
    }

    # 第一步：取残留行 + 探针需要的配置快照，然后**立刻放掉会话** —— 上游探测要发网络请求，
    # 不该攥着数据库连接等它。
    snapshots: list[tuple[str, str, str, Any, bool, Decimal]] = []
    async with _session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Alert)
                    .where(
                        Alert.scope_type == SCOPE,
                        Alert.metric.in_(RECONCILE_METRICS),
                        Alert.period_key == OPEN,
                        Alert.resolved_at.is_(None),
                    )
                    .order_by(Alert.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return summary
        # force=True：对账要看**现在**的配置，不为省一次查询而拿 5 秒前的缓存下结论。
        await provider_config_mod.load(session, settings, force=True)
        referenced = await _referenced_providers(session)
        for row in rows:
            cfg = await provider_config_mod.get(session, settings, row.scope_id)
            snapshots.append(
                (row.id, row.scope_id, row.metric, cfg, row.scope_id in referenced, Decimal(row.value or 0))
            )

    # 第二步：探测（不碰数据库）。
    closing: list[tuple[str, str, str, _Probe, Decimal]] = []
    for alert_id, provider, metric, cfg, referenced_flag, value in snapshots:
        summary["scanned"] += 1
        try:
            if metric == CONFIG_METRIC:
                probe = _config_probe(cfg, referenced_flag)
            else:
                # 上游故障要求两件事都成立：配置现在是好的，且上游此刻真的在应答。
                # 配置都不对就不去探网络 —— 那时候「不可达」说明不了上游的死活。
                cfg_probe = _config_probe(cfg, referenced_flag)
                probe = cfg_probe if not cfg_probe.ok else await probe_upstream(provider, cfg)
        except Exception as exc:  # noqa: BLE001 - 单行探测失败不该打断整轮对账
            summary["errors"].append(
                {"id": alert_id, "provider": provider, "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            continue
        if probe.ok:
            closing.append((alert_id, provider, metric, probe, value))
        else:
            summary["kept"].append(
                {"id": alert_id, "provider": provider, "metric": metric, "reason": probe.reason}
            )

    # 第三步：重新读一遍再关（期间可能有新的失败写进来，也可能别的副本已经关掉了）。
    if not closing:
        return summary
    async with _session_factory()() as session:
        for alert_id, provider, metric, probe, value in closing:
            row = (await session.execute(select(Alert).where(Alert.id == alert_id))).scalar_one_or_none()
            if (
                row is None
                or row.period_key != OPEN
                or row.resolved_at is not None
                or Decimal(row.value or 0) != value
            ):
                summary["kept"].append(
                    {
                        "id": alert_id,
                        "provider": provider,
                        "metric": metric,
                        "reason": "changed_while_probing",
                    }
                )
                continue
            now = utcnow()
            _mark_reconciled(row, probe, now)
            await session.commit()
            await _forget_open_episode(provider, metric)
            duration_s = int((row.detail or {}).get("duration_s") or 0)
            # 自动关也要留一条记录：日志里能查到「谁关的、凭什么关的、开了多久」。
            log.warning(
                "stale alert reconciled: id=%s provider=%s metric=%s reason=%s duration_s=%d",
                row.id,
                provider,
                metric,
                probe.reason,
                duration_s,
                extra={
                    "alert_id": row.id,
                    "provider": provider,
                    "metric": metric,
                    "closed_by": "reconcile",
                    "reconcile_reason": probe.reason,
                },
            )
            summary["closed"].append(
                {
                    "id": row.id,
                    "provider": provider,
                    "metric": metric,
                    "reason": probe.reason,
                    "duration_s": duration_s,
                }
            )
    return summary


async def reconcile_loop(interval_s: float) -> None:
    """启动时跑一次，之后每 `interval_s` 秒一次；被取消才退出。

    后台维护动作：任何一次失败只留日志，绝不让循环死掉（否则「有对账」这件事只是错觉）。
    """
    delay = max(30.0, float(interval_s))
    while True:
        try:
            summary = await reconcile_open_alerts()
            if summary["closed"] or summary["errors"]:
                log.warning(
                    "alert reconcile pass: scanned=%d closed=%d kept=%d errors=%d",
                    summary["scanned"],
                    len(summary["closed"]),
                    len(summary["kept"]),
                    len(summary["errors"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台循环不能因为一次异常就停
            log.warning("alert reconcile failed", exc_info=True)
        await asyncio.sleep(delay)


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
    # 人手工关掉 = 这段故障到此为止。丢掉/清零聚合窗口，下次再坏重新开一条，而不是接着
    # 往里聚合（否则「刚关掉又弹同一条」，等于没关）。
    await _forget_open_episode(alert.scope_id, alert.metric)
    return alert