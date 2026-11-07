"""「这次调用算不算成功」——只此一份口径。

同一个词出现在控制台两个地方：概览卡片（`admin.overview`）和模型对比表
（`admin.model_compare`）。它们各自维护过一份白名单，连精度都不同（2 位 / 1 位），
同一屏上两个数字可以互相矛盾。白名单、分母、精度三项只能从这里取。

规则（两条端点共用）：
  - 只有 `ok` / `fallback` / `cached` 算成功；
  - 表外状态（`error`、空串、未来新增的任何值）一律算**未成功**——未知不能算绿；
  - 分母是窗口内的**全部**调用行，不是"有状态的行"，也不是"有 usage 行的行"；
  - 没有调用时比率是 `None`，不是 `0.0`——"0% 成功"和"没有数据"是两件事。
"""

from __future__ import annotations

from collections.abc import Iterable

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