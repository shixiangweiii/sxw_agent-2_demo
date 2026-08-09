"""Exact-match registry for explicit checkpoint upgraders."""
from __future__ import annotations

from collections.abc import Mapping

from agent.runtime.ports.release_compatibility import (
    CheckpointUpgrader,
    CheckpointUpgradeKey,
)


class ReleaseCompatibilityRegistry:
    """A closed, version-to-version map; there is no transitive best effort."""

    def __init__(
        self,
        upgraders: Mapping[CheckpointUpgradeKey, CheckpointUpgrader] | None = None,
    ) -> None:
        self._upgraders = dict(upgraders or {})

    def register(
        self,
        key: CheckpointUpgradeKey,
        upgrader: CheckpointUpgrader,
    ) -> None:
        if key in self._upgraders:
            raise ValueError(f"checkpoint upgrader already registered: {key!r}")
        self._upgraders[key] = upgrader

    def get(self, key: CheckpointUpgradeKey) -> CheckpointUpgrader | None:
        return self._upgraders.get(key)

