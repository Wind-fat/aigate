"""Provider registry — lazily built, cached per process.

`mock` and `mock-alt` are two independent instances of the same offline provider.
That is not a hack: the circuit breaker is keyed by provider *name*, so demonstrating
"primary dead -> fallback serves" requires the two candidates to be different vendors.
In production they are (DashScope vs DeepSeek vs vLLM).
"""

from __future__ import annotations

from aigate.config import Settings, get_settings
from aigate.providers.base import BaseProvider, ChatResult, ProviderError, ProviderUsage, StreamEvent
from aigate.providers.mock import MockProvider
from aigate.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "BaseProvider",
    "ChatResult",
    "ProviderError",
    "ProviderUsage",
    "StreamEvent",
    "build_provider",
    "get_provider",
    "reset_providers",
    "known_providers",
]

_REGISTRY: dict[str, BaseProvider] = {}

_MOCK_NAMES = {"mock", "mock-alt"}


def known_providers() -> list[str]:
    return ["mock", "mock-alt", "dashscope", "deepseek", "vllm"]


def build_provider(name: str, settings: Settings | None = None) -> BaseProvider:
    settings = settings or get_settings()
    name = name.lower()
    if name in _MOCK_NAMES:
        return MockProvider()
    if name == "dashscope":
        return OpenAICompatibleProvider(
            "dashscope",
            settings.dashscope_base_url,
            settings.dashscope_api_key,
            settings.request_timeout_s,
        )
    if name == "deepseek":
        return OpenAICompatibleProvider(
            "deepseek",
            settings.deepseek_base_url,
            settings.deepseek_api_key,
            settings.request_timeout_s,
        )
    if name == "vllm":
        return OpenAICompatibleProvider(
            "vllm", settings.vllm_base_url, settings.vllm_api_key, settings.request_timeout_s
        )
    raise ValueError(f"unknown provider: {name}")


def get_provider(name: str, settings: Settings | None = None) -> BaseProvider:
    name = (name or "").lower()
    if name not in _REGISTRY:
        _REGISTRY[name] = build_provider(name, settings)
    return _REGISTRY[name]


def reset_providers() -> None:
    _REGISTRY.clear()
