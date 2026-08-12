"""Deterministic release manifests for recoverable Runtime execution.

A Run can only resume on a Worker whose release fingerprint is identical.  The
fingerprint must therefore cover every local source/config input that can alter
checkpoint interpretation, tool identity, prompts, or committed events.  It is
deliberately broader than Python import dependency analysis: dynamic tool and
Skill discovery makes an explicit, auditable source registry safer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

from agent.runtime.domain.models import (
    EngineName,
    ReleaseManifest,
    sha256_json,
)
from common.sqlite_schema import schema_digest

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_DIGEST_VERSION = b"sxw-release-source-v2\0"

_ENGINE_SOURCES: dict[str, tuple[str, ...]] = {
    "plan_execute": ("agent/engine/plan_execute",),
    "agent_loop": ("agent/engine/agent_loop",),
    "native_loop": ("agent/engine/native_loop",),
}

# Shared prompt/tool construction is included for every engine.  This is
# intentionally conservative: plan_execute does not call every loop-only tool,
# but it shares the same Worker tool catalog and can delegate to sub-agents.
_SHARED_AGENT_SOURCES = (
    "agent/engine/base.py",
    "agent/engine/loop_tools",
    "agent/llm",
    "agent/plugins",
    "agent/tools",  # includes builtin_tools.py and knowledge_search.py
    "agent/skills",
    "agent/a2a",
    "agent/claude_skill",  # code + complete skills_data packages/assets
    "agent/context.py",
    "agent/config.py",
    "agent/asyncio_utils.py",
    "agent/tool_args_contract.py",
    "agent/stream/event_converters.py",
)

_RUNTIME_SOURCES = (
    # Includes domain/ports/application, API admission/projection, SQLite SQL,
    # adapters (including this registry), and Worker recovery/lease behavior.
    "agent/runtime",
)

_INTEGRATION_SOURCES = (
    "common/skill_contract.py",
    "skillcenter",
    "a2a_service",
)

_DEPENDENCY_SOURCES = ("requirements.txt",)

# Release identity covers the project's explicit runtime distributions, not an
# environment-wide ``pip freeze``.  The latter contains platform-dependent
# transitive packages (for example uvloop on only some hosts) and would make an
# otherwise equivalent release depend on unrelated machine state.  Keep this
# registry in one-to-one sync with the exact direct pins in requirements.txt.
_RUNTIME_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("google_adk", "google-adk"),
    ("a2a_sdk", "a2a-sdk"),
    ("litellm", "litellm"),
    ("google_genai", "google-genai"),
    ("fastapi", "fastapi"),
    ("starlette", "starlette"),
    ("uvicorn", "uvicorn"),
    ("python_multipart", "python-multipart"),
    ("aiosqlite", "aiosqlite"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("python_dotenv", "python-dotenv"),
    ("pyyaml", "PyYAML"),
    ("jsonschema", "jsonschema"),
    ("openai", "openai"),
    ("numpy", "numpy"),
    ("rank_bm25", "rank-bm25"),
    ("jieba", "jieba"),
)

_SEMANTIC_SETTING_NAMES = (
    "llm_model",
    "llm_base_url",
    "embedding_model",
    "max_loop_iters",
    "sub_agent_engine",
    "native_early_tool_dispatch",
    "native_max_tool_concurrency",
    "native_max_tool_calls_per_turn",
    "native_max_tool_calls_per_run",
    "native_max_tool_argument_bytes",
    "native_max_tool_batch_argument_bytes",
    "native_max_model_output_bytes",
    "native_max_checkpoint_bytes",
    "native_max_tool_catalog_bytes",
    "native_max_skill_event_bytes",
    "native_max_skill_events_per_run",
    "native_max_skill_event_bytes_per_run",
    "native_tool_result_max_chars",
    "context_window_tokens",
    "compact_buffer_tokens",
    "compact_preserve_units",
    "arag_base_url",
    "arag_timeout_ms",
    "skill_center_base_url",
    "skill_center_timeout_ms",
    "skill_center_stream_timeout_ms",
    "sandbox_provider",
    "skill_call_timeout_seconds",
    "skill_max_llm_calls",
    "skill_max_parallel_calls",
    "skill_result_max_chars",
    "runtime_event_flush_ms",
    "runtime_event_flush_bytes",
    "runtime_worker_concurrency",
    "runtime_lease_seconds",
    "runtime_lease_renew_seconds",
    "runtime_default_deadline_seconds",
)

_IGNORED_DIRECTORY_NAMES = frozenset({
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
})
_IGNORED_FILE_SUFFIXES = frozenset({
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
})


def release_source_specs(engine: EngineName) -> dict[str, tuple[str, ...]]:
    """Return the explicit source registry before directory expansion."""

    return {
        "engine_source": _ENGINE_SOURCES[engine],
        "shared_agent": _SHARED_AGENT_SOURCES,
        "runtime_source": _RUNTIME_SOURCES,
        "skill_a2a_integrations": _INTEGRATION_SOURCES,
        "dependency_lock": _DEPENDENCY_SOURCES,
    }


def collect_source_paths(
    root: str | Path,
    specs: Iterable[str],
) -> tuple[str, ...]:
    """Expand required files/directories into stable repository-relative paths.

    Missing/empty roots and symlinks fail fast instead of silently producing a
    partial release. Generated caches, local databases, logs and ``.env`` files
    are excluded so a secret or mutable local state never enters a manifest.
    """

    source_root = Path(root).resolve()
    collected: set[str] = set()
    for raw_spec in specs:
        relative = _validate_relative_path(raw_spec)
        target = source_root / relative
        if target.is_symlink():
            raise RuntimeError(f"release source cannot be a symlink: {relative}")
        if not target.exists():
            raise FileNotFoundError(f"required release source is missing: {relative}")
        if target.is_file():
            if _include_file(target, source_root):
                collected.add(relative)
            else:
                raise RuntimeError(f"required release source was excluded: {relative}")
            continue
        if not target.is_dir():
            raise RuntimeError(f"release source is not a regular file/directory: {relative}")
        before = len(collected)
        for candidate in target.rglob("*"):
            if candidate.is_symlink():
                raise RuntimeError(
                    "release source tree cannot contain symlinks: "
                    f"{candidate.relative_to(source_root).as_posix()}"
                )
            if candidate.is_file() and _include_file(candidate, source_root):
                collected.add(candidate.relative_to(source_root).as_posix())
        if len(collected) == before:
            raise RuntimeError(f"release source directory is empty: {relative}")
    return tuple(sorted(collected))


def release_source_paths(
    engine: EngineName,
    *,
    root: str | Path = _ROOT,
) -> dict[str, tuple[str, ...]]:
    """Return the exact files contributing to each manifest digest group."""

    return {
        group: collect_source_paths(root, specs)
        for group, specs in release_source_specs(engine).items()
    }


def source_digest(root: str | Path, relative_paths: Iterable[str]) -> str:
    """Hash path + content with unambiguous framing and deterministic ordering."""

    source_root = Path(root).resolve()
    digest = hashlib.sha256()
    digest.update(_SOURCE_DIGEST_VERSION)
    normalized = sorted({_validate_relative_path(path) for path in relative_paths})
    if not normalized:
        raise ValueError("release source digest cannot be empty")
    for relative in normalized:
        path = source_root / relative
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"release source is not a regular file: {relative}")
        path_bytes = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def release_semantic_config(settings: Any, engine: EngineName) -> dict[str, Any]:
    """Select non-secret environment settings that change execution semantics."""

    values: dict[str, Any] = {"engine": engine}
    for name in _SEMANTIC_SETTING_NAMES:
        if not hasattr(settings, name):
            raise AttributeError(f"release semantic setting is missing: {name}")
        values[name] = getattr(settings, name)
    return values


def build_release_manifest(
    engine: EngineName,
    *,
    root: str | Path = _ROOT,
    semantic_config: Mapping[str, Any],
    loaded_tool_catalog_sha256: str,
) -> ReleaseManifest:
    if len(loaded_tool_catalog_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in loaded_tool_catalog_sha256
    ):
        raise ValueError("loaded ToolCatalog digest must be lowercase SHA-256")
    paths = release_source_paths(engine, root=root)
    components = {
        f"{group}_sha256": source_digest(root, group_paths)
        for group, group_paths in paths.items()
    }
    schema_path = Path(root).resolve() / "agent/runtime/adapters/sqlite/schema.sql"
    model_provider_policy = {
        key: semantic_config[key]
        for key in ("llm_model", "llm_base_url")
        if key in semantic_config
    }
    components.update({
        "runtime_schema_digest": schema_digest(schema_path.read_bytes()),
        "semantic_config_sha256": sha256_json(dict(semantic_config)),
        "model_provider_policy_sha256": sha256_json(model_provider_policy),
        "checkpoint_codec_sha256": sha256_json({
            "engine": engine,
            "engine_source_sha256": components["engine_source_sha256"],
            "runtime_source_sha256": components["runtime_source_sha256"],
        }),
        "loaded_tool_catalog_sha256": loaded_tool_catalog_sha256,
    })
    components.update({
        f"installed_dependency_{component}": _installed_version(distribution)
        for component, distribution in _RUNTIME_DISTRIBUTIONS
    })
    return ReleaseManifest(engine=engine, components=components)


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"release source path must be repository-relative: {value!r}")
    return path.as_posix()


def _include_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _IGNORED_DIRECTORY_NAMES for part in relative.parts):
        return False
    name = path.name
    if name == ".DS_Store" or name == ".env" or name.startswith(".env."):
        return False
    return path.suffix.lower() not in _IGNORED_FILE_SUFFIXES


def _installed_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "RELEASE_DEPENDENCY_METADATA_MISSING: "
            f"installed distribution metadata is unavailable for {distribution}"
        ) from exc
