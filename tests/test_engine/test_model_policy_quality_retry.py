from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from tests.test_engine.test_model_router_execution import execution as execution


class _QualityProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]], *, accept_retry: bool = True) -> None:
        self.streams = streams
        self.accept_retry = accept_retry
        self.calls: list[dict[str, Any]] = []
        self.quality_calls: list[dict[str, Any]] = []
        self.timeline: list[str] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": copy.deepcopy(messages), "config": config})
        self.timeline.append("chat")
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def prepare_quality_retry(
        self,
        reason: str,
        scope_id: str,
        remaining_budget: int,
        messages: Sequence[Message],
        chat_config: ChatConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        context_overflow_threshold: float | None = None,
    ) -> dict[str, Any] | None:
        self.quality_calls.append(
            {
                "reason": reason,
                "scope_id": scope_id,
                "remaining_budget": remaining_budget,
                "messages": copy.deepcopy(messages),
                "chat_config": chat_config,
                "tools": tools,
            }
        )
        self.timeline.append("quality")
        if not self.accept_retry:
            return None
        return {"provider_id": "fake", "model_id": "quality-c2"}

    async def list_models(self) -> list[Any]:
        return []


def _tool_stream(index: int, *, value: str | None = None) -> list[Any]:
    tool_use_id = f"tool-{index}"
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={"value": str(index) if value is None else value},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _final_stream() -> list[Any]:
    return [ProviderText(text="done"), ProviderDone(input_tokens=5, output_tokens=1)]


def _agent(
    provider: _QualityProvider,
    *,
    budget: int = 1,
    validation_reason: str | None = "invalid_tool_arguments",
    fixed_route: bool = True,
    explicit_correction: bool = False,
    repeat_threshold: int = 0,
) -> Agent:
    metadata: dict[str, Any] = {}
    if fixed_route:
        metadata.update(router_tier_policy="model", routing_applied=True, routed_tier="c1")
    if explicit_correction:
        metadata["_fixed_four_tier_explicit_correction"] = True

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="invalid arguments" if validation_reason else "tool ok",
            is_error=validation_reason is not None,
            execution_status=(
                {
                    "reason": validation_reason,
                    "source": "tool_runtime",
                    "timed_out": False,
                    "truncated": False,
                    "exit_code": None,
                    "preservation_class": "diagnostic",
                    "status": "error",
                    "version": 1,
                }
                if validation_reason is not None
                else None
            ),
        )

    return Agent(
        provider=provider,
        config=AgentConfig(
            model_id="quality-c1",
            provider_id="fake",
            metadata=metadata,
            max_iterations=4,
            max_provider_retries=budget,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            repeated_tool_call_recovery_threshold=repeat_threshold,
            repeated_tool_call_recovery_extra_tools=("echo",),
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_arguments",
        "retryable_tool_input_error",
        "schema_validation_failed",
        "output_validation_failed",
        "invalid_tool_arguments",
        "invalid_json",
        "format_validation_failed",
    ],
)
async def test_real_agent_validation_failure_calls_quality_retry_once(reason: str) -> None:
    provider = _QualityProvider([_tool_stream(1), _tool_stream(2), _final_stream()])
    agent = _agent(provider, validation_reason=reason)

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.quality_calls) == 1
    call = provider.quality_calls[0]
    assert call["reason"] == "validation_failure"
    assert call["remaining_budget"] == 1
    assert call["scope_id"]
    assert any(
        getattr(block, "type", "") == "tool_result"
        for message in call["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert agent.config.model_id == "quality-c2"
    assert len(provider.calls) == 3
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_quality_retry_admits_the_same_assembled_messages_as_dispatch() -> None:
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    agent = _agent(provider)
    agent.config.request_context_prompt = "request-only context"

    events = [event async for event in agent.run_turn("hello")]

    admitted = provider.quality_calls[0]
    assert admitted["messages"] == provider.calls[1]["messages"]
    contents = str(admitted["messages"])
    assert "request-only context" in contents
    assert "Runtime context for this turn" in contents
    assert admitted["tools"] == agent.tool_definitions
    assert any(event.kind == "done" for event in events)


@pytest.mark.parametrize("context_kind", ["request", "runtime"])
async def test_quality_retry_large_transient_context_does_not_commit_or_activate(
    execution, monkeypatch, context_kind,
) -> None:
    import opensquilla.engine.model_router_quality as quality
    from opensquilla.engine.runtime import _SelectorFallbackProvider

    e = execution
    e.turn.config.llm.thinking = "off"
    e.turn.config.llm.max_tokens = 256
    e.turn.config.squilla_router.auto_thinking = False
    before = copy.deepcopy(e.turn.metadata["router_model_decision"])
    advance = AsyncMock(wraps=e.manager.advance_model_router_quality_retry)
    monkeypatch.setattr(e.manager, "advance_model_router_quality_retry", advance)
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    wrapper = _SelectorFallbackProvider(provider, e.selector, e.turn.metadata)
    agent = _agent(provider)
    agent.provider = wrapper
    agent.config.metadata = e.turn.metadata
    agent.config.model_id = e.turn.model
    agent.config.provider_id = "openrouter"
    agent.config.context_window_tokens = 64000
    agent.config.context_window_known = True
    agent.config.max_tokens = 256
    agent.config.thinking = False
    large_context = "extra context " * 1500
    if context_kind == "request":
        agent.config.request_context_prompt = large_context
    else:
        monkeypatch.setattr(agent, "_runtime_context_block", lambda: large_context)
    monkeypatch.setattr(
        quality, "resolve_effective_context_window", lambda *a, **k: (4096, "catalog"),
    )
    # A broken early admission must fail locally, never call a live provider.
    monkeypatch.setattr(type(e.provider), "chat", lambda *a, **k: provider._stream(_final_stream()))
    projected = []
    project = quality.project_provider_final_request

    def capture(candidate, messages, tools, config):
        result = project(candidate, messages, tools, config)
        small = project(candidate, [Message(role="user", content="hello")], tools, config)
        projected.append((copy.deepcopy(messages), result, small))
        assert config.thinking is False
        assert config.thinking_budget_tokens == 0
        # This tool failure crossed the barrier since the preceding physical
        # request; preview must already see the current iteration's policy.
        assert wrapper._image_probe_forbidden is True
        return result

    monkeypatch.setattr(quality, "project_provider_final_request", capture)
    events = [event async for event in agent.run_turn("hello")]

    assert len(projected) == 1
    messages, projection, small_projection = projected[0]
    assert large_context.strip() in str(messages)
    assert small_projection is not None and small_projection.fits
    assert projection is not None and not projection.fits
    advance.assert_not_awaited()
    assert e.turn.metadata["router_model_decision"] == before
    assert e.selector.current_config.model == e.turn.model == "qwen/qwen3.7-flash"
    assert wrapper._provider is provider
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    assert rows[0].turn_context["router_model_decision"] == before
    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)


async def test_real_agent_no_progress_calls_quality_retry_once() -> None:
    provider = _QualityProvider(
        [
            _tool_stream(1, value="same"),
            _tool_stream(2, value="same"),
            _tool_stream(3, value="same"),
            _final_stream(),
        ]
    )
    agent = _agent(provider, validation_reason=None, repeat_threshold=2)

    events = [event async for event in agent.run_turn("hello")]

    assert [call["reason"] for call in provider.quality_calls] == ["no_progress"]
    assert any(
        event.kind == "warning" and event.code == "repeated_tool_call_recovery" for event in events
    )
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.parametrize("synthetic", [False, True])
async def test_repeated_tools_with_real_writes_remain_productive(monkeypatch, synthetic):
    provider = _QualityProvider(
        [*[_tool_stream(index, value="same") for index in range(3)], _final_stream()]
    )
    agent = _agent(provider, validation_reason=None, repeat_threshold=2)
    writes = []
    original_handler = agent.tool_handler

    async def write_tool(call):
        result = await original_handler(call)
        name = "debug_marker" if synthetic else "result"
        writes.append({"relative_path": f"{name}_{len(writes)}.txt", "created": True})
        return result

    agent.tool_handler = write_tool
    monkeypatch.setattr(agent, "_workspace_write_records", lambda: writes)
    events = [event async for event in agent.run_turn("hello")]

    assert len(writes) == (1 if synthetic else 3)
    assert len(provider.quality_calls) == int(synthetic)
    assert any(
        event.kind == "warning" and event.code == "repeated_tool_call_recovery"
        for event in events
    ) is synthetic
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.parametrize("code", ["429", "timeout"])
async def test_provider_infrastructure_errors_do_not_call_quality_retry(code: str) -> None:
    provider = _QualityProvider(
        [
            [
                ProviderError(
                    message="upstream unavailable",
                    code=code,
                )
            ],
            _final_stream(),
        ]
    )

    events = [event async for event in _agent(provider).run_turn("hello")]

    assert provider.quality_calls == []
    assert events


async def test_zero_retry_budget_disables_quality_retry() -> None:
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    agent = _agent(provider, budget=0)

    events = [event async for event in agent.run_turn("hello")]

    assert provider.quality_calls == []
    assert agent.config.model_id == "quality-c1"
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_quality_retry_requires_an_applied_model_policy() -> None:
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    agent = _agent(provider, fixed_route=False, explicit_correction=True)

    events = [event async for event in agent.run_turn("hello")]

    assert provider.quality_calls == []
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_explicit_correction_is_already_resolved_by_model_policy() -> None:
    provider = _QualityProvider([_final_stream()])
    agent = _agent(provider, explicit_correction=True)
    agent.set_history(
        [
            Message(role="user", content="previous request"),
            Message(role="assistant", content="previous answer"),
        ]
    )

    events = [event async for event in agent.run_turn("Your answer is wrong; correct it.")]

    assert provider.timeline == ["chat"]
    assert provider.quality_calls == []
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_refused_quality_retry_is_not_repeated_within_one_user_turn() -> None:
    provider = _QualityProvider(
        [_tool_stream(1), _tool_stream(2), _final_stream()],
        accept_retry=False,
    )
    agent = _agent(provider)

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.quality_calls) == 1
    assert agent.config.model_id == "quality-c1"
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_quality_retry_consumes_same_budget_as_later_provider_retry() -> None:
    provider = _QualityProvider(
        [
            _tool_stream(1),
            [
                ProviderError(
                    message="upstream unavailable",
                    code="429",
                )
            ],
            _final_stream(),
        ]
    )
    agent = _agent(provider, budget=1)

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.quality_calls) == 1
    assert len(provider.calls) == 2
    assert any(
        event.kind == "error" and event.code == "provider_retry_budget_exhausted"
        for event in events
    )


@pytest.mark.parametrize("metadata", [{"router_control_hold_applied": True}, {"routed_tier": "c3"}])
async def test_hold_and_top_tier_block_execution_quality_upgrade(metadata):
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    agent = _agent(provider)
    agent.config.metadata.update(metadata)
    events = [event async for event in agent.run_turn("hello")]
    assert provider.quality_calls == []
    assert any(event.kind == "done" for event in events)


async def test_transport_retry_consumes_budget_before_later_quality_signal():
    provider = _QualityProvider(
        [
            [ProviderError(message="busy", code="429")],
            _tool_stream(1),
            _final_stream(),
        ]
    )
    events = [event async for event in _agent(provider, budget=1).run_turn("hello")]
    assert provider.quality_calls == []
    assert events


async def test_repeated_failed_tools_do_not_claim_successful_no_progress():
    provider = _QualityProvider(
        [_tool_stream(1, value="same"), _tool_stream(2, value="same"), _final_stream()]
    )
    events = [
        event
        async for event in _agent(
            provider, validation_reason="permission_denied", repeat_threshold=2
        ).run_turn("hello")
    ]
    assert provider.quality_calls == []
    assert not any(
        event.kind == "warning" and event.code == "repeated_tool_call_recovery" for event in events
    )


async def test_local_preflight_repair_keeps_first_dispatch_with_zero_retry_budget():
    from tests.test_engine.test_agent_image_capability_guard import (
        _image_message,
        _PreflightRejectProvider,
    )

    provider = _PreflightRejectProvider(code="unsupported_image")
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            model_id="custom-model",
            model_vision_support="unknown",
            metadata={"router_tier_policy": "model", "routing_applied": True, "routed_tier": "c1"},
        ),
    )
    events = [
        event
        async for event in agent.run_turn(
            "Describe this image.",
            extra_messages=[_image_message()],
        )
    ]
    assert len(provider.calls) == 1
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "error" for event in events)


@pytest.mark.parametrize("vision_support", ["supported", "unsupported", "unknown"])
async def test_quality_retry_projects_the_same_physical_leg_as_dispatch(
    execution, monkeypatch, vision_support,
) -> None:
    import opensquilla.engine.model_router_quality as quality
    from opensquilla.engine.model_policy_recovery import ModelPolicyRecovery
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.types import ContentBlockImage, ContentBlockText

    e = execution
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    from opensquilla.provider.image_projection import ImageMarkerState

    # A forced marker on the old deployment must not carry over to a new
    # vision-capable candidate, while the turn-wide unknown-probe barrier must.
    wrapper.configure_image_request_projection(
        force_marker=True, marker_state=ImageMarkerState.NOT_ANALYZED,
        forbid_unknown_probe=True, reason="configured_marker_fallback",
    )
    monkeypatch.setattr(
        quality.shared_catalog(), "resolve_deployment_vision_support",
        lambda *a, **k: vision_support,
    )
    provider_type = type(e.provider)
    monkeypatch.setattr(
        provider_type, "requires_complete_reasoning_history", lambda *a, **k: True,
    )
    monkeypatch.setattr(provider_type, "can_replay_reasoning", lambda *a, **k: False)
    messages = [
        Message(role="user", content="previous request"),
        Message(role="assistant", content="previous reply without native reasoning"),
        Message(role="user", content=[
            ContentBlockText(text="retry with this image"),
            ContentBlockImage(media_type="image/png", data="c3ludGhldGlj"),
        ]),
    ]
    config = ChatConfig(max_tokens=1024, thinking=True)
    projected = []
    dispatched = []
    project = quality.project_provider_final_request

    def capture_projection(provider, request, tools, bound):
        projected.append((copy.deepcopy(request), bound))
        # Previewing a candidate must not change live routing metadata.
        assert "image_input_marker_count" not in e.turn.metadata
        assert not e.turn.metadata.get("reasoning_replay_context_rebuilt")
        return project(provider, request, tools, bound)

    async def chat(provider, request, tools=None, config=None):
        dispatched.append((copy.deepcopy(request), config))
        yield ProviderText(text="done")
        yield ProviderDone(input_tokens=1, output_tokens=1)

    monkeypatch.setattr(quality, "project_provider_final_request", capture_projection)
    monkeypatch.setattr(provider_type, "chat", chat)
    recovery = ModelPolicyRecovery(AgentConfig(
        model_id=e.turn.model, metadata=e.turn.metadata, max_provider_retries=1,
    ))
    updates = await recovery.prepare(
        wrapper, "validation_failure", messages, chat_config=config,
    )
    assert updates is not None
    admitted_messages, admitted_config = projected[0]
    assert admitted_messages != messages
    assert "Recorded conversation context" in str(admitted_messages)
    has_images = any(
        isinstance(block, ContentBlockImage)
        for message in admitted_messages if isinstance(message.content, list)
        for block in message.content
    )
    assert has_images is (vision_support == "supported")
    events = [event async for event in wrapper.chat(messages, config=admitted_config)]
    assert events
    assert dispatched == [(admitted_messages, admitted_config)]
