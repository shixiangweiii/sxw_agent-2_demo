from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import uuid
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.application.tool_outputs import skill_center_output
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ActivityStatus,
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolEffectClass,
    ToolManifest,
    ToolResultEnvelope,
    ToolResultStatus,
    sha256_json,
)


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


async def _running_parent(tmp_path):
    clock = FakeClock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"tool-state-test": "v1"}),
    )
    admitted = await AdmissionService(
        store, clock=clock, default_deadline_ms=60_000
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="exercise tool activity state machine",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=str(uuid.uuid4()),
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="tool-state-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    parent = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="tool-state-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    broker = ToolBroker(
        store,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
    )
    return store, clock, admitted.run, parent, broker


async def _tool_facts(store, run_id: str, logical_key: str):
    events = await store.list_events(run_id, visibility=None)
    tool_call = next(
        event
        for event in events
        if event.event_type is EventType.TOOL_CALL_COMMITTED
        and event.payload["logical_key"] == logical_key
    )
    related = [
        event
        for event in events
        if event.tool_execution_id == tool_call.tool_execution_id
    ]
    edges = [
        (event.payload.get("from"), event.payload["to"])
        for event in related
        if event.event_type is EventType.ACTIVITY_STATUS_CHANGED
    ]
    results = [
        event.payload["status"]
        for event in related
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
    ]
    return tool_call, edges, results


@pytest.mark.asyncio
async def test_read_only_transient_failure_retries_through_legal_activity_edges(tmp_path):
    store, clock, run, parent, broker = await _running_parent(tmp_path)
    calls = 0

    async def flaky_read(_arguments, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient read failure")
        return {"value": "recovered"}

    broker.register(
        ToolManifest(
            name="flaky_read",
            release_digest="flaky-read-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        flaky_read,
    )
    logical_key = "tool-state:read:0"
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key=logical_key,
        tool_name="flaky_read",
        arguments={"query": "x"},
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.preview == {"value": "recovered"}
    assert calls == 2
    tool_call, edges, results = await _tool_facts(
        store, run.envelope.run_id, logical_key
    )
    assert edges == [
        (None, "PENDING"),
        ("PENDING", "CLAIMED"),
        ("CLAIMED", "RUNNING"),
        ("RUNNING", "WAITING_RETRY"),
        ("WAITING_RETRY", "PENDING"),
        ("PENDING", "CLAIMED"),
        ("CLAIMED", "RUNNING"),
        ("RUNNING", "SUCCEEDED"),
    ]
    assert results == ["FAILED", "COMMITTED"]
    assert not any(before == after for before, after in edges)
    assert ("FAILED", "RUNNING") not in edges
    execution = await store.get_tool_execution(tool_call.tool_execution_id)
    activity = await store.get_activity(tool_call.activity_id)
    assert execution["attempt"] == 2
    assert execution["effect_status"] == "COMMITTED"
    assert activity.status is ActivityStatus.SUCCEEDED
    assert activity.attempt == 2


@pytest.mark.asyncio
async def test_read_only_max_attempt_failure_is_terminal_and_replay_is_side_effect_free(
    tmp_path,
):
    store, _clock, run, parent, broker = await _running_parent(tmp_path)
    calls = 0

    async def always_fails(_arguments, _context):
        nonlocal calls
        calls += 1
        raise OSError("persistent read failure")

    broker.register(
        ToolManifest(
            name="bounded_read",
            release_digest="bounded-read-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        always_fails,
    )
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="tool-state:max:0",
        tool_name="bounded_read",
        arguments={"query": "x"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    first = await broker.execute(**call)
    event_count_after_first = len(
        await store.list_events(run.envelope.run_id, visibility=None)
    )
    replay = await broker.execute(**call)

    assert first.status is replay.status is ToolResultStatus.FAILURE
    assert calls == 2
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == event_count_after_first
    tool_call, edges, results = await _tool_facts(
        store, run.envelope.run_id, "tool-state:max:0"
    )
    assert edges[-1] == ("RUNNING", "FAILED")
    assert results == ["FAILED", "FAILED"]
    assert not any(before == after for before, after in edges)
    activity = await store.get_activity(tool_call.activity_id)
    assert activity.status is ActivityStatus.FAILED
    assert activity.attempt == 2


@pytest.mark.asyncio
async def test_tool_reported_errors_are_bounded_before_durable_failure(tmp_path):
    store, _clock, run, parent, broker = await _running_parent(tmp_path)

    async def oversized_error(_arguments, _context):
        return {
            "isError": True,
            "errorCode": "E" * 1000,
            "content": "m" * 20_000,
        }

    broker.register(
        ToolManifest(
            name="oversized_error",
            release_digest="oversized-error-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=1,
        ),
        oversized_error,
        result_adapter=skill_center_output,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="tool-state:oversized-error:0",
        tool_name="oversized_error",
        arguments={},
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert result.status is ToolResultStatus.FAILURE
    assert len(result.error_code) == 128
    assert len(result.error_message) == 8192
    call, _edges, _results = await _tool_facts(
        store, run.envelope.run_id, "tool-state:oversized-error:0",
    )
    execution = await store.get_tool_execution(call.tool_execution_id)
    assert execution["effect_status"] == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect_class",
    [ToolEffectClass.UNKNOWN_EFFECT, ToolEffectClass.NON_IDEMPOTENT_EFFECT],
)
async def test_unsafe_tool_reported_failure_is_conservatively_persisted_unknown(
    tmp_path, effect_class,
):
    store, _clock, run, parent, broker = await _running_parent(tmp_path)

    async def reported_failure(_arguments, _context):
        return ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            preview={"provider_status": "accepted_before_error"},
            external_object_id="provider-job-failure-42",
            error_code="PROVIDER_ERROR",
            error_message="provider response cannot prove the effect was absent",
        )

    broker.register(
        ToolManifest(
            name="unsafe_reported_failure",
            release_digest="unsafe-reported-failure-v1",
            effect_class=effect_class,
            timeout_seconds=1,
            max_attempts=1,
        ),
        reported_failure,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="unsafe-reported-failure:0",
        tool_name="unsafe_reported_failure",
        arguments={},
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert result.status is ToolResultStatus.UNKNOWN
    assert result.external_object_id == "provider-job-failure-42"
    call, _edges, _results = await _tool_facts(
        store, run.envelope.run_id, "unsafe-reported-failure:0",
    )
    execution = await store.get_tool_execution(call.tool_execution_id)
    assert execution["effect_status"] == "MANUAL_REQUIRED"
    persisted = ToolResultEnvelope.model_validate_json(execution["result_json"])
    assert persisted.status is ToolResultStatus.UNKNOWN
    assert persisted.preview == {"provider_status": "accepted_before_error"}
    assert persisted.external_object_id == "provider-job-failure-42"


@pytest.mark.asyncio
async def test_idempotent_unknown_replay_keeps_identity_and_uses_reconcile_boundary(tmp_path):
    store, clock, run, parent, broker = await _running_parent(tmp_path)
    identities: list[tuple[str, int, int]] = []

    async def idempotent_effect(_arguments, context):
        identities.append(
            (context.idempotency_key, context.attempt, context.remaining_ms)
        )
        if context.attempt == 1:
            raise ConnectionError("effect committed but ACK was lost")
        return {"external_id": "task-1"}

    broker.register(
        ToolManifest(
            name="idempotent_effect",
            release_digest="idempotent-effect-v1",
            effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
            timeout_seconds=1,
            max_attempts=2,
            supports_idempotency=True,
        ),
        idempotent_effect,
    )
    logical_key = "tool-state:idempotent:0"
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key=logical_key,
        tool_name="idempotent_effect",
        arguments={"title": "demo"},
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert len(identities) == 2
    assert identities[0][0] == identities[1][0]
    assert [item[1] for item in identities] == [1, 2]
    assert [item[2] for item in identities] == [60_000, 60_000]
    tool_call, edges, results = await _tool_facts(
        store, run.envelope.run_id, logical_key
    )
    assert edges == [
        (None, "PENDING"),
        ("PENDING", "CLAIMED"),
        ("CLAIMED", "RUNNING"),
        ("RUNNING", "RECONCILE"),
        ("RECONCILE", "PENDING"),
        ("PENDING", "CLAIMED"),
        ("CLAIMED", "RUNNING"),
        ("RUNNING", "SUCCEEDED"),
    ]
    assert results == ["UNKNOWN", "COMMITTED"]
    assert not any(before == after for before, after in edges)
    execution = await store.get_tool_execution(tool_call.tool_execution_id)
    assert execution["attempt"] == 2
    assert execution["effect_status"] == "COMMITTED"
    assert execution["idempotency_key"] == identities[0][0]


@pytest.mark.asyncio
async def test_idempotent_retry_success_inherits_known_external_identity_immediately(tmp_path):
    store, _clock, run, parent, broker = await _running_parent(tmp_path)
    calls = 0
    second_prior_identity = None

    async def idempotent_effect(_arguments, context):
        nonlocal calls, second_prior_identity
        calls += 1
        if calls == 1:
            return ToolResultEnvelope(
                status=ToolResultStatus.UNKNOWN,
                preview={"provider_status": "accepted"},
                external_object_id="provider-stable-job-9",
                error_code="ACK_LOST",
                error_message="provider accepted the job but ACK was lost",
            )
        second_prior_identity = context.prior_external_object_id
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"provider_status": "committed"},
        )

    broker.register(
        ToolManifest(
            name="idempotent_identity",
            release_digest="idempotent-identity-v1",
            effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
            timeout_seconds=1,
            max_attempts=2,
            supports_idempotency=True,
        ),
        idempotent_effect,
    )
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="idempotent-identity:0",
        tool_name="idempotent_identity",
        arguments={"value": 1},
        deadline_at_ms=run.envelope.deadline_at,
    )
    result = await broker.execute(**call)
    replay = await broker.execute(**call)

    assert calls == 2
    assert second_prior_identity == "provider-stable-job-9"
    assert result.external_object_id == "provider-stable-job-9"
    assert replay.external_object_id == "provider-stable-job-9"
    tool_call, _edges, _results = await _tool_facts(
        store, run.envelope.run_id, "idempotent-identity:0",
    )
    ledger = await store.get_tool_execution(tool_call.tool_execution_id)
    assert ledger["effect_status"] == "COMMITTED"
    assert ledger["external_object_id"] == "provider-stable-job-9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect_class", "replay_allowed"),
    [
        (ToolEffectClass.READ_ONLY, True),
        (ToolEffectClass.IDEMPOTENT_EFFECT, True),
        (ToolEffectClass.NON_IDEMPOTENT_EFFECT, False),
        (ToolEffectClass.UNKNOWN_EFFECT, False),
    ],
)
async def test_store_frozen_effect_guard_controls_direct_redispatch(
    tmp_path, effect_class, replay_allowed,
):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="store-replay-guard:0",
        tool_name="guarded_tool",
        release_digest="guarded-v1",
        effect_class=effect_class,
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock.now_ms(),
    )
    first = await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    if replay_allowed:
        replay = await store.mark_tool_dispatched(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            now_ms=clock.now_ms(),
        )
        assert replay["attempt"] == 2
    else:
        with pytest.raises(RuntimeFault) as forbidden:
            await store.mark_tool_dispatched(
                tool_execution_id=prepared["tool_execution_id"],
                parent_activity_id=parent.activity_id,
                fencing_token=parent.fencing_token,
                now_ms=clock.now_ms(),
            )
        assert forbidden.value.code == "TOOL_EFFECT_REPLAY_FORBIDDEN"
        unchanged = await store.get_tool_execution(prepared["tool_execution_id"])
        assert unchanged["attempt"] == first["attempt"] == 1
        assert unchanged["effect_status"] == "DISPATCHED"


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_retry", [False, True])
async def test_store_refuses_initial_or_retry_dispatch_at_absolute_deadline(
    tmp_path, safe_retry,
):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="deadline-dispatch:0",
        tool_name="deadline_read",
        release_digest="deadline-read-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({}),
        request={},
        now_ms=clock.now_ms(),
    )
    if safe_retry:
        await store.mark_tool_dispatched(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            now_ms=clock.now_ms(),
        )
        failure = ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="TRANSIENT",
            error_message="retry only before the absolute deadline",
        )
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            effect_status="FAILED",
            result=failure.model_dump(mode="json"),
            result_ref=None,
            error={"code": failure.error_code, "message": failure.error_message},
            external_object_id=None,
            retry_at=clock.now_ms(),
            now_ms=clock.now_ms(),
        )
    assert await store.renew_lease(
        parent.activity_id,
        worker_id="tool-state-worker",
        fencing_token=parent.fencing_token,
        lease_expires_at=run.envelope.deadline_at + 10_000,
        now_ms=clock.now_ms(),
    )
    clock.value = run.envelope.deadline_at

    with pytest.raises(RuntimeFault) as expired:
        await store.mark_tool_dispatched(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            now_ms=clock.now_ms(),
        )
    assert expired.value.code == "TOOL_DISPATCH_DEADLINE_EXPIRED"
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1


@pytest.mark.asyncio
async def test_broker_rechecks_deadline_after_dispatch_commit_before_executor(tmp_path):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    assert await store.renew_lease(
        parent.activity_id,
        worker_id="tool-state-worker",
        fencing_token=parent.fencing_token,
        lease_expires_at=run.envelope.deadline_at + 10_000,
        now_ms=clock.now_ms(),
    )

    class AdvanceAfterDispatch:
        def __getattr__(self, name):
            return getattr(store, name)

        async def mark_tool_dispatched(self, **kwargs):
            row = await store.mark_tool_dispatched(**kwargs)
            clock.value = run.envelope.deadline_at
            return row

    executor_calls = 0

    async def must_not_execute(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        return {"wrong": True}

    broker = ToolBroker(
        AdvanceAfterDispatch(),
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
    )
    broker.register(
        ToolManifest(
            name="deadline_toctou_read",
            release_digest="deadline-toctou-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=1,
        ),
        must_not_execute,
    )
    with pytest.raises(RuntimeFault) as expired:
        await broker.execute(
            run_id=run.envelope.run_id,
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            logical_key="deadline-toctou-dispatch:0",
            tool_name="deadline_toctou_read",
            arguments={},
            deadline_at_ms=run.envelope.deadline_at,
        )
    assert expired.value.code == "TOOL_DISPATCH_DEADLINE_EXPIRED"
    assert executor_calls == 0
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    assert (await store.get_run(run.envelope.run_id)).terminal_status is RunStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_broker_rechecks_deadline_after_reconcile_commit_before_hook(tmp_path):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    arguments = {"value": 1}
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="deadline-toctou-reconcile:0",
        tool_name="deadline_toctou_effect",
        release_digest="deadline-toctou-effect-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json(arguments),
        request=arguments,
        supports_reconcile=True,
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
        error_message="query may clarify the external outcome",
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=unknown.model_dump(mode="json"),
        result_ref=None,
        error={"code": unknown.error_code, "message": unknown.error_message},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    assert await store.renew_lease(
        parent.activity_id,
        worker_id="tool-state-worker",
        fencing_token=parent.fencing_token,
        lease_expires_at=run.envelope.deadline_at + 10_000,
        now_ms=clock.now_ms(),
    )

    class AdvanceAfterReconcile:
        def __getattr__(self, name):
            return getattr(store, name)

        async def mark_tool_reconciling(self, **kwargs):
            row = await store.mark_tool_reconciling(**kwargs)
            clock.value = run.envelope.deadline_at
            return row

    hook_calls = 0

    async def must_not_dispatch(_arguments, _context):
        raise AssertionError("uncertain effect cannot redispatch")

    async def must_not_query(_context):
        nonlocal hook_calls
        hook_calls += 1
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)

    broker = ToolBroker(
        AdvanceAfterReconcile(),
        FilesystemArtifactStore(tmp_path / "artifacts-reconcile"),
        clock=clock,
    )
    broker.register(
        ToolManifest(
            name="deadline_toctou_effect",
            release_digest="deadline-toctou-effect-v1",
            effect_class=ToolEffectClass.UNKNOWN_EFFECT,
            timeout_seconds=1,
            max_attempts=1,
            supports_reconcile=True,
        ),
        must_not_dispatch,
        reconcile=must_not_query,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="deadline-toctou-reconcile:0",
        tool_name="deadline_toctou_effect",
        arguments=arguments,
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.status is ToolResultStatus.UNKNOWN
    assert hook_calls == 0
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect_status", "result"),
    [
        ("COMMITTED", None),
        (
            "COMMITTED",
            ToolResultEnvelope(
                status=ToolResultStatus.FAILURE,
                error_code="FAILED",
                error_message="failure cannot claim committed effect",
            ),
        ),
        (
            "FAILED",
            ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview={"contradiction": True},
            ),
        ),
    ],
)
async def test_store_rejects_effect_and_result_status_contradictions(
    tmp_path, effect_status, result,
):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="effect-result-matrix:0",
        tool_name="matrix_tool",
        release_digest="matrix-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json({}),
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    before = len(await store.list_events(run.envelope.run_id, visibility=None))
    with pytest.raises(RuntimeFault) as mismatch:
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            effect_status=effect_status,
            result=result.model_dump(mode="json") if result is not None else None,
            result_ref=None,
            error=None,
            external_object_id=None,
            now_ms=clock.now_ms(),
        )
    assert mismatch.value.code == "TOOL_RESULT_EFFECT_MISMATCH"
    unchanged = await store.get_tool_execution(prepared["tool_execution_id"])
    assert unchanged["effect_status"] == "DISPATCHED"
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == before


@pytest.mark.asyncio
async def test_store_artifact_metadata_and_normalized_result_ref_are_atomic(tmp_path):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="artifact-ref-matrix:0",
        tool_name="artifact_matrix_tool",
        release_digest="artifact-matrix-v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json({}),
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    result_ref = "a" * 64
    metadata_ref = "b" * 64
    result = ToolResultEnvelope(
        status=ToolResultStatus.SUCCESS,
        preview={"stored": True},
        result_ref=result_ref,
    )
    before = len(await store.list_events(run.envelope.run_id, visibility=None))

    with pytest.raises(RuntimeFault) as mismatch:
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            effect_status="COMMITTED",
            result=result.model_dump(mode="json"),
            result_ref=result_ref,
            error=None,
            external_object_id=None,
            artifact_metadata={
                "artifact_id": metadata_ref,
                "sha256": metadata_ref,
                "size_bytes": 3,
                "media_type": "application/json",
                "storage_path": f"sha256/{metadata_ref[:2]}/{metadata_ref}",
                "created_at": clock.now_ms(),
            },
            now_ms=clock.now_ms(),
        )
    assert mismatch.value.code == "TOOL_RESULT_ARTIFACT_MISMATCH"
    assert (await store.get_tool_execution(prepared["tool_execution_id"]))[
        "effect_status"
    ] == "DISPATCHED"
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == before
    with pytest.raises(RuntimeFault) as absent:
        await store.get_artifact_metadata(metadata_ref)
    assert absent.value.code == "ARTIFACT_NOT_FOUND"


@pytest.mark.asyncio
async def test_interrupt_is_a_valid_committed_tool_result(tmp_path):
    store, clock, run, parent, _broker = await _running_parent(tmp_path)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="interrupt-result:0",
        tool_name="request_input",
        release_digest="request-input-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({}),
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    interrupt = ToolResultEnvelope(
        status=ToolResultStatus.INTERRUPT,
        pending_input={"type": "APPROVAL"},
    )
    settled = await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="COMMITTED",
        result=interrupt.model_dump(mode="json"),
        result_ref=None,
        error=None,
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    assert settled["effect_status"] == "COMMITTED"


@pytest.mark.asyncio
async def test_expired_parent_requeues_safe_dispatch_but_not_unknown_effect(tmp_path):
    store, clock, run, parent, broker = await _running_parent(tmp_path)
    logical_key = "lease:read-only:0"
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key=logical_key,
        tool_name="recoverable_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({"query": "x"}),
        request={"query": "x"},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    assert (await store.get_run(run.envelope.run_id)).status.value == "DISPATCH_PENDING"
    safe_execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert safe_execution["effect_status"] == "DISPATCHED"
    assert (await store.get_activity(prepared["activity_id"])).status is ActivityStatus.RUNNING

    replacement = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="replacement-worker", lease_ms=30_000, now_ms=clock.now_ms()
    )
    assert replacement is not None
    replacement_parent = await store.mark_activity_running(
        replacement.activity.activity_id,
        worker_id="replacement-worker",
        fencing_token=replacement.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    calls = 0

    async def recoverable_read(_arguments, _context):
        nonlocal calls
        calls += 1
        return {"value": "recovered"}

    broker.register(
        ToolManifest(
            name="recoverable_read",
            release_digest="v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        recoverable_read,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=replacement_parent.activity_id,
        fencing_token=replacement_parent.fencing_token,
        logical_key=logical_key,
        tool_name="recoverable_read",
        arguments={"query": "x"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert calls == 1

    # A new run with an UNKNOWN effect must stop at the explicit reconciliation
    # boundary rather than receiving the same automatic lease recovery.
    store2, clock2, run2, parent2, _broker2 = await _running_parent(
        tmp_path / "unknown"
    )
    unknown = await store2.prepare_tool_execution(
        run_id=run2.envelope.run_id,
        parent_activity_id=parent2.activity_id,
        fencing_token=parent2.fencing_token,
        logical_key="lease:unknown:0",
        tool_name="unknown_effect",
        release_digest="v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json({}),
        request={},
        now_ms=clock2.now_ms(),
    )
    await store2.mark_tool_dispatched(
        tool_execution_id=unknown["tool_execution_id"],
        parent_activity_id=parent2.activity_id,
        fencing_token=parent2.fencing_token,
        now_ms=clock2.now_ms(),
    )
    clock2.value += 30_001
    assert await store2.recover_expired(now_ms=clock2.now_ms()) == 1
    assert (await store2.get_run(run2.envelope.run_id)).status.value == "WAITING_INPUT"
    assert (await store2.get_tool_execution(unknown["tool_execution_id"]))[
        "effect_status"
    ] == "MANUAL_REQUIRED"
    assert (await store2.get_activity(unknown["activity_id"])).status is ActivityStatus.MANUAL
    assert await store2.claim_next(
        release_map=await store2.active_releases(),
        worker_id="must-not-claim", lease_ms=30_000, now_ms=clock2.now_ms()
    ) is None

    # Stable-key IDEMPOTENT_EFFECT keeps the same guarded recovery allowance;
    # the recovery helper must not indiscriminately downgrade it to MANUAL.
    store3, clock3, run3, parent3, _broker3 = await _running_parent(
        tmp_path / "idempotent"
    )
    idempotent = await store3.prepare_tool_execution(
        run_id=run3.envelope.run_id,
        parent_activity_id=parent3.activity_id,
        fencing_token=parent3.fencing_token,
        logical_key="lease:idempotent:0",
        tool_name="idempotent_effect",
        release_digest="v1",
        effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
        request_digest=sha256_json({"value": 1}),
        request={"value": 1},
        now_ms=clock3.now_ms(),
    )
    assert idempotent["idempotency_key"]
    await store3.mark_tool_dispatched(
        tool_execution_id=idempotent["tool_execution_id"],
        parent_activity_id=parent3.activity_id,
        fencing_token=parent3.fencing_token,
        now_ms=clock3.now_ms(),
    )
    clock3.value += 30_001
    assert await store3.recover_expired(now_ms=clock3.now_ms()) == 1
    assert (await store3.get_run(run3.envelope.run_id)).status.value == "DISPATCH_PENDING"
    assert (await store3.get_tool_execution(idempotent["tool_execution_id"]))[
        "effect_status"
    ] == "DISPATCHED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_release", "current_effect_class"),
    [
        ("new-release", ToolEffectClass.UNKNOWN_EFFECT),
        ("old-release", ToolEffectClass.READ_ONLY),
    ],
)
async def test_stable_slot_replay_rejects_release_or_effect_semantic_drift(
    tmp_path, current_release, current_effect_class,
):
    store, clock, run, parent, broker = await _running_parent(tmp_path)
    arguments = {"value": 1}
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="semantic-drift:0",
        tool_name="semantic_drift_tool",
        release_digest="old-release",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        request_digest=sha256_json(arguments),
        request=arguments,
        now_ms=clock.now_ms(),
    )
    dispatched = await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    executor_calls = 0

    async def must_not_dispatch(_arguments, _context):
        nonlocal executor_calls
        executor_calls += 1
        return {"unsafe": True}

    broker.register(
        ToolManifest(
            name="semantic_drift_tool",
            release_digest=current_release,
            effect_class=current_effect_class,
            timeout_seconds=1,
            max_attempts=2,
        ),
        must_not_dispatch,
    )
    event_count = len(await store.list_events(run.envelope.run_id, visibility=None))
    with pytest.raises(Exception) as mismatch:
        await broker.execute(
            run_id=run.envelope.run_id,
            parent_activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            logical_key="semantic-drift:0",
            tool_name="semantic_drift_tool",
            arguments=arguments,
            deadline_at_ms=run.envelope.deadline_at,
        )
    assert getattr(mismatch.value, "code", None) == "TOOL_REPLAY_MISMATCH"
    assert executor_calls == 0
    unchanged = await store.get_tool_execution(prepared["tool_execution_id"])
    assert unchanged["effect_status"] == "DISPATCHED"
    assert unchanged["release_digest"] == "old-release"
    assert unchanged["effect_class"] == "UNKNOWN_EFFECT"
    assert unchanged["revision"] == dispatched["revision"]
    assert unchanged["attempt"] == dispatched["attempt"]
    assert len(await store.list_events(run.envelope.run_id, visibility=None)) == event_count
