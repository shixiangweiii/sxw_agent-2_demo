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

    # --- Engine Runtime（引擎由每个 Run 选择，不再由启动期 ENGINE 选型）---
    max_loop_iters: int = Field(default=8, gt=0)
    # 子代理 / 子 Runner（researcher、Claude SKILL 子 Runner）用哪一代循环。
    # auto = 跟随主引擎（plan_execute 视同 adk）。A2A **不受本项影响**：
    # 远端跑在 a2a_service 自己的 ADK 上，agent 侧开关改不了它。
    sub_agent_engine: Literal["auto", "adk", "native"] = "auto"

    # --- native_loop（自研 Tool-Use 循环，不依赖 Agent 框架）---
    # 工具提前派发只改变“何时开始工具”，不影响模型正文或 Skill UI 的流式展示。
    # production 默认 off；experimental_heuristic 保留现有启发式实验机制；当前
    # OpenAI-compatible provider 不提供 block-complete 信号，第三种模式启动失败。
    native_early_tool_dispatch: Literal[
        "off", "experimental_heuristic", "provider_block_complete"
    ] = "off"
    native_max_tool_concurrency: int = Field(default=10, gt=0)   # 对齐 CC 默认
    native_max_tool_calls_per_turn: int = Field(default=64, gt=0)
    native_max_tool_calls_per_run: int = Field(default=256, gt=0)
    native_max_tool_argument_bytes: int = Field(default=64 * 1024, gt=0)
    native_max_tool_batch_argument_bytes: int = Field(default=256 * 1024, gt=0)
    native_max_model_output_bytes: int = Field(default=1024 * 1024, gt=0)
    native_max_checkpoint_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    native_max_tool_catalog_bytes: int = Field(default=1024 * 1024, gt=0)
    native_max_skill_event_bytes: int = Field(default=64 * 1024, gt=0)
    native_max_skill_events_per_run: int = Field(default=2000, gt=0)
    native_max_skill_event_bytes_per_run: int = Field(default=8 * 1024 * 1024, gt=0)
    native_tool_result_max_chars: int = Field(default=8000, gt=0)
    # 上下文压缩（CC 式）：阈值 = 窗口 − buffer。buffer 取值对齐 CC 的 13k。
    # 注意 context_window_tokens 需与实际所用模型匹配；估算逼近该值即触发摘要压缩。
    context_window_tokens: int = Field(default=128000, gt=0)
    compact_buffer_tokens: int = Field(default=13000, gt=0)
    compact_preserve_units: int = Field(default=6, gt=0)          # 压缩后保留的尾部原子单元数

    def model_post_init(self, __context: object) -> None:
        if self.compact_buffer_tokens >= self.context_window_tokens:
            raise ValueError("compact_buffer_tokens must be smaller than context_window_tokens")
        if self.native_max_tool_concurrency > self.native_max_tool_calls_per_turn:
            raise ValueError(
                "native_max_tool_concurrency cannot exceed native_max_tool_calls_per_turn"
            )
        if self.native_max_tool_batch_argument_bytes < self.native_max_tool_argument_bytes:
            raise ValueError(
                "native_max_tool_batch_argument_bytes cannot be smaller than the per-call limit"
            )

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

    # --- trace 可观测（结构化轨迹，供评测结合轨迹定位失败原因）---
    # production 默认 summary。full 会把非敏感用户原文和模型输入落盘，只能在
    # 明确的本机诊断窗口临时开启；结构化字段、嵌套 JSON 字符串与图片/二进制
    # 仍先经 common.trace.redact 脱敏/摘要。
    trace_enabled: bool = True
    trace_payload_level: Literal["none", "summary", "full"] = "summary"
    trace_dir: str = "local_storage/traces"
    trace_max_field_chars: int = Field(default=20000, gt=0)   # 单字段截断，防巨型工具结果
    trace_retention_days: int = Field(default=7, ge=0)        # 0 = 不清理

    # --- Canonical Runtime / Worker ---
    runtime_db_path: str = "local_storage/runtime/runtime.db"
    artifact_root: str = "local_storage/artifacts"
    runtime_worker_id: str = "runtime-worker-local"
    runtime_worker_concurrency: int = Field(default=4, gt=0)
    runtime_worker_poll_ms: int = Field(default=250, gt=0)
    runtime_lease_seconds: int = Field(default=30, gt=0)
    runtime_lease_renew_seconds: int = Field(default=10, gt=0)
    runtime_shutdown_grace_seconds: int = Field(default=20, gt=0)
    runtime_event_flush_ms: int = Field(default=100, gt=0)
    runtime_event_flush_bytes: int = Field(default=2048, gt=0)
    runtime_sse_poll_ms: int = Field(default=250, gt=0)
    runtime_sse_heartbeat_seconds: int = Field(default=15, gt=0)
    runtime_busy_timeout_ms: int = Field(default=5000, gt=0)
    runtime_default_deadline_seconds: int = Field(default=600, gt=0)
    runtime_artifact_cleanup_interval_seconds: int = Field(default=3600, gt=0)
    runtime_artifact_orphan_age_hours: int = Field(default=24, gt=0)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
