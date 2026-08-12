from __future__ import annotations

import json

import pytest

from agent.config import AgentSettings
from common.obs import use_trace_id
from common.trace import (
    KIND_LLM,
    configure_tracing,
    get_trace,
    redact,
    start_span,
)


def test_artifact_backed_tool_payload_is_not_copied_into_trace() -> None:
    secret_slice = "large-sensitive-result" * 10_000
    value = {
        "artifact_ref": "a" * 64,
        "content": {
            "artifact_id": "a" * 64,
            "offset": 0,
            "content": secret_slice,
        },
    }

    sanitized = redact(value)
    assert sanitized["artifact_ref"] == "a" * 64
    assert sanitized["content"]["artifact_backed"] is True
    assert sanitized["content"]["chars"] > len(secret_slice)
    assert secret_slice[:100] not in str(sanitized)


def test_trace_payload_defaults_to_summary() -> None:
    assert AgentSettings(_env_file=None).trace_payload_level == "summary"


def test_exact_token_secret_key_does_not_hide_token_usage_metrics() -> None:
    sanitized = redact({
        "token": "secret-token-value",
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "max_tokens": 512,
    })
    assert sanitized == {
        "token": "***",
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "max_tokens": 512,
    }


@pytest.mark.parametrize("level", ["none", "summary", "full"])
def test_native_messages_and_tool_json_strings_redact_structured_secrets(
    tmp_path,
    level: str,
) -> None:
    password = "PASSWORD-SENTINEL-MUST-NOT-LEAK"
    token = "TOKEN-SENTINEL-MUST-NOT-LEAK"
    api_key = "APIKEY-SENTINEL-MUST-NOT-LEAK"
    nested_api_key = json.dumps({"api_key": api_key, "safe": "nested-visible"})
    messages = [
        {
            "role": "user",
            "content": json.dumps({
                "password": password,
                "safe": "user-visible",
            }),
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "provider-call-1",
                "type": "function",
                "function": {
                    "name": "safe_tool",
                    "arguments": json.dumps({
                        "token": token,
                        "nested": nested_api_key,
                        "safe": "tool-visible",
                    }),
                },
            }],
        },
    ]

    trace_id = f"trace-json-redaction-{level}"
    configure_tracing(
        enabled=True,
        payload_level=level,
        trace_dir=str(tmp_path / "traces"),
        engine="native-loop-trace-security",
    )
    try:
        with use_trace_id(trace_id):
            with start_span("native.llm", KIND_LLM) as span:
                span.set_payload("messages", messages)
                # Native executor records parsed args separately too; retain a
                # JSON-encoded nested envelope to exercise that trace path.
                span.set_payload("args", {
                    "request": json.dumps({
                        "password": password,
                        "token": token,
                        "api_key": api_key,
                        "safe": "args-visible",
                    }),
                })

        trace = get_trace(trace_id, level="full")
        assert trace is not None
        serialized = json.dumps(trace, ensure_ascii=False, sort_keys=True)
        assert password not in serialized
        assert token not in serialized
        assert api_key not in serialized

        llm = next(span for span in trace["spans"] if span["kind"] == KIND_LLM)
        if level == "none":
            assert "payloads" not in llm
        elif level == "summary":
            assert set(llm["payloads"]) == {"messages", "args"}
            assert all(
                set(summary) == {"chars", "sha1", "head"}
                for summary in llm["payloads"].values()
            )
        else:
            # full is allowed to retain non-secret original diagnostics, while
            # structured and JSON-string secret values remain masked.
            assert "user-visible" in serialized
            assert "tool-visible" in serialized
            assert "nested-visible" in serialized
            assert "args-visible" in serialized
            assert serialized.count("***") >= 6
    finally:
        configure_tracing(enabled=False, engine="trace-security-cleanup")
