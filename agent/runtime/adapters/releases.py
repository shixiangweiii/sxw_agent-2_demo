"""Deterministic release manifests for recoverable Runtime execution.

A Run can only resume on a Worker whose release fingerprint is identical.  The
fingerprint must therefore cover every local source/config input that can alter
checkpoint interpretation, tool identity, prompts, or committed events.  It is
deliberately broader than Python import dependency analysis: dynamic tool and
Skill discovery makes an explicit, auditable source registry safer.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable, Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

from agent.runtime.domain.models import (
    EngineName,
    ReleaseManifest,
    sha256_json,
)

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

_SEMANTIC_SETTING_NAMES = (
    "llm_model",
    "llm_base_url",
    "max_loop_iters",
    "sub_agent_engine",
    "native_streaming_tool_exec",
    "native_max_tool_concurrency",
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
    "runtime_lease_seconds",
    "runtime_lease_renew_seconds",
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
        "runtime_contract": _RUNTIME_SOURCES,
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


def tool_catalog_snapshot(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Serialize the actual post-discovery Worker tool surface.

    Local source hashing covers implementation.  This snapshot additionally
    pins remote Skill schemas, A2A card locations/descriptions, Claude Skill
    frontmatter, and tool order as they were loaded at Worker startup.
    """

    snapshot: list[dict[str, Any]] = []
    for tool in tools:
        is_function = inspect.isfunction(tool) or inspect.ismethod(tool)
        entry: dict[str, Any] = {
            "name": (
                getattr(tool, "name", None)
                or getattr(tool, "__name__", None)
                or type(tool).__name__
            ),
            "implementation": (
                f"{getattr(tool, '__module__', type(tool).__module__)}."
                f"{getattr(tool, '__qualname__', type(tool).__qualname__)}"
            ),
            "description": (
                getattr(tool, "description", None)
                or inspect.getdoc(tool)
                or ""
            ),
        }
        if is_function:
            entry["signature"] = str(inspect.signature(tool))
        declaration_loader = getattr(tool, "_get_declaration", None)
        if callable(declaration_loader):
            entry["declaration"] = _to_json_value(declaration_loader())

        metadata: dict[str, Any] = {}
        for attribute in (
            "_skill_id",
            "_tool_name",
            "_raw_schema",
            "concurrency_safe",
            "exclusive_resources",
        ):
            if hasattr(tool, attribute):
                metadata[attribute.lstrip("_")] = _to_json_value(
                    getattr(tool, attribute)
                )
        skill = getattr(tool, "_skill", None)
        if skill is not None:
            metadata["claude_skill"] = {
                key: _to_json_value(getattr(skill, key))
                for key in (
                    "skill_id",
                    "name",
                    "description",
                    "parallel_safe",
                    "exclusive_resources",
                )
            }
        agent = getattr(tool, "agent", None)
        if agent is not None:
            metadata["agent"] = {
                "implementation": f"{type(agent).__module__}.{type(agent).__qualname__}",
                "name": getattr(agent, "name", None),
                "description": getattr(agent, "description", None),
                "agent_card_source": getattr(agent, "_agent_card_source", None),
                "agent_card": _to_json_value(getattr(agent, "_agent_card", None)),
            }
        if metadata:
            entry["metadata"] = metadata
        snapshot.append(entry)
    return snapshot


def tool_catalog_digest(tools: Iterable[Any]) -> str:
    return sha256_json(tool_catalog_snapshot(tools))


def build_release_manifest(
    engine: EngineName,
    *,
    root: str | Path = _ROOT,
    semantic_config: Mapping[str, Any] | None = None,
    loaded_tool_catalog_sha256: str | None = None,
) -> ReleaseManifest:
    paths = release_source_paths(engine, root=root)
    components = {
        f"{group}_sha256": source_digest(root, group_paths)
        for group, group_paths in paths.items()
    }
    components.update({
        "semantic_config_sha256": sha256_json(dict(semantic_config or {})),
        "loaded_tool_catalog_sha256": (
            loaded_tool_catalog_sha256 or sha256_json([])
        ),
        "google_adk": _installed_version("google-adk", "2.6.2"),
        "a2a_sdk": _installed_version("a2a-sdk", "1.1.2"),
        "litellm": _installed_version("litellm", "unknown"),
        "google_genai": _installed_version("google-genai", "unknown"),
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


def _installed_version(distribution: str, fallback: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return fallback


def _to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_json_value(model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {
            str(key): _to_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_to_json_value(item) for item in value), key=repr)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if hasattr(value, "__dict__"):
        public = {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
        if public:
            return _to_json_value(public)
    return str(value)
