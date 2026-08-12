from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from pydantic import ValidationError as PydanticValidationError
from referencing import Registry, Resource

from agent.runtime.api.runs import _sse
from agent.runtime.domain.artifact import ArtifactRef
from agent.runtime.domain.models import (
    CanonicalEvent,
    EvidenceItem,
    EvidenceSet,
    EventType,
    RunStatus,
    RuntimeEnvelope,
    RetrievalStatus,
    ToolResultEnvelope,
    ToolResultStatus,
    WorkingState,
    new_id,
    stable_id,
)


SCHEMA_DIR = Path("docs/reliability/schemas")
SCHEMA_FILENAMES = (
    "runtime-envelope-v1.schema.json",
    "canonical-event-v1.schema.json",
    "working-state-v1.schema.json",
    "tool-result-envelope-v1.schema.json",
    "artifact-v1.schema.json",
    "evidence-v1.schema.json",
)


def _schemas() -> tuple[dict, Registry]:
    by_name = {
        filename: json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        for filename in SCHEMA_FILENAMES
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in by_name.values()
    )
    return by_name, registry


def _validate(filename: str, value: object) -> None:
    by_name, registry = _schemas()
    Draft202012Validator(
        by_name[filename],
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(value)


def _authority_objects(now_ms: int = 1_800_000_000_000):
    release = "a" * 64
    run_id = new_id("run")
    activity_id = stable_id("act", run_id, "engine:0")
    envelope = RuntimeEnvelope(
        request_id=new_id("req"),
        client_request_id="94ad553f-bd65-47e3-9d31-31d095455231",
        idempotency_key="schema-contract",
        conversation_id=new_id("conv"),
        turn_id=new_id("turn"),
        run_id=run_id,
        principal_id="demo-user",
        agent_id="demo-agent",
        engine="native_loop",
        deadline_at=now_ms + 60_000,
        cancel_token_id=new_id("cancel"),
        release_fingerprint=release,
        input_event_id=new_id("evt"),
        attachment_refs=("b" * 64,),
        created_at=now_ms,
    )
    event = CanonicalEvent(
        event_id=new_id("evt"),
        run_id=run_id,
        turn_id=envelope.turn_id,
        activity_id=activity_id,
        seq=7,
        event_type=EventType.OUTPUT_DELTA_COMMITTED,
        producer="engine:native_loop",
        payload={"delta": "committed"},
        occurred_at=now_ms,
        release_fingerprint=release,
    )
    working_state = WorkingState(
        goal="answer reliably",
        constraints=["committed facts only"],
        model_plan=[{"step": 1, "title": "inspect", "status": "running"}],
        confirmed_facts=[{"fact": "event is committed"}],
        open_questions=["need tool?"],
        pending_input={"type": "APPROVAL", "prompt": "continue?"},
        budget={"deadline_at": now_ms + 60_000, "model_calls_used": 1},
        artifact_refs=["b" * 64],
        evidence_refs=["ev_example"],
    )
    return envelope, event, working_state


def test_runtime_checkpoint_and_event_schemas_validate_real_model_dumps() -> None:
    envelope, event, working_state = _authority_objects()

    _validate("runtime-envelope-v1.schema.json", envelope.model_dump(mode="json"))
    _validate("canonical-event-v1.schema.json", event.model_dump(mode="json"))
    _validate("working-state-v1.schema.json", working_state.model_dump(mode="json"))

    assert "-" not in envelope.run_id
    assert "-" not in event.activity_id


def test_terminal_event_schema_matches_store_terminal_annotation() -> None:
    envelope, event, _ = _authority_objects()
    terminal = event.model_copy(update={
        "event_id": new_id("evt"),
        "seq": 8,
        "event_type": EventType.RUN_TERMINATED,
        "payload": {"assistant_chars": 9},
        "terminal_status": RunStatus.SUCCEEDED,
    })

    _validate("canonical-event-v1.schema.json", terminal.model_dump(mode="json"))


def test_native_generation_start_is_a_canonical_event_contract() -> None:
    _, event, _ = _authority_objects()
    generation = event.model_copy(update={
        "event_id": new_id("evt"),
        "event_type": EventType.OUTPUT_GENERATION_STARTED,
        "payload": {
            "message_id": "model-slot-1",
            "generation_id": "generation-1",
            "supersedes_generation_id": None,
            "reason": "recovery",
        },
    })

    _validate("canonical-event-v1.schema.json", generation.model_dump(mode="json"))


@pytest.mark.parametrize(
    "result",
    [
        ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"value": 42},
            result_ref="c" * 64,
            external_object_id="task-42",
        ),
        ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="TOOL_FAILED",
            error_message="failed deterministically",
        ),
        ToolResultEnvelope(
            status=ToolResultStatus.INTERRUPT,
            pending_input={"type": "APPROVAL"},
        ),
        ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT),
        ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            error_code="ACK_LOST",
            error_message="external outcome unavailable",
        ),
    ],
)
def test_tool_result_schema_validates_every_real_result_kind(
    result: ToolResultEnvelope,
) -> None:
    _validate("tool-result-envelope-v1.schema.json", result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "override",
    [
        {"unexpected": True},
        {"result_ref": "A" * 64},
        {"error_code": ""},
        {"error_code": "x" * 129},
        {"error_message": "x" * 8193},
        {"external_object_id": ""},
        {"external_object_id": "x" * 2049},
        {"status": "FAILURE", "error_code": None, "error_message": "failed"},
        {"status": "UNKNOWN", "error_code": "ACK_LOST", "error_message": None},
        {"status": "INTERRUPT", "pending_input": None},
        {"status": "NO_OUTPUT", "preview": {"contradiction": True}},
        {"status": "NO_OUTPUT", "result_ref": "a" * 64},
    ],
)
def test_tool_result_pydantic_rejects_every_payload_rejected_by_frozen_schema(
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "status": "SUCCESS",
        "preview": {"ok": True},
        "result_ref": None,
        "error_code": None,
        "error_message": None,
        "external_object_id": None,
        "pending_input": None,
    }
    payload.update(override)

    with pytest.raises(ValidationError):
        _validate("tool-result-envelope-v1.schema.json", payload)
    with pytest.raises(PydanticValidationError):
        ToolResultEnvelope.model_validate(payload)


def test_artifact_ref_schema_validates_public_model_dump() -> None:
    digest = "d" * 64
    ref = ArtifactRef(
        artifact_id=digest,
        digest_sha256=digest,
        size_bytes=7,
        media_type="text/plain; charset=utf-8",
        filename="answer.txt",
        created_at=datetime(2026, 8, 9, 3, 4, 5, tzinfo=UTC),
    )

    dumped = ref.model_dump(mode="json")
    _validate("artifact-v1.schema.json", dumped)
    assert dumped["created_at"] == "2026-08-09T03:04:05Z"


def test_evidence_schema_validates_broker_authority_with_full_provenance() -> None:
    run_id = new_id("run")
    activity_id = stable_id("act", run_id, "tool:knowledge:0")
    tool_execution_id = stable_id("tool", run_id, "knowledge:0")
    content = "SQLite WAL separates readers from committed writes."
    evidence_set = EvidenceSet(
        query="How does WAL help?",
        query_id="qry_" + "e" * 64,
        run_id=run_id,
        activity_id=activity_id,
        principal_id="demo-user",
        dataset_scope=("runtime-docs",),
        scope="public",
        retrieval_status=RetrievalStatus.HIT,
        rewrites=("SQLite WAL reliability",),
        cost_ms=12,
        degraded_reasons=(),
        tool_execution_id=tool_execution_id,
        retrieved_at="2027-01-15T08:00:00Z",
        evidence=(EvidenceItem(
            n=1,
            evidence_id="ev_" + "f" * 64,
            chunk_id="chunk_1",
            doc_id="runtime-guide",
            title="Runtime Guide",
            document_id="doc_1",
            document_version_id="dver_1",
            index_version="dver_1",
            content_hash="1" * 64,
            dataset_id="runtime-docs",
            scope="public",
            query_id="qry_" + "e" * 64,
            page=2,
            span_start=10,
            span_end=62,
            content=content,
            score=0.91,
            source="fused",
        ),),
    )

    dumped = evidence_set.model_dump(mode="json")
    _validate("evidence-v1.schema.json", dumped)
    evidence = dumped["evidence"][0]
    assert {
        "document_id",
        "document_version_id",
        "index_version",
        "content_hash",
        "page",
        "span_start",
        "span_end",
        "scope",
        "query_id",
    } <= evidence.keys()


def test_internal_epoch_and_public_rfc3339_event_boundaries_do_not_blur() -> None:
    _, event, _ = _authority_objects()
    authority = event.model_dump(mode="json")
    _validate("canonical-event-v1.schema.json", authority)
    assert isinstance(authority["occurred_at"], int)

    public_data = json.loads(
        next(line for line in _sse(event).splitlines() if line.startswith("data: "))[6:]
    )
    assert public_data["occurred_at"].endswith("Z")
    assert "producer" not in public_data
    assert "visibility" not in public_data
    assert "sensitivity" not in public_data

    with pytest.raises(ValidationError):
        _validate(
            "canonical-event-v1.schema.json",
            {**authority, "occurred_at": public_data["occurred_at"]},
        )


def test_schema_additional_properties_catch_domain_model_drift() -> None:
    envelope, _, _ = _authority_objects()
    with pytest.raises(ValidationError):
        _validate(
            "runtime-envelope-v1.schema.json",
            {**envelope.model_dump(mode="json"), "unversioned_new_field": True},
        )
