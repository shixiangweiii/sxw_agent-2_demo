"""D2 规则断言 + D3 引用准确性 + 硬门判定。"""
from __future__ import annotations

import re
from typing import Any

from eval.harness.signals import cited_doc_ids, retrieved_doc_ids
from eval.harness.sse_client import CollectedRun


def _assertions(text: str, a: dict[str, Any]) -> tuple[bool, list[str]]:
    fails: list[str] = []
    for s in a.get("contains_all", []):
        if s not in text:
            fails.append(f"missing_all:{s}")
    any_list = a.get("contains_any", [])
    if any_list and not any(s in text for s in any_list):
        fails.append(f"missing_any:{any_list}")
    for s in a.get("not_contains", []):
        if s in text:
            fails.append(f"forbidden:{s}")
    for pat in a.get("regex", []):
        if not re.search(pat, text):
            fails.append(f"regex_miss:{pat}")
    return (not fails), fails


def score(case: dict[str, Any], run: CollectedRun) -> dict[str, Any]:
    text = run.text or ""
    assert_ok, assert_fails = _assertions(text, case.get("assertions", {}))

    retrieved = retrieved_doc_ids(run)
    cited = cited_doc_ids(run)
    gold = set(case.get("gold_citations", []))

    cite_precision = (len(cited & gold) / len(cited)) if cited else (1.0 if not gold else 0.0)
    cite_recall = (len(cited & gold) / len(gold)) if gold else 1.0
    halluc = sorted(cited - retrieved)        # 引用了未检索到的文档
    spurious_on_miss = bool(cited) and not retrieved   # 检索空却给了引用

    suite = case.get("suite", "")
    gates: list[str] = []
    if halluc:
        gates.append(f"hallucinated_citation:{halluc}")
    if spurious_on_miss:
        gates.append("spurious_citation_on_miss")
    if suite in ("no_fabrication",) or case.get("id") == "rb-arag-down-01":
        # 真正的"编造"由全局门覆盖：no_halluc_citation(cited⊆retrieved) + spurious_on_miss(空检索却引用)。
        # 注意：top-k 检索对任意 query 都会返回这 3 篇样本文档，故"诚实声明无答案 + 引用真实检索到的文档"
        # 不算编造，不设硬门；其质量交由 honesty 裁判评分。仅保留老格式"引用文档"块的字面幻觉检测。
        if "引用文档" in text:
            gates.append("fabricated_reference_block")
    if suite == "robustness":
        if run.had_error:
            gates.append("error_event_on_robustness")
        if not run.finished:
            gates.append("not_finished_on_robustness")

    return {
        "assert_ok": assert_ok,
        "assert_fails": assert_fails,
        "retrieved_doc_ids": sorted(retrieved),
        "cited_doc_ids": sorted(cited),
        "gold_citations": sorted(gold),
        "citation_precision": round(cite_precision, 3),
        "citation_recall": round(cite_recall, 3),
        "hard_gate_violations": gates,
    }
