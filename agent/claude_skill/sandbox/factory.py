"""沙箱工厂：按 provider 返回实现（env SANDBOX_PROVIDER=local|agentbay）。"""
from __future__ import annotations

from typing import Union

from agent.claude_skill.sandbox.agentbay_sandbox import AgentbaySandbox
from agent.claude_skill.sandbox.base import BaseSandbox, EnumSandboxProvider, SandboxError
from agent.claude_skill.sandbox.local_sandbox import LocalSandbox


def build_sandbox(provider: Union[EnumSandboxProvider, str]) -> BaseSandbox:
    p = EnumSandboxProvider(provider) if isinstance(provider, str) else provider
    if p == EnumSandboxProvider.LOCAL:
        return LocalSandbox()
    if p == EnumSandboxProvider.AGENT_BAY:
        return AgentbaySandbox()
    raise SandboxError(f"unknown sandbox provider: {provider}")
