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
    engine: str = "agent_loop"            # plan_execute | agent_loop | native_loop
    max_loop_iters: int = 8
    # 子代理 / 子 Runner（researcher、Claude SKILL 子 Runner）用哪一代循环。
    # auto = 跟随主引擎（plan_execute 视同 adk）。A2A **不受本项影响**：
    # 远端跑在 a2a_service 自己的 ADK 上，agent 侧开关改不了它。
    sub_agent_engine: Literal["auto", "adk", "native"] = "auto"

    # --- native_loop（自研 Tool-Use 循环，不依赖 Agent 框架）---
    # 流式工具执行（CC 行为）：一轮内除最后一个之外的 tool_call 可提前开跑——完整性信号是
    # "出现了更高 index"，所以末个调用必然等到流结束，单工具调用轮没有提速收益。
    # 关掉即退化为"模型流结束后再统一跑工具"，
    # 出问题时用它一键回退定位——两条路径共用同一套分批规则，只是投递时机不同。
    native_streaming_tool_exec: bool = True
    native_max_tool_concurrency: int = Field(default=10, gt=0)   # 对齐 CC 默认
    native_tool_result_max_chars: int = Field(default=8000, gt=0)
    # 上下文压缩（CC 式）：阈值 = 窗口 − buffer。buffer 取值对齐 CC 的 13k。
    # 注意 context_window_tokens 需与实际所用模型匹配；估算逼近该值即触发摘要压缩。
    context_window_tokens: int = Field(default=128000, gt=0)
    compact_buffer_tokens: int = Field(default=13000, gt=0)
    compact_preserve_units: int = Field(default=6, gt=0)          # 压缩后保留的尾部原子单元数

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
