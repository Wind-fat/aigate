"""时间边界：账期与「今天」到底按谁的时钟算。

数据库里一律存 UTC，这是对的——夏令时、跨区结算、PG 的 `timestamptz` 都要求它。
但**给人看的边界**不能用 UTC：中国团队看到的"今日"如果按 UTC 切，早上 8 点之前
看板上永远是昨天；预算"每月 1 号重置"实际发生在北京时间 8 点。

所以这里只做一件事：把「某个时区的自然日/自然月起点」换算成 UTC 瞬时，给查询用。
默认 `Asia/Shanghai`（AIGATE_TIMEZONE 可改），因为这套系统的使用者在中国。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Shanghai"


def zone(name: str | None) -> ZoneInfo:
    """拿不到就退回 UTC，并**不**抛异常：一个拼错的时区名不该让看板 500。"""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, TypeError, ImportError):
        # Windows 的 Python 没有系统 tzdata 时抛的是 ModuleNotFoundError（ImportError 子类），
        # 不是 ZoneInfoNotFoundError；两种情况都退回 UTC，但依赖里已经带了 tzdata 包。
        try:
            return ZoneInfo("UTC")
        except ImportError:  # pragma: no cover - 极端环境
            raise RuntimeError("no timezone database available; add `tzdata` to deps") from None


def local_now(name: str | None = None) -> datetime:
    return datetime.now(zone(name))


def day_start(name: str | None = None, *, days_back: int = 0, now: datetime | None = None) -> datetime:
    """本地自然日 0 点（days_back=0 是今天），返回 **UTC** aware datetime，可直接进 SQL。"""
    tz = zone(name)
    local = (now or datetime.now(UTC)).astimezone(tz)
    start = (local - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start.astimezone(UTC)


def month_start(name: str | None = None, *, now: datetime | None = None) -> datetime:
    """本地自然月 1 号 0 点，返回 UTC aware datetime。"""
    tz = zone(name)
    local = (now or datetime.now(UTC)).astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def local_date(value: datetime, name: str | None = None) -> str:
    """把库里取出的时间换算成展示用的本地日期字符串（YYYY-MM-DD）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone(name)).date().isoformat()