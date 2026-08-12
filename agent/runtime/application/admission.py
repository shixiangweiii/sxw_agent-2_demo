from __future__ import annotations

from dataclasses import dataclass

from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import EngineName, canonical_json, new_id, sha256_json
from agent.runtime.ports.clock import Clock, SystemClock
from agent.runtime.ports.store import AdmissionCommand, AdmissionResult, RuntimeStore


@dataclass(frozen=True)
class CreateRunInput:
    client_request_id: str
    conversation_id: str | None
    principal_id: str
    agent_id: str
    engine: EngineName
    text: str
    attachment_refs: tuple[str, ...]
    deadline_at: int | None
    # 入口观察到的 x-trace-id。**刻意不进 digest_payload**：它是诊断信号，
    # 让它参与幂等摘要会把"同一请求重放但换了 trace_id"误判成 409 冲突。
    trace_id: str = ""

    def digest_payload(self) -> dict[str, object]:
        # Preserve attachment order: it is meaningful for multimodal prompts.
        return {
            "client_request_id": self.client_request_id,
            "conversation_id": self.conversation_id,
            "principal_id": self.principal_id,
            "agent_id": self.agent_id,
            "engine": self.engine,
            "input": {"text": self.text, "attachment_refs": list(self.attachment_refs)},
            "deadline_at": self.deadline_at,
        }


class AdmissionService:
    def __init__(
        self,
        store: RuntimeStore,
        *,
        clock: Clock | None = None,
        default_deadline_ms: int = 600_000,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.default_deadline_ms = default_deadline_ms

    async def create(self, request: CreateRunInput, *, idempotency_key: str) -> AdmissionResult:
        if not idempotency_key.strip():
            raise RuntimeFault("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
        now = self.clock.now_ms()
        deadline = request.deadline_at if request.deadline_at is not None else now + self.default_deadline_ms
        if deadline <= now:
            raise RuntimeFault("DEADLINE_IN_PAST", "deadline_at must be in the future", 400)
        # Hash the normalized Pydantic/application shape.  Whitespace inside user
        # text and attachment order intentionally remain significant.
        # The digest covers only the normalized client request.  A server-derived
        # default deadline is frozen in the first RuntimeEnvelope but must not make
        # a later replay of the same request look different.
        digest = sha256_json(request.digest_payload())
        command = AdmissionCommand(
            request_id=new_id("req"),
            client_request_id=request.client_request_id,
            idempotency_key=idempotency_key,
            request_digest=digest,
            conversation_id=request.conversation_id,
            generated_conversation_id=new_id("conv"),
            turn_id=new_id("turn"),
            run_id=new_id("run"),
            principal_id=request.principal_id,
            agent_id=request.agent_id,
            engine=request.engine,
            deadline_at=deadline,
            cancel_token_id=new_id("cancel"),
            input_event_id=new_id("evt"),
            input_text=request.text,
            attachment_refs=request.attachment_refs,
            created_at=now,
            trace_id=request.trace_id,
        )
        # 幂等接纳与持久化落在注入的 store 实现里，见 SqliteRuntimeStore.admit。
        return await self.store.admit(command)
