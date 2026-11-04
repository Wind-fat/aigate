"""Cost accounting and cheap token estimation.

Money is Decimal everywhere — a float rounding error in a bill is a trust incident.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

MONEY_QUANT = Decimal("0.000001")  # 1e-6 CNY precision

# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_1k: Decimal | float | str,
    output_price_per_1k: Decimal | float | str,
) -> Decimal:
    """Price is per 1K tokens (vendor convention). Returns CNY rounded to 1e-6."""
    in_price = Decimal(str(input_price_per_1k))
    out_price = Decimal(str(output_price_per_1k))
    cost = (Decimal(input_tokens) / Decimal(1000)) * in_price
    cost += (Decimal(output_tokens) / Decimal(1000)) * out_price
    return cost.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def tokens_to_cny(total_tokens: int, price_per_1k: Decimal | float | str) -> Decimal:
    return (Decimal(total_tokens) / Decimal(1000) * Decimal(str(price_per_1k))).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_UP
    )


# --------------------------------------------------------------------------
# tokens (estimation only — real numbers come from upstream `usage`)
# --------------------------------------------------------------------------

_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """Rough char-based estimate: ~1 token per CJK char, ~1 per 4 latin chars."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk + other / 4))


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            total += estimate_tokens(str(content))
        total += 4  # per-message overhead
    return total


# --------------------------------------------------------------------------
# trace masking
# --------------------------------------------------------------------------

PHONE = re.compile(r"1[3-9]\d{9}")
ID_CARD = re.compile(r"\b\d{17}[\dXx]\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def mask_pii(text: str) -> str:
    """Conservative regex masking for trace previews (Stage 2 adds full middleware).

    Order matters: ID card before phone, otherwise the 18-digit ID gets partially
    eaten by the 11-digit phone pattern and produces a mangled mask.
    """
    text = ID_CARD.sub(lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], text)
    text = PHONE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    return EMAIL.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[-1], text)