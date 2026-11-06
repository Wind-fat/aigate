"""时间边界：库里 UTC，展示与账期按 AIGATE_TIMEZONE（默认 Asia/Shanghai）。

这一组是纯函数测试——不依赖当前时钟，否则"跑的时候刚好是月中"会让断言时而通过时而失败。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aigate import timeutil


def test_day_start_uses_local_midnight_not_utc():
    # UTC 17:00 = 北京时间次日 01:00，所以"今天"已经是 1 月 2 日
    now = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
    start = timeutil.day_start("Asia/Shanghai", now=now)
    assert start == datetime(2026, 1, 1, 16, 0, tzinfo=UTC)  # 01-02 00:00 +08


def test_day_start_can_go_back_several_days():
    now = datetime(2026, 1, 10, 3, 0, tzinfo=UTC)  # 北京 11:00
    start = timeutil.day_start("Asia/Shanghai", days_back=6, now=now)
    assert timeutil.local_date(start, "Asia/Shanghai") == "2026-01-04"


def test_month_start_crosses_the_utc_boundary():
    # UTC 2026-02-01 02:00 = 北京 10:00，本月起点是北京 2/1 00:00 = UTC 1/31 16:00
    now = datetime(2026, 2, 1, 2, 0, tzinfo=UTC)
    start = timeutil.month_start("Asia/Shanghai", now=now)
    assert start == datetime(2026, 1, 31, 16, 0, tzinfo=UTC)


def test_local_date_shifts_the_day_for_late_utc_hours():
    late = datetime(2026, 3, 1, 18, 30, tzinfo=UTC)  # 北京 3/2 02:30
    assert timeutil.local_date(late, "Asia/Shanghai") == "2026-03-02"
    assert timeutil.local_date(late, "UTC") == "2026-03-01"


def test_naive_values_are_treated_as_utc():
    # SQLite 回来的是 naive，不处理会让 .astimezone() 按本地时区解释，差 8 小时
    naive = datetime(2026, 3, 1, 18, 30)
    assert timeutil.local_date(naive, "Asia/Shanghai") == "2026-03-02"


def test_bad_timezone_name_falls_back_to_utc():
    assert timeutil.zone("Mars/Olympus") is not None
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert timeutil.day_start("Mars/Olympus", now=now) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)