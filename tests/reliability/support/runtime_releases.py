"""Test-only construction of the mandatory three-engine current release set."""
from __future__ import annotations

from agent.runtime.domain.models import ReleaseManifest


async def activate_test_release(store, manifest: ReleaseManifest) -> str:
    marker = manifest.fingerprint()
    manifests = tuple(
        manifest
        if engine == manifest.engine
        else ReleaseManifest(
            engine=engine,
            components={"test_peer_for": marker},
        )
        for engine in ("plan_execute", "agent_loop", "native_loop")
    )
    releases = await store.activate_current_releases(manifests)
    return releases[manifest.engine]


async def activate_test_releases(
    store,
    *,
    marker: str,
) -> dict[str, str]:
    manifests = tuple(
        ReleaseManifest(engine=engine, components={"test": marker})
        for engine in ("plan_execute", "agent_loop", "native_loop")
    )
    return await store.activate_current_releases(manifests)


__all__ = ["activate_test_release", "activate_test_releases"]
