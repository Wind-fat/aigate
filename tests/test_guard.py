"""PII guard: rules, idempotency, and the one assertion that matters —
raw PII must not reach the provider."""

from __future__ import annotations

from aigate.guard import mask_messages, mask_text

SAMPLE = "手机 13812345678，身份证 110101199001011234，邮箱 bob@example.com，卡号 6222021234567890123"


def test_masks_all_four_rules():
    masked, hits = mask_text(SAMPLE)
    assert hits == 4
    assert "13812345678" not in masked and "138****5678" in masked
    assert "110101199001011234" not in masked and "110101********1234" in masked
    assert "bob@example.com" not in masked
    assert "6222021234567890123" not in masked


def test_masking_is_idempotent():
    once, _ = mask_text(SAMPLE)
    twice, hits = mask_text(once)
    assert twice == once
    assert hits == 0, "a second pass must not find anything, or traces would drift"


def test_mask_messages_preserves_structure():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": [{"type": "text", "text": "电话13812345678"}]},
    ]
    out, hits = mask_messages(messages)
    assert hits == 1
    assert out[0]["content"] == "你是助手"
    assert out[1]["content"][0]["text"].endswith("138****5678")
    # input must not be mutated in place
    assert messages[1]["content"][0]["text"] == "电话13812345678"


async def test_gateway_masks_before_upstream(client, auth_headers):
    payload = {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "我的手机号是13812345678，帮我登记"}],
        "temperature": 0,
    }
    resp = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
    assert resp.status_code == 200

    # The mock provider echoes its input, so a leak here means the gateway
    # forwarded raw PII upstream — exactly what this test exists to prevent.
    content = resp.json()["choices"][0]["message"]["content"]
    assert "13812345678" not in content
    assert "138****5678" in content
    assert resp.json()["x_aigate"]["pii_masked"] == 1
