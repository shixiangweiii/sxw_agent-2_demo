"""Run API client: create -> committed SSE replay/tail -> explicit status."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from eval.harness.config import EvalConfig


@dataclass
class CollectedRun:
    case_id: str
    engine: str
    run_id: str = ""
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_results: list[tuple[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    skill_events: list[dict[str, Any]] = field(default_factory=list)
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    had_error: bool = False
    error_msg: str = ""
    finished: bool = False
    terminal_status: str = ""
    last_seq: int = 0
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    transport_error: str = ""
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str = ""


def _dispatch(
    run: CollectedRun, ev_type: str, envelope: dict[str, Any], t0: float,
) -> None:
    data = envelope.get("payload") or {}
    seq = int(envelope.get("seq") or 0)
    run.last_seq = max(run.last_seq, seq)
    run.raw_events.append({"event": ev_type, "seq": seq, "data": data})
    if ev_type == "text_start":
        # A retry/recovery/reactive compact starts a new generation for the
        # same semantic message.  Discard only the superseded answer body;
        # Tool/Skill/plan projections remain valid process history.
        run.text = ""
    elif ev_type == "text":
        if run.ttft_ms == 0.0:
            run.ttft_ms = (time.monotonic() - t0) * 1000.0
        run.text += str(data.get("delta", ""))
    elif ev_type == "assistant_message":
        # The final committed assistant event is authoritative for both fresh
        # replay and reconnect.  It replaces any partial generation assembled
        # from deltas instead of being appended to it.
        run.text = str(data.get("text", ""))
    elif ev_type == "tool_call":
        run.tool_calls.append((str(data.get("name", "")), dict(data.get("args") or {})))
    elif ev_type == "tool_result":
        run.tool_results.append((str(data.get("name", "")), data.get("response", data.get("result"))))
    elif ev_type == "citation":
        run.citations.extend(data.get("citations") or data.get("refs") or [])
    elif ev_type == "skill_event":
        run.skill_events.append(data)
    elif ev_type == "plan_step":
        run.plan_steps.append(data)
    elif ev_type == "terminal":
        run.finished = True
        run.terminal_status = str(envelope.get("terminal_status") or "")
        if run.terminal_status != "SUCCEEDED":
            run.had_error = True
            run.error_msg = str(data.get("message") or data.get("code") or run.terminal_status)


def _upload_image(client: httpx.Client, cfg: EvalConfig, path: Path, trace_id: str) -> str:
    with path.open("rb") as handle:
        response = client.post(
            f"{cfg.base_url}/api/v1/artifacts",
            files={"file": (path.name, handle, "image/jpeg")},
            headers={"x-trace-id": trace_id},
        )
    response.raise_for_status()
    return str(response.json()["artifact_id"])


def _consume_subscription(
    client: httpx.Client,
    cfg: EvalConfig,
    run: CollectedRun,
    t0: float,
    deadline: float,
) -> None:
    while not run.finished and time.monotonic() < deadline:
        url = f"{cfg.base_url}/api/v1/runs/{run.run_id}/events"
        try:
            with client.stream(
                "GET", url,
                params={"after_seq": run.last_seq},
                headers={"x-trace-id": run.trace_id, "Last-Event-ID": str(run.last_seq)},
            ) as response:
                response.raise_for_status()
                event_type: Optional[str] = None
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line == "":
                        if event_type is not None:
                            raw = "\n".join(data_lines)
                            try:
                                envelope = json.loads(raw) if raw else {}
                            except json.JSONDecodeError:
                                envelope = {"payload": {"_raw": raw}}
                            _dispatch(run, event_type, envelope, t0)
                        event_type, data_lines = None, []
                        if run.finished:
                            return
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].lstrip())
        except Exception as exc:  # committed cursor makes reconnect safe
            run.transport_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)


def chat(
    cfg: EvalConfig,
    case_id: str,
    engine: str,
    query: str,
    image_path: Optional[str] = None,
    base_dir: Optional[Path] = None,
    rep: int = 0,
) -> CollectedRun:
    trace_id = f"eval-{case_id}-r{rep}"
    run = CollectedRun(case_id=case_id, engine=engine, trace_id=trace_id)
    t0 = time.monotonic()
    deadline = t0 + cfg.request_timeout_s
    try:
        with httpx.Client(timeout=cfg.request_timeout_s) as client:
            attachment_refs: list[str] = []
            if image_path:
                path = (base_dir / image_path) if base_dir else Path(image_path)
                if path.exists():
                    attachment_refs.append(_upload_image(client, cfg, path, trace_id))
            create_response = client.post(
                f"{cfg.base_url}/api/v1/runs",
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "conversation_id": None,
                    "principal_id": "eval",
                    "agent_id": cfg.agent_uuid,
                    "engine": engine,
                    "input": {"text": query, "attachment_refs": attachment_refs},
                },
                headers={
                    "x-trace-id": trace_id,
                    "Idempotency-Key": f"{trace_id}-{uuid.uuid4().hex}",
                },
            )
            create_response.raise_for_status()
            run.run_id = str(create_response.json()["run_id"])
            _consume_subscription(client, cfg, run, t0, deadline)
            status_response = client.get(f"{cfg.base_url}/api/v1/runs/{run.run_id}")
            status_response.raise_for_status()
            status = status_response.json()
            terminal = status.get("terminal")
            if terminal:
                run.finished = True
                run.terminal_status = str(terminal.get("status") or "")
                if run.terminal_status != "SUCCEEDED":
                    run.had_error = True
                    run.error_msg = str((terminal.get("payload") or {}).get("message") or run.terminal_status)
    except Exception as exc:  # behavior harness records transport failures; reliability tests own the gate
        run.transport_error = f"{type(exc).__name__}: {exc}"
    run.total_ms = (time.monotonic() - t0) * 1000.0
    return run


def fetch_trace(cfg: EvalConfig, trace_id: str, level: str = "summary") -> Optional[dict[str, Any]]:
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{cfg.base_url}/api/v1/traces/{trace_id}", params={"level": level},
            )
            if response.status_code != 200:
                return None
            return response.json()
    except Exception:
        return None
