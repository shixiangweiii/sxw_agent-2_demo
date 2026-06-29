"""能力预检：探活下游服务，决定哪些 case 因下游缺失而 N/A。"""
from __future__ import annotations

from typing import Any

import httpx

from eval.harness.config import EvalConfig


def _ok(url: str, method: str = "GET") -> bool:
    try:
        resp = httpx.request(method, url, timeout=5.0)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - 探活失败即视为不可用
        return False


def preflight(cfg: EvalConfig) -> dict[str, bool]:
    caps = {
        "arag": _ok(f"{cfg.arag_url}/healthz"),
        "skill_center": _ok(f"{cfg.skill_center_url}/healthz"),
        "a2a": _ok(cfg.a2a_card_url),
    }
    return caps


def preconditions_met(case: dict[str, Any], caps: dict[str, bool]) -> tuple[bool, str]:
    pre = case.get("preconditions", {})
    for dep in ("arag", "skill_center", "a2a"):
        want = pre.get(dep, "any")
        if want == "up" and not caps.get(dep, False):
            return False, f"{dep} required up but down"
        if want == "down" and caps.get(dep, False):
            return False, f"{dep} required down but up"
    return True, ""
