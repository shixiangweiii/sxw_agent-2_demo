"""评测配置；密钥仅经环境变量 DASHSCOPE_API_KEY 读取，绝不落文件。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class EvalConfig:
    base_url: str = "http://127.0.0.1:8000"
    engine: str = "agent_loop"
    agent_uuid: str = "demo-agent"
    arag_url: str = "http://127.0.0.1:8100"
    skill_center_url: str = "http://127.0.0.1:8200"
    a2a_card_url: str = "http://127.0.0.1:8300/.well-known/agent-card.json"
    judge_model: str = "qwen3.7-plus"
    judge_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    request_timeout_s: float = 150.0
    judge_timeout_s: float = 60.0


def api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise SystemExit("[eval] DASHSCOPE_API_KEY 未设置（仅经环境变量注入，勿写入文件）")
    return key
