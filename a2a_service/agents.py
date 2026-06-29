"""A2A 托管的 ADK 子代理定义。"""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from a2a_service.config import A2AServiceSettings


def build_llm(settings: A2AServiceSettings) -> LiteLlm:
    return LiteLlm(
        model=f"openai/{settings.llm_model}",
        api_base=settings.llm_base_url,
        api_key=settings.dashscope_api_key,
        extra_body={"enable_thinking": False},
    )


def build_math_expert(settings: A2AServiceSettings) -> LlmAgent:
    return LlmAgent(
        name="math_expert",
        model=build_llm(settings),
        description="数学专家：擅长精确的算术与数学计算，给出步骤与最终结果。",
        instruction="你是数学专家。对用户的数学问题给出清晰的计算步骤和精确的最终结果，只回答与计算相关的内容。",
    )
