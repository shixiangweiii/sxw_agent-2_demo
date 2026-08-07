"""评测执行器：cases × engine → 驱动 SSE → 评分 → 增量落盘。

用法（执行阶段）：
    python -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000 --out eval/reports/<ts>
    python -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001 --out eval/reports/<ts>
    python -m eval.harness.runner --engine native_loop  --base-url http://127.0.0.1:8002 --out eval/reports/<ts>
    python -m eval.harness.runner --engine agent_loop   --only-arag-down --out eval/reports/<ts>   # arag 停服后跑
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eval.harness.config import EvalConfig
from eval.harness.preflight import preconditions_met, preflight
from eval.harness.scorers import judge_scorer, routing_scorer, rule_scorer
from eval.harness.sse_client import chat

_DATASET = Path("eval/dataset/cases.jsonl")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _passed(routing: dict[str, Any], rule: dict[str, Any]) -> bool:
    return bool(routing["route_ok"] and rule["assert_ok"] and not rule["hard_gate_violations"])


def run(cfg: EvalConfig, out_dir: Path, suite: str = "", only_arag_down: bool = False,
        use_judge: bool = True, dataset: Path = _DATASET, repeat: int = 1) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "runs").mkdir(exist_ok=True)
    caps = preflight(cfg)
    (out_dir / "preflight.json").write_text(
        json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] engine={cfg.engine} base={cfg.base_url} caps={caps}")

    cases = load_cases(dataset)
    results_path = out_dir / "results.jsonl"
    n_pass = n_scored = n_na = 0
    with results_path.open("a", encoding="utf-8") as out:
        for case in cases:
            cid = case["id"]
            if cfg.engine not in case.get("engines", []):
                continue
            if suite and case.get("suite") != suite:
                continue
            arag_down_case = case.get("preconditions", {}).get("arag") == "down"
            if only_arag_down and not arag_down_case:
                continue
            if not only_arag_down and arag_down_case:
                continue  # arag-down 用例只在专门 pass 跑

            met, why = preconditions_met(case, caps)
            if not met:
                rec = {"id": cid, "engine": cfg.engine, "suite": case.get("suite"),
                       "status": "NA", "na_reason": why}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                n_na += 1
                print(f"  - {cid:22s} N/A ({why})")
                continue

            for rep in range(repeat):
                cr = chat(cfg, cid, cfg.engine, case["query"],
                          image_path=case.get("image"), base_dir=Path("eval/dataset"), rep=rep)
                routing = routing_scorer.score(case, cr)
                rule = rule_scorer.score(case, cr)
                judge = judge_scorer.score(cfg, case, cr) if use_judge else {}
                passed = _passed(routing, rule)
                n_scored += 1
                n_pass += int(passed)

                suffix = f".r{rep}" if repeat > 1 else ""
                (out_dir / "runs" / f"{cid}.{cfg.engine}{suffix}.json").write_text(
                    json.dumps(asdict(cr), ensure_ascii=False, indent=2), encoding="utf-8")
                rec = {
                    "id": cid, "engine": cfg.engine, "suite": case.get("suite"), "rep": rep,
                    "status": "scored", "query": case["query"], "answer": cr.text,
                    "tool_calls": routing["all_tool_calls"], "capability_calls": routing["capability_calls"],
                    "ttft_ms": round(cr.ttft_ms, 1), "total_ms": round(cr.total_ms, 1),
                    "had_error": cr.had_error, "finished": cr.finished,
                    "transport_error": cr.transport_error,
                    "routing": routing, "rule": rule, "judge": judge,
                    "hard_gate_violations": rule["hard_gate_violations"], "passed": passed,
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                flag = "PASS" if passed else "FAIL"
                extra = "" if passed else f"  gates={rule['hard_gate_violations']} miss={routing['must_missing']} unexp={routing['unexpected_tools']} af={rule['assert_fails']}"
                rtag = f" r{rep}" if repeat > 1 else ""
                print(f"  - {cid:22s}{rtag} {flag}  tools={routing['capability_calls']}{extra}")

    print(f"[eval] engine={cfg.engine} done: scored={n_scored} pass={n_pass} na={n_na} → {results_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True,
                choices=["agent_loop", "plan_execute", "native_loop"])
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--suite", default="")
    ap.add_argument("--only-arag-down", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--dataset", default=str(_DATASET))
    args = ap.parse_args()
    cfg = EvalConfig(base_url=args.base_url, engine=args.engine)
    run(cfg, Path(args.out), suite=args.suite, only_arag_down=args.only_arag_down,
        use_judge=not args.no_judge, dataset=Path(args.dataset), repeat=args.repeat)


if __name__ == "__main__":
    main()
