"""Minimal in-process metrics.

Prometheus text exposition without pulling in a client library: the gateway's
metric surface is small and hand-rolled counters keep the demo dependency-free.
Swap for prometheus_client when we add histograms in Stage 2.

两种量，别混用：

  counter（`inc` / `observe_ms`）回答"累计多少"——请求数、超时次数。
  gauge（`gauge`）回答"现在是多少"——池预热花了多少毫秒、建好了几条连接。
  后者累加没有意义：第二次启动预热 20ms，总量 50ms 不代表"更慢"。
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

_lock = Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] += value


def observe_ms(name: str, ms: int, **labels: str) -> None:
    """Same as inc but suffixed with _ms_total (a sum, since we lack histograms)."""
    inc(f"{name}_ms_total", float(ms), **labels)


def gauge(name: str, value: float, **labels: str) -> None:
    """把某个量设成"当前值"（覆盖，不累加）。同名同标签后写覆盖先写。"""
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _gauges[key] = float(value)


def _flat(
    items: list[tuple[tuple[str, tuple[tuple[str, str], ...]], float]]
) -> dict[str, float]:
    out: dict[str, float] = {}
    for (name, labels), value in items:
        key = name if not labels else name + "{" + ",".join(f"{k}={v}" for k, v in labels) + "}"
        out[key] = value
    return out


def snapshot() -> dict[str, float]:
    """扁平化快照（计数器 + gauge），给「运行状态」页用（不用解析自己输出的文本）。"""
    with _lock:
        items = list(_counters.items())
        gauges = list(_gauges.items())
    out = _flat(items)
    out.update(_flat(gauges))
    return out


def render() -> str:
    lines: list[str] = []
    with _lock:
        items = sorted(_counters.items(), key=lambda kv: kv[0][0])
        gauges = sorted(_gauges.items(), key=lambda kv: kv[0][0])
    for source in (items, gauges):
        for (name, labels), value in source:
            if labels:
                label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {value:g}")
            else:
                lines.append(f"{name} {value:g}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()
