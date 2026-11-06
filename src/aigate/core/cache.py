"""Semantic cache — two layers, because they answer different questions.

  L1 exact hash   "have I literally seen this prompt?"        -> zero risk
  L2 vector       "have I seen something close enough?"        -> bounded risk

The cache is a product decision wearing an engineering costume: the threshold decides
whether users get "cheaper" or "occasionally wrong". So it is configurable, every hit
records its similarity, and the failure mode is a hard-to-miss column in usage_records
(`cache_hit`), not a log line nobody reads.

Eligibility is deliberately narrow: only single-shot, temperature=0, non-streaming
requests. Everything else is a different answer on every call, and caching it would be
inventing a bug.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metrics
from aigate.core.embedding import Embedder, HashEmbedder, cosine, normalize_text
from aigate.models import SemanticCacheEntry, utcnow

# Fallback version label. The real value comes from the request (X-Prompt-Version),
# because "prompt changed" is a business event, not a deploy: the template can change
# ten times a day without touching code, and every change must invalidate the cache.
DEFAULT_PROMPT_VERSION = "v1"


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _expired(entry: SemanticCacheEntry, now: datetime) -> bool:
    exp = _as_aware(entry.expires_at)
    return exp is not None and exp <= now


@dataclass
class CacheHit:
    layer: str  # "L1" | "L2"
    similarity: float
    content: str
    provider: str
    model_used: str
    input_tokens: int
    output_tokens: int
    entry_id: str


class SemanticCache:
    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: float | None = None,
        ttl_s: int = 86_400,
        max_scan: int = 2_000,
        embedder: Embedder | None = None,
    ) -> None:
        self.enabled = enabled
        self.embedder = embedder or HashEmbedder()
        self.threshold = threshold if threshold is not None else self.embedder.recommended_threshold
        self.ttl_s = ttl_s
        self.max_scan = max_scan

    # ------------------------------------------------------------------ eligibility
    def eligible(self, payload) -> bool:
        if not self.enabled:
            return False
        if getattr(payload, "stream", False):
            return False
        if getattr(payload, "temperature", None) not in (0, 0.0):
            return False
        if getattr(payload, "tools", None):
            return False
        # Any assistant turn means the answer depends on conversation state.
        return all(m.role != "assistant" for m in payload.messages)

    # ------------------------------------------------------------------------- key
    @staticmethod
    def prompt_key(messages, prompt_version: str = DEFAULT_PROMPT_VERSION) -> tuple[str, str]:
        text = "\n".join(m.content for m in messages if isinstance(getattr(m, "content", None), str))
        norm = normalize_text(text)
        digest = hashlib.sha256(f"{prompt_version}\x00{norm}".encode()).hexdigest()
        return digest, norm

    # ---------------------------------------------------------------------- lookup
    async def lookup(
        self,
        session: AsyncSession,
        project_id: str,
        model: str,
        messages,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
    ) -> CacheHit | None:
        if not self.enabled:
            return None

        prompt_hash, norm = self.prompt_key(messages, prompt_version)
        now = utcnow()

        l1 = (
            await session.execute(
                select(SemanticCacheEntry).where(
                    SemanticCacheEntry.project_id == project_id,
                    SemanticCacheEntry.model == model,
                    SemanticCacheEntry.prompt_version == prompt_version,
                    SemanticCacheEntry.prompt_hash == prompt_hash,
                )
            )
        ).scalar_one_or_none()

        if l1 is not None and not _expired(l1, now):
            await self._hit(session, l1, "L1")
            return self._to_hit(l1, "L1", 1.0)

        # L2: nearest neighbour among this tenant's entries for this model.
        vector = await self.embedder.embed(norm)
        rows = (
            (
                await session.execute(
                    select(SemanticCacheEntry)
                    .where(
                        SemanticCacheEntry.project_id == project_id,
                        SemanticCacheEntry.model == model,
                        SemanticCacheEntry.prompt_version == prompt_version,
                    )
                    .order_by(SemanticCacheEntry.created_at.desc())
                    .limit(self.max_scan)
                )
            )
            .scalars()
            .all()
        )

        best: tuple[float, SemanticCacheEntry] | None = None
        for row in rows:
            if _expired(row, now) or not row.embedding:
                continue
            sim = cosine(vector, row.embedding)
            if sim >= self.threshold and (best is None or sim > best[0]):
                best = (sim, row)

        if best is None:
            return None

        sim, row = best
        await self._hit(session, row, "L2")
        return self._to_hit(row, "L2", sim)

    # ----------------------------------------------------------------------- store
    async def store(
        self,
        session: AsyncSession,
        project_id: str,
        model: str,
        messages,
        *,
        provider: str,
        model_used: str,
        content: str,
        input_tokens: int,
        output_tokens: int,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
    ) -> bool:
        if not self.enabled or not content:
            return False

        prompt_hash, norm = self.prompt_key(messages, prompt_version)
        existing = (
            await session.execute(
                select(SemanticCacheEntry.id).where(
                    SemanticCacheEntry.project_id == project_id,
                    SemanticCacheEntry.model == model,
                    SemanticCacheEntry.prompt_version == prompt_version,
                    SemanticCacheEntry.prompt_hash == prompt_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False

        session.add(
            SemanticCacheEntry(
                project_id=project_id,
                model=model,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                prompt_preview=norm[:300],
                embedding=await self.embedder.embed(norm),
                provider=provider,
                model_used=model_used,
                response_content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                expires_at=utcnow() + timedelta(seconds=self.ttl_s),
            )
        )
        await session.commit()
        return True

    # --------------------------------------------------------------------- helpers
    async def _hit(self, session: AsyncSession, entry: SemanticCacheEntry, layer: str) -> None:
        await session.execute(
            update(SemanticCacheEntry)
            .where(SemanticCacheEntry.id == entry.id)
            .values(hit_count=SemanticCacheEntry.hit_count + 1)
        )
        await session.commit()
        metrics.inc("aigate_cache_hits_total", layer=layer)

    @staticmethod
    def _to_hit(entry: SemanticCacheEntry, layer: str, similarity: float) -> CacheHit:
        return CacheHit(
            layer=layer,
            similarity=round(similarity, 4),
            content=entry.response_content,
            provider=entry.provider,
            model_used=entry.model_used,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            entry_id=entry.id,
        )


_cache: SemanticCache | None = None


def build_cache(**kwargs) -> SemanticCache:
    return SemanticCache(**kwargs)


def get_cache(cache: SemanticCache | None = None) -> SemanticCache:
    global _cache
    if cache is not None:
        _cache = cache
    if _cache is None:
        _cache = SemanticCache()
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
