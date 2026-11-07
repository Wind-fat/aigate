"""「这次调用算不算成功」——只此一份口径。

同一个词出现在控制台三个地方：概览卡片（`admin.overview`）、模型对比表
（`admin.model_compare`）、影子对比汇总（`admin_ops.shadow_summary`）。它们各自维护过
一份白名单，连精度都不同（2 位 / 1 位），同一屏上两个数字可以互相矛盾。白名单、分母、
精度三项只能从这里取。

规则（三条端点共用）：
  - 只有 `ok` / `fallback` / `cached` 算成功；
  - 表外状态（`error`、空串、未来新增的任何值）一律算**未成功**——未知不能算绿；
  - 分母是窗口内的**全部**调用行，不是"有状态的行"，也不是"有 usage 行的行"；
  - 没有调用时比率是 `None`，不是 `0.0`——"0% 成功"和"没有数据"是两件事。

两种行、两套分母，一个入口：

  网关调用行（`UsageRecord`）有 `status`，分母是"这段时间所有调用"。
  影子对比行（`ShadowRun`）**没有** `status`：它的成败在写库那一刻就由影子链路判成了
  布尔 `ok`（只有真跑完且供应商返回成功才为真）；分母是"影子跑了几次"，比调用行小得多
  （只有被抽中做对比的那部分）。所以 `strict=False` 只承认行自己带的 `ok`，绝不拿
  `is_success(None)` 把整批影子行判成失败、把成功率压成 0。
  两者共用 `count_success_rows` / `success_rate`，差异只有"分母是哪些行"这一句。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

OK_STATUSES: tuple[str, ...] = ("ok", "fallback", "cached")
SUCCESS_RATE_DIGITS = 2


def is_success(status: str | None) -> bool:
    return status in OK_STATUSES


def count_success(statuses: Iterable[str | None]) -> tuple[int, int]:
    """返回 `(成功数, 总数)`。分母的唯一定义就在这里。"""
    ok = 0
    total = 0
    for status in statuses:
        total += 1
        if is_success(status):
            ok += 1
    return ok, total


def success_rate(ok: int, total: int) -> float | None:
    """百分比，两位小数。`total == 0` 时是 `None`（未知），不是 `0.0`。"""
    if total <= 0:
        return None
    return round(ok / total * 100, SUCCESS_RATE_DIGITS)


def success_rate_of(statuses: Iterable[str | None]) -> float | None:
    """`count_success` + `success_rate` 的便捷组合（测试与统计脚本用）。"""
    ok, total = count_success(statuses)
    return success_rate(ok, total)


def row_is_success(row: Any, *, strict: bool = True) -> bool:
    """一行算不算成功。

    strict=True  网关调用行：只看 `status` 白名单，表外状态一律未成功。
    strict=False 影子对比行：只认行上的布尔 `ok`；行里没有 `ok` 才退回去看 `status`，
                 两者都没有就是未成功（宁可少算成功，也不把未知染成绿色）。
    """
    if strict:
        return is_success(getattr(row, "status", None))
    flag = getattr(row, "ok", None)
    if flag is not None:
        return bool(flag)
    return is_success(getattr(row, "status", None))


def count_success_rows(rows: Iterable[Any], *, strict: bool = True) -> tuple[int, int]:
    """行对象版本的 `count_success`：返回 `(成功数, 总数)`，分母就是传进来的行数。"""
    ok = 0
    total = 0
    for row in rows:
        total += 1
        if row_is_success(row, strict=strict):
            ok += 1
    return ok, total


def success_rate_of_rows(rows: Iterable[Any], *, strict: bool = True) -> float | None:
    """行对象版的成功率入口。影子汇总用 `strict=False`，其余端点用默认值。"""
    ok, total = count_success_rows(rows, strict=strict)
    return success_rate(ok, total)