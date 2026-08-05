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
        # 本方法 = "每转一圈循环执行一次"。ADK 的 while 每步开头都会经过这里。
        self._iter += 1
        # 1) 消息预算：主动裁掉过长历史，防止上下文随工具轮次膨胀。
        #    与 HardenedLiteLlm 里的"超长后截断重试"互补——一个预防，一个救火。
        self._budget.trim(llm_request)

        # 2) 计划续推：从 ADK 的 session state 里读回 update_task_plan 写入的计划。
        #    state 是跨轮持久的，所以模型上一轮登记的计划这一轮还能读到。
        plan: Optional[dict[str, Any]] = None
        try:
            plan = callback_context.state.get(TASK_PLAN_KEY)
        except Exception:  # noqa: BLE001 - state 读取失败不应影响主流程
            plan = None

        # 有未完成步骤就提醒继续推进。`_iter > 1` 是为了避开第一轮——那时计划刚登记，
        # 立刻提醒"继续推进"没有意义反而干扰。
        if plan and has_open_steps(plan) and self._iter > 1:
            self._inject(llm_request, "[系统提醒] 你有未完成的计划步骤，请继续推进；不要重复已完成步骤。")

        # 3) force-summary 软收尾：达到业务轮次上限，用一条系统消息"劝停"模型。
        #    这是软控制——模型仍需要至少再调用一次才能把最终答案写出来，
        #    所以框架硬熔断（max_llm_calls）要比这个值高 2，留出生效窗口。
        if self._iter >= self._max_iters:
            log_kv(logger, logging.WARNING, "LoopControl", "max iters reached, force summary",
                   iter=self._iter, max=self._max_iters)
            self._inject(llm_request,
                         "[系统] 已达最大推理步数，请立即基于已有信息给出最终答案，不要再调用任何工具。")

    @staticmethod
    def _inject(llm_request: Any, text: str) -> None:
        # 以 user 角色追加一条临时消息。关键点：llm_request 只是"本次模型调用的请求视图"，
        # 改它不会写进 session，所以这些系统提醒不会污染对话历史、也不会跨轮累积。
        contents = list(getattr(llm_request, "contents", None) or [])
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        llm_request.contents = contents
