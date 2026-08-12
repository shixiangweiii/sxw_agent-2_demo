from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.config import AgentSettings
from agent.runtime.adapters.releases import release_semantic_config


_RESOURCE_SETTING_NAMES = (
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
    "max_loop_iters",
)


def test_native_resource_limit_defaults_match_the_current_runtime_contract() -> None:
    settings = AgentSettings(_env_file=None)

    assert settings.native_early_tool_dispatch == "off"
    assert settings.native_max_tool_concurrency == 10
    assert settings.native_max_tool_calls_per_turn == 64
    assert settings.native_max_tool_calls_per_run == 256
    assert settings.native_max_tool_argument_bytes == 64 * 1024
    assert settings.native_max_tool_batch_argument_bytes == 256 * 1024
    assert settings.native_max_model_output_bytes == 1024 * 1024
    assert settings.native_max_checkpoint_bytes == 2 * 1024 * 1024
    assert settings.native_max_tool_catalog_bytes == 1024 * 1024
    assert settings.native_max_skill_event_bytes == 64 * 1024
    assert settings.native_max_skill_events_per_run == 2000
    assert settings.native_max_skill_event_bytes_per_run == 8 * 1024 * 1024
    assert settings.max_loop_iters == 8

    release_config = release_semantic_config(settings, "native_loop")
    assert {
        name: release_config[name]
        for name in _RESOURCE_SETTING_NAMES
    } == {
        name: getattr(settings, name)
        for name in _RESOURCE_SETTING_NAMES
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_loop_iters": 0}, "greater than 0"),
        (
            {"context_window_tokens": 100, "compact_buffer_tokens": 100},
            "compact_buffer_tokens must be smaller than context_window_tokens",
        ),
        (
            {
                "native_max_tool_concurrency": 3,
                "native_max_tool_calls_per_turn": 2,
            },
            "native_max_tool_concurrency cannot exceed native_max_tool_calls_per_turn",
        ),
        (
            {
                "native_max_tool_argument_bytes": 9,
                "native_max_tool_batch_argument_bytes": 8,
            },
            "native_max_tool_batch_argument_bytes cannot be smaller than the per-call limit",
        ),
    ],
    ids=[
        "positive-model-loop-cap",
        "compact-buffer-below-window",
        "concurrency-below-turn-call-cap",
        "batch-arguments-at-least-one-call",
    ],
)
def test_native_resource_cross_validation_fails_worker_settings_at_startup(
    changes: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentSettings(_env_file=None, **changes)
