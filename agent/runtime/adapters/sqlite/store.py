"""Explicit-SQL implementation of the Runtime Store port.

Every public mutation opens a short ``BEGIN IMMEDIATE`` transaction.  This
module never calls a model, tool, filesystem adapter or network service while a
transaction is open.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from typing import Any

import aiosqlite
from pydantic import ValidationError

from agent.runtime.adapters.sqlite.database import RuntimeDatabase
from agent.runtime.domain.errors import RuntimeFault, conflict, not_found, unavailable
from agent.runtime.domain.models import (
    SCHEMA_VERSION,
    ACTIVITY_TRANSITIONS,
    ActivityRecord,
    ActivityStatus,
    ActivityType,
    CanonicalEvent,
    CheckpointRecord,
    EventType,
    ReleaseManifest,
    RUN_TRANSITIONS,
    RunRecord,
    RunStatus,
    RuntimeEnvelope,
    Sensitivity,
    TERMINAL_RUN_STATUSES,
    ToolReconciliationAction,
    ToolReconciliationPayload,
    ToolResultEnvelope,
    Visibility,
    WorkingState,
    canonical_json,
    new_id,
    stable_id,
)
from agent.runtime.ports.store import (
    AdmissionCommand,
    AdmissionResult,
    Claim,
    EventDraft,
    SignalResult,
    ToolExecutionPreparation,
)
from agent.runtime.ports.tool import (
    RECONCILIATION_MARKER_KIND,
    ToolReconciliationMarker,
)


# These event types are projections of Store-owned transactions.  Engine/Event
# sinks may submit semantic drafts, but they must never forge admission,
# state-machine, checkpoint, command or final/terminal authority.
_STORE_OWNED_EVENT_TYPES = frozenset({
    EventType.USER_MESSAGE_COMMITTED,
    EventType.RUN_STATUS_CHANGED,
    EventType.ACTIVITY_STATUS_CHANGED,
    EventType.CHECKPOINT_COMMITTED,
    EventType.SIGNAL_RECORDED,
    EventType.CANCEL_REQUESTED,
    EventType.ASSISTANT_MESSAGE_COMMITTED,
    EventType.CITATION_SET_COMMITTED,
    EventType.RUN_TERMINATED,
})

_REPLAY_SAFE_EFFECT_CLASSES = frozenset({"READ_ONLY", "IDEMPOTENT_EFFECT"})


def _effect_row_is_replay_safe(row: Any) -> bool:
    effect_class = str(row["effect_class"])
    return (
        effect_class == "READ_ONLY"
        or (
            effect_class == "IDEMPOTENT_EFFECT"
            and bool(row["idempotency_key"])
        )
    )


def _normalize_settled_tool_result(
    prior: Any,
    *,
    effect_status: str,
    result: dict[str, Any] | None,
    result_ref: str | None,
    external_object_id: str | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Keep durable Tool correlation monotonic and public-envelope-valid."""
    prior_result = _loads(prior["result_json"], None)
    if result is None and effect_status in {
        "UNKNOWN", "RECONCILING", "MANUAL_REQUIRED",
    } and isinstance(prior_result, dict):
        result = dict(prior_result)
    elif result is not None:
        result = dict(result)

    nested_ref = result.get("result_ref") if isinstance(result, dict) else None
    if result_ref is not None and nested_ref is not None and result_ref != nested_ref:
        raise conflict(
            "TOOL_RESULT_LEDGER_MISMATCH",
            "ToolResult result_ref disagrees with the settlement column",
            tool_execution_id=prior["tool_execution_id"],
        )
    effective_ref = result_ref or nested_ref or prior["result_ref"]

    nested_external = (
        result.get("external_object_id") if isinstance(result, dict) else None
    )
    if (
        external_object_id is not None
        and nested_external is not None
        and external_object_id != nested_external
    ):
        raise conflict(
            "TOOL_RESULT_LEDGER_MISMATCH",
            "ToolResult external_object_id disagrees with the settlement column",
            tool_execution_id=prior["tool_execution_id"],
        )
    new_external = external_object_id or nested_external
    if (
        prior["external_object_id"] is not None
        and new_external is not None
        and prior["external_object_id"] != new_external
    ):
        raise conflict(
            "TOOL_EXTERNAL_OBJECT_MISMATCH",
            "stable ToolExecution cannot change its external object identity",
            tool_execution_id=prior["tool_execution_id"],
        )
    effective_external = new_external or prior["external_object_id"]

    if result is not None:
        result["result_ref"] = effective_ref
        result["external_object_id"] = effective_external
        public = {
            key: result[key]
            for key in ToolResultEnvelope.model_fields
            if key in result
        }
        normalized = ToolResultEnvelope.model_validate(public).model_dump(mode="json")
        # Citation-only ledger enrichment remains internal, but the public
        # projection is normalized back into the same object.
        result.update(normalized)
    required_statuses = {
        "COMMITTED": {"SUCCESS", "NO_OUTPUT", "INTERRUPT"},
        "FAILED": {"FAILURE"},
        "UNKNOWN": {"UNKNOWN"},
        "MANUAL_REQUIRED": {"UNKNOWN"},
        "RECONCILING": {"UNKNOWN"},
    }
    allowed_result_statuses = required_statuses.get(effect_status)
    if (
        allowed_result_statuses is not None
        and (
            result is None
            or result.get("status") not in allowed_result_statuses
        )
    ):
        raise conflict(
            "TOOL_RESULT_EFFECT_MISMATCH",
            "ToolResultEnvelope status contradicts the ToolEffect settlement",
            tool_execution_id=prior["tool_execution_id"],
            effect_status=effect_status,
            result_status=(result or {}).get("status"),
        )
    return result, effective_ref, effective_external

def _reconciliation_marker(
    *, tool_execution_id: str, signal_id: str, expected_effect_revision: int,
) -> dict[str, Any]:
    """Build the only Worker-dispatch marker accepted for query-only reconcile."""
    return ToolReconciliationMarker(
        tool_execution_id=tool_execution_id,
        signal_id=signal_id,
        expected_effect_revision=expected_effect_revision,
    ).model_dump(mode="json")


def _is_exact_reconciliation_marker(value: Any) -> bool:
    return ToolReconciliationMarker.parse_exact(value) is not None


def _loads(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


async def _register_artifact_metadata_in_tx(
    conn: aiosqlite.Connection,
    *,
    artifact_id: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    storage_path: str,
    created_at: int,
) -> dict[str, Any]:
    """Insert immutable CAS metadata or return the identical durable row.

    Artifact identity is content-only, while this v1 Store keeps one canonical
    media type per digest.  Silently accepting a second, contradictory type
    would make the upload response disagree with the metadata later consumed by
    an Engine Adapter (most dangerously, image vs text).  Fail closed instead.
    """
    if artifact_id != sha256:
        raise conflict(
            "ARTIFACT_ID_COLLISION",
            "artifact_id must equal its SHA-256 content digest",
        )
    prior = await (await conn.execute(
        "SELECT * FROM artifact_metadata WHERE artifact_id=?", (artifact_id,),
    )).fetchone()
    if prior is not None:
        if (prior["sha256"], int(prior["size_bytes"])) != (sha256, size_bytes):
            raise conflict(
                "ARTIFACT_ID_COLLISION",
                "artifact metadata conflicts with the existing digest/size",
            )
        if (prior["media_type"], prior["storage_path"]) != (media_type, storage_path):
            raise conflict(
                "ARTIFACT_METADATA_CONFLICT",
                "artifact content already has different canonical metadata",
                durable_media_type=prior["media_type"],
                requested_media_type=media_type,
            )
        return dict(prior)

    await conn.execute(
        """INSERT INTO artifact_metadata
           (artifact_id,sha256,size_bytes,media_type,storage_path,created_at,verified_at)
           VALUES (?,?,?,?,?,?,NULL)""",
        (artifact_id, sha256, size_bytes, media_type, storage_path, created_at),
    )
    inserted = await (await conn.execute(
        "SELECT * FROM artifact_metadata WHERE artifact_id=?", (artifact_id,),
    )).fetchone()
    assert inserted is not None
    return dict(inserted)


def _run_from_row(row: aiosqlite.Row) -> RunRecord:
    envelope = RuntimeEnvelope(
        schema_version=row["schema_version"],
        request_id=row["request_id"],
        client_request_id=row["client_request_id"],
        idempotency_key=row["idempotency_key"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        run_id=row["run_id"],
        principal_id=row["principal_id"],
        agent_id=row["agent_id"],
        engine=row["engine"],
        deadline_at=row["deadline_at"],
        cancel_token_id=row["cancel_token_id"],
        release_fingerprint=row["release_fingerprint"],
        input_event_id=row["input_event_id"],
        attachment_refs=tuple(_loads(row["attachment_refs_json"], [])),
        created_at=row["created_at"],
    )
    return RunRecord(
        envelope=envelope,
        status=RunStatus(row["state"]),
        trace_id=row["trace_id"],
        revision=row["revision"],
        next_seq=row["next_seq"],
        current_activity_id=row["current_activity_id"],
        terminal_status=RunStatus(row["terminal_status"]) if row["terminal_status"] else None,
        terminal_payload=_loads(row["terminal_payload_json"]),
        input_text=row["input_text"],
        pending_input=_loads(row["pending_input_json"]),
        updated_at=row["updated_at"],
    )


def _activity_from_row(row: aiosqlite.Row) -> ActivityRecord:
    return ActivityRecord(
        activity_id=row["activity_id"],
        run_id=row["run_id"],
        type=ActivityType(row["type"]),
        logical_key=row["logical_key"],
        status=ActivityStatus(row["state"]),
        attempt=row["attempt"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        fencing_token=row["fencing_token"],
        revision=row["revision"],
        result=_loads(row["result_json"]),
        error=_loads(row["error_json"]),
        resume_payload=_loads(row["resume_payload_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: aiosqlite.Row) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=row["event_id"],
        schema_version=row["schema_version"],
        run_id=row["run_id"],
        turn_id=row["turn_id"],
        activity_id=row["activity_id"],
        tool_execution_id=row["tool_execution_id"],
        seq=row["seq"],
        event_type=EventType(row["event_type"]),
        producer=row["producer"],
        payload=_loads(row["payload_json"]),
        payload_ref=row["payload_ref"],
        visibility=Visibility(row["visibility"]),
        sensitivity=Sensitivity(row["sensitivity"]),
        occurred_at=row["occurred_at"],
        terminal_status=RunStatus(row["terminal_status"]) if row["terminal_status"] else None,
        release_fingerprint=row["release_fingerprint"],
    )


class SqliteRuntimeStore:
    def __init__(self, database: RuntimeDatabase) -> None:
        self.db = database

    async def initialize(self) -> None:
        await self.db.migrate()

    async def register_release(self, manifest: ReleaseManifest, *, activate: bool = True) -> str:
        return (await self.register_releases((manifest,), activate=activate))[manifest.engine]

    async def register_releases(
        self,
        manifests: Sequence[ReleaseManifest],
        *,
        activate: bool = True,
    ) -> dict[str, str]:
        if not manifests:
            return {}
        if len({manifest.engine for manifest in manifests}) != len(manifests):
            raise ValueError("release batch contains duplicate engine names")
        import time

        now = int(time.time() * 1000)
        fingerprints = {
            manifest.engine: manifest.fingerprint() for manifest in manifests
        }
        async with self.db.transaction() as conn:
            for manifest in manifests:
                fingerprint = fingerprints[manifest.engine]
                payload = canonical_json(manifest.model_dump(mode="json"))
                existing = await (await conn.execute(
                    "SELECT manifest_json FROM release_manifests WHERE release_fingerprint=?",
                    (fingerprint,),
                )).fetchone()
                if existing is not None and existing["manifest_json"] != payload:
                    raise conflict(
                        "RELEASE_FINGERPRINT_COLLISION", "release fingerprint collision"
                    )
                await conn.execute(
                    """INSERT OR IGNORE INTO release_manifests
                       (release_fingerprint,engine,manifest_json,schema_version,created_at)
                       VALUES (?,?,?,?,?)""",
                    (fingerprint, manifest.engine, payload, manifest.schema_version, now),
                )
            if activate:
                for manifest in manifests:
                    await conn.execute(
                        """INSERT INTO active_releases(engine,release_fingerprint,activated_at)
                           VALUES (?,?,?) ON CONFLICT(engine) DO UPDATE SET
                           release_fingerprint=excluded.release_fingerprint,
                           activated_at=excluded.activated_at""",
                        (manifest.engine, fingerprints[manifest.engine], now),
                    )
        return fingerprints

    async def active_releases(self) -> dict[str, str]:
        async with self.db.read() as conn:
            rows = await (await conn.execute(
                "SELECT engine,release_fingerprint FROM active_releases"
            )).fetchall()
        return {row["engine"]: row["release_fingerprint"] for row in rows}

    async def admit(self, command: AdmissionCommand) -> AdmissionResult:
        try:
            async with self.db.transaction() as conn:
                prior = await (await conn.execute(
                    """SELECT request_digest,run_id FROM run_requests
                       WHERE principal_id=? AND agent_id=? AND idempotency_key=?""",
                    (command.principal_id, command.agent_id, command.idempotency_key),
                )).fetchone()
                if prior is not None:
                    if prior["request_digest"] != command.request_digest:
                        raise conflict(
                            "IDEMPOTENCY_KEY_REUSE",
                            "the idempotency key was already used with a different request",
                            run_id=prior["run_id"],
                        )
                    row = await self._require_run_row(conn, prior["run_id"])
                    return AdmissionResult(run=_run_from_row(row), reused=True)

                release = await (await conn.execute(
                    "SELECT release_fingerprint FROM active_releases WHERE engine=?",
                    (command.engine,),
                )).fetchone()
                if release is None:
                    raise unavailable(
                        "NO_ACTIVE_RELEASE", f"no active release is registered for {command.engine}",
                    )

                if command.attachment_refs:
                    placeholders = ",".join("?" for _ in command.attachment_refs)
                    rows = await (await conn.execute(
                        f"SELECT artifact_id FROM artifact_metadata WHERE artifact_id IN ({placeholders})",
                        command.attachment_refs,
                    )).fetchall()
                    found = {row["artifact_id"] for row in rows}
                    missing = [item for item in command.attachment_refs if item not in found]
                    if missing:
                        raise RuntimeFault(
                            "ARTIFACT_NOT_FOUND", "one or more attachment_refs do not exist", 400,
                            {"artifact_ids": missing},
                        )

                conversation_id = command.conversation_id or command.generated_conversation_id
                conversation = await (await conn.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,),
                )).fetchone()
                if conversation is None:
                    if command.conversation_id is not None:
                        raise not_found("conversation", conversation_id)
                    await conn.execute(
                        """INSERT INTO conversations
                           (conversation_id,principal_id,agent_id,next_turn_seq,revision,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (conversation_id, command.principal_id, command.agent_id, 2, 1,
                         command.created_at, command.created_at),
                    )
                    turn_seq = 1
                else:
                    if (conversation["principal_id"], conversation["agent_id"]) != (
                        command.principal_id, command.agent_id,
                    ):
                        raise conflict(
                            "CONVERSATION_OWNERSHIP_MISMATCH",
                            "conversation belongs to another principal or agent",
                        )
                    turn_seq = int(conversation["next_turn_seq"])
                    await conn.execute(
                        """UPDATE conversations SET next_turn_seq=next_turn_seq+1,
                           revision=revision+1,updated_at=? WHERE conversation_id=?""",
                        (command.created_at, conversation_id),
                    )

                fingerprint = release["release_fingerprint"]
                await conn.execute(
                    """INSERT INTO runs (
                      run_id,schema_version,request_id,client_request_id,idempotency_key,
                      conversation_id,turn_seq,turn_id,principal_id,agent_id,engine,deadline_at,
                      cancel_token_id,release_fingerprint,input_event_id,attachment_refs_json,
                      input_text,state,revision,next_seq,current_activity_id,terminal_status,
                      terminal_payload_json,pending_input_json,created_at,updated_at,trace_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        command.run_id, SCHEMA_VERSION, command.request_id,
                        command.client_request_id, command.idempotency_key, conversation_id,
                        turn_seq, command.turn_id, command.principal_id, command.agent_id, command.engine,
                        command.deadline_at, command.cancel_token_id, fingerprint,
                        command.input_event_id, canonical_json(list(command.attachment_refs)),
                        command.input_text, RunStatus.DISPATCH_PENDING, 0, 1, None, None,
                        None, None, command.created_at, command.created_at, command.trace_id,
                    ),
                )
                activity_id = stable_id("act", command.run_id, "engine:0")
                await conn.execute(
                    """INSERT INTO activities (
                      activity_id,run_id,type,logical_key,state,attempt,available_at,lease_owner,
                      lease_expires_at,fencing_token,revision,result_json,error_json,
                      pending_input_json,resume_payload_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        activity_id, command.run_id, ActivityType.ENGINE_RUN, "engine:0",
                        ActivityStatus.PENDING, 0, command.created_at, None, None, 0, 0,
                        None, None, None, None, command.created_at, command.created_at,
                    ),
                )
                await conn.execute(
                    "UPDATE runs SET current_activity_id=? WHERE run_id=?",
                    (activity_id, command.run_id),
                )
                # Input attachments are provenance facts of the admission
                # boundary, not merely fields in the frozen envelope.  Link
                # each distinct CAS object to the Run, its first Activity and
                # the committed user-message event in this same transaction.
                # The envelope still preserves the caller's original order
                # (and any repetition) for multimodal prompt compilation.
                for artifact_id in dict.fromkeys(command.attachment_refs):
                    await conn.execute(
                        """INSERT INTO artifact_links (
                           link_id,artifact_id,run_id,activity_id,event_id,relation,
                           sensitivity,retention_until,created_at
                           ) VALUES (?,?,?,?,?,'INPUT_ATTACHMENT','PRIVATE',NULL,?)""",
                        (
                            stable_id("alink", command.run_id, f"input:{artifact_id}"),
                            artifact_id,
                            command.run_id,
                            activity_id,
                            command.input_event_id,
                            command.created_at,
                        ),
                    )
                await conn.execute(
                    """INSERT INTO run_requests
                       (principal_id,agent_id,idempotency_key,request_digest,run_id,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (command.principal_id, command.agent_id, command.idempotency_key,
                     command.request_digest, command.run_id, command.created_at),
                )
                await self._append_in_tx(conn, command.run_id, [
                    EventDraft(
                        EventType.USER_MESSAGE_COMMITTED,
                        {"text": command.input_text, "attachment_refs": list(command.attachment_refs)},
                        event_id=command.input_event_id,
                    ),
                    EventDraft(EventType.RUN_STATUS_CHANGED, {"from": None, "to": RunStatus.ACCEPTED}),
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": RunStatus.ACCEPTED, "to": RunStatus.DISPATCH_PENDING},
                    ),
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": None, "to": ActivityStatus.PENDING, "type": ActivityType.ENGINE_RUN},
                        activity_id=activity_id,
                    ),
                ])
                row = await self._require_run_row(conn, command.run_id)
                return AdmissionResult(run=_run_from_row(row), reused=False)
        except sqlite3.IntegrityError as exc:
            if "uq_active_run_per_conversation" in str(exc) or "runs.conversation_id" in str(exc):
                raise conflict(
                    "CONVERSATION_BUSY",
                    "another non-terminal run already owns this conversation",
                    conversation_id=command.conversation_id or command.generated_conversation_id,
                ) from exc
            raise

    async def get_run(self, run_id: str) -> RunRecord:
        async with self.db.read() as conn:
            return _run_from_row(await self._require_run_row(conn, run_id))

    async def get_activity(self, activity_id: str) -> ActivityRecord:
        async with self.db.read() as conn:
            row = await (await conn.execute(
                "SELECT * FROM activities WHERE activity_id=?", (activity_id,),
            )).fetchone()
            if row is None:
                raise not_found("activity", activity_id)
            return _activity_from_row(row)

    async def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        visibility: Visibility | None = Visibility.PUBLIC,
        limit: int = 500,
    ) -> list[CanonicalEvent]:
        async with self.db.read() as conn:
            await self._require_run_row(conn, run_id)
            sql = "SELECT * FROM run_events WHERE run_id=? AND seq>?"
            params: list[Any] = [run_id, after_seq]
            if visibility is not None:
                sql += " AND visibility=?"
                params.append(visibility)
            sql += " ORDER BY seq LIMIT ?"
            params.append(max(1, min(limit, 5000)))
            rows = await (await conn.execute(sql, params)).fetchall()
        return [_event_from_row(row) for row in rows]

    async def append_events(
        self,
        run_id: str,
        drafts: Sequence[EventDraft],
        *,
        activity_id: str | None = None,
        fencing_token: int | None = None,
        now_ms: int | None = None,
    ) -> list[CanonicalEvent]:
        if not drafts:
            return []
        forbidden = sorted({
            str(draft.event_type)
            for draft in drafts
            if draft.event_type in _STORE_OWNED_EVENT_TYPES
        })
        if forbidden:
            raise conflict(
                "EVENT_AUTHORITY_VIOLATION",
                "Store-owned canonical events cannot be appended by an event producer",
                event_types=forbidden,
            )
        async with self.db.transaction() as conn:
            run = await self._require_run_row(conn, run_id)
            if run["terminal_status"] is not None and any(
                draft.visibility is Visibility.PUBLIC for draft in drafts
            ):
                raise conflict(
                    "RUN_ALREADY_TERMINAL",
                    "terminal Run cannot accept a new public event",
                )
            if activity_id is not None:
                await self._assert_fencing(
                    conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
                )
            return await self._append_in_tx(conn, run_id, drafts)

    async def _append_in_tx(
        self, conn: aiosqlite.Connection, run_id: str, drafts: Sequence[EventDraft],
    ) -> list[CanonicalEvent]:
        row = await self._require_run_row(conn, run_id)
        seq = int(row["next_seq"])
        events: list[CanonicalEvent] = []
        for draft in drafts:
            event = CanonicalEvent(
                event_id=draft.event_id or new_id("evt"),
                run_id=run_id,
                turn_id=row["turn_id"],
                activity_id=draft.activity_id,
                tool_execution_id=draft.tool_execution_id,
                seq=seq,
                event_type=draft.event_type,
                producer=draft.producer,
                payload=draft.payload,
                payload_ref=draft.payload_ref,
                visibility=draft.visibility,
                sensitivity=draft.sensitivity,
                occurred_at=draft.occurred_at or row["updated_at"],
                terminal_status=draft.terminal_status,
                release_fingerprint=row["release_fingerprint"],
            )
            await conn.execute(
                """INSERT INTO run_events (
                  event_id,schema_version,run_id,turn_id,activity_id,tool_execution_id,seq,
                  event_type,producer,payload_json,payload_ref,visibility,sensitivity,
                  occurred_at,terminal_status,release_fingerprint
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, event.schema_version, event.run_id, event.turn_id,
                    event.activity_id, event.tool_execution_id, event.seq, event.event_type,
                    event.producer,
                    canonical_json(event.payload) if event.payload is not None else None,
                    event.payload_ref, event.visibility, event.sensitivity, event.occurred_at,
                    event.terminal_status, event.release_fingerprint,
                ),
            )
            events.append(event)
            seq += 1
        await conn.execute("UPDATE runs SET next_seq=? WHERE run_id=?", (seq, run_id))
        return events

    async def _require_run_row(
        self, conn: aiosqlite.Connection, run_id: str,
    ) -> aiosqlite.Row:
        row = await (await conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))).fetchone()
        if row is None:
            raise not_found("run", run_id)
        return row

    async def _assert_fencing(
        self,
        conn: aiosqlite.Connection,
        activity_id: str,
        fencing_token: int | None,
        *,
        run_id: str | None = None,
        now_ms: int | None = None,
    ) -> aiosqlite.Row:
        row = await (await conn.execute(
            "SELECT * FROM activities WHERE activity_id=?", (activity_id,),
        )).fetchone()
        if row is None:
            raise not_found("activity", activity_id)
        if run_id is not None and row["run_id"] != run_id:
            raise conflict(
                "ACTIVITY_RUN_MISMATCH",
                "activity fencing token belongs to a different Run",
                activity_id=activity_id,
                expected_run_id=run_id,
                actual_run_id=row["run_id"],
            )
        if fencing_token is None or row["fencing_token"] != fencing_token or row["state"] not in (
            ActivityStatus.CLAIMED, ActivityStatus.RUNNING,
        ):
            raise conflict(
                "STALE_FENCING_TOKEN", "worker result was rejected by the activity fence",
                activity_id=activity_id, expected=row["fencing_token"], actual=fencing_token,
            )
        if (
            now_ms is not None
            and (row["lease_expires_at"] is None or row["lease_expires_at"] < now_ms)
        ):
            raise conflict(
                "STALE_FENCING_TOKEN",
                "worker result was rejected because its activity lease expired",
                activity_id=activity_id,
                fencing_token=fencing_token,
                lease_expires_at=row["lease_expires_at"],
                now_ms=now_ms,
            )
        if row["state"] != ActivityStatus.RUNNING:
            raise conflict(
                "ACTIVITY_LEASE_REQUIRED",
                "activity must complete CLAIMED to RUNNING before execution writes",
                activity_id=activity_id,
                activity_state=row["state"],
            )
        return row

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_ms: int,
        now_ms: int,
        engines: Sequence[str] = ("plan_execute", "agent_loop", "native_loop"),
    ) -> Claim | None:
        if not engines:
            return None
        placeholders = ",".join("?" for _ in engines)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE activities SET
                       state='CLAIMED',attempt=attempt+1,lease_owner=?,lease_expires_at=?,
                       fencing_token=fencing_token+1,revision=revision+1,updated_at=?
                     WHERE activity_id=(
                       SELECT a.activity_id FROM activities a JOIN runs r ON r.run_id=a.run_id
                       WHERE a.type='ENGINE_RUN' AND a.state='PENDING' AND a.available_at<=?
                         AND (
                           r.state='DISPATCH_PENDING'
                           OR (
                             r.state='CANCEL_REQUESTED'
                             AND json_extract(a.resume_payload_json,'$.kind')=?
                             AND json_type(a.resume_payload_json,'$.tool_execution_id')='text'
                             AND json_type(a.resume_payload_json,'$.signal_id')='text'
                             AND json_type(a.resume_payload_json,'$.expected_effect_revision')='integer'
                             AND (SELECT COUNT(*) FROM json_each(a.resume_payload_json))=4
                           )
                         )
                         AND r.deadline_at>?
                         AND r.engine IN ({placeholders})
                       ORDER BY a.available_at,a.created_at,a.activity_id LIMIT 1
                     ) AND state='PENDING'
                     RETURNING *""",
                (
                    worker_id, now_ms + lease_ms, now_ms, now_ms,
                    RECONCILIATION_MARKER_KIND, now_ms, *engines,
                ),
            )
            activity_row = await cursor.fetchone()
            if activity_row is None:
                return None
            run_row = await self._require_run_row(conn, activity_row["run_id"])
            cancel_reconcile_claim = RunStatus(run_row["state"]) is RunStatus.CANCEL_REQUESTED
            drafts = [
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": ActivityStatus.PENDING, "to": ActivityStatus.CLAIMED,
                     "attempt": activity_row["attempt"], "fencing_token": activity_row["fencing_token"]},
                    activity_id=activity_row["activity_id"], occurred_at=now_ms,
                ),
            ]
            if cancel_reconcile_claim:
                marker = _loads(activity_row["resume_payload_json"], {})
                if not _is_exact_reconciliation_marker(marker):
                    raise conflict(
                        "TOOL_RECONCILIATION_MISMATCH",
                        "cancel-owned Activity has no exact reconcile-only marker",
                    )
                updated = await conn.execute(
                    """UPDATE runs SET revision=revision+1,current_activity_id=?,updated_at=?
                       WHERE run_id=? AND state='CANCEL_REQUESTED'""",
                    (activity_row["activity_id"], now_ms, activity_row["run_id"]),
                )
            else:
                updated = await conn.execute(
                    """UPDATE runs SET state='RUNNING',revision=revision+1,
                       current_activity_id=?,updated_at=?
                       WHERE run_id=? AND state='DISPATCH_PENDING'""",
                    (activity_row["activity_id"], now_ms, activity_row["run_id"]),
                )
                drafts.append(EventDraft(
                    EventType.RUN_STATUS_CHANGED,
                    {"from": RunStatus.DISPATCH_PENDING, "to": RunStatus.RUNNING},
                    activity_id=activity_row["activity_id"], occurred_at=now_ms,
                ))
            if updated.rowcount != 1:
                raise conflict(
                    "ACTIVITY_CLAIM_RUN_CAS_CONFLICT",
                    "claimed Activity lost its matching Run-state CAS",
                )
            await self._append_in_tx(conn, activity_row["run_id"], drafts)
            run_row = await self._require_run_row(conn, activity_row["run_id"])
            return Claim(run=_run_from_row(run_row), activity=_activity_from_row(activity_row))

    async def mark_activity_running(
        self, activity_id: str, *, worker_id: str, fencing_token: int, now_ms: int,
    ) -> ActivityRecord:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE activities SET state='RUNNING',revision=revision+1,updated_at=?
                   WHERE activity_id=? AND state='CLAIMED' AND lease_owner=? AND fencing_token=?
                     AND lease_expires_at>=?
                   RETURNING *""",
                (now_ms, activity_id, worker_id, fencing_token, now_ms),
            )
            row = await cursor.fetchone()
            if row is None:
                raise conflict("STALE_FENCING_TOKEN", "activity claim is no longer valid")
            await self._append_in_tx(conn, row["run_id"], [EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {"from": ActivityStatus.CLAIMED, "to": ActivityStatus.RUNNING,
                 "attempt": row["attempt"]},
                activity_id=activity_id, occurred_at=now_ms,
            )])
            return _activity_from_row(row)

    async def renew_lease(
        self,
        activity_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        lease_expires_at: int,
        now_ms: int,
    ) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE activities SET lease_expires_at=?,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND lease_owner=? AND fencing_token=?
                     AND state IN ('CLAIMED','RUNNING') AND lease_expires_at>=?""",
                (lease_expires_at, now_ms, activity_id, worker_id, fencing_token, now_ms),
            )
            return cursor.rowcount == 1

    async def save_checkpoint(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        expected_revision: int,
        working_state: WorkingState,
        engine_state: dict[str, Any] | None = None,
        engine_state_ref: str | None = None,
        now_ms: int,
    ) -> CheckpointRecord:
        if engine_state is not None and engine_state_ref is not None:
            raise RuntimeFault("CHECKPOINT_STATE_AMBIGUOUS", "inline state and state ref are exclusive")
        async with self.db.transaction() as conn:
            await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            current = await (await conn.execute(
                """SELECT revision,working_state_json FROM checkpoints
                   WHERE run_id=? ORDER BY revision DESC LIMIT 1""",
                (run_id,),
            )).fetchone()
            actual = int(current["revision"] if current is not None else 0)
            if actual != expected_revision:
                raise conflict(
                    "CHECKPOINT_REVISION_CONFLICT", "checkpoint CAS revision did not match",
                    expected=expected_revision, actual=actual,
                )
            if working_state.release_fingerprint != run["release_fingerprint"]:
                raise conflict(
                    "CHECKPOINT_RELEASE_MISMATCH", "checkpoint release does not match the Run",
                )
            revision = actual + 1
            checkpoint_id = stable_id("ckpt", run_id, f"checkpoint:{revision}")
            await conn.execute(
                """INSERT INTO checkpoints (
                   checkpoint_id,run_id,activity_id,revision,working_state_json,
                   engine_state_json,engine_state_ref,release_fingerprint,schema_version,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    checkpoint_id, run_id, activity_id, revision,
                    canonical_json(working_state.model_dump(mode="json")),
                    canonical_json(engine_state) if engine_state is not None else None,
                    engine_state_ref, run["release_fingerprint"], SCHEMA_VERSION, now_ms,
                ),
            )
            drafts = [EventDraft(
                EventType.CHECKPOINT_COMMITTED,
                {"checkpoint_id": checkpoint_id, "revision": revision},
                activity_id=activity_id, occurred_at=now_ms,
            )]
            prior_plan = (
                WorkingState.model_validate_json(current["working_state_json"]).model_plan
                if current is not None else []
            )
            if prior_plan != working_state.model_plan:
                if working_state.model_plan:
                    total = len(working_state.model_plan)
                    for ordinal, raw in enumerate(working_state.model_plan, start=1):
                        item = dict(raw)
                        item.setdefault("step", ordinal)
                        item.setdefault("total", total)
                        drafts.append(EventDraft(
                            EventType.MODEL_PLAN_UPDATED,
                            item,
                            activity_id=activity_id,
                            producer="runtime-checkpoint",
                            occurred_at=now_ms,
                        ))
                else:
                    drafts.append(EventDraft(
                        EventType.MODEL_PLAN_UPDATED,
                        {"steps": [], "status": "cleared"},
                        activity_id=activity_id,
                        producer="runtime-checkpoint",
                        occurred_at=now_ms,
                    ))
            await self._append_in_tx(conn, run_id, drafts)
            return CheckpointRecord(
                checkpoint_id=checkpoint_id, run_id=run_id, activity_id=activity_id,
                revision=revision, working_state=working_state, engine_state=engine_state,
                engine_state_ref=engine_state_ref, release_fingerprint=run["release_fingerprint"],
                created_at=now_ms,
            )

    async def publish_checkpoint_upgrade(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        source_checkpoint_id: str,
        expected_revision: int,
        from_release_fingerprint: str,
        from_schema_version: str,
        to_release_fingerprint: str,
        to_schema_version: str,
        working_state: WorkingState,
        engine_state: dict[str, Any] | None = None,
        engine_state_ref: str | None = None,
        now_ms: int,
    ) -> CheckpointRecord:
        """Publish a pure checkpoint conversion behind fence and revision CAS.

        The conversion itself happens before this method is called.  This short
        transaction only revalidates the exact source checkpoint, validates the
        registered target release, appends the converted checkpoint, switches
        the Run's effective release and commits its canonical audit event.
        """
        if engine_state is not None and engine_state_ref is not None:
            raise RuntimeFault(
                "CHECKPOINT_STATE_AMBIGUOUS", "inline state and state ref are exclusive",
            )
        if from_release_fingerprint == to_release_fingerprint:
            raise conflict(
                "CHECKPOINT_UPGRADE_NOOP", "checkpoint upgrade must change the release",
            )
        if working_state.release_fingerprint != to_release_fingerprint:
            raise conflict(
                "CHECKPOINT_RELEASE_MISMATCH",
                "upgraded WorkingState does not match the target release",
            )

        async with self.db.transaction() as conn:
            await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            if run["terminal_status"] is not None or run["state"] != RunStatus.RUNNING:
                raise conflict(
                    "RUN_NOT_EXECUTABLE",
                    "checkpoint upgrade requires a non-terminal RUNNING Run",
                    state=run["state"],
                )
            if run["release_fingerprint"] != from_release_fingerprint:
                raise conflict(
                    "CHECKPOINT_UPGRADE_SOURCE_MISMATCH",
                    "Run effective release no longer matches the upgrade source",
                    expected=from_release_fingerprint,
                    actual=run["release_fingerprint"],
                )

            source = await (await conn.execute(
                """SELECT * FROM checkpoints
                   WHERE run_id=? ORDER BY revision DESC LIMIT 1""",
                (run_id,),
            )).fetchone()
            actual_revision = int(source["revision"] if source is not None else 0)
            if actual_revision != expected_revision:
                raise conflict(
                    "CHECKPOINT_REVISION_CONFLICT",
                    "checkpoint upgrade CAS revision did not match",
                    expected=expected_revision,
                    actual=actual_revision,
                )
            if (
                source is None
                or source["checkpoint_id"] != source_checkpoint_id
                or source["release_fingerprint"] != from_release_fingerprint
                or source["schema_version"] != from_schema_version
            ):
                raise conflict(
                    "CHECKPOINT_UPGRADE_SOURCE_MISMATCH",
                    "latest checkpoint does not exactly match the registered upgrade source",
                )

            target = await (await conn.execute(
                """SELECT engine,schema_version FROM release_manifests
                   WHERE release_fingerprint=?""",
                (to_release_fingerprint,),
            )).fetchone()
            if target is None:
                raise conflict(
                    "CHECKPOINT_UPGRADE_TARGET_UNKNOWN",
                    "target release manifest is not registered",
                )
            if target["engine"] != run["engine"]:
                raise conflict(
                    "CHECKPOINT_UPGRADE_ENGINE_MISMATCH",
                    "target release belongs to a different engine",
                    expected=run["engine"],
                    actual=target["engine"],
                )
            if target["schema_version"] != to_schema_version:
                raise conflict(
                    "CHECKPOINT_UPGRADE_SCHEMA_MISMATCH",
                    "target release manifest does not match target checkpoint schema",
                    expected=to_schema_version,
                    actual=target["schema_version"],
                )

            revision = actual_revision + 1
            checkpoint_id = stable_id("ckpt", run_id, f"checkpoint:{revision}")
            await conn.execute(
                """INSERT INTO checkpoints (
                   checkpoint_id,run_id,activity_id,revision,working_state_json,
                   engine_state_json,engine_state_ref,release_fingerprint,schema_version,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    checkpoint_id, run_id, activity_id, revision,
                    canonical_json(working_state.model_dump(mode="json")),
                    canonical_json(engine_state) if engine_state is not None else None,
                    engine_state_ref, to_release_fingerprint, to_schema_version, now_ms,
                ),
            )
            cursor = await conn.execute(
                """UPDATE runs SET release_fingerprint=?,revision=revision+1,updated_at=?
                   WHERE run_id=? AND release_fingerprint=? AND state='RUNNING'
                     AND terminal_status IS NULL""",
                (
                    to_release_fingerprint, now_ms, run_id, from_release_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise conflict(
                    "CHECKPOINT_UPGRADE_SOURCE_MISMATCH",
                    "Run effective release changed before checkpoint upgrade publication",
                )
            await self._append_in_tx(conn, run_id, [EventDraft(
                EventType.CHECKPOINT_COMMITTED,
                {
                    "checkpoint_id": checkpoint_id,
                    "revision": revision,
                    "upgrade": {
                        "source_checkpoint_id": source_checkpoint_id,
                        "source_revision": actual_revision,
                        "from_release_fingerprint": from_release_fingerprint,
                        "from_schema_version": from_schema_version,
                        "to_release_fingerprint": to_release_fingerprint,
                        "to_schema_version": to_schema_version,
                    },
                },
                activity_id=activity_id,
                producer="runtime-release-upgrader",
                occurred_at=now_ms,
            )])
            return CheckpointRecord(
                checkpoint_id=checkpoint_id,
                run_id=run_id,
                activity_id=activity_id,
                revision=revision,
                working_state=working_state,
                engine_state=engine_state,
                engine_state_ref=engine_state_ref,
                release_fingerprint=to_release_fingerprint,
                schema_version=to_schema_version,
                created_at=now_ms,
            )

    async def latest_checkpoint(self, run_id: str) -> CheckpointRecord | None:
        async with self.db.read() as conn:
            await self._require_run_row(conn, run_id)
            row = await (await conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY revision DESC LIMIT 1",
                (run_id,),
            )).fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            checkpoint_id=row["checkpoint_id"], run_id=row["run_id"],
            activity_id=row["activity_id"], revision=row["revision"],
            working_state=WorkingState.model_validate_json(row["working_state_json"]),
            engine_state=_loads(row["engine_state_json"]), engine_state_ref=row["engine_state_ref"],
            release_fingerprint=row["release_fingerprint"], schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    async def compile_history(self, run_id: str) -> list[dict[str, Any]]:
        async with self.db.read() as conn:
            target = await self._require_run_row(conn, run_id)
            rows = await (await conn.execute(
                """SELECT e.event_type,e.payload_json,r.turn_seq,e.seq
                   FROM run_events e JOIN runs r ON r.run_id=e.run_id
                   WHERE r.conversation_id=? AND e.run_id<>?
                     AND e.event_type IN ('USER_MESSAGE_COMMITTED','ASSISTANT_MESSAGE_COMMITTED')
                   ORDER BY r.turn_seq,e.seq""",
                (target["conversation_id"], run_id),
            )).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if row["event_type"] == EventType.USER_MESSAGE_COMMITTED:
                history.append({"role": "user", "text": payload.get("text", ""),
                                "attachment_refs": payload.get("attachment_refs", [])})
            else:
                history.append({"role": "assistant", "text": payload.get("text", "")})
        return history

    async def finalize_success(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        assistant_text: str,
        citations: list[dict[str, Any]],
        now_ms: int,
    ) -> RunRecord:
        async with self.db.transaction() as conn:
            await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            state = RunStatus(run["state"])
            if state in TERMINAL_RUN_STATUSES:
                raise conflict("RUN_ALREADY_TERMINAL", "terminal Run cannot accept a late result")
            if state is RunStatus.CANCEL_REQUESTED:
                await self._append_in_tx(conn, run_id, [EventDraft(
                    EventType.MODEL_MESSAGE_COMMITTED,
                    {"late_result": True, "ignored": True, "preview": assistant_text[:8192]},
                    activity_id=activity_id, visibility=Visibility.INTERNAL, occurred_at=now_ms,
                )])
                unresolved = await self._unresolved_effect_ids(conn, run_id)
                if unresolved:
                    return await self._defer_cancel_for_unresolved_in_tx(
                        conn,
                        run_id=run_id,
                        activity_id=activity_id,
                        fencing_token=fencing_token,
                        unresolved=unresolved,
                        now_ms=now_ms,
                        reason="late engine completion after cancel",
                    )
                return await self._finalize_terminal_in_tx(
                    conn, run_id=run_id, activity_id=activity_id,
                    terminal_status=RunStatus.CANCELLED,
                    payload={"reason": "cancel won before engine completion"}, now_ms=now_ms,
                    prior_state=state,
                )

            # ADK prepares the complete model ToolCall batch before individual
            # callbacks run.  A framework-side validation callback may then
            # short-circuit one member of that batch without entering the
            # broker wrapper.  Such an effect is known *not* to have been
            # dispatched, so close it as a synthetic failure before committing
            # the final answer.  Treating PREPARED as UNKNOWN would incorrectly
            # claim a possible external side effect; ignoring it would leave an
            # unpaired canonical TOOL_CALL on a successful Run.
            await self._close_undispatched_prepared_tools_in_tx(
                conn, run_id=run_id, now_ms=now_ms,
                reason="engine completed without dispatching the prepared tool",
            )

            # Citations are derived only from committed knowledge-search results.
            # Request-local citation projections are deliberately non-authoritative.
            citations = await self._derive_citations_in_tx(conn, run_id, assistant_text)

            # The complete semantic assistant message, citation set and successful
            # terminal are deliberately one atomic transaction.
            await self._append_in_tx(conn, run_id, [
                EventDraft(
                    EventType.ASSISTANT_MESSAGE_COMMITTED, {"text": assistant_text},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.CITATION_SET_COMMITTED, {"citations": citations},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
            ])
            return await self._finalize_terminal_in_tx(
                conn, run_id=run_id, activity_id=activity_id,
                terminal_status=RunStatus.SUCCEEDED,
                payload={"assistant_chars": len(assistant_text), "citation_count": len(citations)},
                now_ms=now_ms, prior_state=state,
            )

    async def _derive_citations_in_tx(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        assistant_text: str,
    ) -> list[dict[str, Any]]:
        markers: list[int] = []
        seen_markers: set[int] = set()
        for raw in re.findall(r"\[(\d{1,4})\]", assistant_text):
            value = int(raw)
            if value > 0 and value not in seen_markers:
                seen_markers.add(value)
                markers.append(value)
        if not markers:
            return []

        rows = await (await conn.execute(
            """SELECT tool_execution_id,result_json,result_ref
               FROM tool_executions
               WHERE run_id=? AND tool_name='knowledge_search'
                 AND effect_status='COMMITTED' AND result_json IS NOT NULL
               ORDER BY created_at,logical_key,tool_execution_id""",
            (run_id,),
        )).fetchall()
        citations: list[dict[str, Any]] = []
        seen_evidence: set[str] = set()
        for marker in markers:
            for row in rows:
                result = _loads(row["result_json"], {})
                if not isinstance(result, dict):
                    continue
                evidence_ref = result.get("evidence_set_ref") or row["result_ref"]
                for evidence in result.get("evidence_index") or []:
                    if not isinstance(evidence, dict) or int(evidence.get("n") or 0) != marker:
                        continue
                    evidence_id = str(evidence.get("evidence_id") or "")
                    if not evidence_id or evidence_id in seen_evidence:
                        continue
                    seen_evidence.add(evidence_id)
                    citation = {
                        "citation_id": stable_id(
                            "cit", run_id, f"{marker}:{evidence_id}"
                        ),
                        "n": marker,
                        "marker": f"[{marker}]",
                        "evidence_id": evidence_id,
                        "evidence_set_ref": evidence_ref,
                        "tool_execution_id": row["tool_execution_id"],
                    }
                    for key in (
                        "title", "doc_id", "document_id", "document_version_id",
                        "index_version", "content_hash", "dataset_id", "scope", "query_id",
                        "page", "span_start", "span_end",
                    ):
                        if evidence.get(key) is not None:
                            citation[key] = evidence[key]
                    citations.append(citation)
        return citations

    async def finalize_failure(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        code: str,
        message: str,
        now_ms: int,
        terminal_status: RunStatus = RunStatus.FAILED,
    ) -> RunRecord:
        if (
            terminal_status not in TERMINAL_RUN_STATUSES
            or terminal_status is RunStatus.SUCCEEDED
        ):
            raise ValueError(
                "failure finalization requires a non-success terminal_status"
            )
        async with self.db.transaction() as conn:
            await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            state = RunStatus(run["state"])
            if state in TERMINAL_RUN_STATUSES:
                raise conflict("RUN_ALREADY_TERMINAL", "Run already has a terminal outcome")
            unresolved = await self._unresolved_effect_ids(conn, run_id)
            if (
                state is RunStatus.CANCEL_REQUESTED
                and unresolved
                and terminal_status is not RunStatus.TIMED_OUT
            ):
                return await self._defer_cancel_for_unresolved_in_tx(
                    conn,
                    run_id=run_id,
                    activity_id=activity_id,
                    fencing_token=fencing_token,
                    unresolved=unresolved,
                    now_ms=now_ms,
                    reason="engine attempt ended after cancel",
                    error={"code": code, "message": message},
                )
            if state is RunStatus.CANCEL_REQUESTED:
                if not unresolved:
                    terminal_status = RunStatus.CANCELLED
                    code = "CANCELLED"
            payload: dict[str, Any] = {"code": code, "message": message}
            if terminal_status is RunStatus.TIMED_OUT and unresolved:
                payload["unresolved_tool_execution_ids"] = unresolved
            return await self._finalize_terminal_in_tx(
                conn, run_id=run_id, activity_id=activity_id,
                terminal_status=terminal_status, payload=payload, now_ms=now_ms,
                prior_state=state,
            )

    async def _defer_cancel_for_unresolved_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        unresolved: list[str],
        now_ms: int,
        reason: str,
        error: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Keep a won cancel CAS nonterminal while effects need reconciliation."""
        # Once cancel owns the Run there is no legal path back to ordinary Tool
        # execution.  Convert even normally replay-safe uncertainty into the
        # strict operator vocabulary before publishing the pending ids.
        unresolved = await self._manualize_cancel_owned_effects_in_tx(
            conn, run_id=run_id, now_ms=now_ms,
        )
        if not unresolved:
            raise conflict(
                "TOOL_RECONCILIATION_INVARIANT",
                "cancel deferral requires at least one operator-actionable ToolEffect",
                run_id=run_id,
            )
        activity = await (await conn.execute(
            "SELECT state FROM activities WHERE activity_id=? AND run_id=?",
            (activity_id, run_id),
        )).fetchone()
        if activity is None:
            raise not_found("activity", activity_id)
        source = ActivityStatus(activity["state"])
        pending = {
            "type": "TOOL_RECONCILIATION",
            "unresolved_tool_execution_ids": unresolved,
        }
        if source is ActivityStatus.RECONCILE:
            await conn.execute(
                """UPDATE activities SET pending_input_json=?,resume_payload_json=NULL,
                   revision=revision+1,updated_at=? WHERE activity_id=? AND state='RECONCILE'""",
                (canonical_json(pending), now_ms, activity_id),
            )
            await conn.execute(
                """UPDATE runs SET pending_input_json=?,revision=revision+1,updated_at=?
                   WHERE run_id=? AND state='CANCEL_REQUESTED'""",
                (canonical_json(pending), now_ms, run_id),
            )
            return _run_from_row(await self._require_run_row(conn, run_id))
        if ActivityStatus.RECONCILE not in ACTIVITY_TRANSITIONS[source]:
            raise conflict(
                "INVALID_ACTIVITY_STATE_TRANSITION",
                "unresolved cancel cannot enter reconciliation from this Activity state",
                source=source,
                target=ActivityStatus.RECONCILE,
            )
        cursor = await conn.execute(
            """UPDATE activities SET state='RECONCILE',error_json=?,pending_input_json=?,
               resume_payload_json=NULL,lease_owner=NULL,lease_expires_at=NULL,
               revision=revision+1,updated_at=?
               WHERE activity_id=? AND run_id=? AND fencing_token=? AND state=?""",
            (
                canonical_json(error) if error is not None else None,
                canonical_json(pending),
                now_ms,
                activity_id,
                run_id,
                fencing_token,
                source,
            ),
        )
        if cursor.rowcount != 1:
            raise conflict(
                "STALE_FENCING_TOKEN",
                "cancel reconciliation lost its Activity CAS",
                activity_id=activity_id,
            )
        await conn.execute(
            """UPDATE runs SET pending_input_json=?,revision=revision+1,updated_at=?
               WHERE run_id=? AND state='CANCEL_REQUESTED'""",
            (canonical_json(pending), now_ms, run_id),
        )
        await self._append_in_tx(conn, run_id, [EventDraft(
            EventType.ACTIVITY_STATUS_CHANGED,
            {
                "from": source,
                "to": ActivityStatus.RECONCILE,
                "reason": reason,
                "unresolved_tool_execution_ids": unresolved,
            },
            activity_id=activity_id,
            occurred_at=now_ms,
        )])
        return _run_from_row(await self._require_run_row(conn, run_id))

    async def _manualize_cancel_owned_effects_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        now_ms: int,
    ) -> list[str]:
        """Make every ownerless cancel-time uncertainty operator actionable."""
        await self._recover_uncertain_effects_to_manual_in_tx(
            conn,
            run_id=run_id,
            cancel_owned=True,
            now_ms=now_ms,
            recovery_reason="CANCEL_OWNED_UNCERTAIN_EFFECT",
        )
        rows = await self._unresolved_effect_rows(conn, run_id)
        actionable = [
            str(row["tool_execution_id"])
            for row in rows
            if row["effect_status"] == "MANUAL_REQUIRED"
        ]
        invalid = [
            str(row["tool_execution_id"])
            for row in rows
            if row["effect_status"] != "MANUAL_REQUIRED"
        ]
        if invalid:
            raise conflict(
                "TOOL_RECONCILIATION_INVARIANT",
                "cancel ownership found uncertainty outside the strict manual boundary",
                unresolved_tool_execution_ids=[
                    str(row["tool_execution_id"]) for row in rows
                ],
                invalid_tool_execution_ids=invalid,
            )
        return actionable

    async def _establish_idle_cancel_reconciliation_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        activity_id: str,
        now_ms: int,
        reason: str,
    ) -> list[str]:
        """Take over an idle parent after cancel without reviving its Engine."""
        parent = await (await conn.execute(
            "SELECT * FROM activities WHERE activity_id=? AND run_id=?",
            (activity_id, run_id),
        )).fetchone()
        if parent is None:
            raise not_found("activity", activity_id)
        source = ActivityStatus(parent["state"])
        resume = _loads(parent["resume_payload_json"], {})
        if _is_exact_reconciliation_marker(resume):
            # An operator already authorized a query-only claim.  claim_next is
            # allowed to execute that exact marker while cancellation owns the
            # Run, and its settlement will manualize any remaining uncertainty.
            return []
        if source in {ActivityStatus.CLAIMED, ActivityStatus.RUNNING}:
            # An active fence owns the settlement.  Coordinator/finalize (or
            # lease recovery) will enter the strict boundary after it stops.
            return []

        actionable = await self._manualize_cancel_owned_effects_in_tx(
            conn, run_id=run_id, now_ms=now_ms,
        )
        if not actionable:
            return []
        pending = {
            "type": "TOOL_RECONCILIATION",
            "unresolved_tool_execution_ids": actionable,
        }
        target = source
        if source not in {ActivityStatus.RECONCILE, ActivityStatus.MANUAL}:
            if ActivityStatus.RECONCILE not in ACTIVITY_TRANSITIONS[source]:
                raise conflict(
                    "INVALID_ACTIVITY_STATE_TRANSITION",
                    "idle cancel takeover cannot enter reconciliation",
                    source=source,
                    target=ActivityStatus.RECONCILE,
                )
            target = ActivityStatus.RECONCILE
        updated = await conn.execute(
            """UPDATE activities SET state=?,pending_input_json=?,resume_payload_json=NULL,
               lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
               WHERE activity_id=? AND run_id=? AND state=? AND revision=?""",
            (
                target, canonical_json(pending), now_ms, activity_id, run_id,
                source, parent["revision"],
            ),
        )
        run_update = await conn.execute(
            """UPDATE runs SET pending_input_json=?,revision=revision+1,updated_at=?
               WHERE run_id=? AND state='CANCEL_REQUESTED' AND terminal_status IS NULL""",
            (canonical_json(pending), now_ms, run_id),
        )
        if updated.rowcount != 1 or run_update.rowcount != 1:
            raise conflict(
                "TOOL_RECONCILIATION_CAS_CONFLICT",
                "idle cancel takeover lost its parent/Run CAS",
            )
        if target is not source:
            await self._append_in_tx(conn, run_id, [EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {
                    "from": source,
                    "to": target,
                    "reason": reason,
                    "unresolved_tool_execution_ids": actionable,
                },
                activity_id=activity_id,
                occurred_at=now_ms,
            )])
        return actionable

    async def _finalize_terminal_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        activity_id: str,
        terminal_status: RunStatus,
        payload: dict[str, Any],
        now_ms: int,
        prior_state: RunStatus,
        activity_terminal_override: ActivityStatus | None = None,
    ) -> RunRecord:
        activity_terminal = activity_terminal_override or {
            RunStatus.SUCCEEDED: ActivityStatus.SUCCEEDED,
            RunStatus.CANCELLED: ActivityStatus.CANCELLED,
        }.get(terminal_status, ActivityStatus.FAILED)
        run_before = await self._require_run_row(conn, run_id)
        actual_run_state = RunStatus(run_before["state"])
        if actual_run_state is not prior_state:
            raise conflict(
                "INVALID_RUN_STATE_TRANSITION",
                "terminal transition prior state does not match the stored Run",
                expected=prior_state,
                actual=actual_run_state,
                target=terminal_status,
            )
        if terminal_status not in RUN_TRANSITIONS[actual_run_state]:
            raise conflict(
                "INVALID_RUN_STATE_TRANSITION",
                "terminal transition is not present in the Run adjacency table",
                source=actual_run_state,
                target=terminal_status,
            )
        if terminal_status is RunStatus.CANCELLED:
            unresolved = await self._unresolved_effect_ids(conn, run_id)
            if unresolved:
                raise conflict(
                    "UNRESOLVED_TOOL_EFFECTS",
                    "Run with uncertain external effects cannot be declared CANCELLED",
                    unresolved_tool_execution_ids=unresolved,
                )
        activity_before = await (await conn.execute(
            "SELECT state FROM activities WHERE activity_id=?", (activity_id,),
        )).fetchone()
        if activity_before is None:
            raise not_found("activity", activity_id)
        actual_activity_state = ActivityStatus(activity_before["state"])
        if activity_terminal not in ACTIVITY_TRANSITIONS[actual_activity_state]:
            raise conflict(
                "INVALID_ACTIVITY_STATE_TRANSITION",
                "terminal transition is not present in the Activity adjacency table",
                source=actual_activity_state,
                target=activity_terminal,
            )
        await self._close_undispatched_prepared_tools_in_tx(
            conn, run_id=run_id, now_ms=now_ms,
            reason=f"Run terminated as {terminal_status} before tool dispatch",
        )
        cursor = await conn.execute(
            """UPDATE runs SET state=?,terminal_status=?,terminal_payload_json=?,
               pending_input_json=NULL,revision=revision+1,updated_at=?
               WHERE run_id=? AND terminal_status IS NULL AND state=?""",
            (
                terminal_status,
                terminal_status,
                canonical_json(payload),
                now_ms,
                run_id,
                prior_state,
            ),
        )
        if cursor.rowcount != 1:
            raise conflict("RUN_ALREADY_TERMINAL", "Run terminal CAS lost")
        await conn.execute(
            """UPDATE activities SET state=?,result_json=?,error_json=?,lease_owner=NULL,
               lease_expires_at=NULL,pending_input_json=NULL,resume_payload_json=NULL,
               revision=revision+1,updated_at=?
               WHERE activity_id=?""",
            (
                activity_terminal,
                canonical_json(payload) if terminal_status is RunStatus.SUCCEEDED else None,
                canonical_json(payload) if terminal_status is not RunStatus.SUCCEEDED else None,
                now_ms, activity_id,
            ),
        )
        await self._append_in_tx(conn, run_id, [
            EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {"from": activity_before["state"], "to": activity_terminal},
                activity_id=activity_id, occurred_at=now_ms,
            ),
            EventDraft(
                EventType.RUN_STATUS_CHANGED,
                {"from": prior_state, "to": terminal_status},
                activity_id=activity_id, occurred_at=now_ms,
                terminal_status=terminal_status,
            ),
            EventDraft(
                EventType.RUN_TERMINATED, payload,
                activity_id=activity_id, occurred_at=now_ms,
                terminal_status=terminal_status,
            ),
        ])
        return _run_from_row(await self._require_run_row(conn, run_id))

    async def schedule_retry(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        fire_at: int,
        error: dict[str, Any],
        now_ms: int,
    ) -> None:
        async with self.db.transaction() as conn:
            activity = await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            if run["state"] == RunStatus.CANCEL_REQUESTED:
                unresolved = await self._unresolved_effect_ids(conn, run_id)
                if unresolved:
                    await self._defer_cancel_for_unresolved_in_tx(
                        conn,
                        run_id=run_id,
                        activity_id=activity_id,
                        fencing_token=fencing_token,
                        unresolved=unresolved,
                        now_ms=now_ms,
                        reason="cancel won before retry scheduling",
                        error=error,
                    )
                    return
                await self._finalize_terminal_in_tx(
                    conn, run_id=run_id, activity_id=activity_id,
                    terminal_status=RunStatus.CANCELLED,
                    payload={"reason": "cancelled before retry"}, now_ms=now_ms,
                    prior_state=RunStatus.CANCEL_REQUESTED,
                )
                return
            await conn.execute(
                """UPDATE activities SET state='WAITING_RETRY',available_at=?,error_json=?,
                   lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND fencing_token=?""",
                (fire_at, canonical_json(error), now_ms, activity_id, fencing_token),
            )
            await conn.execute(
                """UPDATE runs SET state='WAITING_RETRY',revision=revision+1,updated_at=?
                   WHERE run_id=? AND state='RUNNING'""",
                (now_ms, run_id),
            )
            timer_id = stable_id("tmr", run_id, f"retry:{activity_id}:{fire_at}")
            await conn.execute(
                """INSERT OR IGNORE INTO timers
                   (timer_id,run_id,activity_id,kind,fire_at,state,revision,created_at,fired_at)
                   VALUES (?,?,?,?,?,'SCHEDULED',0,?,NULL)""",
                (timer_id, run_id, activity_id, "RETRY", fire_at, now_ms),
            )
            await self._append_in_tx(conn, run_id, [
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": activity["state"], "to": ActivityStatus.WAITING_RETRY,
                     "available_at": fire_at, "error": error},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.RUN_STATUS_CHANGED,
                    {"from": RunStatus.RUNNING, "to": RunStatus.WAITING_RETRY},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
            ])

    async def fire_due_timers(self, *, now_ms: int, limit: int = 100) -> int:
        fired = 0
        async with self.db.transaction() as conn:
            rows = await (await conn.execute(
                """SELECT * FROM timers WHERE state='SCHEDULED' AND fire_at<=?
                   ORDER BY fire_at,timer_id LIMIT ?""",
                (now_ms, limit),
            )).fetchall()
            for timer in rows:
                cursor = await conn.execute(
                    """UPDATE timers SET state='FIRED',fired_at=?,revision=revision+1
                       WHERE timer_id=? AND state='SCHEDULED' AND revision=?""",
                    (now_ms, timer["timer_id"], timer["revision"]),
                )
                if cursor.rowcount != 1:
                    continue
                activity = await (await conn.execute(
                    "SELECT * FROM activities WHERE activity_id=?", (timer["activity_id"],),
                )).fetchone()
                if activity is None or activity["state"] != ActivityStatus.WAITING_RETRY:
                    continue
                await conn.execute(
                    """UPDATE activities SET state='PENDING',available_at=?,revision=revision+1,
                       updated_at=? WHERE activity_id=? AND state='WAITING_RETRY'""",
                    (now_ms, now_ms, activity["activity_id"]),
                )
                await conn.execute(
                    """UPDATE runs SET state='DISPATCH_PENDING',revision=revision+1,updated_at=?
                       WHERE run_id=? AND state='WAITING_RETRY'""",
                    (now_ms, timer["run_id"]),
                )
                await self._append_in_tx(conn, timer["run_id"], [
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": ActivityStatus.WAITING_RETRY, "to": ActivityStatus.PENDING,
                         "timer_id": timer["timer_id"]},
                        activity_id=activity["activity_id"], occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": RunStatus.WAITING_RETRY, "to": RunStatus.DISPATCH_PENDING},
                        activity_id=activity["activity_id"], occurred_at=now_ms,
                    ),
                ])
                fired += 1
        return fired

    async def wait_for_input(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        pending_input: dict[str, Any],
        now_ms: int,
    ) -> RunRecord:
        async with self.db.transaction() as conn:
            activity = await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            await self._recover_uncertain_effects_to_manual_in_tx(
                conn,
                run_id=run_id,
                cancel_owned=run["state"] == RunStatus.CANCEL_REQUESTED,
                now_ms=now_ms,
                recovery_reason=(
                    "CANCEL_OWNED_UNCERTAIN_EFFECT"
                    if run["state"] == RunStatus.CANCEL_REQUESTED
                    else "ENGINE_WAIT_UNCERTAIN_EFFECT"
                ),
            )
            unresolved_rows = await self._unresolved_effect_rows(conn, run_id)
            unresolved = [str(row["tool_execution_id"]) for row in unresolved_rows]
            if run["state"] == RunStatus.CANCEL_REQUESTED:
                if unresolved:
                    return await self._defer_cancel_for_unresolved_in_tx(
                        conn,
                        run_id=run_id,
                        activity_id=activity_id,
                        fencing_token=fencing_token,
                        unresolved=unresolved,
                        now_ms=now_ms,
                        reason="cancel won before wait boundary",
                    )
                return await self._finalize_terminal_in_tx(
                    conn, run_id=run_id, activity_id=activity_id,
                    terminal_status=RunStatus.CANCELLED,
                    payload={"reason": "cancelled before wait"}, now_ms=now_ms,
                    prior_state=RunStatus.CANCEL_REQUESTED,
                )
            if unresolved_rows:
                actionable = [
                    str(row["tool_execution_id"])
                    for row in unresolved_rows
                    if row["effect_status"] == "MANUAL_REQUIRED"
                ]
                invalid = [
                    str(row["tool_execution_id"])
                    for row in unresolved_rows
                    if row["effect_status"] != "MANUAL_REQUIRED"
                    and not _effect_row_is_replay_safe(row)
                ]
                if invalid:
                    raise conflict(
                        "TOOL_RECONCILIATION_INVARIANT",
                        "Engine wait cannot hide a non-replay-safe ToolEffect",
                        unresolved_tool_execution_ids=unresolved,
                        invalid_tool_execution_ids=invalid,
                    )
                if actionable:
                    strict_pending = {
                        "type": "TOOL_RECONCILIATION_REQUIRED",
                        "unresolved_tool_execution_ids": actionable,
                    }
                    activity_update = await conn.execute(
                        """UPDATE activities SET state='RECONCILE',pending_input_json=?,
                           resume_payload_json=NULL,lease_owner=NULL,lease_expires_at=NULL,
                           revision=revision+1,updated_at=? WHERE activity_id=?
                           AND state='RUNNING' AND fencing_token=?""",
                        (
                            canonical_json(strict_pending), now_ms, activity_id,
                            fencing_token,
                        ),
                    )
                    run_update = await conn.execute(
                        """UPDATE runs SET state='WAITING_INPUT',pending_input_json=?,
                           revision=revision+1,updated_at=? WHERE run_id=? AND state='RUNNING'""",
                        (canonical_json(strict_pending), now_ms, run_id),
                    )
                    if activity_update.rowcount != 1 or run_update.rowcount != 1:
                        raise conflict(
                            "TOOL_RECONCILIATION_CAS_CONFLICT",
                            "strict ToolEffect wait lost its parent/Run CAS",
                        )
                    await self._append_in_tx(conn, run_id, [
                        EventDraft(
                            EventType.ACTIVITY_STATUS_CHANGED,
                            {
                                "from": ActivityStatus.RUNNING,
                                "to": ActivityStatus.RECONCILE,
                                "reason": "TOOL_RECONCILIATION_OVERRIDES_GENERIC_WAIT",
                                "unresolved_tool_execution_ids": actionable,
                            },
                            activity_id=activity_id,
                            occurred_at=now_ms,
                        ),
                        EventDraft(
                            EventType.RUN_STATUS_CHANGED,
                            {
                                "from": RunStatus.RUNNING,
                                "to": RunStatus.WAITING_INPUT,
                                "pending_input": strict_pending,
                            },
                            activity_id=activity_id,
                            occurred_at=now_ms,
                        ),
                    ])
                    return _run_from_row(await self._require_run_row(conn, run_id))

                # Only frozen replay-safe effects remain.  A generic HITL wait
                # would strand them forever, so release this attempt and let the
                # replacement Engine/Broker consume their stable slots.
                activity_update = await conn.execute(
                    """UPDATE activities SET state='PENDING',available_at=?,
                       pending_input_json=NULL,resume_payload_json=NULL,lease_owner=NULL,
                       lease_expires_at=NULL,revision=revision+1,updated_at=?
                       WHERE activity_id=? AND state='RUNNING' AND fencing_token=?""",
                    (now_ms, now_ms, activity_id, fencing_token),
                )
                run_update = await conn.execute(
                    """UPDATE runs SET state='DISPATCH_PENDING',pending_input_json=NULL,
                       revision=revision+1,updated_at=? WHERE run_id=? AND state='RUNNING'""",
                    (now_ms, run_id),
                )
                if activity_update.rowcount != 1 or run_update.rowcount != 1:
                    raise conflict(
                        "TOOL_RECONCILIATION_CAS_CONFLICT",
                        "replay-safe ToolEffect reschedule lost its parent/Run CAS",
                    )
                await self._append_in_tx(conn, run_id, [
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {
                            "from": ActivityStatus.RUNNING,
                            "to": ActivityStatus.PENDING,
                            "reason": "REPLAY_SAFE_EFFECT_PRECEDES_GENERIC_WAIT",
                            "unresolved_tool_execution_ids": unresolved,
                        },
                        activity_id=activity_id,
                        occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {
                            "from": RunStatus.RUNNING,
                            "to": RunStatus.DISPATCH_PENDING,
                            "reason": "REPLAY_SAFE_EFFECT_PRECEDES_GENERIC_WAIT",
                        },
                        activity_id=activity_id,
                        occurred_at=now_ms,
                    ),
                ])
                return _run_from_row(await self._require_run_row(conn, run_id))
            payload = canonical_json(pending_input)
            await conn.execute(
                """UPDATE activities SET state='WAITING_INPUT',pending_input_json=?,
                   lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND fencing_token=?""",
                (payload, now_ms, activity_id, fencing_token),
            )
            await conn.execute(
                """UPDATE runs SET state='WAITING_INPUT',pending_input_json=?,
                   revision=revision+1,updated_at=? WHERE run_id=? AND state='RUNNING'""",
                (payload, now_ms, run_id),
            )
            await self._append_in_tx(conn, run_id, [
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": activity["state"], "to": ActivityStatus.WAITING_INPUT,
                     "pending_input": pending_input},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.RUN_STATUS_CHANGED,
                    {"from": RunStatus.RUNNING, "to": RunStatus.WAITING_INPUT,
                     "pending_input": pending_input},
                    activity_id=activity_id, occurred_at=now_ms,
                ),
            ])
            return _run_from_row(await self._require_run_row(conn, run_id))

    async def request_cancel(
        self, *, run_id: str, command_id: str, reason: str, now_ms: int,
    ) -> tuple[RunRecord, bool]:
        async with self.db.transaction() as conn:
            prior = await (await conn.execute(
                "SELECT result_json FROM cancellation_commands WHERE command_id=? AND run_id=?",
                (command_id, run_id),
            )).fetchone()
            if prior is not None:
                return _run_from_row(await self._require_run_row(conn, run_id)), True
            run = await self._require_run_row(conn, run_id)
            state = RunStatus(run["state"])
            if state in TERMINAL_RUN_STATUSES:
                raise conflict("RUN_ALREADY_TERMINAL", "a new cancel command cannot change terminal Run")
            unresolved = await self._unresolved_effect_ids(conn, run_id)
            direct = state in {
                RunStatus.ACCEPTED, RunStatus.DISPATCH_PENDING,
                RunStatus.WAITING_RETRY, RunStatus.WAITING_INPUT,
            } and not unresolved
            target = RunStatus.CANCELLED if direct else RunStatus.CANCEL_REQUESTED
            if state is not RunStatus.CANCEL_REQUESTED and target not in RUN_TRANSITIONS[state]:
                raise conflict(
                    "INVALID_RUN_STATE_TRANSITION",
                    "cancel transition is not present in the Run adjacency table",
                    source=state,
                    target=target,
                    unresolved_tool_execution_ids=unresolved,
                )
            if direct:
                activity_id = run["current_activity_id"]
                activity_state: ActivityStatus | None = None
                if activity_id:
                    activity = await (await conn.execute(
                        "SELECT state FROM activities WHERE activity_id=?",
                        (activity_id,),
                    )).fetchone()
                    if activity is None:
                        raise not_found("activity", activity_id)
                    activity_state = ActivityStatus(activity["state"])
                await self._close_undispatched_prepared_tools_in_tx(
                    conn, run_id=run_id, now_ms=now_ms,
                    reason="Run was cancelled before tool dispatch",
                )
                await conn.execute(
                    """UPDATE runs SET state='CANCELLED',terminal_status='CANCELLED',
                       terminal_payload_json=?,pending_input_json=NULL,revision=revision+1,updated_at=?
                       WHERE run_id=? AND terminal_status IS NULL""",
                    (canonical_json({"command_id": command_id, "reason": reason}), now_ms, run_id),
                )
                if activity_id:
                    await conn.execute(
                        """UPDATE activities SET state='CANCELLED',lease_owner=NULL,
                           lease_expires_at=NULL,pending_input_json=NULL,revision=revision+1,
                           updated_at=? WHERE activity_id=?""",
                        (now_ms, activity_id),
                    )
                await conn.execute(
                    """UPDATE timers SET state='CANCELLED',revision=revision+1
                       WHERE run_id=? AND state='SCHEDULED'""",
                    (run_id,),
                )
                drafts = [EventDraft(
                    EventType.CANCEL_REQUESTED,
                    {"command_id": command_id, "reason": reason},
                    activity_id=activity_id, occurred_at=now_ms,
                )]
                if activity_id is not None and activity_state is not None:
                    drafts.append(EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": activity_state, "to": ActivityStatus.CANCELLED,
                         "reason": "RUN_CANCELLED"},
                        activity_id=activity_id, occurred_at=now_ms,
                    ))
                drafts.extend([
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": state, "to": RunStatus.CANCELLED},
                        activity_id=activity_id, occurred_at=now_ms,
                        terminal_status=RunStatus.CANCELLED,
                    ),
                    EventDraft(
                        EventType.RUN_TERMINATED,
                        {"command_id": command_id, "reason": reason},
                        activity_id=activity_id, occurred_at=now_ms,
                        terminal_status=RunStatus.CANCELLED,
                    ),
                ])
                await self._append_in_tx(conn, run_id, drafts)
                result = {"status": RunStatus.CANCELLED, "direct": True}
            else:
                already_requested = state is RunStatus.CANCEL_REQUESTED
                if already_requested:
                    await conn.execute(
                        """UPDATE runs SET revision=revision+1,updated_at=?
                           WHERE run_id=? AND state='CANCEL_REQUESTED'
                             AND terminal_status IS NULL""",
                        (now_ms, run_id),
                    )
                else:
                    await conn.execute(
                        """UPDATE runs SET state='CANCEL_REQUESTED',revision=revision+1,
                           updated_at=? WHERE run_id=? AND terminal_status IS NULL""",
                        (now_ms, run_id),
                    )
                if unresolved and run["current_activity_id"] is not None:
                    actionable = await self._establish_idle_cancel_reconciliation_in_tx(
                        conn,
                        run_id=run_id,
                        activity_id=run["current_activity_id"],
                        now_ms=now_ms,
                        reason="CANCEL_TAKEOVER_BEFORE_REPLACEMENT_CLAIM",
                    )
                    if actionable:
                        unresolved = actionable
                drafts = [EventDraft(
                    EventType.CANCEL_REQUESTED,
                    {"command_id": command_id, "reason": reason,
                     "already_requested": already_requested,
                     "unresolved_tool_execution_ids": unresolved},
                    activity_id=run["current_activity_id"], occurred_at=now_ms,
                )]
                if not already_requested:
                    drafts.append(EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": state, "to": RunStatus.CANCEL_REQUESTED},
                        activity_id=run["current_activity_id"], occurred_at=now_ms,
                    ))
                await self._append_in_tx(conn, run_id, drafts)
                result = {"status": RunStatus.CANCEL_REQUESTED, "direct": False}
            await conn.execute(
                """INSERT INTO cancellation_commands(command_id,run_id,reason,result_json,created_at)
                   VALUES (?,?,?,?,?)""",
                (command_id, run_id, reason, canonical_json(result), now_ms),
            )
            return _run_from_row(await self._require_run_row(conn, run_id)), False

    async def submit_signal(
        self,
        *,
        run_id: str,
        signal_id: str,
        wait_activity_id: str,
        signal_type: str,
        payload: dict[str, Any],
        payload_digest: str,
        now_ms: int,
    ) -> SignalResult:
        reconciliation: ToolReconciliationPayload | None = None
        if signal_type == "tool_reconciliation":
            try:
                reconciliation = ToolReconciliationPayload.model_validate(payload)
            except ValidationError as exc:
                raise conflict(
                    "TOOL_RECONCILIATION_INVALID",
                    "tool_reconciliation payload violates the public contract",
                    validation_errors=exc.errors(include_url=False),
                ) from exc
            payload = reconciliation.model_dump(mode="json", exclude_none=True)
        late = False
        result: SignalResult | None = None
        async with self.db.transaction() as conn:
            run = await self._require_run_row(conn, run_id)
            prior = await (await conn.execute(
                "SELECT * FROM signals WHERE run_id=? AND signal_id=?",
                (run_id, signal_id),
            )).fetchone()
            if prior is not None:
                if prior["payload_digest"] != payload_digest:
                    raise conflict(
                        "SIGNAL_REPLAY_MISMATCH",
                        "signal_id was reused with different content",
                    )
                prior_status = str(prior["status"])
                result = SignalResult(
                    signal_id=signal_id, status=prior_status,
                    run=_run_from_row(run), reused=True,
                )
                # A rejected command remains rejected on idempotent replay.  A
                # durable REJECTED_LATE audit row must never turn the same HTTP
                # command from 409 into a successful 200 response.
                late = prior_status == "REJECTED_LATE"
            elif RunStatus(run["state"]) in TERMINAL_RUN_STATUSES:
                audit = {"accepted": False, "reason": "RUN_ALREADY_TERMINAL"}
                await conn.execute(
                    """INSERT INTO signals
                       (signal_id,run_id,wait_activity_id,type,payload_digest,payload_json,
                        status,result_json,created_at,consumed_at)
                       VALUES (?,?,?,?,?,?,'REJECTED_LATE',?,?,NULL)""",
                    (signal_id, run_id, wait_activity_id, signal_type, payload_digest,
                     canonical_json(payload), canonical_json(audit), now_ms),
                )
                result = SignalResult(
                    signal_id=signal_id, status="REJECTED_LATE", run=_run_from_row(run),
                )
                late = True
            else:
                activity = await (await conn.execute(
                    "SELECT * FROM activities WHERE activity_id=? AND run_id=?",
                    (wait_activity_id, run_id),
                )).fetchone()
                if activity is None:
                    raise not_found("activity", wait_activity_id)
                activity_state = ActivityStatus(activity["state"])
                if reconciliation is not None:
                    result = await self._apply_tool_reconciliation_in_tx(
                        conn,
                        run=run,
                        parent_activity=activity,
                        signal_id=signal_id,
                        signal_type=signal_type,
                        payload=payload,
                        payload_digest=payload_digest,
                        reconciliation=reconciliation,
                        now_ms=now_ms,
                    )
                    return result
                signalable_states = {
                    ActivityStatus.WAITING_INPUT,
                    ActivityStatus.RECONCILE,
                    ActivityStatus.MANUAL,
                }
                if (
                    run["state"] != RunStatus.WAITING_INPUT
                    or activity_state not in signalable_states
                ):
                    raise conflict(
                        "RUN_NOT_WAITING_INPUT", "signal does not match an active wait boundary",
                    )
                if (
                    activity_state in {ActivityStatus.RECONCILE, ActivityStatus.MANUAL}
                    and (_loads(run["pending_input_json"], {}).get("type")
                         not in {"TOOL_RECONCILIATION", "TOOL_RECONCILIATION_REQUIRED"})
                ):
                    raise conflict(
                        "RUN_NOT_WAITING_INPUT",
                        "reconcile/manual Activity requires a tool-reconciliation boundary",
                    )
                if _loads(run["pending_input_json"], {}).get("type") in {
                    "TOOL_RECONCILIATION",
                    "TOOL_RECONCILIATION_REQUIRED",
                }:
                    raise conflict(
                        "TOOL_RECONCILIATION_SIGNAL_REQUIRED",
                        "a ToolEffect reconciliation boundary only accepts tool_reconciliation",
                    )
                signal_result = {"accepted": True, "consumed": True}
                await conn.execute(
                    """INSERT INTO signals
                       (signal_id,run_id,wait_activity_id,type,payload_digest,payload_json,
                        status,result_json,created_at,consumed_at)
                       VALUES (?,?,?,?,?,?,'CONSUMED',?,?,?)""",
                    (signal_id, run_id, wait_activity_id, signal_type, payload_digest,
                     canonical_json(payload), canonical_json(signal_result), now_ms, now_ms),
                )
                await conn.execute(
                    """UPDATE activities SET state='PENDING',available_at=?,pending_input_json=NULL,
                       resume_payload_json=?,revision=revision+1,updated_at=?
                       WHERE activity_id=? AND state=?""",
                    (now_ms, canonical_json({"signal_id": signal_id, "type": signal_type,
                                             "payload": payload}), now_ms, wait_activity_id,
                     activity_state),
                )
                await conn.execute(
                    """UPDATE runs SET state='DISPATCH_PENDING',pending_input_json=NULL,
                       revision=revision+1,updated_at=? WHERE run_id=? AND state='WAITING_INPUT'""",
                    (now_ms, run_id),
                )
                await self._append_in_tx(conn, run_id, [
                    EventDraft(
                        EventType.SIGNAL_RECORDED,
                        {"signal_id": signal_id, "type": signal_type, "consumed": True},
                        activity_id=wait_activity_id, occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": activity_state, "to": ActivityStatus.PENDING},
                        activity_id=wait_activity_id, occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": RunStatus.WAITING_INPUT, "to": RunStatus.DISPATCH_PENDING},
                        activity_id=wait_activity_id, occurred_at=now_ms,
                    ),
                ])
                result = SignalResult(
                    signal_id=signal_id, status="CONSUMED",
                    run=_run_from_row(await self._require_run_row(conn, run_id)),
                )
        assert result is not None
        if late:
            raise conflict(
                "RUN_ALREADY_TERMINAL", "late signal was audited but cannot resume terminal Run",
                signal_id=signal_id, signal_status="REJECTED_LATE",
            )
        return result

    async def _apply_tool_reconciliation_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run: aiosqlite.Row,
        parent_activity: aiosqlite.Row,
        signal_id: str,
        signal_type: str,
        payload: dict[str, Any],
        payload_digest: str,
        reconciliation: ToolReconciliationPayload,
        now_ms: int,
    ) -> SignalResult:
        """Resolve one MANUAL_REQUIRED ToolEffect and resume its parent atomically."""
        run_id = str(run["run_id"])
        parent_activity_id = str(parent_activity["activity_id"])
        parent_state = ActivityStatus(parent_activity["state"])
        pending = _loads(run["pending_input_json"], {})
        pending_type = pending.get("type") if isinstance(pending, dict) else None
        pending_ids = (
            pending.get("unresolved_tool_execution_ids", [])
            if isinstance(pending, dict)
            else []
        )
        run_state = RunStatus(run["state"])
        cancel_owned = run_state is RunStatus.CANCEL_REQUESTED
        if int(run["deadline_at"]) <= now_ms:
            raise conflict(
                "RUN_DEADLINE_EXCEEDED",
                "tool_reconciliation cannot change an effect after the Run deadline",
                deadline_at=run["deadline_at"],
                now_ms=now_ms,
            )
        if (
            run_state not in {RunStatus.WAITING_INPUT, RunStatus.CANCEL_REQUESTED}
            or run["current_activity_id"] != parent_activity_id
            or parent_state not in {
                ActivityStatus.WAITING_INPUT,
                ActivityStatus.RECONCILE,
                ActivityStatus.MANUAL,
            }
            or pending_type not in {
                "TOOL_RECONCILIATION",
                "TOOL_RECONCILIATION_REQUIRED",
            }
        ):
            raise conflict(
                "RUN_NOT_WAITING_INPUT",
                "tool_reconciliation does not match the Run's active reconciliation boundary",
            )
        if reconciliation.tool_execution_id not in pending_ids:
            raise conflict(
                "TOOL_RECONCILIATION_MISMATCH",
                "ToolExecution is not listed by the active pending-input boundary",
                tool_execution_id=reconciliation.tool_execution_id,
                unresolved_tool_execution_ids=pending_ids,
            )

        execution = await (await conn.execute(
            "SELECT * FROM tool_executions WHERE tool_execution_id=?",
            (reconciliation.tool_execution_id,),
        )).fetchone()
        if execution is None:
            raise not_found("tool_execution", reconciliation.tool_execution_id)
        if execution["run_id"] != run_id:
            raise conflict(
                "TOOL_RECONCILIATION_MISMATCH",
                "ToolExecution belongs to a different Run",
                tool_execution_id=reconciliation.tool_execution_id,
            )
        if execution["effect_status"] != "MANUAL_REQUIRED":
            raise conflict(
                "TOOL_EFFECT_INVALID_TRANSITION",
                "manual reconciliation requires a MANUAL_REQUIRED ToolEffect",
                from_status=execution["effect_status"],
                action=reconciliation.action,
            )
        if (
            reconciliation.action is ToolReconciliationAction.RECONCILE
            and not bool(execution["supports_reconcile"])
        ):
            raise conflict(
                "TOOL_RECONCILE_UNSUPPORTED",
                "ToolExecution has no persisted reconcile capability",
                tool_execution_id=reconciliation.tool_execution_id,
            )

        tool_activity = await (await conn.execute(
            "SELECT * FROM activities WHERE activity_id=? AND run_id=?",
            (execution["activity_id"], run_id),
        )).fetchone()
        if tool_activity is None:
            raise not_found("activity", execution["activity_id"])
        if ActivityStatus(tool_activity["state"]) is not ActivityStatus.MANUAL:
            raise conflict(
                "TOOL_ACTIVITY_INVALID_STATE",
                "MANUAL_REQUIRED ToolEffect must own a MANUAL Tool Activity",
                activity_id=execution["activity_id"],
                activity_state=tool_activity["state"],
            )
        if execution["activity_id"] == parent_activity_id:
            raise conflict(
                "TOOL_RECONCILIATION_MISMATCH",
                "wait Activity cannot be the ToolExecution's child Activity",
            )

        target_effect = {
            ToolReconciliationAction.MARK_COMMITTED: "COMMITTED",
            ToolReconciliationAction.MARK_FAILED: "FAILED",
            ToolReconciliationAction.RECONCILE: "RECONCILING",
        }[reconciliation.action]
        target_tool_activity = {
            ToolReconciliationAction.MARK_COMMITTED: ActivityStatus.SUCCEEDED,
            ToolReconciliationAction.MARK_FAILED: ActivityStatus.FAILED,
            ToolReconciliationAction.RECONCILE: ActivityStatus.PENDING,
        }[reconciliation.action]
        if target_tool_activity not in ACTIVITY_TRANSITIONS[ActivityStatus.MANUAL]:
            raise conflict(
                "TOOL_ACTIVITY_INVALID_TRANSITION",
                "manual reconciliation is absent from the Activity adjacency table",
                source=ActivityStatus.MANUAL,
                target=target_tool_activity,
            )
        if (
            (not cancel_owned or reconciliation.action is ToolReconciliationAction.RECONCILE)
            and ActivityStatus.PENDING not in ACTIVITY_TRANSITIONS[parent_state]
        ):
            raise conflict(
                "INVALID_ACTIVITY_STATE_TRANSITION",
                "reconciliation parent cannot be resumed from its current state",
                source=parent_state,
                target=ActivityStatus.PENDING,
            )

        result = (
            reconciliation.result.model_dump(mode="json", exclude_none=True)
            if reconciliation.result is not None
            else None
        )
        result_ref = reconciliation.result_ref
        error = None
        settlement_external_object_id = reconciliation.external_object_id
        if reconciliation.action is ToolReconciliationAction.RECONCILE:
            result = _loads(execution["result_json"], None)
            result_ref = execution["result_ref"]
            error = _loads(execution["error_json"], None)
            settlement_external_object_id = execution["external_object_id"]
            result, result_ref, settlement_external_object_id = (
                _normalize_settled_tool_result(
                    execution,
                    effect_status="RECONCILING",
                    result=result,
                    result_ref=result_ref,
                    external_object_id=settlement_external_object_id,
                )
            )
        if reconciliation.action is ToolReconciliationAction.MARK_FAILED:
            assert reconciliation.result is not None
            error = {
                "code": reconciliation.result.error_code,
                "message": reconciliation.result.error_message,
                "manual_reconciliation": True,
            }

        tool_result_event_id = (
            new_id("evt")
            if reconciliation.action is not ToolReconciliationAction.RECONCILE
            else None
        )
        if (
            result_ref is not None
            and reconciliation.action is not ToolReconciliationAction.RECONCILE
        ):
            artifact = await (await conn.execute(
                "SELECT artifact_id FROM artifact_metadata WHERE artifact_id=?",
                (result_ref,),
            )).fetchone()
            if artifact is None:
                raise not_found("artifact", result_ref)
            await conn.execute(
                """INSERT INTO artifact_links
                   (link_id,artifact_id,run_id,activity_id,event_id,relation,sensitivity,
                    retention_until,created_at) VALUES (?,?,?,?,?,'TOOL_RECONCILIATION_RESULT',
                    'PRIVATE',NULL,?)""",
                (
                    new_id("alink"), result_ref, run_id, execution["activity_id"],
                    tool_result_event_id, now_ms,
                ),
            )

        effect = await conn.execute(
            """UPDATE tool_executions SET effect_status=?,result_json=?,result_ref=?,
               error_json=?,external_object_id=?,reconcile_state=?,revision=revision+1,
               updated_at=? WHERE tool_execution_id=? AND effect_status='MANUAL_REQUIRED'
               AND revision=? RETURNING revision""",
            (
                target_effect,
                canonical_json(result) if result is not None else None,
                result_ref,
                canonical_json(error) if error is not None else None,
                settlement_external_object_id,
                {
                    ToolReconciliationAction.MARK_COMMITTED: "MANUAL_COMMITTED",
                    ToolReconciliationAction.MARK_FAILED: "MANUAL_FAILED",
                    ToolReconciliationAction.RECONCILE: "MANUAL_RECONCILE_REQUESTED",
                }[reconciliation.action],
                now_ms,
                reconciliation.tool_execution_id,
                execution["revision"],
            ),
        )
        effect_revision_row = await effect.fetchone()
        if effect_revision_row is None:
            raise conflict(
                "TOOL_EXECUTION_CAS_CONFLICT",
                "ToolExecution changed while applying manual reconciliation",
                tool_execution_id=reconciliation.tool_execution_id,
            )
        effect_revision = int(effect_revision_row["revision"])

        tool_activity_update = await conn.execute(
            """UPDATE activities SET state=?,available_at=?,result_json=?,error_json=?,
               revision=revision+1,updated_at=? WHERE activity_id=? AND state='MANUAL'
               AND revision=?""",
            (
                target_tool_activity,
                now_ms,
                canonical_json(result) if result is not None else None,
                canonical_json(error) if error is not None else None,
                now_ms,
                execution["activity_id"],
                tool_activity["revision"],
            ),
        )
        if tool_activity_update.rowcount != 1:
            raise conflict(
                "TOOL_ACTIVITY_CAS_CONFLICT",
                "Tool Activity changed while applying manual reconciliation",
                activity_id=execution["activity_id"],
            )

        signal_result = {
            "accepted": True,
            "consumed": True,
            "tool_execution_id": reconciliation.tool_execution_id,
            "action": reconciliation.action,
            "effect_status": target_effect,
        }
        await conn.execute(
            """INSERT INTO signals
               (signal_id,run_id,wait_activity_id,type,payload_digest,payload_json,
                status,result_json,created_at,consumed_at)
               VALUES (?,?,?,?,?,?,'CONSUMED',?,?,?)""",
            (
                signal_id, run_id, parent_activity_id, signal_type, payload_digest,
                canonical_json(payload), canonical_json(signal_result), now_ms, now_ms,
            ),
        )
        drafts = [
            EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {
                    "from": ActivityStatus.MANUAL,
                    "to": target_tool_activity,
                    "reason": "MANUAL_TOOL_RECONCILIATION",
                    "signal_id": signal_id,
                },
                activity_id=execution["activity_id"],
                tool_execution_id=reconciliation.tool_execution_id,
                occurred_at=now_ms,
            ),
            EventDraft(
                EventType.SIGNAL_RECORDED,
                {
                    "signal_id": signal_id,
                    "type": signal_type,
                    "consumed": True,
                    "tool_execution_id": reconciliation.tool_execution_id,
                    "action": reconciliation.action,
                },
                activity_id=parent_activity_id,
                tool_execution_id=reconciliation.tool_execution_id,
                occurred_at=now_ms,
            ),
        ]
        if reconciliation.action is not ToolReconciliationAction.RECONCILE:
            assert tool_result_event_id is not None
            drafts.insert(0, EventDraft(
                EventType.TOOL_RESULT_COMMITTED,
                {
                    "name": execution["tool_name"],
                    "status": target_effect,
                    "result": result,
                    "result_ref": result_ref,
                    "error": error,
                    "external_object_id": reconciliation.external_object_id,
                    "manual_reconciliation": {
                        "signal_id": signal_id,
                        "action": reconciliation.action,
                        "evidence": reconciliation.evidence,
                    },
                    **({"late_result": True, "cancel_requested": True}
                       if cancel_owned else {}),
                },
                activity_id=execution["activity_id"],
                tool_execution_id=reconciliation.tool_execution_id,
                occurred_at=now_ms,
                event_id=tool_result_event_id,
            ))

        if reconciliation.action is ToolReconciliationAction.RECONCILE:
            # This marker is deliberately the entire resume payload.  The
            # Coordinator recognizes it before any Engine/checkpoint/history
            # work and may invoke only the registered query hook.
            parent_resume = _reconciliation_marker(
                tool_execution_id=reconciliation.tool_execution_id,
                signal_id=signal_id,
                expected_effect_revision=effect_revision,
            )
            parent = await conn.execute(
                """UPDATE activities SET state='PENDING',available_at=?,
                   pending_input_json=NULL,resume_payload_json=?,revision=revision+1,
                   updated_at=? WHERE activity_id=? AND run_id=? AND state=? AND revision=?""",
                (
                    now_ms, canonical_json(parent_resume), now_ms, parent_activity_id,
                    run_id, parent_state, parent_activity["revision"],
                ),
            )
            if cancel_owned:
                run_update = await conn.execute(
                    """UPDATE runs SET pending_input_json=NULL,revision=revision+1,updated_at=?
                       WHERE run_id=? AND state='CANCEL_REQUESTED' AND revision=?""",
                    (now_ms, run_id, run["revision"]),
                )
                mode = "CANCEL_RECONCILE_ONLY"
            else:
                run_update = await conn.execute(
                    """UPDATE runs SET state='DISPATCH_PENDING',pending_input_json=NULL,
                       revision=revision+1,updated_at=? WHERE run_id=?
                       AND state='WAITING_INPUT' AND revision=?""",
                    (now_ms, run_id, run["revision"]),
                )
                mode = "RECONCILE_ONLY"
            if parent.rowcount != 1 or run_update.rowcount != 1:
                raise conflict(
                    "TOOL_RECONCILIATION_CAS_CONFLICT",
                    "reconcile-only scheduling lost its parent/Run CAS",
                )
            drafts.append(EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {
                    "from": parent_state,
                    "to": ActivityStatus.PENDING,
                    "signal_id": signal_id,
                    "mode": mode,
                },
                activity_id=parent_activity_id,
                occurred_at=now_ms,
            ))
            if not cancel_owned:
                drafts.append(EventDraft(
                    EventType.RUN_STATUS_CHANGED,
                    {
                        "from": RunStatus.WAITING_INPUT,
                        "to": RunStatus.DISPATCH_PENDING,
                        "signal_id": signal_id,
                        "mode": mode,
                    },
                    activity_id=parent_activity_id,
                    occurred_at=now_ms,
                ))
            await self._append_in_tx(conn, run_id, drafts)
            return SignalResult(
                signal_id=signal_id,
                status="CONSUMED",
                run=_run_from_row(await self._require_run_row(conn, run_id)),
            )

        if cancel_owned:
            await self._manualize_cancel_owned_effects_in_tx(
                conn, run_id=run_id, now_ms=now_ms,
            )
        unresolved_rows = await self._unresolved_effect_rows(conn, run_id)
        unresolved = [row["tool_execution_id"] for row in unresolved_rows]
        remaining_replay_safe = bool(unresolved_rows) and all(
            _effect_row_is_replay_safe(row)
            and row["effect_status"] != "MANUAL_REQUIRED"
            for row in unresolved_rows
        )
        if unresolved and (cancel_owned or not remaining_replay_safe):
            # Resolve exactly one effect per signal.  Neither a normal Run nor
            # a cancel-owned Run may leave the wait boundary until the last
            # operator-owned uncertainty is gone.  Replay-safe remainder is
            # deliberately handed back to the Broker under its frozen guard.
            actionable = [
                row["tool_execution_id"] for row in unresolved_rows
                if row["effect_status"] == "MANUAL_REQUIRED"
            ]
            invalid = [
                row["tool_execution_id"] for row in unresolved_rows
                if row["effect_status"] != "MANUAL_REQUIRED"
                and not _effect_row_is_replay_safe(row)
            ]
            if not actionable or invalid:
                raise conflict(
                    "TOOL_RECONCILIATION_INVARIANT",
                    "operator wait contains a non-manual, non-replay-safe ToolEffect",
                    unresolved_tool_execution_ids=unresolved,
                    operator_actionable_tool_execution_ids=actionable,
                    invalid_tool_execution_ids=invalid,
                )
            remaining_pending = {
                "type": pending_type,
                "unresolved_tool_execution_ids": actionable,
            }
            parent = await conn.execute(
                """UPDATE activities SET pending_input_json=?,resume_payload_json=NULL,
                   revision=revision+1,updated_at=?
                   WHERE activity_id=? AND run_id=? AND state=? AND revision=?""",
                (
                    canonical_json(remaining_pending), now_ms, parent_activity_id,
                    run_id, parent_state, parent_activity["revision"],
                ),
            )
            run_update = await conn.execute(
                """UPDATE runs SET pending_input_json=?,revision=revision+1,updated_at=?
                   WHERE run_id=? AND state=? AND revision=?""",
                (
                    canonical_json(remaining_pending), now_ms, run_id,
                    run_state, run["revision"],
                ),
            )
            if parent.rowcount != 1 or run_update.rowcount != 1:
                raise conflict(
                    "TOOL_RECONCILIATION_CAS_CONFLICT",
                    "reconciliation lost its remaining-pending CAS",
                )
            await self._append_in_tx(conn, run_id, drafts)
            return SignalResult(
                signal_id=signal_id,
                status="CONSUMED",
                run=_run_from_row(await self._require_run_row(conn, run_id)),
            )

        if cancel_owned:
            resolved_rows = await (await conn.execute(
                """SELECT result_json FROM signals WHERE run_id=?
                   AND type='tool_reconciliation' AND status='CONSUMED'
                   ORDER BY created_at,signal_id""",
                (run_id,),
            )).fetchall()
            resolved_ids = sorted({
                str(value["tool_execution_id"])
                for row in resolved_rows
                for value in [_loads(row["result_json"], {})]
                if isinstance(value, dict) and value.get("tool_execution_id")
            })
            await self._append_in_tx(conn, run_id, drafts)
            current = await self._finalize_terminal_in_tx(
                conn,
                run_id=run_id,
                activity_id=parent_activity_id,
                terminal_status=RunStatus.CANCELLED,
                payload={
                    "reason": "cancelled after manual tool resolution",
                    "reconciliation_signal_id": signal_id,
                    "resolved_tool_execution_ids": resolved_ids,
                    "remaining_unresolved_tool_execution_ids": [],
                },
                now_ms=now_ms,
                prior_state=RunStatus.CANCEL_REQUESTED,
            )
            return SignalResult(signal_id=signal_id, status="CONSUMED", run=current)

        parent_resume = {
            "signal_id": signal_id,
            "type": signal_type,
            "payload": {
                "tool_execution_id": reconciliation.tool_execution_id,
                "action": reconciliation.action,
            },
        }
        parent = await conn.execute(
            """UPDATE activities SET state='PENDING',available_at=?,pending_input_json=NULL,
               resume_payload_json=?,revision=revision+1,updated_at=?
               WHERE activity_id=? AND run_id=? AND state=? AND revision=?""",
            (
                now_ms, canonical_json(parent_resume), now_ms, parent_activity_id,
                run_id, parent_state, parent_activity["revision"],
            ),
        )
        run_update = await conn.execute(
            """UPDATE runs SET state='DISPATCH_PENDING',pending_input_json=NULL,
               revision=revision+1,updated_at=? WHERE run_id=? AND state='WAITING_INPUT'
               AND revision=?""",
            (now_ms, run_id, run["revision"]),
        )
        if parent.rowcount != 1 or run_update.rowcount != 1:
            raise conflict(
                "TOOL_RECONCILIATION_CAS_CONFLICT",
                "Run or parent Activity changed while applying reconciliation",
            )
        drafts.extend((
            EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {"from": parent_state, "to": ActivityStatus.PENDING,
                 "signal_id": signal_id},
                activity_id=parent_activity_id,
                occurred_at=now_ms,
            ),
            EventDraft(
                EventType.RUN_STATUS_CHANGED,
                {"from": RunStatus.WAITING_INPUT, "to": RunStatus.DISPATCH_PENDING,
                 "signal_id": signal_id},
                activity_id=parent_activity_id,
                occurred_at=now_ms,
            ),
        ))
        await self._append_in_tx(conn, run_id, drafts)
        return SignalResult(
            signal_id=signal_id,
            status="CONSUMED",
            run=_run_from_row(await self._require_run_row(conn, run_id)),
        )

    async def settle_reconciliation_query(
        self,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        tool_execution_id: str,
        signal_id: str,
        expected_effect_revision: int,
        now_ms: int,
    ) -> RunRecord:
        """Settle one query-only hook without ever entering the Engine Adapter."""
        async with self.db.transaction() as conn:
            activity = await self._assert_fencing(
                conn, activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, run_id)
            run_state = RunStatus(run["state"])
            if run_state not in {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}:
                raise conflict(
                    "INVALID_RUN_STATE_TRANSITION",
                    "reconcile-only query must settle RUNNING or CANCEL_REQUESTED",
                    actual=run["state"],
                )
            resume = _loads(activity["resume_payload_json"], {})
            if not (
                _is_exact_reconciliation_marker(resume)
                and resume.get("tool_execution_id") == tool_execution_id
                and resume.get("signal_id") == signal_id
                and resume.get("expected_effect_revision") == expected_effect_revision
            ):
                raise conflict(
                    "TOOL_RECONCILIATION_MISMATCH",
                    "claimed Activity does not carry the exact reconcile-only marker",
                )
            signal = await (await conn.execute(
                """SELECT status,type,wait_activity_id FROM signals
                   WHERE run_id=? AND signal_id=?""",
                (run_id, signal_id),
            )).fetchone()
            if (
                signal is None
                or signal["status"] != "CONSUMED"
                or signal["type"] != "tool_reconciliation"
                or signal["wait_activity_id"] != activity_id
            ):
                raise conflict(
                    "TOOL_RECONCILIATION_MISMATCH",
                    "reconcile-only marker has no matching consumed signal",
                )
            execution = await (await conn.execute(
                """SELECT run_id,effect_status,revision FROM tool_executions
                   WHERE tool_execution_id=?""",
                (tool_execution_id,),
            )).fetchone()
            if execution is None:
                raise not_found("tool_execution", tool_execution_id)
            if execution["run_id"] != run_id:
                raise conflict(
                    "TOOL_RECONCILIATION_MISMATCH",
                    "reconcile-only ToolExecution belongs to another Run",
                )
            # Signal CAS produced ``expected``.  A conforming query-only broker
            # performs exactly one authoritative settlement after that.  Any
            # other revision means a stale/corrupt marker and must fail closed.
            if (
                execution["effect_status"] not in {
                    "COMMITTED", "FAILED", "MANUAL_REQUIRED",
                }
                or int(execution["revision"]) != expected_effect_revision + 1
            ):
                raise conflict(
                    "TOOL_RECONCILIATION_MISMATCH",
                    "reconcile-only result does not match the authorized effect revision",
                    expected_effect_revision=expected_effect_revision + 1,
                    actual_effect_revision=execution["revision"],
                    effect_status=execution["effect_status"],
                )
            if run_state is RunStatus.CANCEL_REQUESTED:
                await self._manualize_cancel_owned_effects_in_tx(
                    conn, run_id=run_id, now_ms=now_ms,
                )
            unresolved_rows = await self._unresolved_effect_rows(conn, run_id)
            unresolved = [row["tool_execution_id"] for row in unresolved_rows]
            remaining_replay_safe = bool(unresolved_rows) and all(
                _effect_row_is_replay_safe(row)
                and row["effect_status"] != "MANUAL_REQUIRED"
                for row in unresolved_rows
            )
            resolved_rows = await (await conn.execute(
                """SELECT result_json FROM signals WHERE run_id=?
                   AND type='tool_reconciliation' AND status='CONSUMED'
                   ORDER BY created_at,signal_id""",
                (run_id,),
            )).fetchall()
            resolved = sorted({
                str(value["tool_execution_id"])
                for row in resolved_rows
                for value in [_loads(row["result_json"], {})]
                if (
                    isinstance(value, dict)
                    and value.get("tool_execution_id")
                    and value["tool_execution_id"] not in unresolved
                )
            })
            audit = {
                "reconciliation_signal_id": signal_id,
                "resolved_tool_execution_ids": resolved,
                "remaining_unresolved_tool_execution_ids": unresolved,
            }
            if int(run["deadline_at"]) <= now_ms:
                return await self._finalize_terminal_in_tx(
                    conn,
                    run_id=run_id,
                    activity_id=activity_id,
                    terminal_status=RunStatus.TIMED_OUT,
                    payload={"code": "DEADLINE_EXCEEDED", **audit},
                    now_ms=now_ms,
                    prior_state=run_state,
                )
            if unresolved and not (
                run_state is RunStatus.RUNNING and remaining_replay_safe
            ):
                actionable = [
                    row["tool_execution_id"] for row in unresolved_rows
                    if row["effect_status"] == "MANUAL_REQUIRED"
                ]
                invalid = [
                    row["tool_execution_id"] for row in unresolved_rows
                    if row["effect_status"] != "MANUAL_REQUIRED"
                    and not _effect_row_is_replay_safe(row)
                ]
                if not actionable or invalid:
                    raise conflict(
                        "TOOL_RECONCILIATION_INVARIANT",
                        "query settlement found a non-manual, non-replay-safe ToolEffect",
                        unresolved_tool_execution_ids=unresolved,
                        operator_actionable_tool_execution_ids=actionable,
                        invalid_tool_execution_ids=invalid,
                    )
                if run_state is RunStatus.CANCEL_REQUESTED:
                    return await self._defer_cancel_for_unresolved_in_tx(
                        conn,
                        run_id=run_id,
                        activity_id=activity_id,
                        fencing_token=fencing_token,
                        unresolved=actionable,
                        now_ms=now_ms,
                        reason="cancel reconcile-only query remained unresolved",
                        error={"code": "TOOL_RECONCILIATION_PENDING", **audit},
                    )
                pending = {
                    "type": "TOOL_RECONCILIATION",
                    "unresolved_tool_execution_ids": actionable,
                }
                parent = await conn.execute(
                    """UPDATE activities SET state='RECONCILE',pending_input_json=?,
                       resume_payload_json=NULL,lease_owner=NULL,lease_expires_at=NULL,
                       error_json=?,revision=revision+1,updated_at=?
                       WHERE activity_id=? AND run_id=? AND state='RUNNING'
                         AND fencing_token=?""",
                    (
                        canonical_json(pending),
                        canonical_json({"code": "TOOL_RECONCILIATION_PENDING", **audit}),
                        now_ms, activity_id, run_id, fencing_token,
                    ),
                )
                run_update = await conn.execute(
                    """UPDATE runs SET state='WAITING_INPUT',pending_input_json=?,
                       revision=revision+1,updated_at=? WHERE run_id=? AND state='RUNNING'
                       AND revision=?""",
                    (canonical_json(pending), now_ms, run_id, run["revision"]),
                )
                if parent.rowcount != 1 or run_update.rowcount != 1:
                    raise conflict(
                        "TOOL_RECONCILIATION_CAS_CONFLICT",
                        "reconcile-only wait settlement lost its parent/Run CAS",
                    )
                await self._append_in_tx(conn, run_id, [
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {
                            "from": ActivityStatus.RUNNING,
                            "to": ActivityStatus.RECONCILE,
                            "reason": "RECONCILE_QUERY_REQUIRES_MANUAL_INPUT",
                            **audit,
                        },
                        activity_id=activity_id,
                        occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {
                            "from": RunStatus.RUNNING,
                            "to": RunStatus.WAITING_INPUT,
                            "reason": "RECONCILE_QUERY_REQUIRES_MANUAL_INPUT",
                            **audit,
                        },
                        activity_id=activity_id,
                        occurred_at=now_ms,
                    ),
                ])
                return _run_from_row(await self._require_run_row(conn, run_id))

            if run_state is RunStatus.CANCEL_REQUESTED:
                return await self._finalize_terminal_in_tx(
                    conn,
                    run_id=run_id,
                    activity_id=activity_id,
                    terminal_status=RunStatus.CANCELLED,
                    payload={"reason": "cancelled after tool reconciliation", **audit},
                    now_ms=now_ms,
                    prior_state=RunStatus.CANCEL_REQUESTED,
                )

            parent = await conn.execute(
                """UPDATE activities SET state='PENDING',available_at=?,
                   pending_input_json=NULL,resume_payload_json=NULL,lease_owner=NULL,
                   lease_expires_at=NULL,error_json=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND run_id=? AND state='RUNNING'
                     AND fencing_token=?""",
                (now_ms, now_ms, activity_id, run_id, fencing_token),
            )
            run_update = await conn.execute(
                """UPDATE runs SET state='DISPATCH_PENDING',pending_input_json=NULL,
                   revision=revision+1,updated_at=? WHERE run_id=? AND state='RUNNING'
                   AND revision=?""",
                (now_ms, run_id, run["revision"]),
            )
            if parent.rowcount != 1 or run_update.rowcount != 1:
                raise conflict(
                    "TOOL_RECONCILIATION_CAS_CONFLICT",
                    "reconcile-only completion lost its parent/Run CAS",
                )
            await self._append_in_tx(conn, run_id, [
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {
                        "from": ActivityStatus.RUNNING,
                        "to": ActivityStatus.PENDING,
                        "reason": "RECONCILE_QUERY_RESOLVED",
                        **audit,
                    },
                    activity_id=activity_id,
                    occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.RUN_STATUS_CHANGED,
                    {
                        "from": RunStatus.RUNNING,
                        "to": RunStatus.DISPATCH_PENDING,
                        "reason": "RECONCILE_QUERY_RESOLVED",
                        **audit,
                    },
                    activity_id=activity_id,
                    occurred_at=now_ms,
                ),
            ])
            return _run_from_row(await self._require_run_row(conn, run_id))

    async def is_cancel_requested(self, run_id: str) -> bool:
        run = await self.get_run(run_id)
        return run.status is RunStatus.CANCEL_REQUESTED

    async def _recover_uncertain_effects_to_manual_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        cancel_owned: bool,
        now_ms: int,
        recovery_reason: str = "PARENT_LEASE_EXPIRED_UNCERTAIN_EFFECT",
    ) -> list[str]:
        """Expose ownerless uncertain effects at the strict operator boundary.

        A normal Run keeps the frozen replay allowance for READ_ONLY and
        stable-key IDEMPOTENT_EFFECT executions.  Cancellation never revives
        ordinary execution, so even replay-safe effects must become explicit
        reconciliation facts there.
        """
        rows = await (await conn.execute(
            """SELECT te.*,a.state AS activity_state,a.revision AS activity_revision
               FROM tool_executions te JOIN activities a ON a.activity_id=te.activity_id
               WHERE te.run_id=?
                 AND te.effect_status IN ('DISPATCHED','UNKNOWN','RECONCILING')
               ORDER BY te.tool_execution_id""",
            (run_id,),
        )).fetchall()
        recovered: list[str] = []
        drafts: list[EventDraft] = []
        for row in rows:
            if row["effect_status"] == "RECONCILING" and not cancel_owned:
                continue
            replay_safe = _effect_row_is_replay_safe(row)
            if replay_safe and not cancel_owned:
                continue
            child_source = ActivityStatus(row["activity_state"])
            expected_children = {
                "DISPATCHED": {ActivityStatus.RUNNING},
                "UNKNOWN": {ActivityStatus.RECONCILE},
                "RECONCILING": {ActivityStatus.PENDING, ActivityStatus.RECONCILE},
            }[str(row["effect_status"])]
            if child_source not in expected_children:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "lease-orphaned ToolEffect has an inconsistent Activity state",
                    tool_execution_id=row["tool_execution_id"],
                    effect_status=row["effect_status"],
                    activity_state=child_source,
                    expected_activity_states=sorted(expected_children),
                )
            if ActivityStatus.MANUAL not in ACTIVITY_TRANSITIONS[child_source]:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_TRANSITION",
                    "manual lease recovery is absent from the Activity adjacency table",
                    source=child_source,
                    target=ActivityStatus.MANUAL,
                )

            prior_result = _loads(row["result_json"], {})
            try:
                prior_result_size = len(canonical_json(prior_result).encode("utf-8"))
            except (TypeError, ValueError):
                prior_result_size = 8193
            if not (
                isinstance(prior_result, dict)
                and prior_result.get("status") == "UNKNOWN"
                and prior_result_size <= 8192
            ):
                prior_result = ToolResultEnvelope(
                    status="UNKNOWN",
                    result_ref=row["result_ref"],
                    external_object_id=row["external_object_id"],
                    error_code="TOOL_LEASE_RECOVERY_UNCERTAIN",
                    error_message=(
                        "parent worker lease expired after tool dispatch; "
                        "external outcome is unknown"
                    ),
                ).model_dump(mode="json")
            prior_result, preserved_ref, preserved_external = (
                _normalize_settled_tool_result(
                    row,
                    effect_status="MANUAL_REQUIRED",
                    result=prior_result,
                    result_ref=row["result_ref"],
                    external_object_id=row["external_object_id"],
                )
            )
            prior_error = _loads(row["error_json"], {})
            try:
                prior_error_size = len(canonical_json(prior_error).encode("utf-8"))
            except (TypeError, ValueError):
                prior_error_size = 4097
            if not isinstance(prior_error, dict) or prior_error_size > 4096:
                prior_error = {}
            error = {
                "code": "TOOL_LEASE_RECOVERY_UNCERTAIN",
                "message": (
                    "cancellation requires explicit effect resolution"
                    if cancel_owned
                    else "unsafe ToolEffect cannot be replayed after parent lease expiry"
                ),
                **({"prior_error": prior_error} if prior_error else {}),
            }
            effect = await conn.execute(
                """UPDATE tool_executions SET effect_status='MANUAL_REQUIRED',
                   result_json=?,error_json=?,reconcile_state='MANUAL',
                   revision=revision+1,updated_at=? WHERE tool_execution_id=?
                   AND effect_status=? AND revision=?""",
                (
                    canonical_json(prior_result), canonical_json(error), now_ms,
                    row["tool_execution_id"], row["effect_status"], row["revision"],
                ),
            )
            child = await conn.execute(
                """UPDATE activities SET state='MANUAL',result_json=?,error_json=?,
                   lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND state=? AND revision=?""",
                (
                    canonical_json(prior_result), canonical_json(error), now_ms,
                    row["activity_id"], child_source, row["activity_revision"],
                ),
            )
            if effect.rowcount != 1 or child.rowcount != 1:
                raise conflict(
                    "TOOL_RECONCILIATION_CAS_CONFLICT",
                    "lease recovery lost its ToolEffect/Activity CAS",
                    tool_execution_id=row["tool_execution_id"],
                )
            drafts.extend((
                EventDraft(
                    EventType.TOOL_RESULT_COMMITTED,
                    {
                        "name": row["tool_name"],
                        "status": "MANUAL_REQUIRED",
                        "result": prior_result,
                        "result_ref": preserved_ref,
                        "error": error,
                        "external_object_id": preserved_external,
                        "recovery": recovery_reason,
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {
                        "from": child_source,
                        "to": ActivityStatus.MANUAL,
                        "reason": recovery_reason,
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
            ))
            recovered.append(str(row["tool_execution_id"]))
        if drafts:
            await self._append_in_tx(conn, run_id, drafts)
        return recovered

    async def _recover_interrupted_reconcile_queries_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        now_ms: int,
    ) -> list[str]:
        """Return interrupted query-only hooks to an operator-owned boundary."""
        rows = await (await conn.execute(
            """SELECT te.*,a.state AS activity_state,a.revision AS activity_revision
               FROM tool_executions te JOIN activities a ON a.activity_id=te.activity_id
               WHERE te.run_id=? AND te.effect_status='RECONCILING'
               ORDER BY te.tool_execution_id""",
            (run_id,),
        )).fetchall()
        drafts: list[EventDraft] = []
        recovered: list[str] = []
        for row in rows:
            child_source = ActivityStatus(row["activity_state"])
            if child_source not in {
                ActivityStatus.PENDING,
                ActivityStatus.RECONCILE,
            }:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "interrupted RECONCILING effect must own a scheduled/query Tool Activity",
                    tool_execution_id=row["tool_execution_id"],
                    activity_state=row["activity_state"],
                )
            result = _loads(row["result_json"], None)
            try:
                result_size = len(canonical_json(result).encode("utf-8"))
            except (TypeError, ValueError):
                result_size = 8193
            if not (
                isinstance(result, dict)
                and result.get("status") == "UNKNOWN"
                and result_size <= 8192
            ):
                result = ToolResultEnvelope(
                    status="UNKNOWN",
                    result_ref=row["result_ref"],
                    external_object_id=row["external_object_id"],
                    error_code="TOOL_RECONCILE_INTERRUPTED",
                    error_message="reconcile query lost its parent lease before commit",
                ).model_dump(mode="json")
            result, preserved_ref, preserved_external = _normalize_settled_tool_result(
                row,
                effect_status="MANUAL_REQUIRED",
                result=result,
                result_ref=row["result_ref"],
                external_object_id=row["external_object_id"],
            )
            error = {
                "code": "TOOL_RECONCILE_INTERRUPTED",
                "message": "reconcile query must be explicitly authorized again",
            }
            effect = await conn.execute(
                """UPDATE tool_executions SET effect_status='MANUAL_REQUIRED',
                   result_json=?,error_json=?,
                   reconcile_state='MANUAL',revision=revision+1,updated_at=?
                   WHERE tool_execution_id=? AND effect_status='RECONCILING' AND revision=?""",
                (
                    canonical_json(result), canonical_json(error), now_ms,
                    row["tool_execution_id"], row["revision"],
                ),
            )
            child = await conn.execute(
                """UPDATE activities SET state='MANUAL',result_json=?,error_json=?,
                   lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND state=? AND revision=?""",
                (
                    canonical_json(result), canonical_json(error), now_ms,
                    row["activity_id"], child_source, row["activity_revision"],
                ),
            )
            if effect.rowcount != 1 or child.rowcount != 1:
                raise conflict(
                    "TOOL_RECONCILIATION_CAS_CONFLICT",
                    "interrupted reconcile recovery lost its ToolEffect/Activity CAS",
                    tool_execution_id=row["tool_execution_id"],
                )
            drafts.extend((
                EventDraft(
                    EventType.TOOL_RESULT_COMMITTED,
                    {
                        "name": row["tool_name"],
                        "status": "MANUAL_REQUIRED",
                        "result": result,
                        "result_ref": preserved_ref,
                        "error": error,
                        "external_object_id": preserved_external,
                        "recovery": "INTERRUPTED_RECONCILE_QUERY",
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {
                        "from": child_source,
                        "to": ActivityStatus.MANUAL,
                        "reason": "TOOL_RECONCILE_INTERRUPTED",
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
            ))
            recovered.append(str(row["tool_execution_id"]))
        if drafts:
            await self._append_in_tx(conn, run_id, drafts)
        return recovered

    async def recover_expired(self, *, now_ms: int) -> int:
        recovered = 0
        async with self.db.transaction() as conn:
            rows = await (await conn.execute(
                """SELECT * FROM activities WHERE state IN ('CLAIMED','RUNNING')
                   AND lease_expires_at<? ORDER BY lease_expires_at,activity_id""",
                (now_ms,),
            )).fetchall()
            for activity in rows:
                run = await self._require_run_row(conn, activity["run_id"])
                initial_unresolved = await self._unresolved_effect_ids(
                    conn, activity["run_id"]
                )
                if int(run["deadline_at"]) <= now_ms:
                    payload: dict[str, Any] = {"code": "DEADLINE_EXCEEDED"}
                    if initial_unresolved:
                        payload["unresolved_tool_execution_ids"] = initial_unresolved
                    await self._finalize_terminal_in_tx(
                        conn,
                        run_id=activity["run_id"],
                        activity_id=activity["activity_id"],
                        terminal_status=RunStatus.TIMED_OUT,
                        payload=payload,
                        now_ms=now_ms,
                        prior_state=RunStatus(run["state"]),
                        activity_terminal_override=(
                            ActivityStatus.CANCELLED
                            if ActivityStatus(activity["state"]) is ActivityStatus.CLAIMED
                            else None
                        ),
                    )
                    recovered += 1
                    continue
                resume_payload = _loads(activity["resume_payload_json"], {})
                reconcile_only_recovery = _is_exact_reconciliation_marker(resume_payload)
                recovered_uncertain_effects = (
                    await self._recover_uncertain_effects_to_manual_in_tx(
                        conn,
                        run_id=activity["run_id"],
                        cancel_owned=(
                            RunStatus(run["state"]) is RunStatus.CANCEL_REQUESTED
                        ),
                        now_ms=now_ms,
                    )
                )
                interrupted_reconciliations = (
                    await self._recover_interrupted_reconcile_queries_in_tx(
                        conn, run_id=activity["run_id"], now_ms=now_ms,
                    )
                )
                unresolved_rows = await self._unresolved_effect_rows(
                    conn, activity["run_id"]
                )
                unresolved = [row["tool_execution_id"] for row in unresolved_rows]
                automatically_replayable = bool(unresolved_rows) and all(
                    _effect_row_is_replay_safe(row)
                    and row["effect_status"] != "MANUAL_REQUIRED"
                    for row in unresolved_rows
                )
                if RunStatus(run["state"]) is RunStatus.CANCEL_REQUESTED and not unresolved:
                    # The cancelling worker disappeared after the cancel CAS.  With
                    # no uncertain effect there is nothing to reconcile, so recovery
                    # may deterministically close CANCELLED instead of redispatching.
                    await self._finalize_terminal_in_tx(
                        conn,
                        run_id=activity["run_id"],
                        activity_id=activity["activity_id"],
                        terminal_status=RunStatus.CANCELLED,
                        payload={"reason": "cancel recovered after worker lease expired"},
                        now_ms=now_ms,
                        prior_state=RunStatus.CANCEL_REQUESTED,
                    )
                    recovered += 1
                    continue
                must_reconcile = bool(unresolved) and (
                    RunStatus(run["state"]) is RunStatus.CANCEL_REQUESTED
                    or not automatically_replayable
                )
                next_activity = (
                    ActivityStatus.RECONCILE if must_reconcile else ActivityStatus.PENDING
                )
                cursor = await conn.execute(
                    """UPDATE activities SET state=?,available_at=?,lease_owner=NULL,
                       lease_expires_at=NULL,
                       resume_payload_json=CASE WHEN ? THEN NULL ELSE resume_payload_json END,
                       revision=revision+1,updated_at=?
                       WHERE activity_id=? AND state IN ('CLAIMED','RUNNING')
                         AND fencing_token=? AND lease_expires_at<?""",
                    (next_activity, now_ms, int(reconcile_only_recovery), now_ms,
                     activity["activity_id"],
                     activity["fencing_token"], now_ms),
                )
                if cursor.rowcount != 1:
                    continue
                if must_reconcile:
                    actionable = [
                        row["tool_execution_id"] for row in unresolved_rows
                        if row["effect_status"] == "MANUAL_REQUIRED"
                    ]
                    invalid = [
                        row["tool_execution_id"] for row in unresolved_rows
                        if row["effect_status"] != "MANUAL_REQUIRED"
                        and not _effect_row_is_replay_safe(row)
                    ]
                    if not actionable or invalid:
                        raise conflict(
                            "TOOL_RECONCILIATION_INVARIANT",
                            "lease recovery has no operator-actionable ToolEffect",
                            unresolved_tool_execution_ids=unresolved,
                            invalid_tool_execution_ids=invalid,
                        )
                    pending = {"type": "TOOL_RECONCILIATION",
                               "unresolved_tool_execution_ids": actionable}
                    if interrupted_reconciliations:
                        pending["interrupted_reconcile_tool_execution_ids"] = (
                            interrupted_reconciliations
                        )
                    if recovered_uncertain_effects:
                        pending["lease_recovered_manual_tool_execution_ids"] = (
                            recovered_uncertain_effects
                        )
                    await conn.execute(
                        """UPDATE activities SET pending_input_json=?,resume_payload_json=NULL
                           WHERE activity_id=?""",
                        (canonical_json(pending), activity["activity_id"]),
                    )
                    if RunStatus(run["state"]) is RunStatus.CANCEL_REQUESTED:
                        # Cancellation won before recovery.  Unknown effects keep
                        # the Run in CANCEL_REQUESTED until reconcile/deadline;
                        # recovery must never turn cancellation back into a wait.
                        await conn.execute(
                            """UPDATE runs SET pending_input_json=?,revision=revision+1,
                               updated_at=? WHERE run_id=? AND state='CANCEL_REQUESTED'""",
                            (canonical_json(pending), now_ms, activity["run_id"]),
                        )
                        next_run = RunStatus.CANCEL_REQUESTED
                    else:
                        await conn.execute(
                            """UPDATE runs SET state='WAITING_INPUT',pending_input_json=?,
                               revision=revision+1,updated_at=? WHERE run_id=?
                               AND state='RUNNING'""",
                            (canonical_json(pending), now_ms, activity["run_id"]),
                        )
                        next_run = RunStatus.WAITING_INPUT
                else:
                    await conn.execute(
                        """UPDATE runs SET state='DISPATCH_PENDING',revision=revision+1,
                           updated_at=? WHERE run_id=? AND state='RUNNING'""",
                        (now_ms, activity["run_id"]),
                    )
                    next_run = RunStatus.DISPATCH_PENDING
                drafts = [EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": activity["state"], "to": next_activity,
                     "reason": "LEASE_EXPIRED", "unresolved_tool_execution_ids": unresolved},
                    activity_id=activity["activity_id"], occurred_at=now_ms,
                )]
                if RunStatus(run["state"]) is not next_run:
                    drafts.append(EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": run["state"], "to": next_run, "reason": "LEASE_EXPIRED"},
                        activity_id=activity["activity_id"], occurred_at=now_ms,
                    ))
                await self._append_in_tx(conn, activity["run_id"], drafts)
                recovered += 1
        return recovered

    async def expire_deadlines(self, *, now_ms: int, limit: int = 100) -> int:
        expired = 0
        async with self.db.transaction() as conn:
            rows = await (await conn.execute(
                """SELECT * FROM runs WHERE terminal_status IS NULL AND deadline_at<=?
                   ORDER BY deadline_at,run_id LIMIT ?""",
                (now_ms, limit),
            )).fetchall()
            for run in rows:
                unresolved = await self._unresolved_effect_ids(conn, run["run_id"])
                payload: dict[str, Any] = {"code": "DEADLINE_EXCEEDED"}
                if unresolved:
                    payload["unresolved_tool_execution_ids"] = unresolved
                cursor = await conn.execute(
                    """UPDATE runs SET state='TIMED_OUT',terminal_status='TIMED_OUT',
                       terminal_payload_json=?,revision=revision+1,updated_at=?
                       WHERE run_id=? AND terminal_status IS NULL""",
                    (canonical_json(payload), now_ms, run["run_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                await self._close_undispatched_prepared_tools_in_tx(
                    conn, run_id=run["run_id"], now_ms=now_ms,
                    reason="Run deadline elapsed before tool dispatch",
                )
                drafts: list[EventDraft] = []
                if run["current_activity_id"]:
                    activity = await (await conn.execute(
                        "SELECT state FROM activities WHERE activity_id=?",
                        (run["current_activity_id"],),
                    )).fetchone()
                    prior_activity = (
                        ActivityStatus(activity["state"]) if activity is not None else None
                    )
                    activity_terminal = (
                        ActivityStatus.FAILED
                        if prior_activity in {
                            ActivityStatus.RUNNING,
                            ActivityStatus.RECONCILE,
                            ActivityStatus.MANUAL,
                        }
                        else ActivityStatus.CANCELLED
                    )
                    await conn.execute(
                        """UPDATE activities SET state=?,error_json=?,lease_owner=NULL,
                           lease_expires_at=NULL,revision=revision+1,updated_at=?
                           WHERE activity_id=?""",
                        (activity_terminal, canonical_json(payload), now_ms,
                         run["current_activity_id"]),
                    )
                    drafts.append(EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": prior_activity, "to": activity_terminal,
                         "reason": "DEADLINE_EXCEEDED"},
                        activity_id=run["current_activity_id"],
                        occurred_at=now_ms,
                    ))
                await conn.execute(
                    """UPDATE timers SET state='CANCELLED',revision=revision+1
                       WHERE run_id=? AND state='SCHEDULED'""",
                    (run["run_id"],),
                )
                drafts.extend([
                    EventDraft(
                        EventType.RUN_STATUS_CHANGED,
                        {"from": run["state"], "to": RunStatus.TIMED_OUT},
                        activity_id=run["current_activity_id"], occurred_at=now_ms,
                        terminal_status=RunStatus.TIMED_OUT,
                    ),
                    EventDraft(
                        EventType.RUN_TERMINATED, payload,
                        activity_id=run["current_activity_id"], occurred_at=now_ms,
                        terminal_status=RunStatus.TIMED_OUT,
                    ),
                ])
                await self._append_in_tx(conn, run["run_id"], drafts)
                expired += 1
        return expired

    async def heartbeat_worker(
        self, *, worker_id: str, release_map: dict[str, str], state: str, now_ms: int,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                """INSERT INTO runtime_workers
                   (worker_id,release_map_json,state,started_at,heartbeat_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                   release_map_json=excluded.release_map_json,state=excluded.state,
                   heartbeat_at=excluded.heartbeat_at""",
                (worker_id, canonical_json(release_map), state, now_ms, now_ms),
            )

    async def register_artifact_metadata(
        self,
        *,
        artifact_id: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        storage_path: str,
        created_at: int,
    ) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            return await _register_artifact_metadata_in_tx(
                conn,
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=size_bytes,
                media_type=media_type,
                storage_path=storage_path,
                created_at=created_at,
            )

    async def get_artifact_metadata(self, artifact_id: str) -> dict[str, Any]:
        async with self.db.read() as conn:
            row = await (await conn.execute(
                "SELECT * FROM artifact_metadata WHERE artifact_id=?", (artifact_id,),
            )).fetchone()
        if row is None:
            raise not_found("artifact", artifact_id)
        return dict(row)

    async def link_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str | None,
        activity_id: str | None,
        event_id: str | None,
        relation: str,
        sensitivity: str,
        retention_until: int | None,
        now_ms: int,
    ) -> str:
        link_id = new_id("alink")
        async with self.db.transaction() as conn:
            await conn.execute(
                """INSERT INTO artifact_links
                   (link_id,artifact_id,run_id,activity_id,event_id,relation,sensitivity,
                    retention_until,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (link_id, artifact_id, run_id, activity_id, event_id, relation,
                 sensitivity, retention_until, now_ms),
            )
        return link_id

    async def referenced_artifact_ids(self) -> set[str]:
        async with self.db.read() as conn:
            rows = await (await conn.execute(
                "SELECT DISTINCT artifact_id FROM artifact_metadata"
            )).fetchall()
        return {row["artifact_id"] for row in rows}

    async def prepare_tool_execution(
        self,
        *,
        run_id: str,
        parent_activity_id: str,
        fencing_token: int,
        logical_key: str,
        tool_name: str,
        release_digest: str,
        effect_class: str,
        request_digest: str,
        request: dict[str, Any],
        now_ms: int,
        supports_reconcile: bool = False,
        framework_call_id: str | None = None,
    ) -> dict[str, Any]:
        prepared = await self.prepare_tool_execution_batch(
            run_id=run_id,
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            preparations=(ToolExecutionPreparation(
                logical_key=logical_key,
                tool_name=tool_name,
                release_digest=release_digest,
                effect_class=str(effect_class),
                request_digest=request_digest,
                request=request,
                supports_reconcile=supports_reconcile,
                framework_call_id=framework_call_id,
            ),),
            now_ms=now_ms,
        )
        return prepared[0]

    async def prepare_tool_execution_batch(
        self,
        *,
        run_id: str,
        parent_activity_id: str,
        fencing_token: int,
        preparations: Sequence[ToolExecutionPreparation],
        now_ms: int,
    ) -> list[dict[str, Any]]:
        """Prepare a complete model ToolCall batch in one SQLite transaction.

        The full batch is preflighted before the first insert.  A replay
        mismatch in any slot therefore rolls back every new Activity,
        ToolExecution and canonical event in the batch.
        """
        if not preparations:
            return []
        logical_keys = [item.logical_key for item in preparations]
        if len(logical_keys) != len(set(logical_keys)):
            raise conflict(
                "TOOL_BATCH_DUPLICATE_SLOT",
                "a ToolCall batch contains duplicate stable logical slots",
            )

        async with self.db.transaction() as conn:
            await self._assert_fencing(
                conn, parent_activity_id, fencing_token, run_id=run_id, now_ms=now_ms,
            )
            prior_by_key: dict[str, aiosqlite.Row] = {}
            for item in preparations:
                prior = await (await conn.execute(
                    "SELECT * FROM tool_executions WHERE run_id=? AND logical_key=?",
                    (run_id, item.logical_key),
                )).fetchone()
                if prior is None:
                    continue
                if (
                    prior["tool_name"] != item.tool_name
                    or prior["request_digest"] != item.request_digest
                    or prior["release_digest"] != item.release_digest
                    or prior["effect_class"] != item.effect_class
                    or bool(prior["supports_reconcile"]) != item.supports_reconcile
                ):
                    raise conflict(
                        "TOOL_REPLAY_MISMATCH",
                        "a stable tool slot changed request or frozen Tool semantics",
                        logical_key=item.logical_key,
                        persisted={
                            "tool_name": prior["tool_name"],
                            "request_digest": prior["request_digest"],
                            "release_digest": prior["release_digest"],
                            "effect_class": prior["effect_class"],
                            "supports_reconcile": bool(prior["supports_reconcile"]),
                        },
                        requested={
                            "tool_name": item.tool_name,
                            "request_digest": item.request_digest,
                            "release_digest": item.release_digest,
                            "effect_class": item.effect_class,
                            "supports_reconcile": item.supports_reconcile,
                        },
                    )
                prior_by_key[item.logical_key] = prior

            drafts: list[EventDraft] = []
            for item in preparations:
                if item.logical_key in prior_by_key:
                    continue
                tool_execution_id = stable_id("tool", run_id, item.logical_key)
                activity_id = stable_id("act", run_id, f"tool:{item.logical_key}")
                await conn.execute(
                    """INSERT INTO activities (
                      activity_id,run_id,type,logical_key,state,attempt,available_at,lease_owner,
                      lease_expires_at,fencing_token,revision,result_json,error_json,
                      pending_input_json,resume_payload_json,created_at,updated_at
                    ) VALUES (?,?,? ,?,'PENDING',0,?,NULL,NULL,0,0,NULL,NULL,NULL,NULL,?,?)""",
                    (activity_id, run_id, ActivityType.TOOL_CALL,
                     f"tool:{item.logical_key}", now_ms, now_ms, now_ms),
                )
                await conn.execute(
                    """INSERT INTO tool_executions (
                      tool_execution_id,run_id,activity_id,logical_key,tool_name,release_digest,
                      effect_class,supports_reconcile,effect_status,attempt,request_digest,
                      request_json,idempotency_key,
                      result_json,result_ref,error_json,external_object_id,reconcile_state,revision,
                      created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,'PREPARED',0,?,?,?,NULL,NULL,NULL,NULL,NULL,0,?,?)""",
                    (tool_execution_id, run_id, activity_id, item.logical_key,
                     item.tool_name, item.release_digest, item.effect_class,
                     int(item.supports_reconcile),
                     item.request_digest, canonical_json(item.request), tool_execution_id,
                     now_ms, now_ms),
                )
                call_payload: dict[str, Any] = {
                    "name": item.tool_name,
                    "args": item.request,
                    "logical_key": item.logical_key,
                    "effect_class": item.effect_class,
                    "supports_reconcile": item.supports_reconcile,
                    "idempotency_key": tool_execution_id,
                }
                if item.framework_call_id:
                    call_payload["framework_call_id"] = item.framework_call_id
                drafts.extend((
                    EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {"from": None, "to": ActivityStatus.PENDING,
                         "type": ActivityType.TOOL_CALL},
                        activity_id=activity_id,
                        tool_execution_id=tool_execution_id,
                        occurred_at=now_ms,
                    ),
                    EventDraft(
                        EventType.TOOL_CALL_COMMITTED,
                        call_payload,
                        activity_id=activity_id,
                        tool_execution_id=tool_execution_id,
                        occurred_at=now_ms,
                    ),
                ))
            if drafts:
                await self._append_in_tx(conn, run_id, drafts)

            rows: list[dict[str, Any]] = []
            for item in preparations:
                row = await (await conn.execute(
                    "SELECT * FROM tool_executions WHERE run_id=? AND logical_key=?",
                    (run_id, item.logical_key),
                )).fetchone()
                if row is None:  # pragma: no cover - guarded by this transaction
                    raise RuntimeError("prepared ToolExecution disappeared in transaction")
                rows.append(dict(row))
            return rows

    async def mark_tool_dispatched(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        fencing_token: int,
        now_ms: int,
    ) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            parent = await self._assert_fencing(
                conn, parent_activity_id, fencing_token, now_ms=now_ms,
            )
            run = await self._require_run_row(conn, parent["run_id"])
            if run["terminal_status"] is not None or run["state"] != RunStatus.RUNNING:
                raise conflict(
                    "TOOL_DISPATCH_RUN_NOT_RUNNING",
                    "a new Tool dispatch requires an authoritative RUNNING Run",
                    run_id=parent["run_id"],
                    run_status=run["state"],
                )
            if int(run["deadline_at"]) <= now_ms:
                raise conflict(
                    "TOOL_DISPATCH_DEADLINE_EXPIRED",
                    "a Tool dispatch cannot start at or after the Run deadline",
                    run_id=parent["run_id"],
                    deadline_at=run["deadline_at"],
                    now_ms=now_ms,
                )
            prior = await (await conn.execute(
                "SELECT * FROM tool_executions WHERE tool_execution_id=?",
                (tool_execution_id,),
            )).fetchone()
            if prior is None:
                raise not_found("tool_execution", tool_execution_id)
            if prior["run_id"] != parent["run_id"]:
                raise conflict(
                    "TOOL_PARENT_RUN_MISMATCH",
                    "ToolExecution belongs to a different Run than its parent Activity",
                    tool_execution_id=tool_execution_id,
                    parent_activity_id=parent_activity_id,
                )
            if prior["effect_status"] == "COMMITTED":
                return dict(prior)
            if prior["effect_status"] == "MANUAL_REQUIRED":
                raise conflict(
                    "TOOL_ACTIVITY_TERMINAL",
                    "manual ToolExecution cannot be dispatched again",
                    tool_execution_id=tool_execution_id,
                )
            if prior["effect_status"] != "PREPARED" and (
                not _effect_row_is_replay_safe(prior)
                or prior["reconcile_state"] in {
                    "MANUAL_FAILED",
                    "MANUAL_RECONCILE_REQUESTED",
                }
            ):
                raise conflict(
                    "TOOL_EFFECT_REPLAY_FORBIDDEN",
                    "the frozen ToolEffect ledger forbids another external dispatch",
                    tool_execution_id=tool_execution_id,
                    effect_status=prior["effect_status"],
                    effect_class=prior["effect_class"],
                    reconcile_state=prior["reconcile_state"],
                )

            activity = await (await conn.execute(
                "SELECT * FROM activities WHERE activity_id=?", (prior["activity_id"],),
            )).fetchone()
            if activity is None:
                raise not_found("activity", prior["activity_id"])
            current = ActivityStatus(activity["state"])
            effect_status = str(prior["effect_status"])
            drafts: list[EventDraft] = []

            if effect_status == "DISPATCHED":
                if current is not ActivityStatus.RUNNING:
                    raise conflict(
                        "TOOL_ACTIVITY_INVALID_STATE",
                        "a recovered DISPATCHED ToolExecution must still be RUNNING",
                        effect_status=effect_status,
                        activity_state=current,
                    )
                await conn.execute(
                    """UPDATE activities SET state='RECONCILE',revision=revision+1,
                       updated_at=? WHERE activity_id=? AND state='RUNNING'""",
                    (now_ms, prior["activity_id"]),
                )
                drafts.append(EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": ActivityStatus.RUNNING, "to": ActivityStatus.RECONCILE,
                     "reason": "prior dispatch lost its parent fencing lease"},
                    activity_id=prior["activity_id"],
                    tool_execution_id=tool_execution_id,
                    occurred_at=now_ms,
                ))
                current = ActivityStatus.RECONCILE

            expected_state = {
                "PREPARED": ActivityStatus.PENDING,
                "FAILED": ActivityStatus.WAITING_RETRY,
                "UNKNOWN": ActivityStatus.RECONCILE,
                "RECONCILING": ActivityStatus.RECONCILE,
                "DISPATCHED": ActivityStatus.RECONCILE,
            }.get(effect_status)
            if expected_state is None or current is not expected_state:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "ToolExecution effect and Activity state are inconsistent for dispatch",
                    effect_status=effect_status,
                    activity_state=current,
                    expected_state=expected_state,
                )
            if current is ActivityStatus.WAITING_RETRY and activity["available_at"] > now_ms:
                raise conflict(
                    "TOOL_RETRY_NOT_READY",
                    "ToolExecution retry is not available yet",
                    available_at=activity["available_at"],
                    now_ms=now_ms,
                )

            if current in {ActivityStatus.WAITING_RETRY, ActivityStatus.RECONCILE}:
                await conn.execute(
                    """UPDATE activities SET state='PENDING',available_at=?,
                       revision=revision+1,updated_at=?
                       WHERE activity_id=? AND state=?""",
                    (now_ms, now_ms, prior["activity_id"], current),
                )
                drafts.append(EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": current, "to": ActivityStatus.PENDING,
                     "reason": "stable ToolExecution retry"},
                    activity_id=prior["activity_id"],
                    tool_execution_id=tool_execution_id,
                    occurred_at=now_ms,
                ))
                current = ActivityStatus.PENDING

            cursor = await conn.execute(
                """UPDATE tool_executions SET effect_status='DISPATCHED',attempt=attempt+1,
                   dispatch_fencing_token=?,reconcile_state=NULL,
                   revision=revision+1,updated_at=?
                   WHERE tool_execution_id=? AND effect_status=? AND revision=? RETURNING *""",
                (fencing_token, now_ms, tool_execution_id, effect_status, prior["revision"]),
            )
            row = await cursor.fetchone()
            if row is None:
                raise conflict(
                    "TOOL_EXECUTION_CAS_CONFLICT",
                    "ToolExecution changed while preparing dispatch",
                    tool_execution_id=tool_execution_id,
                )

            claimed = await conn.execute(
                """UPDATE activities SET state='CLAIMED',attempt=attempt+1,
                   revision=revision+1,updated_at=? WHERE activity_id=? AND state='PENDING'""",
                (now_ms, row["activity_id"]),
            )
            if claimed.rowcount != 1:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "Tool Activity lost PENDING -> CLAIMED CAS",
                    activity_id=row["activity_id"],
                )
            drafts.append(EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {"from": ActivityStatus.PENDING, "to": ActivityStatus.CLAIMED,
                 "attempt": row["attempt"]},
                activity_id=row["activity_id"], tool_execution_id=tool_execution_id,
                occurred_at=now_ms,
            ))
            running = await conn.execute(
                """UPDATE activities SET state='RUNNING',revision=revision+1,updated_at=?
                   WHERE activity_id=? AND state='CLAIMED'""",
                (now_ms, row["activity_id"]),
            )
            if running.rowcount != 1:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "Tool Activity lost CLAIMED -> RUNNING CAS",
                    activity_id=row["activity_id"],
                )
            drafts.append(EventDraft(
                EventType.ACTIVITY_STATUS_CHANGED,
                {"from": ActivityStatus.CLAIMED, "to": ActivityStatus.RUNNING,
                 "attempt": row["attempt"]},
                activity_id=row["activity_id"], tool_execution_id=tool_execution_id,
                occurred_at=now_ms,
            ))
            await self._append_in_tx(conn, row["run_id"], drafts)
            return dict(row)

    async def mark_tool_reconciling(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        fencing_token: int,
        now_ms: int,
    ) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            parent = await self._assert_fencing(
                conn, parent_activity_id, fencing_token, now_ms=now_ms,
            )
            prior = await (await conn.execute(
                "SELECT * FROM tool_executions WHERE tool_execution_id=?",
                (tool_execution_id,),
            )).fetchone()
            if prior is None:
                raise not_found("tool_execution", tool_execution_id)
            if prior["run_id"] != parent["run_id"]:
                raise conflict(
                    "TOOL_PARENT_RUN_MISMATCH",
                    "ToolExecution belongs to a different Run than its parent Activity",
                    tool_execution_id=tool_execution_id,
                    parent_activity_id=parent_activity_id,
                )
            run = await self._require_run_row(conn, prior["run_id"])
            resume = _loads(parent["resume_payload_json"], {})
            exact_operator_query = (
                _is_exact_reconciliation_marker(resume)
                and resume.get("tool_execution_id") == tool_execution_id
                and resume.get("expected_effect_revision") == prior["revision"]
            )
            if int(run["deadline_at"]) <= now_ms:
                raise conflict(
                    "TOOL_RECONCILE_DEADLINE_EXPIRED",
                    "a Tool reconcile query cannot start at or after the Run deadline",
                    run_id=prior["run_id"],
                    deadline_at=run["deadline_at"],
                    now_ms=now_ms,
                )
            if (
                (exact_operator_query and run["state"] not in {
                    RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED,
                })
                or (not exact_operator_query and run["state"] != RunStatus.RUNNING)
            ):
                raise conflict(
                    "TOOL_RECONCILE_RUN_NOT_EXECUTABLE",
                    "Tool reconcile query has no authoritative Run execution right",
                    run_id=prior["run_id"],
                    run_status=run["state"],
                    exact_operator_query=exact_operator_query,
                )
            if prior["effect_status"] == "RECONCILING":
                activity = await (await conn.execute(
                    "SELECT * FROM activities WHERE activity_id=?", (prior["activity_id"],),
                )).fetchone()
                if activity is None:
                    raise not_found("activity", prior["activity_id"])
                current = ActivityStatus(activity["state"])
                if current is ActivityStatus.RECONCILE:
                    return dict(prior)
                if current is not ActivityStatus.PENDING:
                    raise conflict(
                        "TOOL_ACTIVITY_INVALID_STATE",
                        "a signalled reconciliation must resume from PENDING",
                        activity_state=current,
                    )
                drafts: list[EventDraft] = []
                for source, target in (
                    (ActivityStatus.PENDING, ActivityStatus.CLAIMED),
                    (ActivityStatus.CLAIMED, ActivityStatus.RUNNING),
                    (ActivityStatus.RUNNING, ActivityStatus.RECONCILE),
                ):
                    cursor = await conn.execute(
                        """UPDATE activities SET state=?,revision=revision+1,updated_at=?
                           WHERE activity_id=? AND state=?""",
                        (target, now_ms, prior["activity_id"], source),
                    )
                    if cursor.rowcount != 1:
                        raise conflict(
                            "TOOL_ACTIVITY_CAS_CONFLICT",
                            "Tool Activity changed while starting reconciliation",
                            activity_id=prior["activity_id"],
                        )
                    drafts.append(EventDraft(
                        EventType.ACTIVITY_STATUS_CHANGED,
                        {
                            "from": source,
                            "to": target,
                            "reason": "MANUAL_RECONCILE_QUERY",
                        },
                        activity_id=prior["activity_id"],
                        tool_execution_id=tool_execution_id,
                        occurred_at=now_ms,
                    ))
                await self._append_in_tx(conn, prior["run_id"], drafts)
                return dict(prior)
            if prior["effect_status"] not in {"DISPATCHED", "UNKNOWN"}:
                raise conflict(
                    "TOOL_EFFECT_INVALID_TRANSITION",
                    "only DISPATCHED or UNKNOWN effects can enter reconciliation",
                    effect_status=prior["effect_status"],
                )
            activity = await (await conn.execute(
                "SELECT * FROM activities WHERE activity_id=?", (prior["activity_id"],),
            )).fetchone()
            if activity is None:
                raise not_found("activity", prior["activity_id"])
            current = ActivityStatus(activity["state"])
            drafts: list[EventDraft] = []
            if current is ActivityStatus.RUNNING:
                await conn.execute(
                    """UPDATE activities SET state='RECONCILE',revision=revision+1,
                       updated_at=? WHERE activity_id=? AND state='RUNNING'""",
                    (now_ms, prior["activity_id"]),
                )
                drafts.append(EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": ActivityStatus.RUNNING, "to": ActivityStatus.RECONCILE,
                     "reason": "tool reconciliation started"},
                    activity_id=prior["activity_id"],
                    tool_execution_id=tool_execution_id,
                    occurred_at=now_ms,
                ))
            elif current is not ActivityStatus.RECONCILE:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "Tool Activity cannot enter reconciliation from its current state",
                    activity_state=current,
                )
            cursor = await conn.execute(
                """UPDATE tool_executions SET effect_status='RECONCILING',
                   reconcile_state='RECONCILING',revision=revision+1,updated_at=?
                   WHERE tool_execution_id=? AND effect_status=? AND revision=? RETURNING *""",
                (now_ms, tool_execution_id, prior["effect_status"], prior["revision"]),
            )
            row = await cursor.fetchone()
            if row is None:
                raise conflict(
                    "TOOL_EXECUTION_CAS_CONFLICT",
                    "ToolExecution changed while entering reconciliation",
                    tool_execution_id=tool_execution_id,
                )
            if drafts:
                await self._append_in_tx(conn, row["run_id"], drafts)
            return dict(row)

    async def settle_tool_execution(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        fencing_token: int,
        effect_status: str,
        result: dict[str, Any] | None,
        result_ref: str | None,
        error: dict[str, Any] | None,
        external_object_id: str | None,
        artifact_metadata: dict[str, Any] | None = None,
        artifact_relation: str = "TOOL_RESULT",
        retry_at: int | None = None,
        now_ms: int,
    ) -> dict[str, Any]:
        allowed = {"COMMITTED", "FAILED", "UNKNOWN", "MANUAL_REQUIRED", "RECONCILING"}
        if effect_status not in allowed:
            raise ValueError(f"invalid tool settlement: {effect_status}")
        if retry_at is not None and effect_status != "FAILED":
            raise ValueError("retry_at is only valid for a FAILED read-only attempt")
        async with self.db.transaction() as conn:
            parent = await self._assert_fencing(
                conn, parent_activity_id, fencing_token, now_ms=now_ms,
            )
            prior = await (await conn.execute(
                "SELECT * FROM tool_executions WHERE tool_execution_id=?",
                (tool_execution_id,),
            )).fetchone()
            if prior is None:
                raise not_found("tool_execution", tool_execution_id)
            if prior["run_id"] != parent["run_id"]:
                raise conflict(
                    "TOOL_PARENT_RUN_MISMATCH",
                    "ToolExecution belongs to a different Run than its parent Activity",
                    tool_execution_id=tool_execution_id,
                    parent_activity_id=parent_activity_id,
                )
            run = await self._require_run_row(conn, prior["run_id"])
            late_result = RunStatus(run["state"]) is RunStatus.CANCEL_REQUESTED
            result, result_ref, external_object_id = _normalize_settled_tool_result(
                prior,
                effect_status=effect_status,
                result=result,
                result_ref=result_ref,
                external_object_id=external_object_id,
            )
            if prior["effect_status"] == "COMMITTED":
                return dict(prior)
            if prior["effect_status"] == effect_status:
                # An ACK replay for the same already committed effect state is idempotent and
                # must not create a duplicate TOOL_RESULT or same-state Activity event.
                return dict(prior)
            allowed_effect_transitions = {
                "DISPATCHED": {"COMMITTED", "FAILED", "UNKNOWN", "RECONCILING"},
                "UNKNOWN": {"COMMITTED", "RECONCILING", "MANUAL_REQUIRED"},
                "RECONCILING": {"COMMITTED", "FAILED", "UNKNOWN", "MANUAL_REQUIRED"},
            }
            if effect_status not in allowed_effect_transitions.get(prior["effect_status"], set()):
                raise conflict(
                    "TOOL_EFFECT_INVALID_TRANSITION",
                    "illegal ToolExecution effect transition",
                    from_status=prior["effect_status"],
                    to_status=effect_status,
                )
            activity = await (await conn.execute(
                "SELECT * FROM activities WHERE activity_id=?", (prior["activity_id"],),
            )).fetchone()
            if activity is None:
                raise not_found("activity", prior["activity_id"])
            current_activity_state = ActivityStatus(activity["state"])
            activity_state = {
                "COMMITTED": ActivityStatus.SUCCEEDED,
                "FAILED": (
                    ActivityStatus.WAITING_RETRY
                    if retry_at is not None
                    else ActivityStatus.FAILED
                ),
                "UNKNOWN": ActivityStatus.RECONCILE,
                "RECONCILING": ActivityStatus.RECONCILE,
                "MANUAL_REQUIRED": ActivityStatus.MANUAL,
            }[effect_status]
            state_changed = current_activity_state is not activity_state
            if (
                state_changed
                and activity_state not in ACTIVITY_TRANSITIONS[current_activity_state]
            ):
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_TRANSITION",
                    "illegal Tool Activity transition while settling an attempt",
                    from_status=current_activity_state,
                    to_status=activity_state,
                    effect_status=effect_status,
                )
            tool_result_event_id = new_id("evt")
            linked_artifact_id: str | None = None
            if artifact_metadata is not None:
                if artifact_metadata.get("artifact_id") != result_ref:
                    raise conflict(
                        "TOOL_RESULT_ARTIFACT_MISMATCH",
                        "Artifact metadata must identify the normalized ToolResult ref",
                        tool_execution_id=tool_execution_id,
                        result_ref=result_ref,
                        metadata_artifact_id=artifact_metadata.get("artifact_id"),
                    )
                await _register_artifact_metadata_in_tx(
                    conn,
                    artifact_id=artifact_metadata["artifact_id"],
                    sha256=artifact_metadata["sha256"],
                    size_bytes=artifact_metadata["size_bytes"],
                    media_type=artifact_metadata["media_type"],
                    storage_path=artifact_metadata["storage_path"],
                    created_at=artifact_metadata["created_at"],
                )
                linked_artifact_id = artifact_metadata["artifact_id"]
            elif result_ref is not None:
                referenced = await (await conn.execute(
                    "SELECT artifact_id FROM artifact_metadata WHERE artifact_id=?",
                    (result_ref,),
                )).fetchone()
                if referenced is None:
                    raise not_found("artifact", result_ref)
                linked_artifact_id = result_ref
            if linked_artifact_id is not None:
                await conn.execute(
                    """INSERT INTO artifact_links
                       (link_id,artifact_id,run_id,activity_id,event_id,relation,sensitivity,
                        retention_until,created_at) VALUES (?,?,?,?,?,?,?,NULL,?)""",
                    (
                        new_id("alink"), linked_artifact_id, prior["run_id"],
                        prior["activity_id"], tool_result_event_id,
                        artifact_relation, "PRIVATE", now_ms,
                    ),
                )
            cursor = await conn.execute(
                """UPDATE tool_executions SET effect_status=?,result_json=?,result_ref=?,
                   error_json=?,external_object_id=?,reconcile_state=?,revision=revision+1,
                   updated_at=? WHERE tool_execution_id=? AND effect_status=? AND revision=?""",
                (effect_status, canonical_json(result) if result is not None else None,
                 result_ref, canonical_json(error) if error is not None else None,
                 external_object_id,
                 "MANUAL" if effect_status == "MANUAL_REQUIRED" else effect_status,
                 now_ms, tool_execution_id, prior["effect_status"], prior["revision"]),
            )
            if cursor.rowcount != 1:
                raise conflict(
                    "TOOL_EXECUTION_CAS_CONFLICT",
                    "ToolExecution changed while settling its result",
                    tool_execution_id=tool_execution_id,
                )
            await conn.execute(
                """UPDATE activities SET state=?,available_at=?,result_json=?,error_json=?,
                   revision=revision+1,updated_at=? WHERE activity_id=?""",
                (activity_state, retry_at if retry_at is not None else now_ms,
                 canonical_json(result) if result is not None else None,
                 canonical_json(error) if error is not None else None,
                 now_ms, prior["activity_id"]),
            )
            drafts = [
                EventDraft(
                    EventType.TOOL_RESULT_COMMITTED,
                    {"name": prior["tool_name"], "status": effect_status,
                     "result": result, "result_ref": result_ref,
                     "error": error, "external_object_id": external_object_id,
                     **({"late_result": True} if late_result else {})},
                    activity_id=prior["activity_id"], tool_execution_id=tool_execution_id,
                    occurred_at=now_ms, event_id=tool_result_event_id,
                ),
            ]
            if state_changed:
                drafts.append(EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {"from": current_activity_state, "to": activity_state,
                     **({"available_at": retry_at} if retry_at is not None else {})},
                    activity_id=prior["activity_id"], tool_execution_id=tool_execution_id,
                    occurred_at=now_ms,
                ))
            await self._append_in_tx(conn, prior["run_id"], drafts)
            row = await (await conn.execute(
                "SELECT * FROM tool_executions WHERE tool_execution_id=?",
                (tool_execution_id,),
            )).fetchone()
            return dict(row)

    async def get_tool_execution(self, tool_execution_id: str) -> dict[str, Any]:
        async with self.db.read() as conn:
            row = await (await conn.execute(
                "SELECT * FROM tool_executions WHERE tool_execution_id=?",
                (tool_execution_id,),
            )).fetchone()
        if row is None:
            raise not_found("tool_execution", tool_execution_id)
        return dict(row)

    async def unresolved_tool_execution_ids(self, run_id: str) -> list[str]:
        async with self.db.read() as conn:
            await self._require_run_row(conn, run_id)
            return await self._unresolved_effect_ids(conn, run_id)

    async def _close_undispatched_prepared_tools_in_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        now_ms: int,
        reason: str,
    ) -> list[str]:
        """Close PREPARED ToolCalls at an irrevocable Run terminal boundary.

        PREPARED is the durable-before-dispatch state.  It is intentionally not
        an unresolved-effect state because no external call has happened yet.
        Once the owning engine has produced a terminal outcome, however, leaving
        the row PREPARED would create an orphan Tool Activity and an unpaired
        canonical TOOL_CALL.  Atomically record a synthetic failure result and
        cancel the never-claimed Activity.  This helper is never used during
        retry/recovery, where PREPARED remains safely replayable.
        """
        rows = await (await conn.execute(
            """SELECT te.tool_execution_id,te.activity_id,te.tool_name,a.state
               FROM tool_executions te
               JOIN activities a ON a.activity_id=te.activity_id
               WHERE te.run_id=? AND te.effect_status='PREPARED'
               ORDER BY te.created_at,te.logical_key,te.tool_execution_id""",
            (run_id,),
        )).fetchall()
        if not rows:
            return []

        drafts: list[EventDraft] = []
        closed: list[str] = []
        for row in rows:
            activity_state = ActivityStatus(row["state"])
            if activity_state is not ActivityStatus.PENDING:
                raise conflict(
                    "TOOL_ACTIVITY_INVALID_STATE",
                    "a PREPARED ToolExecution must own a PENDING Activity",
                    tool_execution_id=row["tool_execution_id"],
                    activity_state=activity_state,
                )
            error = {
                "code": "TOOL_NOT_DISPATCHED",
                "message": reason,
                "not_dispatched": True,
            }
            execution = await conn.execute(
                """UPDATE tool_executions SET effect_status='FAILED',error_json=?,
                   reconcile_state='NOT_DISPATCHED',revision=revision+1,updated_at=?
                   WHERE tool_execution_id=? AND effect_status='PREPARED'""",
                (canonical_json(error), now_ms, row["tool_execution_id"]),
            )
            activity = await conn.execute(
                """UPDATE activities SET state='CANCELLED',error_json=?,
                   lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=?
                   WHERE activity_id=? AND state='PENDING'""",
                (canonical_json(error), now_ms, row["activity_id"]),
            )
            if execution.rowcount != 1 or activity.rowcount != 1:
                raise conflict(
                    "TOOL_EXECUTION_CAS_CONFLICT",
                    "prepared ToolExecution changed while closing terminal Run",
                    tool_execution_id=row["tool_execution_id"],
                )
            drafts.extend((
                EventDraft(
                    EventType.TOOL_RESULT_COMMITTED,
                    {
                        "name": row["tool_name"],
                        "status": "FAILED",
                        "result": None,
                        "result_ref": None,
                        "error": error,
                        "external_object_id": None,
                        "synthetic": True,
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
                EventDraft(
                    EventType.ACTIVITY_STATUS_CHANGED,
                    {
                        "from": ActivityStatus.PENDING,
                        "to": ActivityStatus.CANCELLED,
                        "reason": "TOOL_NOT_DISPATCHED",
                    },
                    activity_id=row["activity_id"],
                    tool_execution_id=row["tool_execution_id"],
                    occurred_at=now_ms,
                ),
            ))
            closed.append(row["tool_execution_id"])
        await self._append_in_tx(conn, run_id, drafts)
        return closed

    async def _unresolved_effect_ids(
        self, conn: aiosqlite.Connection, run_id: str,
    ) -> list[str]:
        rows = await self._unresolved_effect_rows(conn, run_id)
        return [row["tool_execution_id"] for row in rows]

    async def _unresolved_effect_rows(
        self, conn: aiosqlite.Connection, run_id: str,
    ) -> list[aiosqlite.Row]:
        return list(await (await conn.execute(
            """SELECT tool_execution_id,effect_class,effect_status,idempotency_key
               FROM tool_executions WHERE run_id=?
               AND effect_status IN ('DISPATCHED','UNKNOWN','RECONCILING','MANUAL_REQUIRED')
               ORDER BY tool_execution_id""",
            (run_id,),
        )).fetchall())
