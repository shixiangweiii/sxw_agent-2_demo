"""SSE 客户端：向 agent 发 multipart 请求，解析 SSE 事件流 → CollectedRun。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from eval.harness.config import EvalConfig


@dataclass
class CollectedRun:
    case_id: str
    engine: str
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_results: list[tuple[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    skill_events: list[dict[str, Any]] = field(default_factory=list)
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    had_error: bool = False
    error_msg: str = ""
    finished: bool = False
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    transport_error: str = ""
    raw_events: list[dict[str, Any]] = field(default_factory=list)


def _dispatch(run: CollectedRun, ev_type: str, data: dict[str, Any], t0: float) -> None:
    run.raw_events.append({"event": ev_type, "data": data})
    if ev_type == "text":
        if run.ttft_ms == 0.0:
            run.ttft_ms = (time.monotonic() - t0) * 1000.0
        run.text += str(data.get("delta", ""))
    elif ev_type == "tool_call":
        run.tool_calls.append((str(data.get("name", "")), dict(data.get("args") or {})))
    elif ev_type == "tool_result":
        run.tool_results.append((str(data.get("name", "")), data.get("response")))
    elif ev_type == "citation":
        run.citations.append(data)
    elif ev_type == "skill_event":
        run.skill_events.append(data)
    elif ev_type == "plan_step":
        run.plan_steps.append(data)
    elif ev_type == "done":
        run.finished = True
    elif ev_type == "error":
        run.had_error = True
        run.error_msg = str(data.get("message", ""))


def chat(
    cfg: EvalConfig, case_id: str, engine: str, query: str,
    image_path: Optional[str] = None, base_dir: Optional[Path] = None, rep: int = 0,
) -> CollectedRun:
    run = CollectedRun(case_id=case_id, engine=engine)
    url = f"{cfg.base_url}/api/v1/chat/{cfg.agent_uuid}/stream"
    session_id = f"{engine}::{case_id}" + (f"::r{rep}" if rep else "")
    form = {"query": query, "user_id": "eval", "session_id": session_id}
    files: Optional[dict[str, Any]] = None
    fh = None
    if image_path:
        p = (base_dir / image_path) if base_dir else Path(image_path)
        if p.exists():
            fh = p.open("rb")
            files = {"image": (p.name, fh, "image/jpeg")}
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=cfg.request_timeout_s) as client:
            with client.stream("POST", url, data=form, files=files) as resp:
                resp.raise_for_status()
                ev_type: Optional[str] = None
                data_lines: list[str] = []
                for line in resp.iter_lines():
                    if line == "":
                        if ev_type is not None:
                            raw = "\n".join(data_lines)
                            try:
                                payload = json.loads(raw) if raw else {}
                            except json.JSONDecodeError:
                                payload = {"_raw": raw}
                            _dispatch(run, ev_type, payload, t0)
                        ev_type, data_lines = None, []
                        continue
                    if line.startswith("event:"):
                        ev_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].lstrip())
    except Exception as exc:  # noqa: BLE001 - 传输异常记录为 transport_error，不抛
        run.transport_error = f"{type(exc).__name__}: {exc}"
    finally:
        if fh is not None:
            fh.close()
    run.total_ms = (time.monotonic() - t0) * 1000.0
    return run
