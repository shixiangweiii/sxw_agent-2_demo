from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from agent.config import AgentSettings
from agent.runtime.adapters.releases import (
    build_release_manifest,
    release_semantic_config,
    release_source_paths,
    release_source_specs,
    tool_catalog_digest,
)


@pytest.mark.parametrize(
    ("engine", "engine_entrypoints"),
    [
        (
            "plan_execute",
            {
                "agent/engine/plan_execute/plan_execute_engine.py",
                "agent/engine/plan_execute/decision_planner.py",
                "agent/engine/plan_execute/execution_planner.py",
            },
        ),
        (
            "agent_loop",
            {
                "agent/engine/agent_loop/agent_loop_engine.py",
                "agent/engine/agent_loop/loop_processor.py",
                "agent/engine/agent_loop/message_budget.py",
            },
        ),
        (
            "native_loop",
            {
                "agent/engine/native_loop/engine.py",
                "agent/engine/native_loop/loop.py",
                "agent/engine/native_loop/llm_client.py",
                "agent/engine/native_loop/messages.py",
                "agent/engine/native_loop/compact.py",
                "agent/engine/native_loop/tools.py",
            },
        ),
    ],
)
def test_release_registry_covers_engine_and_recoverable_shared_semantics(
    engine, engine_entrypoints
):
    groups = release_source_paths(engine)
    assert engine_entrypoints <= set(groups["engine_source"])

    shared = set(groups["shared_agent"])
    assert {
        "agent/engine/base.py",
        "agent/engine/loop_tools/__init__.py",  # LOOP_INSTRUCTION
        "agent/tools/builtin_tools.py",
        "agent/tools/knowledge_search.py",
        "agent/skills/catalog.py",
        "agent/a2a/loader.py",
        "agent/claude_skill/claude_skill_tool.py",
        "agent/claude_skill/skills_data/data_analysis/SKILL.md",
        "agent/context.py",
        "agent/config.py",
    } <= shared

    runtime = set(groups["runtime_contract"])
    assert {
        "agent/runtime/domain/models.py",
        "agent/runtime/ports/engine.py",
        "agent/runtime/application/coordinator.py",
        "agent/runtime/application/events.py",
        "agent/runtime/application/tool_broker.py",
        "agent/runtime/adapters/legacy_engines.py",
        "agent/runtime/adapters/brokered_tools.py",
        "agent/runtime/adapters/releases.py",
        "agent/runtime/adapters/sqlite/store.py",
        "agent/runtime/adapters/sqlite/migrations/001_runtime.sql",
        "agent/runtime/worker/dispatcher.py",
    } <= runtime

    integrations = set(groups["skill_a2a_integrations"])
    assert {
        "common/skill_contract.py",
        "skillcenter/skills.py",
        "skillcenter/a2a_api.py",
        "a2a_service/agents.py",
    } <= integrations

    every_path = set().union(*map(set, groups.values()))
    assert "requirements.txt" in every_path
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in every_path)
    assert not any(PurePosixPath(path).name.startswith(".env") for path in every_path)


def _make_synthetic_release_tree(root: Path, engine: str) -> None:
    for specs in release_source_specs(engine).values():
        for relative in specs:
            target = root / relative
            if PurePosixPath(relative).suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"source:{relative}\n", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "sentinel.py").write_text(
                    f"# source:{relative}\n", encoding="utf-8"
                )
    tools = root / "agent/tools"
    (tools / "builtin_tools.py").write_text("BUILTINS = 1\n", encoding="utf-8")
    (tools / "knowledge_search.py").write_text("SEARCH = 1\n", encoding="utf-8")


def test_source_content_and_directory_membership_change_manifest_digest(tmp_path):
    _make_synthetic_release_tree(tmp_path, "native_loop")
    baseline = build_release_manifest("native_loop", root=tmp_path)

    builtin = tmp_path / "agent/tools/builtin_tools.py"
    builtin.write_text("BUILTINS = 2\n", encoding="utf-8")
    content_changed = build_release_manifest("native_loop", root=tmp_path)

    assert content_changed.fingerprint() != baseline.fingerprint()
    assert (
        content_changed.components["shared_agent_sha256"]
        != baseline.components["shared_agent_sha256"]
    )
    for stable_group in (
        "engine_source_sha256",
        "runtime_contract_sha256",
        "skill_a2a_integrations_sha256",
        "dependency_lock_sha256",
    ):
        assert content_changed.components[stable_group] == baseline.components[stable_group]

    (tmp_path / "agent/tools/new_runtime_tool.py").write_text(
        "NEW_TOOL = True\n", encoding="utf-8"
    )
    membership_changed = build_release_manifest("native_loop", root=tmp_path)
    assert membership_changed.fingerprint() != content_changed.fingerprint()
    assert (
        membership_changed.components["shared_agent_sha256"]
        != content_changed.components["shared_agent_sha256"]
    )


def test_worker_semantic_config_changes_release_without_persisting_secret(tmp_path):
    _make_synthetic_release_tree(tmp_path, "agent_loop")
    first_settings = AgentSettings(
        _env_file=None,
        dashscope_api_key="must-not-enter-release",
        llm_model="model-a",
    )
    second_settings = AgentSettings(
        _env_file=None,
        dashscope_api_key="different-secret",
        llm_model="model-b",
    )
    first_config = release_semantic_config(first_settings, "agent_loop")
    second_config = release_semantic_config(second_settings, "agent_loop")

    assert "dashscope_api_key" not in first_config
    assert "must-not-enter-release" not in repr(first_config)
    first = build_release_manifest(
        "agent_loop", root=tmp_path, semantic_config=first_config
    )
    second = build_release_manifest(
        "agent_loop", root=tmp_path, semantic_config=second_config
    )
    assert first.components["engine_source_sha256"] == second.components["engine_source_sha256"]
    assert first.components["semantic_config_sha256"] != second.components["semantic_config_sha256"]
    assert first.fingerprint() != second.fingerprint()


def test_loaded_skill_or_a2a_schema_changes_release_component(tmp_path):
    _make_synthetic_release_tree(tmp_path, "agent_loop")

    class DiscoveredTool:
        def __init__(self, description: str, schema: dict):
            self.name = "remote_skill"
            self.description = description
            self._skill_id = "skill-42"
            self._raw_schema = schema

        def _get_declaration(self):
            return {
                "name": self.name,
                "description": self.description,
                "parameters_json_schema": self._raw_schema,
            }

    first_catalog = tool_catalog_digest([
        DiscoveredTool("published v1", {"type": "object", "properties": {}})
    ])
    second_catalog = tool_catalog_digest([
        DiscoveredTool(
            "published v2",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
    ])
    assert first_catalog != second_catalog

    first = build_release_manifest(
        "agent_loop",
        root=tmp_path,
        loaded_tool_catalog_sha256=first_catalog,
    )
    second = build_release_manifest(
        "agent_loop",
        root=tmp_path,
        loaded_tool_catalog_sha256=second_catalog,
    )
    assert (
        first.components["loaded_tool_catalog_sha256"]
        != second.components["loaded_tool_catalog_sha256"]
    )
    assert first.components["shared_agent_sha256"] == second.components["shared_agent_sha256"]
    assert first.fingerprint() != second.fingerprint()
