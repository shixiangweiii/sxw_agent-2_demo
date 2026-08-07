"""native_loop：不依赖任何 Agent 框架的自研 Tool-Use 循环（Gen3）。

以 Claude Code 的 `query.ts:queryLoop()` 为蓝本。与 `agent_loop`（ADK 驱动）
面对**完全相同**的工具面、系统指令和 SSE 契约，唯一区别是循环归谁驱动：
这里的 `while` 在 loop.py 里，由本引擎自己拥有。
"""
