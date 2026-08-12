from __future__ import annotations

import time
from pathlib import Path

from eval.harness.sse_client import CollectedRun, _dispatch


def _event(seq: int, **payload: object) -> dict[str, object]:
    return {"seq": seq, "payload": payload}


def test_eval_harness_replaces_superseded_generation_and_uses_final_authority() -> None:
    run = CollectedRun(case_id="generation", engine="native_loop")
    started_at = time.monotonic() - 0.01

    _dispatch(run, "text_start", _event(1, generation_id="generation-1"), started_at)
    _dispatch(run, "text", _event(2, delta="superseded partial"), started_at)
    _dispatch(run, "tool_call", _event(3, name="calculator", args={"x": 1}), started_at)
    _dispatch(run, "skill_event", _event(4, type="progress"), started_at)
    _dispatch(run, "text_start", _event(5, generation_id="generation-2"), started_at)

    assert run.text == ""
    assert run.tool_calls == [("calculator", {"x": 1})]
    assert run.skill_events == [{"type": "progress"}]

    _dispatch(run, "text", _event(6, delta="recovered draft"), started_at)
    _dispatch(
        run,
        "assistant_message",
        _event(7, text="authoritative final assistant"),
        started_at,
    )

    assert run.text == "authoritative final assistant"
    assert run.last_seq == 7
    assert [event["event"] for event in run.raw_events] == [
        "text_start",
        "text",
        "tool_call",
        "skill_event",
        "text_start",
        "text",
        "assistant_message",
    ]
    assert run.ttft_ms > 0


def test_obsolete_protocol_gate_scans_current_sql_json_and_evidence_helpers() -> None:
    source = Path("scripts/check.sh").read_text(encoding="utf-8")

    assert '".sql"' in source
    assert '".json"' in source
    assert "_evidence_set_from_legacy_hits" in source
    assert "hits_to_evidence" in source
