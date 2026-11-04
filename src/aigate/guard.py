"""Inbound PII guard.

Two jobs, same rules:
  1. mask before the request leaves the gateway (privacy)
  2. mask before a prompt is written to the trace (auditability)

Rule order is not cosmetic: an 18-digit ID card also contains an 11-digit run that
looks like a phone number, so ID must run first or you get a mangled mask.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _apply(pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str], text: str) -> tuple[str, int]:
    hits = 0

    def _wrap(m: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        return repl(m)

    return pattern.sub(_wrap, text), hits


def mask_text(text: str) -> tuple[str, int]:
    """Return (masked_text, number_of_matched_spans)."""
    if not text:
        return text, 0
    total = 0
    text, n = _apply(ID_CARD, lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], text)
    total += n
    text, n = _apply(
        BANK_CARD, lambda m: m.group(0)[:4] + "*" * (len(m.group(0)) - 8) + m.group(0)[-4:], text
    )
    total += n
    text, n = _apply(PHONE, lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    total += n
    text, n = _apply(EMAIL, lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[-1], text)
    total += n
    return text, total


def mask_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Mask string contents in an OpenAI-style message list, structure preserved.

    Returns new objects: mutating the caller's list in place would make the PII
    count depend on call order, which is exactly the kind of bug that shows up
    only in production traces.
    """
    hits = 0
    out: list[dict[str, Any]] = []
    for msg in messages:
        clone = dict(msg)
        content = clone.get("content")
        if isinstance(content, str):
            clone["content"], n = mask_text(content)
            hits += n
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    masked, n = mask_text(part["text"])
                    hits += n
                    part = {**part, "text": masked}
                parts.append(part)
            clone["content"] = parts
        out.append(clone)
    return out, hits
