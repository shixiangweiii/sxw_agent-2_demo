"""a2a_service 入口：用 ADK `to_a2a` 把 math_expert 子代理暴露为 A2A 服务。

`to_a2a` 返回 Starlette 应用，暴露 `/.well-known/agent-card.json`（agent-card 发现）+ JSON-RPC（message/send·stream）。
agent-card 的 url 由 host/port 决定，故以独立服务方式运行（端口正确、无需挂载改写）。
"""
from __future__ import annotations

from google.adk.a2a.utils.agent_to_a2a import to_a2a

from a2a_service.agents import build_math_expert
from a2a_service.config import get_settings

settings = get_settings()

# Starlette ASGI 应用；用 `uvicorn a2a_service.main:app --port 8300` 运行。
app = to_a2a(
    build_math_expert(settings),
    host=settings.a2a_service_host,
    port=settings.a2a_service_port,
    protocol="http",
)
