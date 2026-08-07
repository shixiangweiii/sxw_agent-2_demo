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
    # 与服务端轨迹的联查键。注意**单靠它不唯一**：同一条 case 会分别发给两个引擎实例，
    # 唯一键是 (engine, trace_id)——results.jsonl 里 engine 是独立字段，天然成立。
    trace_id: str = ""


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
        # 服务端回带的 trace_id 是**权威**的：轨迹文件就是按它命名的。
        # 正常情况下与我们发出去的一致；不一致说明服务端没采纳请求头，
        # 此时按服务端的走，否则会照着一个不存在的 id 去捞轨迹。
        if data.get("trace_id"):
            run.trace_id = str(data["trace_id"])
    elif ev_type == "error":
        run.had_error = True
        run.error_msg = str(data.get("message", ""))


def chat(
    cfg: EvalConfig, case_id: str, engine: str, query: str,
    image_path: Optional[str] = None, base_dir: Optional[Path] = None, rep: int = 0,
) -> CollectedRun:
    # 主动指定 trace_id（而不是让服务端生成再回读）：可读、可预测，
    # 报告里一眼能看出是哪条 case 的哪次重复；`TraceMiddleware` 优先采纳请求头。
    trace_id = f"eval-{case_id}-r{rep}"
    run = CollectedRun(case_id=case_id, engine=engine, trace_id=trace_id)
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
            with client.stream("POST", url, data=form, files=files,
                               headers={"x-trace-id": trace_id}) as resp:
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


def fetch_trace(cfg: EvalConfig, trace_id: str, level: str = "summary") -> Optional[dict[str, Any]]:
    """从被测 agent 捞回结构化轨迹。失败返回 None——**绝不让取轨迹失败影响评测本身**。

    默认取 summary：入库的报告只留骨架 + payload 摘要，完整输入留在 agent 本机的
    `local_storage/traces/`（已 gitignore），由记录里的 `trace_file` 指过去。
    """
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(f"{cfg.base_url}/api/v1/traces/{trace_id}",
                              params={"level": level})
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:  # noqa: BLE001 - 轨迹是诊断增强，不是评测前置条件
        return None
