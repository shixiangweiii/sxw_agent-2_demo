"""两代 Tool-Use 循环引擎共用的工具面与指令。

`agent_loop`（ADK 驱动）与 `native_loop`（自研循环）必须面对**逐个相同**的工具集和
系统指令，否则两者的差异就变成了"工具面之争 / prompt 之争"，而不是"循环归谁驱动"
这条我们真正想对比的轴。所以共享部分一律收在本包，两边 import 同一份。

本模块（包的 ``__init__``）只放纯文本常量，不引入任何框架依赖，两代引擎都能安全引用。
子模块按需依赖：``task_plan_tool`` / ``tool_search_tool`` 是普通函数工具（两代共用），
``sub_agent_tool`` 是 researcher 的 ADK AgentTool 包装（仅 `agent_loop` 使用，
`native_loop` 有自己的等价实现，但对外暴露同名同描述的工具）。
"""
from __future__ import annotations

LOOP_INSTRUCTION = (
    "你是一个生产级智能体（Agent-Loop 模式）。在一个循环里思考、调用工具、观察结果，直到产出最终答案。\n"
    "规则：\n"
    "1) 复杂任务可调用 update_task_plan 记录计划和进度；它不是调度器，实际顺序仍由本循环逐轮决定。\n"
    "2) 需要专用能力（如翻译、文本统计）时，先调用 tool_search 发现工具，再调用对应工具。\n"
    "3) 复杂、开放的子问题可委派给 researcher 子代理。\n"
    # 注：A/B(repeat3) 显示对 agent_loop 加重的"必须检索/严格忠实"约束反而使 kq-rag/kq-multidoc
    # 偶发跳过检索、忠实度下降（KQ 15/15→9/15、faith 4.5→3.5），故保留原较轻指令；
    # 同款约束对 plan_execute 是净增益（见 eval/reports/AB-prompt-v1.md）。
    "4) 回答知识型/企业内部问题前先调用 knowledge_search 检索；若返回了资料，"
    "回答时在引用处用 [n] 标注（n 为资料序号）；若无资料则据常识回答，绝不编造引用。\n"
    "5) Claude Skill 是与普通工具相同的 tool-use：一次调用对应一个有界的 Agent-as-Tool 结果。"
    "有数据依赖的 Skill 必须等上游 tool_result 后在下一轮调用；同轮多个 Skill 调用只能用于彼此独立的任务。\n"
    "6) 工具失败时检查结构化 error.code 与 error.retryable，决定重试、换路或降级，不要中断。\n"
    "7) 信息足够后用简洁中文给出最终答案，并停止调用工具。"
)

# 循环控制注入的临时提醒（只进本次模型请求，不写入会话历史）。
PLAN_CONTINUATION_REMINDER = "[系统提醒] 你有未完成的计划步骤，请继续推进；不要重复已完成步骤。"
FORCE_SUMMARY_REMINDER = "[系统] 已达最大推理步数，请立即基于已有信息给出最终答案，不要再调用任何工具。"


def resolve_sub_agent_engine(sub_agent_engine: str, main_engine: str) -> str:
    """把 ``SUB_AGENT_ENGINE`` 解析成 ``adk`` / ``native``。

    ``auto`` 跟随主引擎；``plan_execute`` 不是循环引擎，视同 ``adk``。
    单变量而非多变量是刻意的——多个独立开关会造出无法逐一手工验证的组合矩阵。
    """
    choice = (sub_agent_engine or "auto").strip().lower()
    if choice == "auto":
        return "native" if main_engine == "native_loop" else "adk"
    return "native" if choice == "native" else "adk"


__all__ = [
    "FORCE_SUMMARY_REMINDER",
    "LOOP_INSTRUCTION",
    "PLAN_CONTINUATION_REMINDER",
    "resolve_sub_agent_engine",
]
