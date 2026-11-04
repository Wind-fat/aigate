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
    """Trace-preview masking.

    The rules live in `aigate.guard` so inbound masking and trace masking can never
    drift apart — a privacy rule that exists in only one of the two paths is a bug.
    """
    from aigate.guard import mask_text

    return mask_text(text)[0]
