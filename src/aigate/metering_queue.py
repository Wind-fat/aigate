"""Asynchronous, batched, crash-tolerant metering writer.

Three layers of protection, each fixing a specific failure we can name:

1. **Batching** (why it exists at all). A synchronous `INSERT + INSERT + COMMIT` per
   request made SQLite's single-writer lock the throughput ceiling: 20 concurrent clients,
   ~42 rps, 94% of client latency spent queueing behind the lock.

2. **Disk spool** (crash safety). An in-memory queue loses whatever is pending on
   `kill -9`, OOM, or host reboot. Billing records are the one thing this system must not
   lose, so every event is appended to an NDJSON spool *before* it is acknowledged, and
   removed only after the batch commits.

3. **Replay + dedupe** (at-least-once). On startup the spool is replayed. A crash between
   commit and spool-ack will therefore replay rows that already exist, so the insert path
   filters request_ids that are already in `request_traces` — at-least-once delivery with
   effective exactly-once accounting.

Durability boundary, stated honestly: the spool is flushed to the OS on every append and
fsynced on every ack. A *process* crash loses nothing; an *OS/power* crash can lose the
last few milliseconds of writes. That is the right trade for a local tool; a payment
system would fsync on every append and eat the latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aigate.models import ApiKey, RequestTrace, UsageRecord, utcnow

log = logging.getLogger("aigate.metering")

LAST_USED_RESOLUTION = timedelta(seconds=60)
REPLAY_BATCH = 200


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

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost_cny"] = str(self.cost_cny)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> UsageEvent:
        payload = dict(data)
        payload["cost_cny"] = Decimal(str(payload.get("cost_cny", "0")))
        return cls(**payload)


@dataclass(slots=True)
class _Item:
    seq: int
    event: UsageEvent


class Spool:
    """Append-only NDJSON spool with per-batch acknowledgement.

    Small on purpose: it holds only events that have not been committed yet, so in
    steady state it is a few hundred lines and rewriting it on ack costs nothing.
    """

    def __init__(self, path: Path, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._pending: dict[int, str] = {}

    # ------------------------------------------------------------------ reading
    def load(self) -> list[_Item]:
        if not self.path.exists():
            return []
        items: list[_Item] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # a torn last line after a crash is expected; skip it rather than die
                log.warning("skipping corrupt spool line: %.80s", line)
                continue
            seq = int(record.pop("_seq", 0))
            try:
                event = UsageEvent.from_json(record)
            except TypeError:
                log.warning("skipping spool line with unknown schema: %.80s", line)
                continue
            items.append(_Item(seq=seq, event=event))
            self._pending[seq] = line
        return items

    # ------------------------------------------------------------------ writing
    def append(self, seq: int, event: UsageEvent) -> None:
        line = json.dumps({"_seq": seq, **event.to_json()}, ensure_ascii=False)
        self._pending[seq] = line
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()  # process-crash safe; OS-crash safe comes from ack() fsync
        self._compact_if_huge()

    def ack(self, seqs: list[int]) -> None:
        """Drop committed events from the spool (rewrite + fsync)."""
        if not seqs:
            return
        for seq in seqs:
            self._pending.pop(seq, None)
        self._rewrite()

    def truncate(self) -> None:
        self._pending.clear()
        if self.path.exists():
            self.path.unlink()

    # ------------------------------------------------------------------- helpers
    def _rewrite(self) -> None:
        if not self._pending:
            if self.path.exists():
                self.path.unlink()
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(self._pending.values()) + "\n", encoding="utf-8")
        # Windows refuses fsync() on a read-only handle, so reopen read-write.
        with tmp.open("r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _compact_if_huge(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                log.warning("spool exceeded %d bytes; compacting", self.max_bytes)
                self._rewrite()
        except OSError:  # pragma: no cover - filesystem hiccup must not break ingest
            pass

    def pending_count(self) -> int:
        return len(self._pending)


class MeteringWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        spool: Spool | None = None,
        batch_size: int = 100,
        flush_interval_s: float = 0.2,
        max_queue: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._spool = spool
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self._flush_waiters: list[asyncio.Future[None]] = []
        self._seq = 0

        # observability: numbers that prove batching and replay actually work
        self.written = 0
        self.batches = 0
        self.inline_writes = 0
        self.replayed = 0
        self.duplicates_skipped = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.replay()
        self._task = asyncio.create_task(self._run(), name="aigate-metering-writer")

    async def stop(self, *, timeout: float = 10.0) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self.flush(), timeout=timeout)
        except TimeoutError:  # pragma: no cover - shutdown path
            log.warning("metering flush timed out; %d events remain in the spool", self._spool_remaining())
        finally:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown
                pass
            self._task = None

    async def replay(self) -> None:
        """Write anything left in the spool before serving traffic."""
        if self._spool is None:
            return
        items = self._spool.load()
        if not items:
            return
        log.warning("replaying %d un-acknowledged metering events from spool", len(items))
        for offset in range(0, len(items), REPLAY_BATCH):
            chunk = items[offset : offset + REPLAY_BATCH]
            try:
                await self._write(chunk)
            except Exception:  # noqa: BLE001 - keep the spool intact for the next boot
                log.exception("replay failed; leaving %d events in the spool", len(items) - offset)
                return
            self.replayed += len(chunk)

    # --------------------------------------------------------------------- ingest
    async def submit(self, event: UsageEvent) -> None:
        """Spool first, then queue.

        Order matters: a spool write failure must surface as a slow request, not as a
        silently lost charge.
        """
        self._seq += 1
        item = _Item(seq=self._seq, event=event)

        if self._spool is not None:
            try:
                self._spool.append(item.seq, event)
            except OSError:
                log.exception("spool append failed; falling back to inline write")

        if self._task is None or self._task.done() or self._queue.full():
            self.inline_writes += 1
            if self._task is not None and self._queue.full():
                log.warning("metering queue full; writing inline")
            await self._write([item])
            return
        self._queue.put_nowait(item)

    async def flush(self) -> None:
        """Block until everything queued so far is committed."""
        if self._task is None or self._task.done():
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._flush_waiters.append(waiter)
        await self._queue.put(None)  # sentinel travels behind everything already queued
        await waiter

    # ---------------------------------------------------------------------- loop
    async def _run(self) -> None:  # pragma: no cover - exercised through flush()
        while True:
            item = await self._queue.get()
            if item is None:
                self._notify_flush_waiters()
                continue

            batch = [item]
            sentinel_seen = False
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
                    sentinel_seen = True
                    break
                batch.append(nxt)

            try:
                await self._write(batch)
            except Exception:  # noqa: BLE001 - a bad batch must not kill the writer
                log.exception("metering batch failed (%d events); they stay in the spool", len(batch))
            finally:
                # Wake flush() waiters only AFTER the batch is committed.
                if sentinel_seen:
                    self._notify_flush_waiters()

    def _notify_flush_waiters(self) -> None:
        waiters, self._flush_waiters = self._flush_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _spool_remaining(self) -> int:
        return self._spool.pending_count() if self._spool else 0

    # --------------------------------------------------------------------- write
    async def _write(self, items: list[_Item]) -> None:
        if not items:
            return

        async with self._session_factory() as session:
            # Idempotency: replay after a crash can re-offer events that were already
            # committed (crash between COMMIT and spool ack). Drop them up front instead
            # of relying on a unique constraint that only one of the two tables has.
            request_ids = [i.event.request_id for i in items]
            existing = set(
                (
                    await session.execute(
                        select(RequestTrace.request_id).where(RequestTrace.request_id.in_(request_ids))
                    )
                )
                .scalars()
                .all()
            )
            fresh = [i for i in items if i.event.request_id not in existing]
            self.duplicates_skipped += len(items) - len(fresh)

            if fresh:
                events = [i.event for i in fresh]
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

        # Only now is it safe to drop the spool entries — and note: ALL of them, not just
        # the fresh ones. A duplicate was already in the database, so its committed state
        # is satisfied; leaving it on disk would replay it forever and never drain.
        if self._spool is not None:
            self._spool.ack([i.seq for i in items])
        self.written += len(fresh)
        self.batches += 1

    # ---------------------------------------------------------------------- stats
    def stats(self) -> dict[str, int | float]:
        avg = self.written / self.batches if self.batches else 0.0
        return {
            "written": self.written,
            "batches": self.batches,
            "inline_writes": self.inline_writes,
            "replayed": self.replayed,
            "duplicates_skipped": self.duplicates_skipped,
            "avg_batch": round(avg, 2),
            "queued": self._queue.qsize(),
            "spooled": self._spool_remaining(),
        }


_writer: MeteringWriter | None = None


def configure_writer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    spool_path: str | os.PathLike[str] | None = None,
    batch_size: int = 100,
    flush_interval_s: float = 0.2,
    max_queue: int = 10_000,
) -> MeteringWriter:
    global _writer
    spool = Spool(Path(spool_path)) if spool_path else None
    _writer = MeteringWriter(
        session_factory,
        spool=spool,
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