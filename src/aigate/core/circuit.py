"""Per-provider circuit breaker.

Why a breaker on top of fallback: fallback alone retries a dead vendor on every
single request. The breaker stops calling it after N consecutive failures and
probes again after a cooldown, so recovery is automatic but cheap.

State is per-process. With N replicas each has its own view — acceptable for
MVP, documented in ADR-0004 as the reason Redis would be needed later.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probing: bool = False
    total_opened: int = 0


@dataclass
class CircuitBreaker:
    threshold: int = 3
    cooldown_s: float = 30.0
    _states: dict[str, _State] = field(default_factory=dict)

    def _state(self, provider: str) -> _State:
        return self._states.setdefault(provider, _State())

    def allow(self, provider: str) -> tuple[bool, str]:
        """(allowed, reason). reason is 'closed' | 'circuit_open' | 'half_open_probe'."""
        st = self._state(provider)
        if st.opened_at is None:
            return True, "closed"
        if time.monotonic() - st.opened_at >= self.cooldown_s:
            if not st.probing:
                st.probing = True
                return True, "half_open_probe"
            return False, "circuit_open"
        return False, "circuit_open"

    def record_success(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures = 0
        st.opened_at = None
        st.probing = False

    def record_failure(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures += 1
        st.probing = False
        if st.consecutive_failures >= self.threshold and st.opened_at is None:
            st.opened_at = time.monotonic()
            st.total_opened += 1

    def is_open(self, provider: str) -> bool:
        return self._state(provider).opened_at is not None

    def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "open": st.opened_at is not None,
                "consecutive_failures": st.consecutive_failures,
                "opened_times": st.total_opened,
            }
            for name, st in self._states.items()
        }

    def reset(self) -> None:
        self._states.clear()


_breaker = CircuitBreaker()


def get_breaker() -> CircuitBreaker:
    return _breaker


def configure_breaker(threshold: int, cooldown_s: float) -> CircuitBreaker:
    global _breaker
    _breaker = CircuitBreaker(threshold=threshold, cooldown_s=cooldown_s)
    return _breaker
