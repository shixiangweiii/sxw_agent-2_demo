"""D1 路由评分：对照 expected_route 判定命中 / 漏调 / 错调 / 过度路由。"""
from __future__ import annotations

from typing import Any

from eval.harness.signals import capability_calls, tool_names
from eval.harness.sse_client import CollectedRun


def score(case: dict[str, Any], run: CollectedRun) -> dict[str, Any]:
    er = case.get("expected_route", {})
    must_call = list(er.get("must_call", []))
    acceptable = list(er.get("acceptable", []))
    must_not = list(er.get("must_not_call", []))
    allowed = set(must_call) | set(acceptable)

    actual = tool_names(run)
    actual_set = set(actual)
    cap = capability_calls(run)

    must_hit = [t for t in must_call if t in actual_set]
    must_missing = [t for t in must_call if t not in actual_set]
    over_routed = [t for t in must_not if t in actual_set]
    unexpected = [t for t in cap if t not in allowed] if (allowed or must_not or not must_call) else []
    # 负例（无 must_call、无 acceptable）：任何能力型调用都算 unexpected
    if not allowed and not must_call:
        unexpected = list(cap)

    route_ok = (not must_missing) and (not over_routed) and (not unexpected)

    if must_call:
        first_cap = cap[0] if cap else None
        first_cap_ok = first_cap in allowed if first_cap is not None else False
    else:
        first_cap_ok = (len(cap) == 0) or all(t in allowed for t in cap)

    return {
        "route_ok": route_ok,
        "must_call": must_call,
        "must_hit": must_hit,
        "must_missing": must_missing,
        "over_routed": over_routed,
        "unexpected_tools": unexpected,
        "first_cap_ok": first_cap_ok,
        "capability_calls": cap,
        "all_tool_calls": actual,
    }
