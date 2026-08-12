from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.api.runs import router as run_router
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ACTIVITY_TRANSITIONS,
    ActivityStatus,
    EngineOutcome,
    EngineOutcomeKind,
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolEffectClass,
    ToolManifest,
    ToolReconciliationPayload,
    ToolResultEnvelope,
    ToolResultStatus,
    sha256_json,
)


def test_reconcile_only_recovery_edges_are_explicit_domain_adjacency():
    assert ActivityStatus.MANUAL in ACTIVITY_TRANSITIONS[ActivityStatus.PENDING]
    assert ActivityStatus.RECONCILE in ACTIVITY_TRANSITIONS[ActivityStatus.CLAIMED]
    assert ActivityStatus.CANCELLED in ACTIVITY_TRANSITIONS[ActivityStatus.RECONCILE]


@dataclass
class FakeClock:
    value: int = 1_900_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


async def _runtime(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"manual-reconcile": "v1"}),
    )
    return store, FakeClock(), release


async def _admit(store, clock, *, key: str):
    return (
        await AdmissionService(store, clock=clock, default_deadline_ms=60_000).create(
            CreateRunInput(
                client_request_id=str(uuid.uuid4()),
                conversation_id=None,
                principal_id="manual-user",
                agent_id="manual-agent",
                engine="native_loop",
                text="exercise manual reconciliation",
                attachment_refs=(),
                deadline_at=None,
            ),
            idempotency_key=key,
        )
    ).run


async def _claim_and_start(store, clock, *, worker_id: str = "manual-worker"):
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id=worker_id, lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id=worker_id,
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return claim, activity


async def _manual_wait(
    store,
    clock,
    *,
    key: str = "manual-wait",
    supports_reconcile: bool = False,
):
    run = await _admit(store, clock, key=key)
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="manual-effect:0",
        tool_name="manual_effect",
        release_digest="manual-effect-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        supports_reconcile=supports_reconcile,
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    unknown = ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code="ACK_LOST",
        error_message="external outcome is unknown",
    ).model_dump(mode="json")
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=unknown,
        result_ref=None,
        error={"code": "ACK_LOST", "message": "external outcome is unknown"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="MANUAL_REQUIRED",
        result=unknown,
        result_ref=None,
        error={"code": "TOOL_EFFECT_UNKNOWN", "message": "operator decision required"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    waiting = await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        pending_input={
            "type": "TOOL_RECONCILIATION_REQUIRED",
            "unresolved_tool_execution_ids": [prepared["tool_execution_id"]],
        },
        now_ms=clock.now_ms(),
    )
    assert waiting.status is RunStatus.WAITING_INPUT
    return run, parent, prepared


async def _manual_wait_many(
    store,
    clock,
    *,
    key: str,
    count: int,
    supports_reconcile: bool = False,
):
    """Build several independently uncertain effects behind one wait boundary."""
    run = await _admit(store, clock, key=key)
    _claim, parent = await _claim_and_start(store, clock)
    executions = []
    unknown = ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code="ACK_LOST",
        error_message="external outcome is unknown",
    ).model_dump(mode="json")
    for ordinal in range(count):
        prepared = await store.prepare_tool_execution(
            run_id=run.envelope.run_id,
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            logical_key=f"manual-effect:{ordinal}",
            tool_name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class="UNKNOWN_EFFECT",
            request_digest=sha256_json({"value": ordinal}),
            request={"value": ordinal},
            supports_reconcile=supports_reconcile,
            now_ms=clock.now_ms(),
        )
        await store.mark_tool_dispatched(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            now_ms=clock.now_ms(),
        )
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            effect_status="UNKNOWN",
            result=unknown,
            result_ref=None,
            error={"code": "ACK_LOST", "message": "external outcome is unknown"},
            external_object_id=None,
            now_ms=clock.now_ms(),
        )
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            effect_status="MANUAL_REQUIRED",
            result=unknown,
            result_ref=None,
            error={"code": "TOOL_EFFECT_UNKNOWN", "message": "operator decision required"},
            external_object_id=None,
            now_ms=clock.now_ms(),
        )
        executions.append(prepared)
    waiting = await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        pending_input={
            "type": "TOOL_RECONCILIATION_REQUIRED",
            "unresolved_tool_execution_ids": [
                item["tool_execution_id"] for item in executions
            ],
        },
        now_ms=clock.now_ms(),
    )
    assert waiting.status is RunStatus.WAITING_INPUT
    return run, parent, executions


def _normalize_payload(payload: dict) -> dict:
    return ToolReconciliationPayload.model_validate(payload).model_dump(
        mode="json", exclude_none=True,
    )


async def _submit(store, clock, run_id: str, parent_id: str, payload: dict, *, signal_id: str):
    normalized = _normalize_payload(payload)
    return await store.submit_signal(
        run_id=run_id,
        signal_id=signal_id,
        wait_activity_id=parent_id,
        signal_type="tool_reconciliation",
        payload=normalized,
        payload_digest=sha256_json({
            "type": "tool_reconciliation",
            "payload": normalized,
            "wait_activity_id": parent_id,
        }),
        now_ms=clock.now_ms(),
    )


@pytest.mark.asyncio
async def test_manual_mark_committed_is_one_atomic_effect_activity_event_and_artifact_boundary(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(store, clock)
    artifact_id = "a" * 64
    await store.register_artifact_metadata(
        artifact_id=artifact_id,
        sha256=artifact_id,
        size_bytes=123,
        media_type="application/json",
        storage_path=f"sha256/aa/{artifact_id}",
        created_at=clock.now_ms(),
    )
    with pytest.raises(RuntimeFault) as missing_ref:
        await _submit(
            store,
            clock,
            run.envelope.run_id,
            parent.activity_id,
            {
                "tool_execution_id": execution["tool_execution_id"],
                "action": "mark_committed",
                "evidence": {"source": "operator-console"},
                "result": {"status": "SUCCESS", "preview": {"created": True}},
                "result_ref": "b" * 64,
            },
            signal_id="manual-missing-artifact",
        )
    assert missing_ref.value.code == "ARTIFACT_NOT_FOUND"
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.WAITING_INPUT
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    before = await store.list_events(run.envelope.run_id, visibility=None)
    payload = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": "mark_committed",
        "evidence": {"source": "operator-console", "ticket": "INC-42"},
        "result": {"status": "SUCCESS", "preview": {"created": True}},
        "result_ref": artifact_id,
        "external_object_id": "external-task-42",
    }

    accepted = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, payload,
        signal_id="manual-committed-1",
    )

    assert accepted.status == "CONSUMED"
    assert accepted.run.status is RunStatus.DISPATCH_PENDING
    settled = await store.get_tool_execution(execution["tool_execution_id"])
    assert settled["effect_status"] == "COMMITTED"
    assert settled["reconcile_state"] == "MANUAL_COMMITTED"
    assert settled["result_ref"] == artifact_id
    assert (await store.get_activity(settled["activity_id"])).status is ActivityStatus.SUCCEEDED
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.PENDING

    events = await store.list_events(run.envelope.run_id, visibility=None)
    appended = events[len(before):]
    assert [event.event_type for event in appended] == [
        EventType.TOOL_RESULT_COMMITTED,
        EventType.ACTIVITY_STATUS_CHANGED,
        EventType.SIGNAL_RECORDED,
        EventType.ACTIVITY_STATUS_CHANGED,
        EventType.RUN_STATUS_CHANGED,
    ]
    assert appended[0].payload["manual_reconciliation"]["evidence"]["ticket"] == "INC-42"
    assert appended[0].payload["result_ref"] == artifact_id
    async with store.db.read() as conn:
        link = await (await conn.execute(
            """SELECT relation,event_id FROM artifact_links
               WHERE artifact_id=? AND run_id=? AND activity_id=?""",
            (artifact_id, run.envelope.run_id, settled["activity_id"]),
        )).fetchone()
    assert dict(link) == {
        "relation": "TOOL_RECONCILIATION_RESULT",
        "event_id": appended[0].event_id,
    }

    replay = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, payload,
        signal_id="manual-committed-1",
    )
    assert replay.reused is True
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == len(events)

    redispatches = 0
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)

    async def must_not_redispatch(_arguments, _context):
        nonlocal redispatches
        redispatches += 1
        raise AssertionError("a manually committed effect must be reused")

    broker.register(ToolManifest(
        name="manual_effect",
        release_digest="manual-effect-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=1,
        max_attempts=1,
    ), must_not_redispatch)

    class Adapter:
        name = "native_loop"
        release_fingerprint = _release

        async def execute(self, request, io):
            result = await broker.execute(
                run_id=request.envelope.run_id,
                parent_activity_id=request.activity_id,
                fencing_token=request.fencing_token,
                logical_key="manual-effect:0",
                tool_name="manual_effect",
                arguments={"value": 1},
                deadline_at_ms=request.envelope.deadline_at,
            )
            assert result.status is ToolResultStatus.SUCCESS
            assert result.result_ref == artifact_id
            await io.emit("text", {"delta": "manual result recovered"})
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="manual-commit-coordinator", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    coordinator = RunCoordinator(
        store, EngineRegistry({"native_loop": Adapter()}), clock=clock,
    )
    assert await coordinator.execute_claim(
        claim, worker_id="manual-commit-coordinator",
    ) is RunStatus.SUCCEEDED
    assert redispatches == 0
    assert (await store.get_run(run.envelope.run_id)).terminal_status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_manual_external_only_commit_normalizes_ledger_and_replays_without_executor(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="manual-external-object-only",
    )
    external_object_id = "provider-task-external-only-42"
    accepted = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_committed",
            "evidence": {"source": "provider-ledger"},
            "result": {"status": "SUCCESS"},
            "external_object_id": external_object_id,
        },
        signal_id="manual-external-only-signal",
    )
    assert accepted.run.status is RunStatus.DISPATCH_PENDING
    ledger = await store.get_tool_execution(execution["tool_execution_id"])
    assert ledger["external_object_id"] == external_object_id
    persisted_result = ToolResultEnvelope.model_validate_json(ledger["result_json"])
    assert persisted_result.external_object_id == external_object_id
    tool_result = next(
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.payload.get("manual_reconciliation", {}).get("signal_id")
        == "manual-external-only-signal"
    )
    assert tool_result.payload["external_object_id"] == external_object_id
    assert tool_result.payload["result"]["external_object_id"] == external_object_id

    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    executor_calls = 0

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        raise AssertionError("committed external object must replay from the ledger")

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
        ),
        must_not_dispatch,
    )
    _claim, resumed_parent = await _claim_and_start(
        store, clock, worker_id="external-only-replay-worker",
    )
    replay = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=resumed_parent.activity_id,
        fencing_token=resumed_parent.fencing_token,
        logical_key="manual-effect:0",
        tool_name="manual_effect",
        arguments={"value": 1},
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert replay.status is ToolResultStatus.SUCCESS
    assert replay.external_object_id == external_object_id
    assert executor_calls == 0

    with pytest.raises(ValueError, match="must identify the same object"):
        ToolReconciliationPayload.model_validate({
            "tool_execution_id": "tool_" + "a" * 32,
            "action": "mark_committed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "SUCCESS",
                "external_object_id": "nested-object",
            },
            "external_object_id": "different-top-level-object",
        })


@pytest.mark.asyncio
async def test_unsafe_dispatched_lease_recovery_enters_manual_then_mark_failed_resumes(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="unsafe-dispatched-lease-recovery")
    _claim, parent = await _claim_and_start(store, clock)
    execution = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="unsafe-dispatched:0",
        tool_name="unsafe_dispatched",
        release_digest="unsafe-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.RUNNING

    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    waiting = await store.get_run(run.envelope.run_id)
    recovered = await store.get_tool_execution(execution["tool_execution_id"])
    assert waiting.status is RunStatus.WAITING_INPUT
    assert waiting.pending_input["unresolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    assert recovered["effect_status"] == "MANUAL_REQUIRED"
    assert ToolResultEnvelope.model_validate_json(recovered["result_json"]).status is (
        ToolResultStatus.UNKNOWN
    )
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL
    recovery_result = next(
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.tool_execution_id == execution["tool_execution_id"]
        and event.payload.get("recovery") == "PARENT_LEASE_EXPIRED_UNCERTAIN_EFFECT"
    )
    assert recovery_result.payload["result"]["status"] == "UNKNOWN"

    resolved = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "provider confirms no effect",
            },
        },
        signal_id="unsafe-dispatched-mark-failed",
    )
    assert resolved.run.status is RunStatus.DISPATCH_PENDING
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "FAILED"


@pytest.mark.asyncio
async def test_cancel_unknown_lease_recovery_enters_manual_then_mark_committed_cancels(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="cancel-unknown-lease-recovery")
    _claim, parent = await _claim_and_start(store, clock)
    execution = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="cancel-unknown:0",
        tool_name="cancel_unknown",
        release_digest="cancel-unknown-v1",
        effect_class="NON_IDEMPOTENT_EFFECT",
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    unknown = ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code="ACK_LOST",
        error_message="provider ACK was lost",
    ).model_dump(mode="json")
    await store.settle_tool_execution(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=unknown,
        result_ref=None,
        error={"code": "ACK_LOST", "message": "provider ACK was lost"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-unknown-recovery",
        reason="stop after unknown effect",
        now_ms=clock.now_ms(),
    )
    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    recovered_run = await store.get_run(run.envelope.run_id)
    recovered = await store.get_tool_execution(execution["tool_execution_id"])
    assert recovered_run.status is RunStatus.CANCEL_REQUESTED
    assert recovered["effect_status"] == "MANUAL_REQUIRED"
    assert ToolResultEnvelope.model_validate_json(recovered["result_json"]).error_code == (
        "ACK_LOST"
    )
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL

    resolved = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_committed",
            "evidence": {"source": "provider-ledger"},
            "result": {"status": "SUCCESS"},
            "external_object_id": "provider-object-after-recovery",
        },
        signal_id="cancel-unknown-mark-committed",
    )
    assert resolved.run.terminal_status is RunStatus.CANCELLED
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "external_object_id"
    ] == "provider-object-after-recovery"


@pytest.mark.asyncio
async def test_cancel_inside_idempotent_executor_blocks_ack_lost_redispatch_and_enters_strict_wait(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="cancel-inside-idempotent-executor")
    _claim, parent = await _claim_and_start(store, clock)
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    calls = 0

    async def cancel_then_lose_ack(_arguments, _context):
        nonlocal calls
        calls += 1
        await store.request_cancel(
            run_id=run.envelope.run_id,
            command_id="cancel-from-executor",
            reason="cancel while downstream ACK is unavailable",
            now_ms=clock.now_ms(),
        )
        raise ConnectionError("external effect may have committed; ACK lost")

    broker.register(
        ToolManifest(
            name="idempotent_cancel_effect",
            release_digest="idempotent-cancel-v1",
            effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
            timeout_seconds=1,
            max_attempts=2,
            supports_idempotency=True,
        ),
        cancel_then_lose_ack,
    )
    with pytest.raises(RuntimeFault) as stopped:
        await broker.execute(
            run_id=run.envelope.run_id,
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            logical_key="cancel-effect:0",
            tool_name="idempotent_cancel_effect",
            arguments={"value": 1},
            deadline_at_ms=run.envelope.deadline_at,
        )
    assert stopped.value.code == "TOOL_DISPATCH_RUN_NOT_RUNNING"
    assert calls == 1
    tool_call = next(
        event
        for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.TOOL_CALL_COMMITTED
    )
    execution = await store.get_tool_execution(tool_call.tool_execution_id)
    assert execution["attempt"] == 1
    assert execution["effect_status"] == "UNKNOWN"
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.CANCEL_REQUESTED

    deferred = await store.finalize_failure(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        code="CANCELLED",
        message="cancel owns the attempt",
        terminal_status=RunStatus.CANCELLED,
        now_ms=clock.now_ms(),
    )
    assert deferred.status is RunStatus.CANCEL_REQUESTED
    assert deferred.pending_input["unresolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE

    resolved = await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_NOT_COMMITTED",
                "error_message": "provider ledger confirms no external object",
            },
        },
        signal_id="resolve-cancelled-ack-loss",
    )
    assert resolved.run.terminal_status is RunStatus.CANCELLED
    assert calls == 1


@pytest.mark.asyncio
async def test_cancel_after_claim_before_coordinator_never_enters_engine_adapter(tmp_path):
    store, clock, release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="cancel-after-claim-before-coordinator")
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="pre-adapter-cancel-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-after-claim",
        reason="do not enter the model",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    adapter_calls = 0

    class Adapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, _request, _io):
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("cancelled claim must not enter EngineAdapter")

    coordinator = RunCoordinator(
        store, EngineRegistry({"native_loop": Adapter()}), clock=clock,
    )
    status = await coordinator.execute_claim(
        claim, worker_id="pre-adapter-cancel-worker",
    )

    assert status is RunStatus.CANCELLED
    assert adapter_calls == 0
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.CANCELLED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1


@pytest.mark.asyncio
async def test_generic_engine_wait_cannot_hide_unsafe_dispatched_effect(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="generic-wait-cannot-hide-tool-effect")
    _claim, parent = await _claim_and_start(store, clock)
    execution = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="unsafe-before-hitl:0",
        tool_name="unsafe_before_hitl",
        release_digest="unsafe-before-hitl-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )

    waiting = await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        pending_input={"type": "APPROVAL", "prompt": "generic prompt must not win"},
        now_ms=clock.now_ms(),
    )
    assert waiting.status is RunStatus.WAITING_INPUT
    assert waiting.pending_input == {
        "type": "TOOL_RECONCILIATION_REQUIRED",
        "unresolved_tool_execution_ids": [execution["tool_execution_id"]],
    }
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE

    with pytest.raises(RuntimeFault) as ordinary:
        await store.submit_signal(
            run_id=run.envelope.run_id,
            signal_id="ordinary-approval-must-not-consume",
            wait_activity_id=parent.activity_id,
            signal_type="approval",
            payload={"approved": True},
            payload_digest=sha256_json({
                "type": "approval",
                "payload": {"approved": True},
                "wait_activity_id": parent.activity_id,
            }),
            now_ms=clock.now_ms(),
        )
    assert ordinary.value.code == "TOOL_RECONCILIATION_SIGNAL_REQUIRED"

    resolved = await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_NOT_COMMITTED",
                "error_message": "provider confirms no side effect",
            },
        },
        signal_id="strict-tool-resolution-after-generic-wait",
    )
    assert resolved.run.status is RunStatus.DISPATCH_PENDING


@pytest.mark.asyncio
async def test_unknown_result_correlation_survives_manual_restart_and_reconcile_query(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="unknown-correlation-roundtrip")
    _claim, parent = await _claim_and_start(store, clock)
    artifact_id = "e" * 64
    await store.register_artifact_metadata(
        artifact_id=artifact_id,
        sha256=artifact_id,
        size_bytes=17,
        media_type="application/json",
        storage_path=f"sha256/{artifact_id[:2]}/{artifact_id}",
        created_at=clock.now_ms(),
    )
    executor_calls = 0

    async def uncertain_executor(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            preview={"provider_status": "accepted"},
            result_ref=artifact_id,
            external_object_id="provider-job-42",
            error_code="ACK_LOST",
            error_message="provider accepted the job but the final ACK was lost",
        )

    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    manifest = ToolManifest(
        name="correlated_effect",
        release_digest="correlated-effect-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=1,
        max_attempts=1,
        supports_reconcile=True,
    )

    async def unused_initial_hook(_context):
        raise AssertionError("explicit UNKNOWN first enters manual, not an implicit hook")

    broker.register(manifest, uncertain_executor, reconcile=unused_initial_hook)
    manual = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="correlated-effect:0",
        tool_name="correlated_effect",
        arguments={"value": 1},
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert executor_calls == 1
    assert manual.status is ToolResultStatus.UNKNOWN
    assert manual.result_ref == artifact_id
    assert manual.external_object_id == "provider-job-42"
    tool_call = next(
        event
        for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.TOOL_CALL_COMMITTED
    )
    execution_id = tool_call.tool_execution_id
    ledger = await store.get_tool_execution(execution_id)
    assert ledger["effect_status"] == "MANUAL_REQUIRED"
    assert ledger["result_ref"] == artifact_id
    assert ledger["external_object_id"] == "provider-job-42"

    waiting = await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        pending_input={"type": "APPROVAL"},
        now_ms=clock.now_ms(),
    )
    assert waiting.pending_input["unresolved_tool_execution_ids"] == [execution_id]
    authorized = await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution_id,
            "action": "reconcile",
            "evidence": {"source": "operator", "reason": "query provider by job id"},
        },
        signal_id="reconcile-correlated-effect",
    )
    assert authorized.run.status is RunStatus.DISPATCH_PENDING
    scheduled = await store.get_tool_execution(execution_id)
    scheduled_result = ToolResultEnvelope.model_validate_json(scheduled["result_json"])
    assert scheduled["effect_status"] == "RECONCILING"
    assert scheduled_result.result_ref == artifact_id
    assert scheduled_result.external_object_id == "provider-job-42"

    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    observed_context = None
    redispatches = 0

    async def must_not_redispatch(_arguments, _context):
        nonlocal redispatches
        redispatches += 1
        raise AssertionError("reconcile-only must never call the original executor")

    async def reconcile(context):
        nonlocal observed_context
        observed_context = context
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"provider_status": "committed"},
            result_ref=artifact_id,
            external_object_id="provider-job-42",
        )

    restarted_broker = ToolBroker(
        restarted, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    restarted_broker.register(
        manifest, must_not_redispatch, reconcile=reconcile,
    )
    claim = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="correlation-reconcile-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    coordinator = RunCoordinator(
        restarted,
        EngineRegistry({}),
        clock=clock,
        tool_reconciler=restarted_broker,
    )
    assert await coordinator.execute_claim(
        claim, worker_id="correlation-reconcile-worker",
    ) is RunStatus.DISPATCH_PENDING
    assert redispatches == 0
    assert observed_context.prior_result_ref == artifact_id
    assert observed_context.prior_external_object_id == "provider-job-42"
    assert observed_context.prior_preview == {"provider_status": "accepted"}
    committed = await restarted.get_tool_execution(execution_id)
    committed_result = ToolResultEnvelope.model_validate_json(committed["result_json"])
    assert committed["effect_status"] == "COMMITTED"
    assert committed_result.result_ref == artifact_id
    assert committed_result.external_object_id == "provider-job-42"


@pytest.mark.asyncio
async def test_inconclusive_hook_correlation_is_visible_to_reauthorized_hook_after_restart(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="hook-discovers-correlation", supports_reconcile=True,
    )
    await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="hook-discovers-correlation-first",
    )
    first_seen = None

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("reconcile-only must not call original executor")

    async def inconclusive(context):
        nonlocal first_seen
        first_seen = context.prior_external_object_id
        return ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            preview={"provider_status": "still_running"},
            external_object_id="provider-job-from-hook-7",
            error_code="PROVIDER_PENDING",
            error_message="provider job exists but is not terminal",
        )

    manifest = ToolManifest(
        name="manual_effect",
        release_digest="manual-effect-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=1,
        max_attempts=1,
        supports_reconcile=True,
    )
    first_broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    first_broker.register(manifest, must_not_dispatch, reconcile=inconclusive)
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="hook-correlation-first-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    first_coordinator = RunCoordinator(
        store, EngineRegistry({}), clock=clock, tool_reconciler=first_broker,
    )
    assert await first_coordinator.execute_claim(
        claim, worker_id="hook-correlation-first-worker",
    ) is RunStatus.WAITING_INPUT
    assert first_seen is None
    discovered = await store.get_tool_execution(execution["tool_execution_id"])
    assert discovered["effect_status"] == "MANUAL_REQUIRED"
    assert discovered["external_object_id"] == "provider-job-from-hook-7"

    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    await _submit(
        restarted,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator", "attempt": 2},
        },
        signal_id="hook-discovers-correlation-second",
    )
    second_seen = None

    async def conclusive(context):
        nonlocal second_seen
        second_seen = (
            context.prior_external_object_id,
            context.prior_preview,
        )
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"provider_status": "committed"},
            external_object_id="provider-job-from-hook-7",
        )

    second_broker = ToolBroker(
        restarted, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    second_broker.register(manifest, must_not_dispatch, reconcile=conclusive)
    second_claim = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="hook-correlation-second-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert second_claim is not None
    second_coordinator = RunCoordinator(
        restarted, EngineRegistry({}), clock=clock, tool_reconciler=second_broker,
    )
    assert await second_coordinator.execute_claim(
        second_claim, worker_id="hook-correlation-second-worker",
    ) is RunStatus.DISPATCH_PENDING
    assert second_seen == (
        "provider-job-from-hook-7",
        {"provider_status": "still_running"},
    )
    committed = await restarted.get_tool_execution(execution["tool_execution_id"])
    assert committed["effect_status"] == "COMMITTED"
    assert committed["external_object_id"] == "provider-job-from-hook-7"


@pytest.mark.asyncio
async def test_cancel_takes_over_replay_safe_effect_before_replacement_claim(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="cancel-safe-effect-before-replacement")
    _claim, parent = await _claim_and_start(store, clock)
    execution = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="safe-before-cancel:0",
        tool_name="safe_before_cancel",
        release_digest="safe-before-cancel-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({"query": "value"}),
        request={"query": "value"},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.PENDING

    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-safe-replacement",
        reason="cancel owns the idle replay boundary",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    assert cancelled.pending_input["unresolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE
    assert await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="must-not-replay-after-cancel",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    ) is None

    resolved = await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "operator"},
            "result": {
                "status": "FAILURE",
                "error_code": "READ_NOT_APPLIED",
                "error_message": "read-only call has no durable side effect",
            },
        },
        signal_id="resolve-safe-effect-after-cancel",
    )
    assert resolved.run.terminal_status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_mixed_recovery_waits_only_for_manual_then_replays_safe_effect(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="mixed-safe-unsafe-recovery")
    _claim, parent = await _claim_and_start(store, clock)
    safe = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="mixed-safe:0",
        tool_name="mixed_safe_read",
        release_digest="mixed-safe-v1",
        effect_class="READ_ONLY",
        request_digest=sha256_json({"query": "value"}),
        request={"query": "value"},
        now_ms=clock.now_ms(),
    )
    unsafe = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="mixed-unsafe:0",
        tool_name="mixed_unsafe_effect",
        release_digest="mixed-unsafe-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock.now_ms(),
    )
    for execution in (safe, unsafe):
        await store.mark_tool_dispatched(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            now_ms=clock.now_ms(),
        )

    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    waiting = await store.get_run(run.envelope.run_id)
    assert waiting.status is RunStatus.WAITING_INPUT
    assert waiting.pending_input["unresolved_tool_execution_ids"] == [
        unsafe["tool_execution_id"]
    ]
    assert (await store.get_tool_execution(safe["tool_execution_id"]))[
        "effect_status"
    ] == "DISPATCHED"
    assert (await store.get_tool_execution(unsafe["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"

    resolved = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": unsafe["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "unsafe effect was not committed",
            },
        },
        signal_id="mixed-unsafe-manual-resolution",
    )
    assert resolved.run.status is RunStatus.DISPATCH_PENDING
    assert resolved.run.pending_input is None

    _replacement_claim, replacement_parent = await _claim_and_start(
        store, clock, worker_id="mixed-safe-replay-worker",
    )
    safe_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def replay_safe(_arguments, context):
        nonlocal safe_calls
        safe_calls += 1
        assert context.idempotency_key == safe["idempotency_key"]
        return {"value": "replayed safely"}

    broker.register(
        ToolManifest(
            name="mixed_safe_read",
            release_digest="mixed-safe-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        replay_safe,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=replacement_parent.activity_id,
        fencing_token=replacement_parent.fencing_token,
        logical_key="mixed-safe:0",
        tool_name="mixed_safe_read",
        arguments={"query": "value"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert safe_calls == 1
    assert (await store.get_tool_execution(safe["tool_execution_id"]))[
        "effect_status"
    ] == "COMMITTED"


@pytest.mark.asyncio
async def test_two_manual_decisions_have_one_effect_cas_winner(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="manual-concurrent-decision",
    )
    committed = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": "mark_committed",
        "evidence": {"source": "operator-a"},
        "result": {"status": "SUCCESS", "preview": {"confirmed": True}},
    }
    failed = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": "mark_failed",
        "evidence": {"source": "operator-b"},
        "result": {
            "status": "FAILURE",
            "error_code": "NOT_COMMITTED",
            "error_message": "external effect was absent",
        },
    }
    outcomes = await asyncio.gather(
        _submit(
            store, clock, run.envelope.run_id, parent.activity_id, committed,
            signal_id="manual-race-committed",
        ),
        _submit(
            store, clock, run.envelope.run_id, parent.activity_id, failed,
            signal_id="manual-race-failed",
        ),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if not isinstance(item, BaseException)]
    losers = [item for item in outcomes if isinstance(item, RuntimeFault)]
    assert len(winners) == len(losers) == 1
    assert losers[0].code == "RUN_NOT_WAITING_INPUT"
    settled = await store.get_tool_execution(execution["tool_execution_id"])
    assert settled["effect_status"] in {"COMMITTED", "FAILED"}
    events = await store.list_events(run.envelope.run_id, visibility=None)
    manual_results = [
        event for event in events
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.payload.get("manual_reconciliation") is not None
    ]
    assert len(manual_results) == 1
    async with store.db.read() as conn:
        signal_rows = await (await conn.execute(
            "SELECT signal_id FROM signals WHERE run_id=? ORDER BY signal_id",
            (run.envelope.run_id,),
        )).fetchall()
    assert len(signal_rows) == 1


@pytest.mark.asyncio
async def test_manual_mark_failed_is_sticky_and_never_redispatches_unknown_effect(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="manual-failed-sticky")
    _claim, parent = await _claim_and_start(store, clock)
    calls = 0

    async def ambiguous(_arguments, _context):
        nonlocal calls
        calls += 1
        raise ConnectionError("ACK lost")

    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)
    broker.register(ToolManifest(
        name="charge_once",
        release_digest="charge-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=1,
        max_attempts=7,
    ), ambiguous)
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="charge:0",
        tool_name="charge_once",
        arguments={"amount": 10},
        deadline_at_ms=run.envelope.deadline_at,
    )
    first = await broker.execute(**call)
    assert first.status is ToolResultStatus.UNKNOWN
    execution_id = next(
        event.tool_execution_id
        for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.TOOL_CALL_COMMITTED
    )
    await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        pending_input={
            "type": "TOOL_RECONCILIATION_REQUIRED",
            "unresolved_tool_execution_ids": [execution_id],
        },
        now_ms=clock.now_ms(),
    )
    await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution_id,
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger", "effect_absent": True},
            "result": {
                "status": "FAILURE",
                "error_code": "EXTERNAL_EFFECT_NOT_COMMITTED",
                "error_message": "provider ledger proves no charge was committed",
            },
        },
        signal_id="manual-failed-1",
    )
    resumed = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="manual-worker-2", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert resumed is not None
    parent2 = await store.mark_activity_running(
        resumed.activity.activity_id,
        worker_id="manual-worker-2",
        fencing_token=resumed.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    replay = await broker.execute(**dict(call, fencing_token=parent2.fencing_token))
    assert replay.status is ToolResultStatus.FAILURE
    assert replay.error_code == "EXTERNAL_EFFECT_NOT_COMMITTED"
    assert calls == 1
    execution = await store.get_tool_execution(execution_id)
    assert execution["effect_status"] == "FAILED"
    assert execution["reconcile_state"] == "MANUAL_FAILED"


@pytest.mark.asyncio
async def test_reconcile_signal_then_coordinator_queries_hook_and_finishes_without_repeat_wait(
    tmp_path,
):
    store, clock, release = await _runtime(tmp_path)
    run = await _admit(store, clock, key="manual-reconcile-coordinator")
    dispatches = 0
    reconciles = 0
    adapter_calls = 0
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)

    async def ambiguous(_arguments, _context):
        nonlocal dispatches
        dispatches += 1
        raise ConnectionError("external ACK was lost")

    async def reconcile(_context):
        nonlocal reconciles
        reconciles += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"confirmed": True},
            external_object_id="external-confirmed-1",
        )

    broker.register(
        ToolManifest(
            name="reconcilable_effect",
            release_digest="reconcilable-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        ambiguous,
        reconcile=reconcile,
    )

    class Adapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, request, io):
            nonlocal adapter_calls
            adapter_calls += 1
            result = await broker.execute(
                run_id=request.envelope.run_id,
                parent_activity_id=request.activity_id,
                fencing_token=request.fencing_token,
                logical_key="reconcilable:0",
                tool_name="reconcilable_effect",
                arguments={"task": "create"},
                deadline_at_ms=request.envelope.deadline_at,
            )
            if result.status is ToolResultStatus.SUCCESS:
                await io.emit("text", {"delta": "confirmed exactly once"})
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    coordinator = RunCoordinator(
        store, EngineRegistry({"native_loop": Adapter()}), clock=clock,
        tool_reconciler=broker,
    )
    first = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="coordinator-1", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert first is not None
    assert await coordinator.execute_claim(first, worker_id="coordinator-1") is RunStatus.WAITING_INPUT
    waiting = await store.get_run(run.envelope.run_id)
    execution_id = waiting.pending_input["unresolved_tool_execution_ids"][0]
    execution = await store.get_tool_execution(execution_id)
    assert execution["effect_status"] == "MANUAL_REQUIRED"
    assert bool(execution["supports_reconcile"]) is True

    await _submit(
        store,
        clock,
        run.envelope.run_id,
        waiting.current_activity_id,
        {
            "tool_execution_id": execution_id,
            "action": "reconcile",
            "evidence": {"source": "operator", "reason": "query provider ledger"},
        },
        signal_id="query-again-1",
    )
    signalled = await store.get_tool_execution(execution_id)
    assert signalled["effect_status"] == "RECONCILING"
    assert (await store.get_activity(signalled["activity_id"])).status is ActivityStatus.PENDING

    second = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="coordinator-2", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert second is not None
    assert await coordinator.execute_claim(
        second, worker_id="coordinator-2",
    ) is RunStatus.DISPATCH_PENDING
    assert adapter_calls == 1
    third = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="coordinator-3", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert third is not None
    assert await coordinator.execute_claim(
        third, worker_id="coordinator-3",
    ) is RunStatus.SUCCEEDED
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.SUCCEEDED
    assert dispatches == 1
    assert reconciles == 1
    assert adapter_calls == 2
    assert (await store.get_tool_execution(execution_id))["effect_status"] == "COMMITTED"


@pytest.mark.asyncio
async def test_reconcile_hook_confirmed_failure_is_failed_and_never_becomes_committed(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="manual-reconcile-failure", supports_reconcile=True,
    )
    await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator", "reason": "query provider ledger"},
        },
        signal_id="query-failure-1",
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="reconcile-failure-worker", lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert claim.run.envelope.run_id == run.envelope.run_id
    dispatches = 0
    reconciles = 0
    adapter_calls = 0
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)

    async def must_not_dispatch(_arguments, _context):
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("reconciliation is a query, not an ordinary redispatch")

    async def confirmed_failure(_context):
        nonlocal reconciles
        reconciles += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="EFFECT_NOT_COMMITTED",
            error_message="provider ledger confirms no effect",
        )

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=7,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=confirmed_failure,
    )
    class MustNotExecuteAdapter:
        name = "native_loop"
        release_fingerprint = _release

        async def execute(self, _request, _io):
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("reconcile-only claim must bypass EngineAdapter")

    coordinator = RunCoordinator(
        store,
        EngineRegistry({"native_loop": MustNotExecuteAdapter()}),
        clock=clock,
        tool_reconciler=broker,
    )
    assert await coordinator.execute_claim(
        claim, worker_id="reconcile-failure-worker",
    ) is RunStatus.DISPATCH_PENDING
    assert dispatches == 0
    assert reconciles == 1
    assert adapter_calls == 0
    settled = await store.get_tool_execution(execution["tool_execution_id"])
    assert settled["effect_status"] == "FAILED"
    assert settled["reconcile_state"] == "FAILED"
    assert (await store.get_activity(settled["activity_id"])).status is ActivityStatus.FAILED


@pytest.mark.asyncio
async def test_kill_during_reconcile_hook_recovers_to_manual_then_resignals_and_finishes(
    tmp_path,
):
    store, clock, release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="reconcile-hook-kill", supports_reconcile=True,
    )
    entered = asyncio.Event()
    original_dispatches = 0

    async def must_not_dispatch(_arguments, _context):
        nonlocal original_dispatches
        original_dispatches += 1
        raise AssertionError("manual reconcile must never redispatch the effect")

    async def killed_hook(_context):
        entered.set()
        await asyncio.Future()

    first_broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    first_broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=60,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=killed_hook,
    )

    before_signal = len(await store.list_events(run.envelope.run_id, visibility=None))
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator", "ticket": "KILL-1"},
        },
        signal_id="reconcile-before-kill",
    )
    authorization_events = (
        await store.list_events(run.envelope.run_id, visibility=None)
    )[before_signal:]
    assert EventType.TOOL_RESULT_COMMITTED not in {
        event.event_type for event in authorization_events
    }

    class MustNotExecuteAdapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, _request, _io):
            raise AssertionError("reconcile-only claim reached EngineAdapter")

    first_coordinator = RunCoordinator(
        store,
        EngineRegistry({"native_loop": MustNotExecuteAdapter()}),
        clock=clock,
        tool_reconciler=first_broker,
    )
    first_claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="killed-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert first_claim is not None
    task = asyncio.create_task(
        first_coordinator.execute_claim(first_claim, worker_id="killed-worker")
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "RECONCILING"

    clock.value += 30_001
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    recovered_effect = await restarted.get_tool_execution(execution["tool_execution_id"])
    assert recovered_effect["effect_status"] == "MANUAL_REQUIRED"
    assert (await restarted.get_activity(recovered_effect["activity_id"])).status is ActivityStatus.MANUAL
    recovered_run = await restarted.get_run(run.envelope.run_id)
    assert recovered_run.status is RunStatus.WAITING_INPUT
    assert (await restarted.get_activity(parent.activity_id)).resume_payload is None

    successful_queries = 0
    second_broker = ToolBroker(
        restarted, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def successful_hook(_context):
        nonlocal successful_queries
        successful_queries += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"confirmed": True},
            external_object_id="external-after-restart",
        )

    second_broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=successful_hook,
    )
    await _submit(
        restarted, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator", "ticket": "KILL-1-RETRY"},
        },
        signal_id="reconcile-after-kill",
    )
    adapter_calls = 0

    class FinalAdapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, _request, io):
            nonlocal adapter_calls
            adapter_calls += 1
            await io.emit("text", {"delta": "recovered"})
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    coordinator = RunCoordinator(
        restarted,
        EngineRegistry({"native_loop": FinalAdapter()}),
        clock=clock,
        tool_reconciler=second_broker,
    )
    query_claim = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="restarted-query", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert query_claim is not None
    assert await coordinator.execute_claim(
        query_claim, worker_id="restarted-query",
    ) is RunStatus.DISPATCH_PENDING
    assert adapter_calls == 0
    final_claim = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="restarted-engine", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert final_claim is not None
    assert await coordinator.execute_claim(
        final_claim, worker_id="restarted-engine",
    ) is RunStatus.SUCCEEDED
    assert adapter_calls == successful_queries == 1
    assert original_dispatches == 0
    terminal_events = [
        event for event in await restarted.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_started", [False, True])
async def test_kill_before_hook_start_recovers_scheduled_child_to_manual(
    tmp_path, parent_started,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="reconcile-before-hook-kill", supports_reconcile=True,
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="reconcile-scheduled-kill",
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="dies-before-broker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert claim.run.envelope.run_id == run.envelope.run_id
    if parent_started:
        claimed_parent = await store.mark_activity_running(
            claim.activity.activity_id,
            worker_id="dies-before-broker",
            fencing_token=claim.activity.fencing_token,
            now_ms=clock.now_ms(),
        )
    else:
        claimed_parent = claim.activity
    child = await store.get_activity(execution["activity_id"])
    assert child.status is ActivityStatus.PENDING

    clock.value += 30_001
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    assert (await restarted.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert (await restarted.get_activity(execution["activity_id"])).status is ActivityStatus.MANUAL
    recovered_parent = await restarted.get_activity(claimed_parent.activity_id)
    assert recovered_parent.status is ActivityStatus.RECONCILE
    assert recovered_parent.resume_payload is None
    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.WAITING_INPUT


@pytest.mark.asyncio
async def test_claimed_reconcile_parent_with_expired_deadline_recovers_timed_out(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="claimed-reconcile-deadline", supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-claimed-reconcile-deadline",
        reason="deadline and lease expire together",
        now_ms=clock.now_ms(),
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="claimed-reconcile-deadline-query",
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="claimed-deadline-worker", lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert claim.activity.status is ActivityStatus.CLAIMED

    clock.value = run.envelope.deadline_at + 1
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    terminal = await restarted.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT
    assert terminal.terminal_payload["unresolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    recovered_parent = await restarted.get_activity(parent.activity_id)
    assert recovered_parent.status is ActivityStatus.CANCELLED
    assert recovered_parent.lease_owner is None
    assert recovered_parent.lease_expires_at is None


@pytest.mark.asyncio
async def test_exact_reconcile_claim_rechecks_deadline_before_hook(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="exact-reconcile-deadline", supports_reconcile=True,
    )
    await _submit(
        store,
        clock,
        run.envelope.run_id,
        parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="exact-reconcile-deadline-signal",
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="exact-deadline-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await store.renew_lease(
        claim.activity.activity_id,
        worker_id="exact-deadline-worker",
        fencing_token=claim.activity.fencing_token,
        lease_expires_at=run.envelope.deadline_at + 10_000,
        now_ms=clock.now_ms(),
    )
    hook_calls = 0

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("exact reconciliation cannot redispatch")

    async def reconcile(_context):
        nonlocal hook_calls
        hook_calls += 1
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=reconcile,
    )
    clock.value = run.envelope.deadline_at
    coordinator = RunCoordinator(
        store, EngineRegistry({}), clock=clock, tool_reconciler=broker,
    )

    assert await coordinator.execute_claim(
        claim, worker_id="exact-deadline-worker",
    ) is RunStatus.TIMED_OUT
    assert hook_calls == 0
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT
    assert terminal.terminal_payload["unresolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1


@pytest.mark.asyncio
async def test_kill_after_hook_settlement_clears_marker_and_resumes_engine(tmp_path):
    store, clock, release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="reconcile-after-settle-kill", supports_reconcile=True,
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="reconcile-settled-kill",
    )
    _claim, running_parent = await _claim_and_start(
        store, clock, worker_id="dies-after-settle",
    )
    marker = running_parent.resume_payload
    assert marker is not None
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )
    executor_calls = 0

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        raise AssertionError("original executor must remain unreachable")

    async def confirmed(_context):
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=confirmed,
    )
    await broker.reconcile_only(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=running_parent.fencing_token,
        expected_effect_revision=marker["expected_effect_revision"],
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "COMMITTED"

    clock.value += 30_001
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    recovered_parent = await restarted.get_activity(parent.activity_id)
    assert recovered_parent.status is ActivityStatus.PENDING
    assert recovered_parent.resume_payload is None
    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING

    adapter_calls = 0

    class FinalAdapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, _request, io):
            nonlocal adapter_calls
            adapter_calls += 1
            await io.emit("text", {"delta": "settled before crash"})
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    coordinator = RunCoordinator(
        restarted, EngineRegistry({"native_loop": FinalAdapter()}), clock=clock,
    )
    claim = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="after-settle-recovery", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(
        claim, worker_id="after-settle-recovery",
    ) is RunStatus.SUCCEEDED
    assert adapter_calls == 1
    assert executor_calls == 0


@pytest.mark.asyncio
async def test_cancel_kill_after_hook_settlement_deadline_recovers_timed_out_once(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="cancel-settle-kill-deadline", supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-settle-kill",
        reason="exercise recovery deadline precedence",
        now_ms=clock.now_ms(),
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="cancel-settle-kill-query",
    )
    _claim, running_parent = await _claim_and_start(
        store, clock, worker_id="cancel-dies-after-hook",
    )
    marker = running_parent.resume_payload
    assert marker is not None
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("original executor must remain unreachable")

    async def confirmed(_context):
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=confirmed,
    )
    await broker.reconcile_only(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=running_parent.fencing_token,
        expected_effect_revision=marker["expected_effect_revision"],
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "COMMITTED"

    clock.value = run.envelope.deadline_at + 1
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    timed_out = await restarted.get_run(run.envelope.run_id)
    assert timed_out.terminal_status is RunStatus.TIMED_OUT
    assert timed_out.terminal_payload["code"] == "DEADLINE_EXCEEDED"
    # The effect was conclusively committed before the crash, so it is audited
    # as a late result but is no longer listed as unresolved.
    assert "unresolved_tool_execution_ids" not in timed_out.terminal_payload
    assert await restarted.expire_deadlines(now_ms=clock.now_ms()) == 0
    terminals = [
        event for event in await restarted.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]
    assert len(terminals) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "result", "expected_effect"),
    [
        (
            "mark_committed",
            {"status": "SUCCESS", "preview": {"confirmed": True}},
            "COMMITTED",
        ),
        (
            "mark_failed",
            {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "provider ledger confirms no effect",
            },
            "FAILED",
        ),
    ],
)
async def test_cancel_first_manual_mark_settles_effect_and_unique_cancelled_terminal(
    tmp_path, action, result, expected_effect,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key=f"cancel-{action}",
    )
    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id=f"cancel-command-{action}",
        reason="operator cancelled while effect was uncertain",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    decision = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": action,
        "evidence": {"source": "provider-ledger"},
        "result": result,
    }
    accepted = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, decision,
        signal_id=f"cancel-signal-{action}",
    )
    assert accepted.run.status is RunStatus.CANCELLED
    assert accepted.run.terminal_status is RunStatus.CANCELLED
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == expected_effect
    assert (await store.get_activity(execution["activity_id"])).status is (
        ActivityStatus.SUCCEEDED
        if expected_effect == "COMMITTED"
        else ActivityStatus.FAILED
    )
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.CANCELLED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    manual_result = next(
        event for event in events
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.payload.get("manual_reconciliation", {}).get("signal_id")
        == f"cancel-signal-{action}"
    )
    assert manual_result.payload["late_result"] is True
    assert manual_result.payload["cancel_requested"] is True
    terminals = [event for event in events if event.event_type is EventType.RUN_TERMINATED]
    assert len(terminals) == 1
    assert terminals[0].payload["reconciliation_signal_id"] == f"cancel-signal-{action}"
    assert terminals[0].payload["resolved_tool_execution_ids"] == [
        execution["tool_execution_id"]
    ]
    assert await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="must-not-revive", lease_ms=30_000, now_ms=clock.now_ms(),
    ) is None
    replay = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, decision,
        signal_id=f"cancel-signal-{action}",
    )
    assert replay.reused is True
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == len(events)


@pytest.mark.asyncio
async def test_stale_reconcile_effect_revision_fails_before_hook_or_event(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="stale-reconcile-revision", supports_reconcile=True,
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="stale-reconcile-revision-signal",
    )
    marker = (await store.get_activity(parent.activity_id)).resume_payload
    assert marker is not None
    hook_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("original executor must remain unreachable")

    async def hook(_context):
        nonlocal hook_calls
        hook_calls += 1
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=hook,
    )
    before = await store.list_events(run.envelope.run_id, visibility=None)
    with pytest.raises(RuntimeFault) as stale:
        await broker.reconcile_only(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            expected_effect_revision=marker["expected_effect_revision"] - 1,
            deadline_at_ms=run.envelope.deadline_at,
        )
    assert stale.value.code == "TOOL_RECONCILIATION_MISMATCH"
    assert hook_calls == 0
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == len(before)
    assert (await store.get_tool_execution(execution["tool_execution_id"]))[
        "effect_status"
    ] == "RECONCILING"


@pytest.mark.asyncio
async def test_cancel_reconcile_only_resolves_one_of_many_then_last_signal_cancels(tmp_path):
    store, clock, release = await _runtime(tmp_path)
    run, parent, executions = await _manual_wait_many(
        store,
        clock,
        key="cancel-reconcile-many",
        count=2,
        supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-many-command",
        reason="stop but preserve effect truth",
        now_ms=clock.now_ms(),
    )
    first_id, second_id = [item["tool_execution_id"] for item in executions]
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": first_id,
            "action": "reconcile",
            "evidence": {"source": "provider-query"},
        },
        signal_id="cancel-many-query-first",
    )
    executor_calls = 0
    query_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        raise AssertionError("cancel reconcile-only redispatched the effect")

    async def confirmed(_context):
        nonlocal query_calls
        query_calls += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"confirmed": True},
            external_object_id="external-many-first",
        )

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=confirmed,
    )

    coordinator = RunCoordinator(
        store,
        # Empty registry is intentional proof that the exact marker branch is
        # evaluated before any Engine lookup.
        EngineRegistry({}),
        clock=clock,
        tool_reconciler=broker,
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="cancel-reconcile-query", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert claim.run.status is RunStatus.CANCEL_REQUESTED
    assert await coordinator.execute_claim(
        claim, worker_id="cancel-reconcile-query",
    ) is RunStatus.CANCEL_REQUESTED
    after_first = await store.get_run(run.envelope.run_id)
    assert after_first.status is RunStatus.CANCEL_REQUESTED
    assert after_first.pending_input["unresolved_tool_execution_ids"] == [second_id]
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE
    assert executor_calls == 0
    assert query_calls == 1

    final = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": second_id,
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "second effect was not committed",
            },
        },
        signal_id="cancel-many-final",
    )
    assert final.run.terminal_status is RunStatus.CANCELLED
    terminal = [
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]
    assert len(terminal) == 1
    assert terminal[0].payload["resolved_tool_execution_ids"] == sorted(
        [first_id, second_id]
    )
    assert terminal[0].payload["remaining_unresolved_tool_execution_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_hook_calls"),
    [
        ("unregistered", 0),
        ("release_mismatch", 0),
        ("hook_raises", 1),
        ("hook_inconclusive", 1),
    ],
)
async def test_cancel_reconcile_hook_unavailable_or_inconclusive_returns_to_manual(
    tmp_path, failure_mode, expected_hook_calls,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store,
        clock,
        key=f"cancel-reconcile-{failure_mode}",
        supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id=f"cancel-{failure_mode}",
        reason="query only",
        now_ms=clock.now_ms(),
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator", "mode": failure_mode},
        },
        signal_id=f"query-{failure_mode}",
    )
    executor_calls = 0
    hook_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        raise AssertionError("reconcile-only path invoked original executor")

    async def problematic_hook(_context):
        nonlocal hook_calls
        hook_calls += 1
        if failure_mode == "hook_raises":
            raise ConnectionError("query transport failed")
        if failure_mode == "hook_inconclusive":
            return None
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    if failure_mode != "unregistered":
        broker.register(
            ToolManifest(
                name="manual_effect",
                release_digest=(
                    "different-release"
                    if failure_mode == "release_mismatch"
                    else "manual-effect-v1"
                ),
                effect_class=ToolEffectClass.UNKNOWN_EFFECT,
                timeout_seconds=1,
                max_attempts=1,
                supports_reconcile=True,
            ),
            must_not_dispatch,
            reconcile=problematic_hook,
        )
    coordinator = RunCoordinator(
        store,
        EngineRegistry({}),
        clock=clock,
        tool_reconciler=broker,
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id=f"worker-{failure_mode}", lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(
        claim, worker_id=f"worker-{failure_mode}",
    ) is RunStatus.CANCEL_REQUESTED
    assert executor_calls == 0
    assert hook_calls == expected_hook_calls
    effect = await store.get_tool_execution(execution["tool_execution_id"])
    assert effect["effect_status"] == "MANUAL_REQUIRED"
    assert (await store.get_activity(effect["activity_id"])).status is ActivityStatus.MANUAL
    waiting_parent = await store.get_activity(parent.activity_id)
    assert waiting_parent.status is ActivityStatus.RECONCILE
    assert waiting_parent.resume_payload is None
    assert (await store.get_run(run.envelope.run_id)).pending_input[
        "unresolved_tool_execution_ids"
    ] == [execution["tool_execution_id"]]

    # The failed query did not consume the operator's authority forever: a new
    # strict decision can still close the cancel-owned effect deterministically.
    final = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "manual follow-up proves no effect",
            },
        },
        signal_id=f"manual-after-{failure_mode}",
    )
    assert final.run.terminal_status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_owned_hook_kill_recovers_to_cancel_reconciliation_not_running(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="cancel-hook-kill", supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-hook-kill-command",
        reason="cancel before reconcile query",
        now_ms=clock.now_ms(),
    )
    query_payload = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": "reconcile",
        "evidence": {"source": "operator"},
    }
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, query_payload,
        signal_id="cancel-hook-killed-query",
    )
    entered = asyncio.Event()
    executor_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        raise AssertionError("cancel query invoked original executor")

    async def killed_hook(_context):
        entered.set()
        await asyncio.Future()

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=60,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=killed_hook,
    )
    coordinator = RunCoordinator(
        store, EngineRegistry({}), clock=clock, tool_reconciler=broker,
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="cancel-hook-killed-worker", lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert claim.run.status is RunStatus.CANCEL_REQUESTED
    task = asyncio.create_task(coordinator.execute_claim(
        claim, worker_id="cancel-hook-killed-worker",
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    clock.value += 30_001
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    recovered_run = await restarted.get_run(run.envelope.run_id)
    recovered_parent = await restarted.get_activity(parent.activity_id)
    recovered_effect = await restarted.get_tool_execution(execution["tool_execution_id"])
    assert recovered_run.status is RunStatus.CANCEL_REQUESTED
    assert recovered_parent.status is ActivityStatus.RECONCILE
    assert recovered_parent.resume_payload is None
    assert recovered_effect["effect_status"] == "MANUAL_REQUIRED"
    assert (await restarted.get_activity(recovered_effect["activity_id"])).status is ActivityStatus.MANUAL
    assert executor_calls == 0

    # Replaying the old authorization is audit-only idempotency; only a new
    # strict signal may change the recovered MANUAL_REQUIRED effect.
    replay = await _submit(
        restarted, clock, run.envelope.run_id, parent.activity_id, query_payload,
        signal_id="cancel-hook-killed-query",
    )
    assert replay.reused is True
    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.CANCEL_REQUESTED
    final = await _submit(
        restarted, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "manual follow-up confirms no effect",
            },
        },
        signal_id="cancel-hook-kill-final",
    )
    assert final.run.terminal_status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_final_manual_mark_rolls_back_all_authority_if_terminal_event_fails(
    tmp_path,
):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="cancel-manual-terminal-rollback",
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-terminal-injection",
        reason="inject terminal failure",
        now_ms=clock.now_ms(),
    )
    payload = {
        "tool_execution_id": execution["tool_execution_id"],
        "action": "mark_committed",
        "evidence": {"source": "provider-ledger"},
        "result": {"status": "SUCCESS", "preview": {"confirmed": True}},
    }
    before_events = await store.list_events(run.envelope.run_id, visibility=None)
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER fail_cancel_reconciliation_terminal
               BEFORE INSERT ON run_events
               WHEN NEW.event_type='RUN_TERMINATED'
               BEGIN SELECT RAISE(ABORT,'injected cancel terminal failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        await _submit(
            store, clock, run.envelope.run_id, parent.activity_id, payload,
            signal_id="cancel-terminal-atomic-signal",
        )
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.CANCEL_REQUESTED
    effect = await store.get_tool_execution(execution["tool_execution_id"])
    assert effect["effect_status"] == "MANUAL_REQUIRED"
    assert (await store.get_activity(effect["activity_id"])).status is ActivityStatus.MANUAL
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == len(before_events)
    async with store.db.read() as conn:
        signal = await (await conn.execute(
            "SELECT signal_id FROM signals WHERE run_id=? AND signal_id=?",
            (run.envelope.run_id, "cancel-terminal-atomic-signal"),
        )).fetchone()
    assert signal is None

    async with store.db.transaction() as conn:
        await conn.execute("DROP TRIGGER fail_cancel_reconciliation_terminal")
    accepted = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id, payload,
        signal_id="cancel-terminal-atomic-signal",
    )
    assert accepted.run.terminal_status is RunStatus.CANCELLED
    terminal_events = [
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_manual_signal_before_cancel_and_cancel_before_signal_both_terminalize_once(
    tmp_path,
):
    # signal-first: effect becomes certain and the subsequent cancel is direct.
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="signal-before-cancel",
    )
    signalled = await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "provider-ledger"},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_ABSENT",
                "error_message": "effect was absent",
            },
        },
        signal_id="signal-first-resolution",
    )
    assert signalled.run.status is RunStatus.DISPATCH_PENDING
    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-after-signal",
        reason="stop after effect resolution",
        now_ms=clock.now_ms(),
    )
    assert cancelled.terminal_status is RunStatus.CANCELLED
    assert len([
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]) == 1


@pytest.mark.asyncio
async def test_reconciliation_deadline_guards_signal_query_and_stale_fence(tmp_path):
    store, clock, release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(
        store, clock, key="reconcile-deadline", supports_reconcile=True,
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-deadline",
        reason="deadline race",
        now_ms=clock.now_ms(),
    )
    await _submit(
        store, clock, run.envelope.run_id, parent.activity_id,
        {
            "tool_execution_id": execution["tool_execution_id"],
            "action": "reconcile",
            "evidence": {"source": "operator"},
        },
        signal_id="deadline-query",
    )
    hook_calls = 0
    adapter_calls = 0
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock,
    )

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("query boundary invoked the original executor")

    async def finishes_at_deadline(_context):
        nonlocal hook_calls
        hook_calls += 1
        clock.value = run.envelope.deadline_at
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker.register(
        ToolManifest(
            name="manual_effect",
            release_digest="manual-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=120,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=finishes_at_deadline,
    )

    class MustNotExecuteAdapter:
        name = "native_loop"
        release_fingerprint = release

        async def execute(self, _request, _io):
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("deadline reconcile reached EngineAdapter")

    coordinator = RunCoordinator(
        store,
        EngineRegistry({"native_loop": MustNotExecuteAdapter()}),
        clock=clock,
        tool_reconciler=broker,
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="deadline-query-worker", lease_ms=120_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(
        claim, worker_id="deadline-query-worker",
    ) is RunStatus.TIMED_OUT
    assert hook_calls == 1
    assert adapter_calls == 0
    timed_out = await store.get_run(run.envelope.run_id)
    assert timed_out.terminal_status is RunStatus.TIMED_OUT
    async with store.db.read() as conn:
        activity_row = await (await conn.execute(
            "SELECT lease_owner,lease_expires_at FROM activities WHERE activity_id=?",
            (parent.activity_id,),
        )).fetchone()
    assert dict(activity_row) == {"lease_owner": None, "lease_expires_at": None}
    with pytest.raises(RuntimeFault) as stale:
        await store.settle_reconciliation_query(
            run_id=run.envelope.run_id,
            activity_id=parent.activity_id,
            fencing_token=claim.activity.fencing_token,
            tool_execution_id=execution["tool_execution_id"],
            signal_id="deadline-query",
            expected_effect_revision=claim.activity.resume_payload[
                "expected_effect_revision"
            ],
            now_ms=clock.now_ms(),
        )
    assert stale.value.code == "STALE_FENCING_TOKEN"
    assert len([
        event for event in await store.list_events(run.envelope.run_id, visibility=None)
        if event.event_type is EventType.RUN_TERMINATED
    ]) == 1

    # A separate Run proves submit_signal itself cannot cross the absolute
    # deadline merely because the maintenance scan has not run yet.
    second, second_parent, second_execution = await _manual_wait(
        store, clock, key="reconcile-signal-after-deadline",
    )
    clock.value = second.envelope.deadline_at
    with pytest.raises(RuntimeFault) as late_signal:
        await _submit(
            store, clock, second.envelope.run_id, second_parent.activity_id,
            {
                "tool_execution_id": second_execution["tool_execution_id"],
                "action": "mark_failed",
                "evidence": {"source": "operator"},
                "result": {
                    "status": "FAILURE",
                    "error_code": "LATE",
                    "error_message": "decision arrived after deadline",
                },
            },
            signal_id="late-manual-signal",
        )
    assert late_signal.value.code == "RUN_DEADLINE_EXCEEDED"
    assert (await store.get_tool_execution(second_execution["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    second_timed_out = await store.get_run(second.envelope.run_id)
    assert second_timed_out.terminal_status is RunStatus.TIMED_OUT
    assert second_timed_out.terminal_payload["unresolved_tool_execution_ids"] == [
        second_execution["tool_execution_id"]
    ]


def _api(store: SqliteRuntimeStore) -> FastAPI:
    app = FastAPI()
    app.state.runtime_store = store
    app.state.settings = SimpleNamespace(
        runtime_default_deadline_seconds=60,
        runtime_sse_heartbeat_seconds=15,
        runtime_sse_poll_ms=1,
    )
    app.include_router(run_router)

    @app.exception_handler(RuntimeFault)
    async def handle(_request: Request, exc: RuntimeFault):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    return app


@pytest.mark.asyncio
async def test_public_api_validates_reconciliation_and_fails_closed_on_mismatch(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(store, clock, key="manual-api")
    transport = httpx.ASGITransport(app=_api(store))
    url = f"/api/v1/runs/{run.envelope.run_id}/signals"
    base = {
        "signal_id": "api-manual-1",
        "wait_activity_id": parent.activity_id,
        "type": "tool_reconciliation",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(url, json={
            **base,
            "payload": {
                "tool_execution_id": execution["tool_execution_id"],
                "action": "mark_committed",
                "result": {"status": "SUCCESS", "preview": {"ok": True}},
            },
        })
        assert invalid.status_code == 422

        contradictory_no_output = await client.post(url, json={
            **dict(base, signal_id="api-manual-no-output-ref"),
            "payload": {
                "tool_execution_id": execution["tool_execution_id"],
                "action": "mark_committed",
                "evidence": {"source": "operator"},
                "result": {"status": "NO_OUTPUT"},
                "result_ref": "a" * 64,
            },
        })
        assert contradictory_no_output.status_code == 422

        mismatched = await client.post(url, json={
            **base,
            "payload": {
                "tool_execution_id": "tool_" + "f" * 32,
                "action": "mark_failed",
                "evidence": {"source": "operator"},
                "result": {
                    "status": "FAILURE",
                    "error_code": "NOT_COMMITTED",
                    "error_message": "not found in external ledger",
                },
            },
        })
        assert mismatched.status_code == 409
        assert mismatched.json()["error"]["code"] == "TOOL_RECONCILIATION_MISMATCH"

        unsupported = await client.post(url, json={
            **dict(base, signal_id="api-manual-reconcile-unsupported"),
            "payload": {
                "tool_execution_id": execution["tool_execution_id"],
                "action": "reconcile",
                "evidence": {"source": "operator", "reason": "query again"},
            },
        })
        assert unsupported.status_code == 409
        assert unsupported.json()["error"]["code"] == "TOOL_RECONCILE_UNSUPPORTED"

        accepted = await client.post(url, json={
            **dict(base, signal_id="api-manual-2"),
            "payload": {
                "tool_execution_id": execution["tool_execution_id"],
                "action": "mark_committed",
                "evidence": {"source": "operator"},
                "result": {"status": "SUCCESS", "preview": {"ok": True}},
            },
        })
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "CONSUMED"
        assert accepted.json()["run"]["status"] == "DISPATCH_PENDING"


@pytest.mark.asyncio
async def test_public_api_accepts_strict_manual_resolution_after_cancel_wins(tmp_path):
    store, clock, _release = await _runtime(tmp_path)
    run, parent, execution = await _manual_wait(store, clock, key="manual-api-cancel")
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="api-cancel-before-manual",
        reason="cancel first",
        now_ms=clock.now_ms(),
    )
    transport = httpx.ASGITransport(app=_api(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run.envelope.run_id}/signals",
            json={
                "signal_id": "api-cancel-manual-failed",
                "wait_activity_id": parent.activity_id,
                "type": "tool_reconciliation",
                "payload": {
                    "tool_execution_id": execution["tool_execution_id"],
                    "action": "mark_failed",
                    "evidence": {"source": "provider-ledger"},
                    "result": {
                        "status": "FAILURE",
                        "error_code": "EFFECT_ABSENT",
                        "error_message": "external ledger confirms no effect",
                    },
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CONSUMED"
    assert body["run"]["status"] == "CANCELLED"
    assert body["run"]["terminal"]["status"] == "CANCELLED"
