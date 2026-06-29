"""LoopController：Agent-Loop 续推控制（每轮模型调用前生效，由插件 before_model_callback 委托）。

职责：迭代计数 + 消息预算裁剪 + 计划续推提醒 + 达上限强制收尾（force-summary）。
对应原项目 `_SingleLoopRequestProcessor`，但落在 ADK 公版更稳的 Plugin 扩展点上。
每个请求新建一个实例，故迭代状态用简单自增即可。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.genai import types

from agent.engine.agent_loop.message_budget import MessageBudget
from agent.engine.agent_loop.task_plan_tool import TASK_PLAN_KEY, has_open_steps
from common.obs import get_logger, log_kv

logger = get_logger("agent.loop")


class LoopController:
    def __init__(self, max_iters: int, budget: Optional[MessageBudget] = None) -> None:
        self._max_iters = max(1, max_iters)
        self._budget = budget or MessageBudget()
        self._iter = 0

    def before_model(self, callback_context: Any, llm_request: Any) -> None:
        self._iter += 1
        self._budget.trim(llm_request)

        plan: Optional[dict[str, Any]] = None
        try:
            plan = callback_context.state.get(TASK_PLAN_KEY)
        except Exception:  # noqa: BLE001 - state 读取失败不应影响主流程
            plan = None

        if plan and has_open_steps(plan) and self._iter > 1:
            self._inject(llm_request, "[系统提醒] 你有未完成的计划步骤，请继续推进；不要重复已完成步骤。")

        if self._iter >= self._max_iters:
            log_kv(logger, logging.WARNING, "LoopControl", "max iters reached, force summary",
                   iter=self._iter, max=self._max_iters)
            self._inject(llm_request,
                         "[系统] 已达最大推理步数，请立即基于已有信息给出最终答案，不要再调用任何工具。")

    @staticmethod
    def _inject(llm_request: Any, text: str) -> None:
        contents = list(getattr(llm_request, "contents", None) or [])
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        llm_request.contents = contents
