"""skill-center 服务配置（pydantic-settings，env 驱动）。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class SkillCenterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    skill_center_port: int = 8200
    # A2A 子代理运行时（skill-center 作为注册表，/instance/list 指向该 a2a 服务的 agent-card）
    a2a_service_base_url: str = "http://127.0.0.1:8300"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> SkillCenterSettings:
    return SkillCenterSettings()
