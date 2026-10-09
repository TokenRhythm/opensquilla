from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


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
    ) -> dict[str, Any] | None:
        self.quality_calls.append(
            {
                "reason": reason,
                "scope_id": scope_id,
                "remaining_budget": remaining_budget,
                "messages": copy.deepcopy(messages),
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
        metadata["fixed_four_tier_v2_decision_id"] = "quality-route"
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


@pytest.mark.parametrize("reason", ["invalid_tool_arguments", "schema_validation_failed"])
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


@pytest.mark.parametrize("code", ["429", "timeout"])
async def test_provider_infrastructure_errors_do_not_call_quality_retry(code: str) -> None:
    provider = _QualityProvider(
        [
            [
                ProviderError(
                    message="upstream unavailable",
                    code=code,
                    request_started=True,
                    physical_request_count=1,
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


async def test_quality_retry_requires_a_fixed_route_decision() -> None:
    provider = _QualityProvider([_tool_stream(1), _final_stream()])
    agent = _agent(provider, fixed_route=False, explicit_correction=True)

    events = [event async for event in agent.run_turn("hello")]

    assert provider.quality_calls == []
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_explicit_correction_prepares_quality_retry_before_first_chat() -> None:
    provider = _QualityProvider([_final_stream()])
    agent = _agent(provider, explicit_correction=True)
    agent.set_history(
        [
            Message(role="user", content="previous request"),
            Message(role="assistant", content="previous answer"),
        ]
    )

    events = [event async for event in agent.run_turn("Your answer is wrong; correct it.")]

    assert provider.timeline == ["quality", "chat"]
    assert provider.quality_calls[0]["reason"] == "explicit_correction"
    assert any(message.role == "assistant" for message in provider.quality_calls[0]["messages"])
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
                    request_started=True,
                    physical_request_count=1,
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
