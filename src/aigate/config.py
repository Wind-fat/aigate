"""Application settings — single source of truth, loaded from env with prefix AIGATE_."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AIGATE_", extra="ignore")

    app_name: str = "AIGate"
    version: str = "0.2.0"
    env: str = "dev"
    log_level: str = "INFO"

    # --- storage ---
    database_url: str = "sqlite+aiosqlite:///./aigate.db"
    # create_all on boot. Set false in production once Alembic owns the schema.
    auto_create_schema: bool = True

    # --- control plane auth ---
    admin_token: str = "dev-admin-token"

    # --- data plane ---
    default_provider: str = "mock"
    request_timeout_s: float = 30.0
    default_rpm: int = 60

    # --- rate limiting / circuit breaker ---
    rate_limiter: str = "memory"  # memory | redis
    redis_url: str = "redis://127.0.0.1:6379/0"
    circuit_threshold: int = 3
    circuit_cooldown_s: float = 30.0

    # --- providers ---
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    vllm_base_url: str = "http://127.0.0.1:8000/v1"
    vllm_api_key: str = "not-needed"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
