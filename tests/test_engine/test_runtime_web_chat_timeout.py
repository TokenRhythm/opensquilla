from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import DoneEvent
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext


@pytest.fixture(autouse=True)
def _clear_runtime_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_TIMEOUT", raising=False)


def _web_context() -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        interaction_mode=InteractionMode.INTERACTIVE,
    )


def _override(
    runner: TurnRunner,
    *,
    explicit: float | None = None,
    tool_context: ToolContext | None = None,
    input_mode: str = "user",
    metadata: dict[str, Any] | None = None,
) -> float | None:
    return runner._web_chat_runtime_timeout_override(
        "agent:main:web-timeout",
        explicit=explicit,
        tool_context=tool_context or _web_context(),
        input_mode=input_mode,
        turn_metadata=metadata or {},
    )


def test_web_chat_runtime_timeout_defaults_to_disabled() -> None:
    config = GatewayConfig()
    runner = TurnRunner(provider_selector=None, config=config)

    assert config.web_chat_runtime_timeout_seconds == 0.0
    assert _override(runner) is None


def test_web_chat_runtime_timeout_keeps_shorter_existing_budget() -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(
            agent_runtime_timeout_seconds=900.0,
            web_chat_runtime_timeout_seconds=1800.0,
        ),
    )

    assert _override(runner) == 900.0


def test_web_chat_runtime_timeout_preserves_disable_semantics() -> None:
    runtime_disabled = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(
            agent_runtime_timeout_seconds=0.0,
            web_chat_runtime_timeout_seconds=1800.0,
        ),
    )
    cap_disabled = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(web_chat_runtime_timeout_seconds=0.0),
    )

    assert _override(runtime_disabled) == 0.0
    assert _override(cap_disabled) is None


def test_explicit_runtime_timeout_has_priority_over_web_cap() -> None:
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())

    assert _override(runner, explicit=2700.0) == 2700.0
    assert _override(runner, explicit=0.0) == 0.0


@pytest.mark.parametrize(
    "case",
    [
        "cli",
        "unattended",
        "system_input",
        "coding_context",
        "coding_metadata",
        "meta_match",
        "meta_launch",
        "meta_resume",
        "meta_replay",
        "meta_replay_error",
    ],
)
def test_web_chat_runtime_timeout_exemptions(case: str) -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(web_chat_runtime_timeout_seconds=1800.0),
    )
    context = _web_context()
    input_mode = "user"
    metadata: dict[str, Any] = {}

    if case == "cli":
        context.caller_kind = CallerKind.CLI
    elif case == "unattended":
        context.interaction_mode = InteractionMode.UNATTENDED
    elif case == "system_input":
        input_mode = "system_event"
    elif case == "coding_context":
        context.coding_mode = True
    elif case == "coding_metadata":
        metadata["coding_mode"] = True
    elif case == "meta_match":
        metadata["meta_match"] = object()
    elif case == "meta_launch":
        metadata["meta_launch"] = {"name": "meta-test"}
    elif case == "meta_resume":
        metadata["meta_resume"] = ("claim", "parsed")
    elif case == "meta_replay":
        metadata["meta_replay"] = {
            "name": "meta-test",
            "run_id": "run-test",
            "mode": "failed-step",
        }
    elif case == "meta_replay_error":
        metadata["meta_replay_error"] = "expired"

    assert (
        _override(
            runner,
            tool_context=context,
            input_mode=input_mode,
            metadata=metadata,
        )
        is None
    )


def test_web_chat_runtime_timeout_rejects_negative_config() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig(web_chat_runtime_timeout_seconds=-1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_kwargs", "expected_timeout"),
    [
        ({}, 48 * 60 * 60),
        ({"web_chat_runtime_timeout_seconds": 1800.0}, 1800.0),
    ],
)
async def test_web_chat_runtime_timeout_reaches_agent_config(
    monkeypatch: pytest.MonkeyPatch,
    config_kwargs: dict[str, Any],
    expected_timeout: float,
) -> None:
    seen_configs: list[dict[str, Any]] = []
    real_agent_config = AgentConfig

    def recording_agent_config(**kwargs: Any) -> AgentConfig:
        seen_configs.append(kwargs)
        return real_agent_config(**kwargs)

    monkeypatch.setattr("opensquilla.engine.types.AgentConfig", recording_agent_config)
    provider = MagicMock(provider_name="stub")

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield DoneEvent()

    provider.chat = _chat
    selector = MagicMock()
    selector.resolve.return_value = provider
    selector.clone.return_value = selector
    selector.current_config = SimpleNamespace(model="stub-model")
    session_manager = MagicMock()
    session_manager.get = AsyncMock(return_value=None)
    session_manager.append_message = AsyncMock(return_value=None)
    session_manager.update = AsyncMock(return_value=None)
    session_manager.get_compaction_summary = AsyncMock(return_value=None)
    session_manager.get_transcript = AsyncMock(return_value=[])
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=session_manager,
        config=GatewayConfig(**config_kwargs),
    )

    async for _ in runner.run(
        message="hi",
        session_key="agent:main:web-timeout",
        tool_context=_web_context(),
        run_kind="client_supplied_anything",
    ):
        pass

    assert any(config.get("timeout") == expected_timeout for config in seen_configs)
