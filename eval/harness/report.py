"""聚合 results.jsonl → summary.md + metrics.json。

用法：python -m eval.harness.report --out eval/reports/<ts>
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 1)


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _judge_scores(records: list[dict[str, Any]], dim: str) -> list[float]:
    out: list[float] = []
    for r in records:
        j = (r.get("judge") or {}).get(dim)
        if isinstance(j, dict) and isinstance(j.get("score"), (int, float)) and j["score"] > 0:
            out.append(float(j["score"]))
    return out


def aggregate(out_dir: Path) -> dict[str, Any]:
    records = [json.loads(l) for l in (out_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    scored = [r for r in records if r.get("status") == "scored"]
    na = [r for r in records if r.get("status") == "NA"]

    by_engine: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        by_engine[r["engine"]].append(r)

    metrics: dict[str, Any] = {"engines": {}, "na_count": len(na),
                               "na": [{"id": r["id"], "engine": r.get("engine"), "reason": r.get("na_reason")} for r in na]}
    hard_violations: list[dict[str, Any]] = []
    spot_check: list[dict[str, Any]] = []

    for engine, recs in by_engine.items():
        n = len(recs)
        passed = sum(int(r["passed"]) for r in recs)
        route_ok = sum(int(r["routing"]["route_ok"]) for r in recs)
        first_cap = sum(int(r["routing"]["first_cap_ok"]) for r in recs)
        over = [r["id"] for r in recs if r["routing"]["over_routed"]]
        assert_ok = sum(int(r["rule"]["assert_ok"]) for r in recs)

        qa = [r for r in recs if r["rule"]["gold_citations"]]
        cite_p = _avg([r["rule"]["citation_precision"] for r in qa])
        cite_r = _avg([r["rule"]["citation_recall"] for r in qa])

        faith = _judge_scores(recs, "faithfulness")
        rel = _judge_scores(recs, "relevance")
        hon = _judge_scores(recs, "honesty")

        for r in recs:
            if r["hard_gate_violations"]:
                hard_violations.append({"id": r["id"], "engine": engine,
                                        "violations": r["hard_gate_violations"]})
            for dim in ("faithfulness", "honesty"):
                j = (r.get("judge") or {}).get(dim) or {}
                risk = j.get("unsupported_claims") or j.get("fabricated")
                if risk:
                    spot_check.append({"id": r["id"], "engine": engine, "dim": dim,
                                       "risk": risk, "reason": j.get("reason")})

        metrics["engines"][engine] = {
            "n": n, "pass": passed, "pass_rate": round(passed / n, 3) if n else 0,
            "route_accuracy": round(route_ok / n, 3) if n else 0,
            "first_capability_hit": round(first_cap / n, 3) if n else 0,
            "assert_pass_rate": round(assert_ok / n, 3) if n else 0,
            "over_routing_cases": over,
            "citation_precision": cite_p, "citation_recall": cite_r,
            "faithfulness_avg": _avg(faith), "faithfulness_ge4": round(sum(s >= 4 for s in faith) / len(faith), 3) if faith else None,
            "relevance_avg": _avg(rel),
            "honesty_avg": _avg(hon), "honesty_ge4": round(sum(s >= 4 for s in hon) / len(hon), 3) if hon else None,
            "ttft_ms_p50": _pct([r["ttft_ms"] for r in recs], 0.5),
            "ttft_ms_p95": _pct([r["ttft_ms"] for r in recs], 0.95),
            "total_ms_p50": _pct([r["total_ms"] for r in recs], 0.5),
            "total_ms_p95": _pct([r["total_ms"] for r in recs], 0.95),
        }

        # per-suite pass
        by_suite: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in recs:
            by_suite[r["suite"]].append(r)
        metrics["engines"][engine]["by_suite"] = {
            s: {"n": len(rs), "pass": sum(int(x["passed"]) for x in rs)} for s, rs in by_suite.items()
        }

    metrics["hard_gate_violations"] = hard_violations
    metrics["manual_spot_check"] = spot_check

    # 稳定性（reps>1 才有意义）：按 (engine,id) 看各 rep 间 pass 与路由是否一致
    grp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        grp[(r["engine"], r["id"])].append(r)
    metrics["max_reps"] = max((len(rs) for rs in grp.values()), default=1)
    stability: list[dict[str, Any]] = []
    for (engine, cid), rs in grp.items():
        if len(rs) < 2:
            continue
        pass_k = sum(int(x["passed"]) for x in rs)
        routes = sorted({tuple(x["capability_calls"]) for x in rs})
        if pass_k != len(rs) or len(routes) > 1:
            stability.append({"id": cid, "engine": engine, "reps": len(rs),
                              "pass_k": pass_k, "distinct_routes": [list(t) for t in routes]})
    metrics["stability_flags"] = stability
    return metrics


def write_summary(out_dir: Path, metrics: dict[str, Any]) -> None:
    caps = {}
    pf = out_dir / "preflight.json"
    if pf.exists():
        caps = json.loads(pf.read_text(encoding="utf-8"))
    lines: list[str] = []
    lines.append(f"# Eval Report {out_dir.name}  (model=qwen3.7-plus)")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- 能力预检：{caps}")
    lines.append(f"- N/A 用例数：{metrics['na_count']}")
    lines.append("")
    lines.append("## 总览（按引擎）")
    lines.append("")
    lines.append("| 引擎 | N | Pass率 | 路由准确 | 首调命中 | 断言通过 | 引用P | 引用R | 忠实均分(≥4) | 相关均分 | 诚实均分 | TTFT p50/p95(ms) | 总时延 p50/p95(ms) |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for engine, m in metrics["engines"].items():
        f4 = f"{m['faithfulness_avg']}({m['faithfulness_ge4']})" if m["faithfulness_ge4"] is not None else "-"
        hon = f"{m['honesty_avg']}({m['honesty_ge4']})" if m["honesty_ge4"] is not None else "-"
        lines.append(
            f"| {engine} | {m['n']} | {m['pass_rate']} | {m['route_accuracy']} | {m['first_capability_hit']} | "
            f"{m['assert_pass_rate']} | {m['citation_precision']} | {m['citation_recall']} | {f4} | "
            f"{m['relevance_avg']} | {hon} | {m['ttft_ms_p50']}/{m['ttft_ms_p95']} | {m['total_ms_p50']}/{m['total_ms_p95']} |")
    lines.append("")
    lines.append("## 分套件 Pass（pass/n）")
    lines.append("")
    for engine, m in metrics["engines"].items():
        parts = " · ".join(f"{s}: {d['pass']}/{d['n']}" for s, d in sorted(m["by_suite"].items()))
        lines.append(f"- **{engine}**：{parts}")
    lines.append("")
    lines.append("## 硬门违规（安全/诚实，空=全部通过）")
    lines.append("")
    if metrics["hard_gate_violations"]:
        for v in metrics["hard_gate_violations"]:
            lines.append(f"- ❌ `{v['id']}` [{v['engine']}]: {v['violations']}")
    else:
        lines.append("- ✅ 无硬门违规")
    lines.append("")
    lines.append("## 过度路由（闲聊/无须工具却调工具）")
    lines.append("")
    over_any = False
    for engine, m in metrics["engines"].items():
        if m["over_routing_cases"]:
            over_any = True
            lines.append(f"- {engine}: {m['over_routing_cases']}")
    if not over_any:
        lines.append("- ✅ 无过度路由")
    lines.append("")
    if metrics.get("max_reps", 1) > 1:
        lines.append(f"## 稳定性（每用例 {metrics['max_reps']} 次重复，仅列不稳定项）")
        lines.append("")
        if metrics.get("stability_flags"):
            for s in metrics["stability_flags"]:
                lines.append(f"- `{s['id']}` [{s['engine']}] pass {s['pass_k']}/{s['reps']} · 路由分布={s['distinct_routes']}")
        else:
            lines.append("- ✅ 所有用例在各 rep 间一致（pass 与路由均稳定）")
        lines.append("")
    lines.append("## 人工抽检清单（裁判自报风险点，需人工确认）")
    lines.append("")
    if metrics["manual_spot_check"]:
        for s in metrics["manual_spot_check"]:
            lines.append(f"- `{s['id']}` [{s['engine']}] {s['dim']}: {s['risk']}  —— {s.get('reason')}")
    else:
        lines.append("- （无）")
    lines.append("")
    if metrics["na"]:
        lines.append("## N/A 用例")
        lines.append("")
        for r in metrics["na"]:
            lines.append(f"- `{r['id']}` [{r['engine']}]: {r['reason']}")
        lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out_dir = Path(args.out)
    metrics = aggregate(out_dir)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(out_dir, metrics)
    print(f"[eval] report → {out_dir}/summary.md  +  metrics.json")


if __name__ == "__main__":
    main()
