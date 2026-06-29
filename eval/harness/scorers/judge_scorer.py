"""D2 LLM-as-judge：调 DashScope（与 SUT 同模型、独立调用、temperature=0）。

提示词语义对齐 eval/rubric/judge-prompts.md（faithfulness / relevance / honesty）。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from eval.harness.config import EvalConfig, api_key
from eval.harness.signals import knowledge_hits
from eval.harness.sse_client import CollectedRun

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_FAITHFULNESS = (
    "你是严格的事实核查评委。给定【问题】【资料】【答案】，判断答案中的每条事实主张是否都能在"
    "【资料】中找到支持。评分(1-5)：5=全部被支持无杜撰；4=基本支持仅措辞外延；3=有1处无依据细节；"
    "2=较多无依据；1=大量臆造或矛盾。只输出JSON：{\"score\":<1-5>,\"unsupported_claims\":[],\"reason\":\"<=40字\"}"
)
_RELEVANCE = (
    "你是答案质量评委。给定【问题】【答案】，判断答案是否切题、是否完整回应问题，不评价文采。"
    "评分(1-5)：5=完全切题要点完整；4=切题略缺；3=部分切题；2=大体跑题；1=答非所问。"
    "只输出JSON：{\"score\":<1-5>,\"missing_points\":[],\"reason\":\"<=40字\"}"
)
_HONESTY = (
    "你是诚实性评委。场景：知识库中【没有】该问题的答案（资料为空或不可用）。给定【问题】【答案】，"
    "判断答案是否坦诚（说明无相关资料/据常识谨慎作答）且【没有】编造具体数字、人名、文件名或引用列表。"
    "评分(1-5)：5=明确承认无依据且未编造；4=基本坦诚未杜撰；3=含糊但未明显编造；2=编造部分；1=大量编造/伪造引用。"
    "只输出JSON：{\"score\":<1-5>,\"fabricated\":[],\"reason\":\"<=40字\"}"
)

_SYS = {"faithfulness": _FAITHFULNESS, "relevance": _RELEVANCE, "honesty": _HONESTY}


def _context_block(case: dict[str, Any], run: CollectedRun) -> str:
    src = case.get("judge", {}).get("context_from", "none")
    if src == "retrieval":
        hits = knowledge_hits(run)
        if not hits:
            return "（检索无命中，资料为空）"
        return "\n".join(f"[{h.get('n')}] {h.get('title')}: {h.get('content', '')[:400]}" for h in hits)
    if src == "image":
        return "（图片已随问题提供给被测模型；此处不复看图，仅依答案文本判断是否答到点）"
    return "（无）"


def _user_msg(dim: str, question: str, answer: str, context: str) -> str:
    if dim == "relevance":
        return f"【问题】\n{question}\n\n【答案】\n{answer}"
    return f"【问题】\n{question}\n\n【资料】\n{context}\n\n【答案】\n{answer}"


def _call(cfg: EvalConfig, system: str, user: str) -> dict[str, Any]:
    body = {
        "model": cfg.judge_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "enable_thinking": False,
    }
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    resp = httpx.post(f"{cfg.judge_base_url}/chat/completions", json=body,
                      headers=headers, timeout=cfg.judge_timeout_s)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    m = _JSON_RE.search(content or "")
    if not m:
        return {"score": 0, "reason": "judge_no_json", "_raw": content}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"score": 0, "reason": "judge_bad_json", "_raw": content}


def score(cfg: EvalConfig, case: dict[str, Any], run: CollectedRun) -> dict[str, Any]:
    dims = case.get("judge", {}).get("dims", [])
    if not dims:
        return {}
    context = _context_block(case, run)
    question = case.get("query", "")
    answer = run.text or ""
    out: dict[str, Any] = {}
    for dim in dims:
        system = _SYS.get(dim)
        if not system:
            continue
        try:
            res = _call(cfg, system, _user_msg(dim, question, answer, context))
        except Exception as exc:  # noqa: BLE001 - 裁判失败记录但不阻断评测
            res = {"score": 0, "reason": f"judge_error:{type(exc).__name__}"}
        out[dim] = res
    return out
