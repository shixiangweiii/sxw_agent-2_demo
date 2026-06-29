"""用当前评分器重新评分已保存的 runs/*.json（确定性，不重新调用 LLM；保留原 judge 结果）。

用途：评分器口径修正后，无需重跑 LLM 即可刷新 results.jsonl。
    python -m eval.harness.rescore --out eval/reports/<ts>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.harness.runner import _passed, load_cases
from eval.harness.scorers import routing_scorer, rule_scorer
from eval.harness.sse_client import CollectedRun


def rescore(out_dir: Path, dataset: Path) -> None:
    cases = {c["id"]: c for c in load_cases(dataset)}
    results_path = out_dir / "results.jsonl"
    old = [json.loads(l) for l in results_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    judge_by_key = {(r["id"], r["engine"]): r.get("judge", {}) for r in old if r.get("status") == "scored"}
    na_records = [r for r in old if r.get("status") == "NA"]

    new_records: list[dict[str, Any]] = []
    for run_file in sorted((out_dir / "runs").glob("*.json")):
        cr = CollectedRun(**json.loads(run_file.read_text(encoding="utf-8")))
        case = cases.get(cr.case_id)
        if case is None:
            continue
        routing = routing_scorer.score(case, cr)
        rule = rule_scorer.score(case, cr)
        judge = judge_by_key.get((cr.case_id, cr.engine), {})
        passed = _passed(routing, rule)
        new_records.append({
            "id": cr.case_id, "engine": cr.engine, "suite": case.get("suite"), "status": "scored",
            "query": case["query"], "answer": cr.text,
            "tool_calls": routing["all_tool_calls"], "capability_calls": routing["capability_calls"],
            "ttft_ms": round(cr.ttft_ms, 1), "total_ms": round(cr.total_ms, 1),
            "had_error": cr.had_error, "finished": cr.finished, "transport_error": cr.transport_error,
            "routing": routing, "rule": rule, "judge": judge,
            "hard_gate_violations": rule["hard_gate_violations"], "passed": passed,
        })

    with results_path.open("w", encoding="utf-8") as f:
        for r in new_records + na_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[eval] rescored {len(new_records)} runs (+{len(na_records)} NA) → {results_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dataset", default="eval/dataset/cases.jsonl")
    args = ap.parse_args()
    rescore(Path(args.out), Path(args.dataset))


if __name__ == "__main__":
    main()
