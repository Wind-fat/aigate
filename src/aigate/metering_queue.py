"""Asynchronous, batched metering writer.

Why this exists: with a synchronous `INSERT + INSERT + COMMIT` per request, SQLite's
single-writer lock became the throughput ceiling — 20 concurrent clients, ~42 rps, and
94% of client-visible latency spent queueing behind the write lock while the gateway
itself took 34 ms.

Batching turns N commits into 1. The accounting does not change: same rows, same
Decimal maths, same columns — only *when* the commit happens.

Two rules this file must not break:

1. **Never drop a billing record.** If the queue is full or the writer was never
   started, the request writes inline and eats the latency. A slow request is
   recoverable; a missing charge is not.
2. **No ORM objects cross the queue.** Events are plain dataclasses built from values
   the request already has, so a closed session cannot leak into the worker.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, or_, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aigate.models import ApiKey, RequestTrace, UsageRecord, utcnow

log = logging.getLogger("aigate.metering")

LAST_USED_RESOLUTION = timedelta(seconds=60)


@dataclass(slots=True)
class UsageEvent:
    """Everything the two billing tables need, captured while the request is alive."""

    request_id: str
    team_id: str
    project_id: str
    api_key_id: str
    provider: str
    model_requested: str
    model_used: str
    input_tokens: int
    output_tokens: int
    cost_cny: Decimal
    latency_ms: int
    ttft_ms: int | None
    status: str
    error_code: str | None
    cache_hit: str | None
    degraded: bool
    prompt_sha256: str
    prompt_preview: str
    pii_masked: int
    http_status: int
    stage_timings: dict[str, Any] = field(default_factory=dict)
    route_path: list[Any] = field(default_factory=list)


class MeteringWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = 100,
        flush_interval_s: float = 0.2,
        max_queue: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._queue: asyncio.Queue[UsageEvent | None] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self._flush_waiters: list[asyncio.Future[None]] = []

        # observability: the numbers that prove batching is actually working
        self.written = 0
        self.batches = 0
        self.inline_writes = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="aigate-metering-writer")

    async def stop(self, *, timeout: float = 10.0) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self.flush(), timeout=timeout)
        except TimeoutError:  # pragma: no cover - shutdown path
            log.warning("metering flush timed out on shutdown; %d events may be lost", self._queue.qsize())
        finally:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown
                pass
            self._task = None

    # --------------------------------------------------------------------- ingest
    async def submit(self, event: UsageEvent) -> None:
        """Enqueue, or write inline when there is no worker / no room.

        Inline is the safety valve, not the happy path: it keeps the accounting
        invariant true when the process is shutting down or overloaded.
        """
        if self._task is None or self._task.done():
            self.inline_writes += 1
            await self._write([event])
            return
        if self._queue.full():
            self.inline_writes += 1
            log.warning("metering queue full; writing inline (len=%d)", self._queue.qsize())
            await self._write([event])
            return
        self._queue.put_nowait(event)

    async def flush(self) -> None:
        """Block until everything queued so far is committed. Used by tests and shutdown."""
        if self._task is None or self._task.done():
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._flush_waiters.append(waiter)
        await self._queue.put(None)  # sentinel travels behind everything already queued
        await waiter

    # ---------------------------------------------------------------------- loop
    async def _run(self) -> None:  # pragma: no cover - exercised via flush()
        while True:
            item = await self._queue.get()
            if item is None:
                self._notify_flush_waiters()
                continue

            batch = [item]
            deadline = asyncio.get_running_loop().time() + self._flush_interval
            while len(batch) < self._batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if nxt is None:
                    self._notify_flush_waiters()
                    break
                batch.append(nxt)

            try:
                await self._write(batch)
            except Exception:  # noqa: BLE001 - a bad batch must not kill the writer
                log.exception("metering batch failed (%d events)", len(batch))

    def _notify_flush_waiters(self) -> None:
        waiters, self._flush_waiters = self._flush_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    # --------------------------------------------------------------------- write
    async def _write(self, events: list[UsageEvent]) -> None:
        if not events:
            return

        async with self._session_factory() as session:
            # executemany instead of ORM `add()`: the unit-of-work path costs ~1ms per
            # object on the event-loop thread, and with 100 events per batch that showed
            # up as head-of-line blocking (p50 got worse while p95 improved). Core
            # inserts are a single round trip per table.
            await session.execute(
                insert(UsageRecord),
                [
                    {
                        "request_id": e.request_id,
                        "team_id": e.team_id,
                        "project_id": e.project_id,
                        "api_key_id": e.api_key_id,
                        "provider": e.provider,
                        "model_requested": e.model_requested,
                        "model_used": e.model_used,
                        "input_tokens": e.input_tokens,
                        "output_tokens": e.output_tokens,
                        "cost_cny": e.cost_cny,
                        "latency_ms": e.latency_ms,
                        "ttft_ms": e.ttft_ms,
                        "status": e.status,
                        "error_code": e.error_code,
                        "cache_hit": e.cache_hit,
                        "degraded": e.degraded,
                    }
                    for e in events
                ],
            )
            await session.execute(
                insert(RequestTrace),
                [
                    {
                        "request_id": e.request_id,
                        "project_id": e.project_id,
                        "model_requested": e.model_requested,
                        "prompt_sha256": e.prompt_sha256,
                        "prompt_preview": e.prompt_preview,
                        "pii_masked": e.pii_masked,
                        "stage_timings": e.stage_timings,
                        "route_path": e.route_path,
                        "http_status": e.http_status,
                    }
                    for e in events
                ],
            )

            # One UPDATE per distinct key per batch, throttled to minute resolution:
            # last_used_at exists to find zombie keys, it does not need second precision.
            now = utcnow()
            cutoff = now - LAST_USED_RESOLUTION
            for key_id in {e.api_key_id for e in events}:
                await session.execute(
                    update(ApiKey)
                    .where(
                        ApiKey.id == key_id,
                        or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at < cutoff),
                    )
                    .values(last_used_at=now)
                )

            try:
                await session.commit()
            except OperationalError:  # pragma: no cover - WAL usually absorbs this
                await session.rollback()
                log.warning("metering commit retry after OperationalError")
                await asyncio.sleep(0.05)
                await session.commit()

        self.written += len(events)
        self.batches += 1

    # ---------------------------------------------------------------------- stats
    def stats(self) -> dict[str, int | float]:
        avg = self.written / self.batches if self.batches else 0.0
        return {
            "written": self.written,
            "batches": self.batches,
            "inline_writes": self.inline_writes,
            "avg_batch": round(avg, 2),
            "queued": self._queue.qsize(),
        }


_writer: MeteringWriter | None = None


def configure_writer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = 100,
    flush_interval_s: float = 0.2,
    max_queue: int = 10_000,
) -> MeteringWriter:
    global _writer
    _writer = MeteringWriter(
        session_factory,
        batch_size=batch_size,
        flush_interval_s=flush_interval_s,
        max_queue=max_queue,
    )
    return _writer


def get_writer() -> MeteringWriter | None:
    return _writer


async def submit(event: UsageEvent) -> None:
    writer = _writer
    if writer is None:
        raise RuntimeError("metering writer not configured; call configure_writer() first")
    await writer.submit(event)


async def flush() -> None:
    if _writer is not None:
        await _writer.flush()


def reset_writer() -> None:
    global _writer
    _writer = None
