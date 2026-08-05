"""agent 服务配置（pydantic-settings，env 驱动）。"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM (DashScope, OpenAI-compatible) ---
    dashscope_api_key: str = "sk-***"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.7-plus"
    embedding_model: str = "text-embedding-v3"

    # --- Engine ---
    engine: str = "agent_loop"            # plan_execute | agent_loop
    max_loop_iters: int = 8

    # --- Services ---
    agent_port: int = 8000
    arag_base_url: str = "http://127.0.0.1:8100"
    arag_timeout_ms: int = 8000

    # --- skill-center ---
    skill_center_base_url: str = "http://127.0.0.1:8200"
    skill_center_timeout_ms: int = 8000
    skill_center_stream_timeout_ms: int = 60000
    agent_uuid: str = "demo-agent"

    # --- claude-skill 沙箱 ---
    sandbox_provider: Literal["local", "agentbay"] = "local"  # agentbay 为桩，不可跑
    skill_call_timeout_seconds: float = Field(default=120, gt=0)
    skill_max_llm_calls: int = Field(default=16, gt=0)
    skill_max_parallel_calls: int = Field(default=2, gt=0)
    skill_result_max_chars: int = Field(default=8000, gt=0)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
