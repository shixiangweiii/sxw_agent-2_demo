from __future__ import annotations

from common.trace import redact


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
