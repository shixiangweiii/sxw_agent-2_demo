from __future__ import annotations

import asyncio
import logging
import signal

from agent.config import get_settings
from agent.context import (
    attach_a2a_agents,
    attach_claude_skill_tools,
    attach_skill_tools,
    build_agent_context,
)
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.artifact_tools import build_read_artifact_tool
from agent.runtime.adapters.legacy_engines import LegacyEngineAdapter
from agent.runtime.adapters.native_reliability_demo import (
    DemoEffectsStore,
    NativeReliabilityDemoAdapter,
    RoutedNativeAdapter,
)
from agent.runtime.adapters.releases import (
    build_release_manifest,
    release_semantic_config,
    tool_catalog_digest,
)
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.worker.dispatcher import RuntimeWorker
from common.obs import get_logger, log_kv, setup_logging
from common.trace import configure_tracing

logger = get_logger("agent.runtime.worker.main")


async def build_worker() -> RuntimeWorker:
    settings = get_settings()
    database = RuntimeDatabase(
        settings.runtime_db_path,
        busy_timeout_ms=settings.runtime_busy_timeout_ms,
    )
    store = SqliteRuntimeStore(database)
    await store.initialize()

    # Only this process loads the LLM and remote tool catalogs.  The API process
    # remains a lightweight durable admission/event service.
    context = build_agent_context(settings) # llm配置，内置tool，技能执行器
    await attach_skill_tools(context) # 加载 skills tools
    attach_claude_skill_tools(context) # 加载本地 claude-skill 包
    await attach_a2a_agents(context) # 加载 a2a
    artifact_store = FilesystemArtifactStore(settings.artifact_root)
    context.tools.append(build_read_artifact_tool(
        artifact_store, store.get_artifact_metadata,
    ))
    broker = ToolBroker(store, artifact_store) # ToolBroker "效应感知的持久化工具调度协议"*（Effect-aware durable tool dispatch protocol）
    loaded_catalog_sha256 = tool_catalog_digest(context.tools)
    manifests = {
        engine: build_release_manifest(
            engine,
            semantic_config=release_semantic_config(settings, engine),
            loaded_tool_catalog_sha256=loaded_catalog_sha256,
        )
        for engine in ("plan_execute", "agent_loop", "native_loop")
    }
    adapters = {} # 打表存放3种引擎的
    for engine, manifest in manifests.items():
        adapters[engine] = LegacyEngineAdapter(
            engine=engine,
            context=context,
            release_fingerprint=manifest.fingerprint(),
            artifact_store=artifact_store,
            artifact_metadata_loader=store.get_artifact_metadata,
            tool_broker=broker,
        )
    demo = NativeReliabilityDemoAdapter(
        release_fingerprint=adapters["native_loop"].release_fingerprint,
        tool_broker=broker,
        effects=DemoEffectsStore(settings.demo_effects_db_path),
    )
    adapters["native_loop"] = RoutedNativeAdapter(adapters["native_loop"], demo)
    # 最终构造出来的结果：
    '''
    adapters["plan_execute"]  = LegacyEngineAdapter(engine="plan_execute")
    adapters["agent_loop"]    = LegacyEngineAdapter(engine="agent_loop")
    adapters["native_loop"]   = RoutedNativeAdapter(
                                    normal = LegacyEngineAdapter(engine="native_loop"),
                                    demo   = NativeReliabilityDemoAdapter(...)
                                )
    '''
    # Publish all active pointers only after every adapter/tool has constructed
    # successfully.  API admissions can never observe a half-new release set.
    await store.register_releases(tuple(manifests.values()), activate=True)
    registry = EngineRegistry(adapters)
    coordinator = RunCoordinator(
        store,
        registry,
        event_flush_ms=settings.runtime_event_flush_ms,
        event_flush_bytes=settings.runtime_event_flush_bytes,
        tool_reconciler=broker,
    )
    return RuntimeWorker(
        store=store,
        coordinator=coordinator,
        worker_id=settings.runtime_worker_id,
        release_map=registry.releases,
        concurrency=settings.runtime_worker_concurrency,
        lease_ms=settings.runtime_lease_seconds * 1000,
        renew_ms=settings.runtime_lease_renew_seconds * 1000,
        poll_ms=settings.runtime_worker_poll_ms,
        shutdown_grace_ms=settings.runtime_shutdown_grace_seconds * 1000,
        artifact_store=artifact_store,
        artifact_cleanup_interval_ms=(
            settings.runtime_artifact_cleanup_interval_seconds * 1000
        ),
        artifact_orphan_age_ms=settings.runtime_artifact_orphan_age_hours * 3_600_000,
    )


async def _run() -> None:
    worker = await build_worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass
    await worker.run()


def main() -> None:
    settings = get_settings() # LLM API调用配置等等基础配置信息
    setup_logging(settings.log_level)
    configure_tracing(
        enabled=settings.trace_enabled,
        payload_level=settings.trace_payload_level,
        trace_dir=settings.trace_dir,
        max_field_chars=settings.trace_max_field_chars,
        retention_days=settings.trace_retention_days,
        engine="runtime-worker",
    )
    log_kv(logger, logging.INFO, "Worker", "booting", db=settings.runtime_db_path)
    asyncio.run(_run())


if __name__ == "__main__":
    main() # worker模块启动
