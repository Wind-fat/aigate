"""出站客户端：本机地址一律绕过系统环境代理。

httpx 只认环境变量里的 `no_proxy`，不读 Windows 的 `proxy_bypass()`（ProxyOverride）。
本机 ProxyServer=127.0.0.1:7897 时，发往 127.0.0.1:8000（默认 vLLM）的请求会被塞进
代理，表现为「vLLM 明明起着，网关却连不上/502」。
"""

from __future__ import annotations

import httpx
import pytest

from aigate.core.embedding import APIEmbedder
from aigate.providers.openai_compat import OpenAICompatibleProvider

PROXY = "http://127.0.0.1:7897"

LOOPBACK_BASE_URLS = [
    "http://127.0.0.1:8000/v1",
    "http://127.0.0.2:8000/v1",
    "http://localhost:8000/v1",
    "http://[::1]:8000/v1",
]
REMOTE_BASE_URLS = ["https://api.deepseek.com/v1", "https://dashscope.aliyuncs.com/compatible-mode/v1"]


@pytest.fixture
def env_proxy(monkeypatch):
    """模拟本机系统代理：httpx 会读这三个变量。"""
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    monkeypatch.setenv("ALL_PROXY", PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    return PROXY


def _spy_async_client(monkeypatch) -> list[dict]:
    captured: list[dict] = []
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        captured.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


@pytest.mark.parametrize("base_url", LOOPBACK_BASE_URLS)
async def test_openai_compat_loopback_client_disables_env_proxy(monkeypatch, env_proxy, base_url):
    captured = _spy_async_client(monkeypatch)
    provider = OpenAICompatibleProvider("local", base_url)
    try:
        assert captured, "provider 必须真的构造了 httpx.AsyncClient"
        assert captured[0].get("trust_env") is False, f"{base_url} 是本机地址，不能继承环境代理"

        # 行为层面的第二条证据：真实 transport 上没有挂 proxy
        transport = provider._client._transport_for_url(httpx.URL(f"{base_url}/chat/completions"))
        assert getattr(transport._pool, "_proxy_url", None) is None
    finally:
        await provider.aclose()


async def test_openai_compat_remote_client_keeps_env_proxy(monkeypatch, env_proxy):
    """远端供应商仍然要能用公司出口代理，别为了修本机把远程代理一起关掉。"""
    captured = _spy_async_client(monkeypatch)
    provider = OpenAICompatibleProvider("deepseek", REMOTE_BASE_URLS[0])
    try:
        assert captured[0].get("trust_env") is True
        transport = provider._client._transport_for_url(httpx.URL(f"{REMOTE_BASE_URLS[0]}/chat/completions"))
        assert getattr(transport._pool, "_proxy_url", None) is not None
    finally:
        await provider.aclose()


class _Resp:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": [{"embedding": [0.6, 0.8]}]}


class _FakeAsyncClient:
    created: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        _FakeAsyncClient.created.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return _Resp()


@pytest.mark.parametrize("base_url", LOOPBACK_BASE_URLS)
async def test_api_embedder_loopback_disables_env_proxy(monkeypatch, env_proxy, base_url):
    _FakeAsyncClient.created = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    await APIEmbedder(base_url, "sk-test", model="bge-m3", dim=2).embed("你好")
    assert _FakeAsyncClient.created
    assert _FakeAsyncClient.created[-1].get("trust_env") is False


async def test_api_embedder_remote_keeps_env_proxy(monkeypatch, env_proxy):
    _FakeAsyncClient.created = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    await APIEmbedder(REMOTE_BASE_URLS[1], "sk-test", model="text-embedding-v3", dim=2).embed("你好")
    assert _FakeAsyncClient.created[-1].get("trust_env") is True
