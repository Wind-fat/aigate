from decimal import Decimal

import pytest

from aigate.metering import compute_cost, estimate_tokens, mask_pii, tokens_to_cny


def test_compute_cost_basic():
    # 1000 in @0.001 + 1000 out @0.002 = 0.001 + 0.002
    assert compute_cost(1000, 1000, "0.001", "0.002") == Decimal("0.003000")


def test_compute_cost_zero_tokens():
    assert compute_cost(0, 0, "0.001", "0.002") == Decimal("0.000000")


def test_compute_cost_rounding_is_stable():
    # half-up at 1e-6: 1 token @ 0.0000005*... verify quantisation, not float noise
    cost = compute_cost(1, 0, Decimal("0.000001"), Decimal("0"))
    assert cost == Decimal("0.000000")
    assert isinstance(cost, Decimal)


def test_tokens_to_cny():
    assert tokens_to_cny(2000, "0.001") == Decimal("0.002000")


def test_estimate_tokens_handles_cjk_and_latin():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello world!") == 3


def test_mask_pii_covers_phone_id_and_email():
    text = "联系我 13812345678 / 110101199001011234 / bob@example.com"
    masked = mask_pii(text)
    assert "13812345678" not in masked
    assert "138****5678" in masked
    assert "110101********1234" in masked
    assert "bob@example.com" not in masked


@pytest.mark.parametrize("tokens,expected", [(0, "0.000000"), (500, "0.000500")])
def test_tokens_to_cny_table(tokens, expected):
    assert tokens_to_cny(tokens, "0.001") == Decimal(expected)
