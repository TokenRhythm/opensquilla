from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import TurnRunner
from opensquilla.observability.prompt_report import build_prompt_report
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.session.models import TranscriptEntry
from opensquilla.tools.types import ToolContext


class _NeverCalledProvider:
    provider_name = "never-called"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise AssertionError("artifact tool capability preflight must run before the provider")


class _DoneProvider:
    provider_name = "done"

    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[Any] = []
        self.tools: list[Any] | None = None
        self.config: Any | None = None

    async def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> Any:
        self.calls += 1
        self.messages = list(messages)
        self.tools = tools
        self.config = config
        yield TextDeltaEvent(text="verified")
        yield DoneEvent(stop_reason="end_turn")


async def _unused_tool_handler(call: Any) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content="unused",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        ModelCapabilities(supports_tools=False),
        ModelCapabilities(supports_tools=True),
    ],
)
async def test_artifact_mutation_rejects_unverified_tool_model_before_provider(
    capabilities: ModelCapabilities | None,
) -> None:
    provider = _NeverCalledProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            metadata={"artifact_operation_class": "selection_edit"},
            model_capabilities=capabilities,
        ),
        tool_definitions=[
            ToolDefinition(
                name="document_apply",
                description="Apply granted document mutations.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_unused_tool_handler,
    )

    events = [event async for event in agent.run_turn("apply the annotation")]

    assert provider.calls == 0
    assert any(
        event.kind == "error" and event.code == "artifact_model_tools_unsupported"
        for event in events
    )


@pytest.mark.asyncio
async def test_artifact_mutation_allows_authoritatively_verified_tool_model() -> None:
    provider = _DoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            metadata={"artifact_operation_class": "selection_edit"},
            model_capabilities=ModelCapabilities(supports_tools=True),
            model_tools_capability_verified=True,
        ),
        tool_definitions=[
            ToolDefinition(
                name="document_apply",
                description="Apply granted document mutations.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_unused_tool_handler,
    )

    events = [event async for event in agent.run_turn("apply the annotation")]

    assert provider.calls == 1
    assert not any(
        event.kind == "error" and event.code == "artifact_model_tools_unsupported"
        for event in events
    )


@pytest.mark.asyncio
async def test_restricted_artifact_raw_provider_request_has_no_workspace_or_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext

    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    secret_marker = "RAW_PROVIDER_WORKSPACE_SECRET"
    for filename in ("AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"):
        (tmp_path / filename).write_text(
            f"{secret_marker}:{filename}",
            encoding="utf-8",
        )
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )
    tool_defs = [
        ToolDefinition(
            name="document_inspect",
            description="Inspect the restricted document context.",
            input_schema=ToolInputSchema(properties={}, required=[]),
        )
    ]
    context = ToolContext(
        workspace_dir=str(tmp_path),
        exclusive_tools={"document_inspect"},
    )
    prompt_metadata: dict[str, Any] = {}
    base_prompt = runner._assemble_prompt(
        "main",
        tool_defs,
        session_key="agent:main:webchat:raw-provider-projection",
        prompt_metadata=prompt_metadata,
        bootstrap_context_mode="restricted_tool_boundary",
        workspace_dir=str(tmp_path),
    )
    turn, _ = await runner._run_pipeline(
        "apply the accepted annotation",
        "agent:main:webchat:raw-provider-projection",
        None,
        None,
        tool_defs,
        base_prompt,
        [],
        tool_context=context,
        skill_catalog=SimpleNamespace(
            generation=3,
            skills=(SimpleNamespace(name="must-not-reach-provider"),),
        ),
    )
    turn.metadata.update(prompt_metadata)
    final_prompt, cache_breakpoints, request_context_prompt = (
        runner._resolve_prompt_config(turn)
    )
    report = build_prompt_report(
        turn_id="turn-projection",
        session_key=turn.session_key,
        session_id="session-projection",
        agent_id="main",
        system_prompt=final_prompt,
        tool_defs=turn.tool_defs,
        metadata=turn.metadata,
    )

    provider = _DoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt=final_prompt,
            cache_breakpoints=cache_breakpoints,
            request_context_prompt=request_context_prompt,
            workspace_dir=turn.metadata.get("bootstrap_workspace_dir") or None,
            metadata={
                **turn.metadata,
                "artifact_operation_class": "selection_edit",
            },
            model_capabilities=ModelCapabilities(supports_tools=True),
            model_tools_capability_verified=True,
        ),
        tool_definitions=turn.tool_defs,
        tool_handler=_unused_tool_handler,
        tool_context=context,
    )

    _events = [event async for event in agent.run_turn(turn.message)]

    raw_request = repr((provider.messages, provider.tools, provider.config))
    assert provider.calls == 1
    assert report.injected_workspace_files_count == 0
    assert report.skill_count == 0
    assert secret_marker not in raw_request
    assert str(tmp_path) not in raw_request
    assert "Working directory:" not in raw_request
    assert "## Workspace Files (injected)" not in raw_request
    assert "<available_skills>" not in raw_request
    assert "must-not-reach-provider" not in raw_request
    assert provider.config is not None
    assert provider.config.system == final_prompt


@pytest.mark.asyncio
async def test_restricted_artifact_primary_request_strips_historical_tool_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _DoneProvider()
    compact_calls = 0

    async def forbidden_compaction(_request: Any) -> Any:
        nonlocal compact_calls
        compact_calls += 1
        raise AssertionError("restricted turn invoked an auxiliary compactor")

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        forbidden_compaction,
    )
    context = ToolContext(
        exclusive_tools={"document_inspect"},
        allowed_tools={"document_inspect"},
        surfaced_tools={"document_inspect"},
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            restricted_turn=True,
            context_window_tokens=200_000,
            model_capabilities=ModelCapabilities(supports_tools=True),
            model_tools_capability_verified=True,
        ),
        tool_definitions=[
            ToolDefinition(
                name="document_inspect",
                description="Inspect the restricted document context.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_unused_tool_handler,
        tool_context=context,
    )
    leaked_path = "/private/workspace/restricted-secret.html"
    agent.set_history(
        [
            Message(role="user", content="Earlier ordinary request"),
            Message(
                role="assistant",
                content=[
                    ContentBlockText(text="Earlier safe response"),
                    ContentBlockToolUse(
                        id="historic-command",
                        name="exec_command",
                        input={"cmd": f"cat {leaked_path}"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="historic-command",
                        content=f"contents from {leaked_path}",
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("apply the annotation")]

    raw_request = repr(provider.messages)
    assert provider.calls == 1
    assert compact_calls == 0
    assert leaked_path not in raw_request
    assert "exec_command" not in raw_request
    assert "Earlier safe response" in raw_request
    assert not any(event.kind == "error" for event in events)


@pytest.mark.asyncio
async def test_restricted_artifact_inline_overflow_never_calls_compactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_calls = 0

    async def forbidden_compaction(_request: Any) -> Any:
        nonlocal compact_calls
        compact_calls += 1
        raise AssertionError("restricted turn invoked an auxiliary compactor")

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        forbidden_compaction,
    )
    agent = Agent(
        provider=_DoneProvider(),
        config=AgentConfig(
            restricted_turn=True,
            context_window_tokens=100,
            context_overflow_threshold=0.5,
        ),
        tool_context=ToolContext(exclusive_tools={"document_inspect"}),
    )

    outcome = await agent._check_context_overflow(
        [Message(role="user", content="x" * 800)],
        estimated_context_tokens=200,
        request_context_insert_index=0,
        runtime_context_insert_index=0,
        protected_turn_start_index=1,
    )

    assert outcome is None
    assert compact_calls == 0
    assert agent._last_compaction_refusal_reason == (
        "restricted_turn_compaction_disabled"
    )


@pytest.mark.asyncio
async def test_restricted_history_load_omits_durable_summary_plaintext() -> None:
    leaked_path = "/private/workspace/durable-summary-secret.html"
    session_key = "agent:main:webchat:restricted-summary"
    manager = MagicMock()
    manager.get_transcript = AsyncMock(
        return_value=[
            TranscriptEntry(
                session_id="session-summary",
                session_key=session_key,
                role="system",
                content=f"[Context Summary]\nran exec_command on {leaked_path}",
            ),
            TranscriptEntry(
                session_id="session-summary",
                session_key=session_key,
                role="user",
                content="ordinary retained history",
            ),
        ]
    )
    manager.get_context_states = AsyncMock(return_value=[])
    manager.get_summaries = AsyncMock(return_value=[])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="anthropic")
        config = SimpleNamespace(
            model_capabilities=None,
            preserve_historical_images=False,
            workspace_dir=None,
            materialize_historical_attachments=False,
        )

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

    restricted_agent = _HistoryCapture()
    restricted_summary = await runner._load_history(
        restricted_agent,
        session_key,
        trim_last_user=False,
        restricted_turn=True,
    )

    assert restricted_summary is None
    assert leaked_path not in repr(restricted_agent.history)
    assert "ordinary retained history" in repr(restricted_agent.history)
    manager.get_context_states.assert_not_awaited()
    manager.get_summaries.assert_not_awaited()

    ordinary_agent = _HistoryCapture()
    ordinary_summary = await runner._load_history(
        ordinary_agent,
        session_key,
        trim_last_user=False,
    )

    assert ordinary_summary is not None
    assert leaked_path in ordinary_summary
    manager.get_context_states.assert_awaited()
    manager.get_summaries.assert_awaited()
