from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path, PurePosixPath

import pytest

import agent.runtime.adapters.releases as releases_module
from agent.config import AgentSettings
from agent.runtime.adapters.releases import (
    build_release_manifest,
    release_semantic_config,
    release_source_paths,
    release_source_specs,
)
from agent.engine.native_loop.tools import ToolRegistry, ToolSpec
from agent.runtime.adapters.brokered_tools import build_runtime_tool_catalog
from agent.runtime.domain.models import sha256_json
from common.sqlite_schema import schema_digest


EMPTY_CATALOG_DIGEST = sha256_json([])


def _normalize_distribution_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def _locked_runtime_requirements() -> dict[str, str]:
    locked: dict[str, str] = {}
    requirements_path = Path(__file__).resolve().parents[2] / "requirements.txt"
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        requirement = line.partition("#")[0].strip()
        if not requirement:
            continue
        assert requirement.count("==") == 1, (
            f"runtime dependency must use one exact == pin: {requirement}"
        )
        name_and_extras, locked_version = requirement.split("==", 1)
        distribution = _normalize_distribution_name(name_and_extras.split("[", 1)[0])
        assert distribution not in locked, f"duplicate runtime dependency: {distribution}"
        assert locked_version, f"runtime dependency has an empty pin: {distribution}"
        locked[distribution] = locked_version
    return locked


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

    runtime = set(groups["runtime_source"])
    assert {
        "agent/runtime/domain/models.py",
        "agent/runtime/ports/engine.py",
        "agent/runtime/application/coordinator.py",
        "agent/runtime/application/events.py",
        "agent/runtime/application/tool_broker.py",
        "agent/runtime/adapters/adk_engines.py",
        "agent/runtime/adapters/brokered_tools.py",
        "agent/runtime/adapters/releases.py",
        "agent/runtime/adapters/sqlite/store.py",
        "agent/runtime/adapters/sqlite/schema.sql",
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
    schema = root / "agent/runtime/adapters/sqlite/schema.sql"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text("CREATE TABLE schema_meta (id INTEGER PRIMARY KEY);\n", encoding="utf-8")


def test_source_content_and_directory_membership_change_manifest_digest(tmp_path):
    _make_synthetic_release_tree(tmp_path, "native_loop")
    baseline = build_release_manifest(
        "native_loop", root=tmp_path, semantic_config={},
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )
    assert baseline.components["runtime_schema_digest"] == schema_digest(
        (tmp_path / "agent/runtime/adapters/sqlite/schema.sql").read_bytes()
    )

    builtin = tmp_path / "agent/tools/builtin_tools.py"
    builtin.write_text("BUILTINS = 2\n", encoding="utf-8")
    content_changed = build_release_manifest(
        "native_loop", root=tmp_path, semantic_config={},
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )

    assert content_changed.fingerprint() != baseline.fingerprint()
    assert (
        content_changed.components["shared_agent_sha256"]
        != baseline.components["shared_agent_sha256"]
    )
    for stable_group in (
        "engine_source_sha256",
        "runtime_source_sha256",
        "skill_a2a_integrations_sha256",
        "dependency_lock_sha256",
    ):
        assert content_changed.components[stable_group] == baseline.components[stable_group]

    (tmp_path / "agent/tools/new_runtime_tool.py").write_text(
        "NEW_TOOL = True\n", encoding="utf-8"
    )
    membership_changed = build_release_manifest(
        "native_loop", root=tmp_path, semantic_config={},
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )
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
        "agent_loop", root=tmp_path, semantic_config=first_config,
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )
    second = build_release_manifest(
        "agent_loop", root=tmp_path, semantic_config=second_config,
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )
    assert first.components["engine_source_sha256"] == second.components["engine_source_sha256"]
    assert first.components["semantic_config_sha256"] != second.components["semantic_config_sha256"]
    assert first.fingerprint() != second.fingerprint()


def test_loaded_skill_or_a2a_schema_changes_release_component(tmp_path):
    _make_synthetic_release_tree(tmp_path, "agent_loop")

    async def invoke(_args, _context):
        return None

    def catalog_digest(description: str, parameters: dict) -> str:
        return build_runtime_tool_catalog(ToolRegistry([ToolSpec(
            name="remote_skill",
            description=description,
            parameters=parameters,
            run=invoke,
            implementation="remote.skill-42",
        )])).digest

    first_catalog = catalog_digest(
        "published v1", {"type": "object", "properties": {}},
    )
    second_catalog = catalog_digest(
        "published v2",
        {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
        },
    )
    assert first_catalog != second_catalog

    first = build_release_manifest(
        "agent_loop",
        root=tmp_path,
        semantic_config={},
        loaded_tool_catalog_sha256=first_catalog,
    )
    second = build_release_manifest(
        "agent_loop",
        root=tmp_path,
        semantic_config={},
        loaded_tool_catalog_sha256=second_catalog,
    )
    assert (
        first.components["loaded_tool_catalog_sha256"]
        != second.components["loaded_tool_catalog_sha256"]
    )
    assert first.components["shared_agent_sha256"] == second.components["shared_agent_sha256"]
    assert first.fingerprint() != second.fingerprint()


def test_missing_installed_dependency_metadata_fails_release_construction(
    tmp_path, monkeypatch,
):
    _make_synthetic_release_tree(tmp_path, "native_loop")

    def missing(_distribution: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(releases_module, "version", missing)
    with pytest.raises(RuntimeError, match="RELEASE_DEPENDENCY_METADATA_MISSING"):
        build_release_manifest(
            "native_loop",
            root=tmp_path,
            semantic_config={},
            loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
        )


def test_runtime_dependency_lock_matches_release_registry_and_installed_metadata(
    tmp_path,
):
    locked = _locked_runtime_requirements()
    registered = {
        _normalize_distribution_name(distribution): component
        for component, distribution in releases_module._RUNTIME_DISTRIBUTIONS
    }
    assert set(locked) == set(registered)

    _make_synthetic_release_tree(tmp_path, "native_loop")
    manifest = build_release_manifest(
        "native_loop",
        root=tmp_path,
        semantic_config={},
        loaded_tool_catalog_sha256=EMPTY_CATALOG_DIGEST,
    )
    for distribution, component in registered.items():
        assert installed_version(distribution) == locked[distribution]
        assert (
            manifest.components[f"installed_dependency_{component}"]
            == locked[distribution]
        )
