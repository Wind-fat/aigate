"""Embeddings.

Two implementations, same interface:

  HashEmbedder   -- deterministic feature hashing, no network, no key, free.
                    Used for dev, tests and the offline demo.
  APIEmbedder    -- any OpenAI-compatible /embeddings endpoint (DashScope bge-m3).

Why hash embeddings are legitimate here and not a toy: semantic caching only needs a
*stable* similarity ordering over short queries. It does not need semantic understanding
of long documents. Feature hashing over CJK unigrams + bigrams does that job, and it
makes the whole cache testable without a network — which is worth more than 3 extra
points of recall in a cache.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

import httpx

DIM = 256
_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_LATIN = re.compile(r"[a-z0-9]+")
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalisation is deliberately conservative.

    Aggressive normalisation (dropping punctuation, stemming) raises the hit rate and
    also raises the chance of answering the wrong question. For a cache, a missed hit
    costs money; a wrong hit costs trust.
    """
    return _WS.sub(" ", text.strip().lower())


def tokenize(text: str) -> list[str]:
    text = normalize_text(text)
    tokens: list[str] = []
    for run in _CJK_RUN.findall(text):
        tokens.extend(run)  # unigrams
        tokens.extend(a + b for a, b in zip(run, run[1:], strict=False))  # bigrams
    tokens.extend(_LATIN.findall(text))
    return tokens


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Inputs are expected to be L2-normalised; dot product is then the cosine."""
    return sum(x * y for x, y in zip(a, b, strict=False))


class Embedder(ABC):
    dim: int = DIM
    # A cache threshold is only meaningful *relative to an embedder*: hash embeddings
    # live in a lower-similarity regime than bge-m3, so a single global 0.92 would make
    # the offline cache silently useless. Calibrate per embedder instead.
    recommended_threshold: float = 0.92

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...


class HashEmbedder(Embedder):
    dim = DIM
    # Measured on a small paraphrase set: paraphrase ~0.71, unrelated ~0.05.
    # Threshold sits in that gap, closer to the low end because a wrong hit is worse
    # than a miss only up to the point where the cache stops paying for itself.
    recommended_threshold = 0.65

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        return _l2(vec)


class APIEmbedder(Embedder):
    """OpenAI-compatible embeddings (DashScope text-embedding-v3, bge-m3, ...)."""

    recommended_threshold = 0.92

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-v3", dim: int = 1024):
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._key = api_key
        self._model = model
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(self._url, json={"model": self._model, "input": [text]})
            resp.raise_for_status()
            data = resp.json()
        return _l2([float(x) for x in data["data"][0]["embedding"]])


def build_embedder(backend: str = "hash", **kwargs) -> Embedder:
    if backend == "api":
        return APIEmbedder(**kwargs)
    return HashEmbedder()
