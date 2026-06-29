"""a2a_service 配置（pydantic-settings，env 驱动）。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class A2AServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    a2a_service_host: str = "127.0.0.1"
    a2a_service_port: int = 8300

    dashscope_api_key: str = "sk-***"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.7-plus"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> A2AServiceSettings:
    return A2AServiceSettings()
