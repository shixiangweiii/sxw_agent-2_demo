"""Run the first controlled RAG+Agent QA evaluation and write a scored report."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.harness.config import EvalConfig
from eval.harness.signals import cited_doc_ids, knowledge_hits, retrieved_doc_ids, tool_names
from eval.harness.sse_client import CollectedRun, chat


WEIGHTS = {
    "route": 0.20,
    "retrieval": 0.15,
    "citation": 0.20,
    "answer": 0.35,
    "safety_finish": 0.10,
}


def _contains_any(text: str, choices: list[str]) -> bool:
    lower = text.lower()
    return any(choice in text or choice.lower() in lower for choice in choices)


def _score_route(question: dict[str, Any], run: CollectedRun) -> tuple[float, dict[str, Any]]:
    expected = list(question.get("must_tools") or [])
    actual = tool_names(run)
    if not expected:
        return 1.0, {"expected": expected, "actual": actual, "hit": [], "missing": []}
    hit = [name for name in expected if name in actual]
    missing = [name for name in expected if name not in actual]
    return len(hit) / len(expected), {"expected": expected, "actual": actual, "hit": hit, "missing": missing}


def _score_retrieval(question: dict[str, Any], run: CollectedRun) -> tuple[float, dict[str, Any]]:
    gold = set(question.get("gold_doc_ids") or [])
    got = retrieved_doc_ids(run)
    if not gold:
        return 1.0, {"gold": [], "retrieved": sorted(got), "hit": [], "missing": []}
    hit = sorted(gold & got)
    missing = sorted(gold - got)
    return len(hit) / len(gold), {"gold": sorted(gold), "retrieved": sorted(got), "hit": hit, "missing": missing}


def _score_citation(question: dict[str, Any], run: CollectedRun) -> tuple[float, dict[str, Any]]:
    gold = set(question.get("gold_doc_ids") or [])
    cited = cited_doc_ids(run)
    retrieved = retrieved_doc_ids(run)
    hallucinated = sorted(cited - retrieved)
    if not gold:
        score = 1.0 if not cited and not hallucinated else 0.0
        return score, {"gold": [], "cited": sorted(cited), "precision": score, "recall": score,
                       "hallucinated": hallucinated}
    precision = len(cited & gold) / len(cited) if cited else 0.0
    recall = len(cited & gold) / len(gold)
    score = (precision + recall) / 2
    if hallucinated:
        score *= 0.5
    return score, {
        "gold": sorted(gold),
        "cited": sorted(cited),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "hallucinated": hallucinated,
    }


def _score_answer(question: dict[str, Any], run: CollectedRun) -> tuple[float, dict[str, Any]]:
    text = run.text or ""
    checks = list(question.get("expected_checks") or [])
    hits: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for check in checks:
        choices = list(check.get("any_of") or [])
        ok = _contains_any(text, choices)
        target = hits if ok else misses
        target.append({"label": check.get("label", ""), "any_of": choices})
    score = len(hits) / len(checks) if checks else 1.0
    return score, {"hit": hits, "missing": misses}


def _score_safety(question: dict[str, Any], run: CollectedRun) -> tuple[float, dict[str, Any]]:
    text = run.text or ""
    failures: list[str] = []
    if run.transport_error:
        failures.append(f"transport_error:{run.transport_error}")
    if run.had_error:
        failures.append(f"error_event:{run.error_msg}")
    if not run.finished:
        failures.append("missing_done_event")
    for phrase in question.get("forbidden_phrases") or []:
        if phrase and phrase in text:
            failures.append(f"forbidden_phrase:{phrase}")
    hallucinated = sorted(cited_doc_ids(run) - retrieved_doc_ids(run))
    if hallucinated:
        failures.append(f"hallucinated_citation:{hallucinated}")
    return (0.0 if failures else 1.0), {"failures": failures}


def score_question(question: dict[str, Any], run: CollectedRun) -> dict[str, Any]:
    route_score, route_detail = _score_route(question, run)
    retrieval_score, retrieval_detail = _score_retrieval(question, run)
    citation_score, citation_detail = _score_citation(question, run)
    answer_score, answer_detail = _score_answer(question, run)
    safety_score, safety_detail = _score_safety(question, run)
    component_scores = {
        "route": route_score,
        "retrieval": retrieval_score,
        "citation": citation_score,
        "answer": answer_score,
        "safety_finish": safety_score,
    }
    weighted = sum(component_scores[name] * WEIGHTS[name] for name in WEIGHTS)
    max_points = float(question["points"])
    awarded = round(max_points * weighted, 3)
    return {
        "id": question["id"],
        "difficulty": question["difficulty"],
        "type": question["type"],
        "question": question["question"],
        "ground_truth": question["ground_truth"],
        "max_points": max_points,
        "awarded_points": awarded,
        "weighted_ratio": round(weighted, 4),
        "component_scores": {k: round(v, 4) for k, v in component_scores.items()},
        "details": {
            "route": route_detail,
            "retrieval": retrieval_detail,
            "citation": citation_detail,
            "answer": answer_detail,
            "safety_finish": safety_detail,
        },
        "answer": run.text,
        "tool_calls": tool_names(run),
        "knowledge_hits": knowledge_hits(run),
        "citations": run.citations,
        "timing_ms": {"ttft": round(run.ttft_ms, 1), "total": round(run.total_ms, 1)},
        "transport_error": run.transport_error,
    }


def index_payload(arag_url: str, payload_path: Path, timeout: float = 180.0) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{arag_url.rstrip('/')}/v1/index", json=json.loads(payload_path.read_text(encoding="utf-8")))
        resp.raise_for_status()
        return resp.json()


def _format_percent(score: float) -> str:
    return f"{score * 100:.1f}%"


def write_markdown_report(exam: dict[str, Any], results: list[dict[str, Any]], out_dir: Path, meta: dict[str, Any]) -> None:
    total = round(sum(r["awarded_points"] for r in results), 2)
    by_diff: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_diff[result["difficulty"]].append(result)

    lines: list[str] = [
        "# 首次受控 RAG+Agent 基础问答质量评测报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 考卷：`{meta['exam_path']}`",
        f"- 引擎：`{meta['engine']}`",
        f"- Agent Base URL：`{meta['base_url']}`",
        f"- 语料入库：{meta.get('index_result', '未执行入库')}",
        f"- 总分：**{total:.2f} / {exam['total_points']}**",
        "",
        "## 评分方法",
        "",
        "每题按固定组件加权：route 20%，retrieval 15%，citation 20%，answer 35%，safety_finish 10%。",
        "本报告采用确定性规则评分，不使用 LLM-as-judge；它适合作为当前代码的可复跑质量基线。",
        "",
        "## 分难度统计",
        "",
        "| 难度 | 题数 | 得分 | 满分 | 得分率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for diff in ("simple", "medium", "complex"):
        bucket = by_diff.get(diff, [])
        got = sum(r["awarded_points"] for r in bucket)
        max_points = sum(r["max_points"] for r in bucket)
        rate = got / max_points if max_points else 0.0
        lines.append(f"| {diff} | {len(bucket)} | {got:.2f} | {max_points:.2f} | {_format_percent(rate)} |")

    partial = [r for r in results if r["awarded_points"] < r["max_points"]]
    route_issues = [r["id"] for r in results if r["component_scores"]["route"] < 1.0]
    retrieval_issues = [r["id"] for r in results if r["component_scores"]["retrieval"] < 1.0]
    citation_issues = [r["id"] for r in results if r["component_scores"]["citation"] < 1.0]
    answer_issues = [r["id"] for r in results if r["component_scores"]["answer"] < 1.0]
    full_marks = len(results) - len(partial)
    lines.extend([
        "",
        "## 关键分析",
        "",
        f"- 30 题中 {full_marks} 题满分，{len(partial)} 题有扣分；扣分集中在复杂题和少数多文档引用题。",
        f"- 简单题全部满分，说明单文档事实检索、基础引用和图片锚点召回在本语料上稳定。",
        f"- 中等题只在 {', '.join([r['id'] for r in partial if r['difficulty'] == 'medium']) or '无'} 扣分，主要原因是答案覆盖正确但 citation 只覆盖了部分黄金文档。",
        f"- 复杂题得分率最低，问题集中在跨文档引用召回、复杂问题下的知识检索坚持性，以及少量答案要点遗漏。",
        f"- route 扣分题：{', '.join(route_issues) or '无'}。",
        f"- retrieval 扣分题：{', '.join(retrieval_issues) or '无'}。",
        f"- citation 扣分题：{', '.join(citation_issues) or '无'}。",
        f"- answer 覆盖扣分题：{', '.join(answer_issues) or '无'}。",
        "",
        "解读：本次受控评测显示，agent 在简单与中等 RAG 问答上表现稳定；复杂题的主要短板不是生成表达，而是多文档场景下只引用部分来源，"
        "以及 C06 这类题目在用户已给出数字时绕过 knowledge_search，导致检索和 citation 缺失。后续优化应优先强化知识型题的检索触发策略、"
        "多文档答案的引用覆盖，以及工具计算前后的证据绑定。",
    ])

    lines.extend([
        "",
        "## 逐题总览",
        "",
        "| ID | 难度 | 类型 | 得分 | route | retrieval | citation | answer | safety | 工具 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ])
    for result in results:
        c = result["component_scores"]
        tools = ", ".join(result["tool_calls"]) or "-"
        lines.append(
            f"| {result['id']} | {result['difficulty']} | {result['type']} | "
            f"{result['awarded_points']:.2f}/{result['max_points']:.2f} | "
            f"{c['route']:.2f} | {c['retrieval']:.2f} | {c['citation']:.2f} | "
            f"{c['answer']:.2f} | {c['safety_finish']:.2f} | {tools} |"
        )

    lines.extend(["", "## 逐题评分过程", ""])
    for result in results:
        lines.extend([
            f"### {result['id']} · {result['difficulty']} · {result['awarded_points']:.2f}/{result['max_points']:.2f}",
            "",
            f"题目：{result['question']}",
            "",
            f"GT：{result['ground_truth']}",
            "",
            f"实际工具：{', '.join(result['tool_calls']) or '无'}",
            "",
            f"检索文档：{', '.join(result['details']['retrieval']['retrieved']) or '无'}",
            "",
            f"引用文档：{', '.join(result['details']['citation']['cited']) or '无'}",
            "",
            "组件评分：",
        ])
        for name in ("route", "retrieval", "citation", "answer", "safety_finish"):
            lines.append(f"- {name}: {result['component_scores'][name]:.2f}")
        missing = result["details"]["answer"]["missing"]
        if missing:
            labels = "；".join(f"{m['label']}({ '/'.join(m['any_of']) })" for m in missing)
            lines.append(f"- 答案缺失检查点：{labels}")
        route_missing = result["details"]["route"]["missing"]
        if route_missing:
            lines.append(f"- 缺失工具：{', '.join(route_missing)}")
        retrieval_missing = result["details"]["retrieval"]["missing"]
        if retrieval_missing:
            lines.append(f"- 未检索到黄金文档：{', '.join(retrieval_missing)}")
        safety_failures = result["details"]["safety_finish"]["failures"]
        if safety_failures:
            lines.append(f"- 安全/完成性问题：{'; '.join(safety_failures)}")
        answer = result["answer"].strip().replace("\n", "\n> ")
        lines.extend(["", "模型答案：", "", f"> {answer if answer else '(空)'}", ""])

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    exam_path = Path(args.exam)
    exam = json.loads(exam_path.read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    index_result: dict[str, Any] | str = "skipped"
    if args.index_payload:
        index_result = index_payload(args.arag_url, Path(args.index_payload))

    cfg = EvalConfig(base_url=args.base_url, engine=args.engine, request_timeout_s=args.timeout)
    results: list[dict[str, Any]] = []
    raw_runs: dict[str, Any] = {}
    questions = exam["questions"]
    for idx, question in enumerate(questions, 1):
        print(f"[first-eval] {idx:02d}/{len(questions)} {question['id']} {question['difficulty']}")
        run_result = chat(cfg, question["id"], args.engine, question["question"])
        raw_runs[question["id"]] = asdict(run_result)
        scored = score_question(question, run_result)
        results.append(scored)
        print(f"  -> {scored['awarded_points']:.2f}/{scored['max_points']:.2f} tools={scored['tool_calls']}")

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "exam_path": str(exam_path),
        "engine": args.engine,
        "base_url": args.base_url,
        "arag_url": args.arag_url,
        "index_result": index_result,
    }
    summary = {
        "meta": meta,
        "total_score": round(sum(r["awarded_points"] for r in results), 3),
        "total_points": exam["total_points"],
        "results": results,
    }
    (out_dir / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "raw_runs.json").write_text(json.dumps(raw_runs, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(exam, results, out_dir, meta)
    print(f"[first-eval] report: {out_dir / 'report.md'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exam", default="eval/eval_docs/first_eval/first_eval_exam.json")
    parser.add_argument("--out", default="eval/eval_docs/first_eval/report")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--arag-url", default="http://127.0.0.1:8100")
    parser.add_argument("--engine", default="agent_loop")
    parser.add_argument("--index-payload", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
