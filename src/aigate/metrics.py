"""Minimal in-process metrics.

Prometheus text exposition without pulling in a client library: the gateway's
metric surface is small and hand-rolled counters keep the demo dependency-free.
Swap for prometheus_client when we add histograms in Stage 2.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

_lock = Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] += value


def observe_ms(name: str, ms: int, **labels: str) -> None:
    """Same as inc but suffixed with _ms_total (a sum, since we lack histograms)."""
    inc(f"{name}_ms_total", float(ms), **labels)


def snapshot() -> dict[str, float]:
    """扁平化计数器快照，给「运行状态」页用（不用去解析自己输出的 Prometheus 文本）。"""
    out: dict[str, float] = {}
    with _lock:
        items = list(_counters.items())
    for (name, labels), value in items:
        key = name if not labels else name + "{" + ",".join(f"{k}={v}" for k, v in labels) + "}"
        out[key] = value
    return out


def render() -> str:
    lines: list[str] = []
    with _lock:
        items = sorted(_counters.items(), key=lambda kv: kv[0][0])
    for (name, labels), value in items:
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_str}}} {value:g}")
        else:
            lines.append(f"{name} {value:g}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    with _lock:
        _counters.clear()
