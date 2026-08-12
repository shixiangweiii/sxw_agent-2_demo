"""Strict, immutable current Tool catalog.

Catalog construction is a Worker startup boundary.  A duplicate name, broken
declaration, invalid Draft 2020-12 schema, or non-deterministic release input
is a startup error; there is no partial catalog mode after a source returned
successfully.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from agent.runtime.application.tool_outputs import ToolResultAdapter
from agent.runtime.domain.models import ToolManifest, canonical_json, sha256_json

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class ToolBinding:
    name: str
    description: str
    parameters: Mapping[str, Any]
    manifest: ToolManifest
    executor: Any = field(compare=False, repr=False)
    result_adapter: ToolResultAdapter = field(compare=False, repr=False)
    implementation: str

    def __post_init__(self) -> None:
        name = self.name.strip()
        description = self.description.strip()
        if not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not description:
            raise ValueError(f"{name}: description must be non-empty")
        if self.manifest.name != name:
            raise ValueError(f"{name}: ToolManifest name mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest.release_digest):
            raise ValueError(f"{name}: tool release digest must be SHA-256")
        if (
            self.manifest.concurrency_safe
            and self.manifest.effect_class.value != "READ_ONLY"
        ):
            raise ValueError(f"{name}: only READ_ONLY tools may be concurrency_safe")
        if len(set(self.manifest.exclusive_resources)) != len(
            self.manifest.exclusive_resources
        ):
            raise ValueError(f"{name}: exclusive_resources must be unique")
        if not callable(self.executor):
            raise TypeError(f"{name}: executor must be callable")
        if not callable(self.result_adapter):
            raise TypeError(f"{name}: result_adapter must be callable")
        if not self.implementation:
            raise ValueError(f"{name}: implementation identity must be non-empty")
        normalized = normalize_parameter_schema(dict(self.parameters), tool_name=name)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "parameters", _freeze_json(normalized))

    def release_snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameter_schema(),
            "manifest": self.manifest.model_dump(mode="json"),
            "implementation": self.implementation,
            "result_adapter": callable_identity(self.result_adapter),
        }

    def parameter_schema(self) -> dict[str, Any]:
        """Return the normalized schema as mutable JSON values.

        The catalog freezes nested lists as tuples. Consumers must compare
        against this fully thawed view instead of copying only the root mapping,
        which reports false drift for schema arrays such as required.
        """

        return _thaw_json(self.parameters)


class ToolCatalog:
    def __init__(self, bindings: Iterable[ToolBinding], *, max_bytes: int = 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("tool catalog max_bytes must be positive")
        by_name: dict[str, ToolBinding] = {}
        ordered: list[ToolBinding] = []
        for binding in bindings:
            if binding.name in by_name:
                raise ValueError(f"duplicate tool name: {binding.name}")
            by_name[binding.name] = binding
            ordered.append(binding)
        snapshot = [binding.release_snapshot() for binding in ordered]
        encoded = canonical_json(snapshot).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(
                f"tool catalog exceeds {max_bytes} UTF-8 bytes: {len(encoded)}"
            )
        self._ordered = tuple(ordered)
        self._by_name = MappingProxyType(by_name)
        self._snapshot_json = encoded.decode("utf-8")
        self._digest = sha256_json(snapshot)
        self._size_bytes = len(encoded)

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    def get(self, name: str) -> ToolBinding | None:
        return self._by_name.get(name)

    def require(self, name: str) -> ToolBinding:
        binding = self.get(name)
        if binding is None:
            raise KeyError(name)
        return binding

    def all(self) -> tuple[ToolBinding, ...]:
        return self._ordered

    def snapshot(self) -> list[dict[str, Any]]:
        return json.loads(self._snapshot_json)

    def wire_declarations(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": binding.name,
                    "description": binding.description,
                    "parameters": binding.parameter_schema(),
                },
            }
            for binding in self._ordered
        ]

    def __len__(self) -> int:
        return len(self._ordered)


def normalize_parameter_schema(schema: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise TypeError(f"{tool_name}: parameters schema must be an object")
    normalized = _normalize_schema_value(schema)
    if normalized.get("type") != "object":
        raise ValueError(f"{tool_name}: parameters schema root type must be object")
    properties = normalized.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"{tool_name}: parameters schema requires object properties")
    try:
        Draft202012Validator.check_schema(normalized)
    except Exception as exc:
        raise ValueError(f"{tool_name}: invalid Draft 2020-12 parameters schema") from exc
    # Round-trip catches values such as Python classes that jsonschema itself
    # may ignore as annotations but cannot enter an immutable release manifest.
    canonical_json(normalized)
    return normalized


def callable_identity(value: Any) -> str:
    module = getattr(value, "__module__", type(value).__module__)
    qualname = getattr(value, "__qualname__", type(value).__qualname__)
    return f"{module}.{qualname}"


def tool_release_digest(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any],
    policy: Mapping[str, Any],
    executor: Any,
    result_adapter: ToolResultAdapter,
) -> str:
    return sha256_json({
        "name": name,
        "description": description,
        "parameters": dict(parameters),
        "policy": dict(policy),
        "implementation": callable_identity(executor),
        "result_adapter": callable_identity(result_adapter),
    })


def _normalize_schema_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_schema_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_schema_value(item) for item in value]
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _normalize_schema_value(model_dump(mode="json", exclude_none=True))
    raise TypeError(f"JSON Schema contains non-JSON value {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "ToolBinding",
    "ToolCatalog",
    "callable_identity",
    "normalize_parameter_schema",
    "tool_release_digest",
]
