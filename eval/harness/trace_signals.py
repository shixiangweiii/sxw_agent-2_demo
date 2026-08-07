"""从服务端轨迹提取失败模式标签 —— 把"哪条 FAIL"变成"为什么 FAIL"。

首版报告（`eval/reports/20260629-090605/ANALYSIS.md` §4）里，两条失败的归因写的是
"已核对 runs/*.json 原始事件流确认"——人肉读 SSE 猜出来的。本模块把那套人工推断
固化成规则。

★ **纪律：规则只能读三代引擎都会产出的字段。**
   `native.turn` / `adk.turn` 同名字段（iter / forced_summary / plan_continuation）、
   `tool.*` span、`retrieval.*` span、root span 的 finish_reason —— 这些是对等的。
   引擎专属的（native 的 transition、compact）只进"补充说明"，不参与判定，
   否则"谁埋点更深"会变成引擎对比里的假优势。
"""
from __future__ import annotations

from typing import Any, Optional

# 召回质量存疑的阈值。**必须按 RRF 的量纲推导，不能拍脑袋**：
# arag 用 `score(d) = Σ 1/(k + rank_i(d))`，k=60、rank 从 0 起
# （arag/components/reranker.py:13），于是
#   · 双路都排第 1  → 2/60 ≈ 0.0333（理论上限）
#   · 单路排第 1    → 1/60 ≈ 0.0167
#   · 单路排第 10   → 1/70 ≈ 0.0143
# 取 0.015 = "最好的命中在任何一路里都排到 10 名开外"。
# 反例警示：初版误取 0.05，高于理论上限，会把 100% 的检索都标成低分。
# 该标签只提示人工看一眼，不参与 pass/fail 判定。
_LOW_SCORE = 0.015
# 委派类工具：失败要单独标出来，否则会被淹没在普通工具错误里
_DELEGATION_PREFIXES = ("researcher", "claude_skill_", "math_expert")


def _spans(trace: Optional[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    if not trace:
        return []
    return [s for s in trace.get("spans", []) if s.get("kind") == kind]


def _attr(span: dict[str, Any], key: str, default: Any = None) -> Any:
    return (span.get("attributes") or {}).get(key, default)


def summarize(trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """把轨迹压成一行可读的结构（进 results.jsonl，报告直接引用）。"""
    if not trace:
        return {}
    turns = _spans(trace, "turn")
    tools = _spans(trace, "tool")
    retrievals = _spans(trace, "retrieval")
    llms = _spans(trace, "llm")
    root = next((s for s in trace.get("spans", []) if s.get("kind") == "request"), None)

    tokens = sum(int(_attr(s, "total_tokens") or 0) for s in llms)
    return {
        "turns": len(turns),
        "tool_calls": [_attr(s, "tool") for s in tools],
        "tool_errors": [_attr(s, "tool") for s in tools if s.get("status") == "error"],
        "retrieval_hits": [_attr(s, "hit_count") for s in retrievals],
        "retrieval_degraded": any(_attr(s, "degraded") for s in retrievals),
        "total_tokens": tokens or None,
        "finish_reason": _attr(root, "finish_reason") if root else None,
        "forced_summary": any(_attr(s, "forced_summary") for s in turns),
        "compacted": len(_spans(trace, "compact")) or None,
        "trace_file": trace.get("trace_file"),
    }


def label(case: dict[str, Any], record: dict[str, Any],
          trace: Optional[dict[str, Any]]) -> list[str]:
    """产出失败/风险标签。空列表 = 轨迹层面没发现异常。"""
    if not trace:
        # 如实标注而不是静默给空：否则"没标签"会被误读成"一切正常"。
        return ["no_trace"]

    labels: list[str] = []
    turns = _spans(trace, "turn")
    tools = _spans(trace, "tool")
    retrievals = _spans(trace, "retrieval")
    root = next((s for s in trace.get("spans", []) if s.get("kind") == "request"), None)
    finish_reason = _attr(root, "finish_reason") if root else None

    # ── 检索 ───────────────────────────────────────────────────────────────
    # 欠路由：知识型题目（有黄金引用）却整轮没发起过一次检索。
    # 首版报告里 kq-rag-01 就是这个模式，当时只能归结为"随机性"。
    if case.get("gold_citations") and not retrievals:
        labels.append("under_routing")
    if any(_attr(s, "degraded") for s in retrievals):
        labels.append("retrieval_degraded")
    elif retrievals and all((_attr(s, "hit_count") or 0) == 0 for s in retrievals):
        labels.append("retrieval_empty")
    else:
        # 只有补回了 arag 的 score 才判得了这一条——分不清"召回差"还是
        # "模型没用好召回"，正是引用类失败最难归因的一环。
        scores = [x for s in retrievals for x in (_attr(s, "scores") or [])]
        if scores and max(scores) < _LOW_SCORE:
            labels.append("retrieval_low_score")

    # ── 工具 ───────────────────────────────────────────────────────────────
    failed = [s for s in tools if s.get("status") == "error"]
    if failed:
        # 区分"喂回后恢复了"和"整轮真的塌了"——这正是 ToolErrorFeedback 想证明的能力。
        labels.append("tool_error_fatal" if record.get("had_error") else "tool_error_recovered")
    if any(_attr(s, "failure") == "args_parse_error" for s in tools):
        labels.append("args_parse_error")
    if any(str(_attr(s, "tool") or "").startswith(_DELEGATION_PREFIXES) for s in failed):
        labels.append("sub_agent_failed")

    # ── 循环控制 ───────────────────────────────────────────────────────────
    if any(_attr(s, "forced_summary") for s in turns):
        labels.append("force_summary_hit")
    if finish_reason in ("hard_cap", "model_error"):
        labels.append(f"{finish_reason}_hit" if finish_reason == "hard_cap" else "model_error")
    if not record.get("finished"):
        labels.append("stream_unfinished")

    # ── 补充说明（引擎专属，不参与对比判定）─────────────────────────────
    if _spans(trace, "compact"):
        labels.append("context_compacted")

    return labels
