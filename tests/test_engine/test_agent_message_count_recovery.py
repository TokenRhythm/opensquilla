from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import (
    Agent,
    AgentConfig,
    ErrorEvent,
    ThinkingLevel,
    ToolResult,
    WarningEvent,
)
from opensquilla.engine.types import CompactionEvent
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    OpenAIProvider,
    ProviderMessageCountProjection,
    ProviderMessageLimitProof,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
    build_ensemble_provider_from_config,
)
from opensquilla.provider import (
    DoneEvent as ProviderDoneEvent,
)
from opensquilla.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.session.compaction import CompactionResult


class _ExactMessageLimitProvider:
    provider_name = "tokenrhythm"

    def __init__(self, limits: list[int | None]) -> None:
        self._limits = limits
        self._projector = OpenAIProvider(
            api_key="test",
            model="glm-5.2",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
        self.calls: list[list[Message]] = []
        self.projections: list[ProviderMessageCountProjection] = []

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        return self._projector.project_message_count(
            messages,
            config,
            additional_messages=additional_messages,
        )

    def project_final_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> Any:
        return self._projector.project_final_request(
            messages,
            tools,
            config,
            message_limit=message_limit,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        projection = self.project_message_count(messages, config)
        self.projections.append(projection)
        call_index = len(self.calls) - 1
        limit = self._limits[call_index] if call_index < len(self._limits) else None
        return self._stream(projection, limit)

    async def _stream(
        self,
        projection: ProviderMessageCountProjection,
        limit: int | None,
    ) -> AsyncIterator[Any]:
        if limit is not None:
            assert projection.actual_wire_messages > limit
            yield ProviderErrorEvent(
                message="TokenRhythm chat request failed (HTTP 400): BAD_REQUEST; too many",
                code="400",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages,
                    limit=limit,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind=projection.provider_kind,
                    model=projection.model,
                    base_host=projection.base_host,
                ),
            )
            return
        yield TextDeltaEvent(text="ok")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _LiveTurnMessageLimitProvider(_ExactMessageLimitProvider):
    """Build a long tool loop, reject its count once, then accept recovery."""

    def __init__(self, *, completed_rounds: int = 10, limit: int = 20) -> None:
        super().__init__([])
        self.completed_rounds = completed_rounds
        self.limit = limit
        self.rounds_started = 0
        self.limit_rejected = False

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        projection = self.project_message_count(messages, config)
        self.projections.append(projection)
        if self.rounds_started < self.completed_rounds:
            action = "tool"
            tool_index = self.rounds_started
            self.rounds_started += 1
        elif not self.limit_rejected:
            assert projection.actual_wire_messages > self.limit
            action = "limit"
            tool_index = -1
            self.limit_rejected = True
        else:
            action = "final"
            tool_index = -1
        return self._live_stream(action, tool_index, projection)

    async def _live_stream(
        self,
        action: str,
        tool_index: int,
        projection: ProviderMessageCountProjection,
    ) -> AsyncIterator[Any]:
        if action == "tool":
            tool_id = f"live-provider-{tool_index}"
            yield ToolUseStartEvent(tool_use_id=tool_id, tool_name="check")
            yield ToolUseEndEvent(
                tool_use_id=tool_id,
                tool_name="check",
                arguments={"part": tool_index},
            )
            yield ProviderDoneEvent(
                stop_reason="tool_use",
                input_tokens=1,
                output_tokens=1,
            )
            return
        if action == "limit":
            yield ProviderErrorEvent(
                message="TokenRhythm chat request failed (HTTP 400): BAD_REQUEST; too many",
                code="400",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages,
                    limit=self.limit,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind=projection.provider_kind,
                    model=projection.model,
                    base_host=projection.base_host,
                ),
            )
            return
        yield TextDeltaEvent(text="completed after live-turn recovery")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)


class _ScaffoldLimitToolProvider:
    """Exercise count recovery across one-shot reasoning scaffold cleanup."""

    provider_name = "tokenrhythm"

    def __init__(self) -> None:
        self._projector = OpenAIProvider(
            api_key="test",
            model="glm-5.2",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
        self._phase = "reasoning"
        self.calls: list[dict[str, Any]] = []

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        return self._projector.project_message_count(
            messages,
            config,
            additional_messages=additional_messages,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        projection = self.project_message_count(messages, config)
        phase = self._phase
        if phase == "reasoning":
            action = "reasoning"
            self._phase = "tool"
        elif projection.actual_wire_messages > 100:
            action = "limit"
        elif phase == "tool":
            action = "tool"
            self._phase = "final"
        else:
            action = "final"
            self._phase = "done"
        self.calls.append(
            {
                "action": action,
                "messages": list(messages),
                "projection": projection,
            }
        )
        return self._stream(action, projection)

    async def _stream(
        self,
        action: str,
        projection: ProviderMessageCountProjection,
    ) -> AsyncIterator[Any]:
        if action == "reasoning":
            yield ProviderDoneEvent(
                stop_reason="stop",
                input_tokens=1,
                output_tokens=1,
                reasoning_tokens=1,
                reasoning_content="one-shot internal reasoning",
                model="glm-5.2",
            )
            return
        if action == "limit":
            yield ProviderErrorEvent(
                message="TokenRhythm chat request failed (HTTP 400): BAD_REQUEST; too many",
                code="400",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages,
                    limit=100,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind=projection.provider_kind,
                    model=projection.model,
                    base_host=projection.base_host,
                ),
            )
            return
        if action == "tool":
            yield ToolUseStartEvent(tool_use_id="call-1", tool_name="echo")
            yield ToolUseEndEvent(
                tool_use_id="call-1",
                tool_name="echo",
                arguments={"value": "hello"},
            )
            yield ProviderDoneEvent(
                stop_reason="tool_use",
                input_tokens=1,
                output_tokens=1,
                model="glm-5.2",
            )
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(
            stop_reason="stop",
            input_tokens=1,
            output_tokens=1,
            model="glm-5.2",
        )

    async def list_models(self) -> list[Any]:
        return []


class _PerturbedAssistantTailLimitProvider:
    """Reject only the 101-message assistant-tail perturbation request."""

    provider_name = "tokenrhythm"

    def __init__(self) -> None:
        self._projector = OpenAIProvider(
            api_key="test",
            model="glm-5.2",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
        self._reasoning_sent = False
        self.calls: list[dict[str, Any]] = []

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        return self._projector.project_message_count(
            messages,
            config,
            additional_messages=additional_messages,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        projection = self.project_message_count(messages, config)
        if not self._reasoning_sent:
            action = "reasoning"
            self._reasoning_sent = True
        elif projection.actual_wire_messages > 100:
            action = "limit"
        else:
            action = "final"
        self.calls.append(
            {
                "action": action,
                "messages": list(messages),
                "projection": projection,
            }
        )
        return self._stream(action, projection)

    async def _stream(
        self,
        action: str,
        projection: ProviderMessageCountProjection,
    ) -> AsyncIterator[Any]:
        if action == "reasoning":
            yield ProviderDoneEvent(
                stop_reason="stop",
                input_tokens=1,
                output_tokens=1,
                reasoning_tokens=1,
                reasoning_content="assistant-tail reasoning",
                model="glm-5.2",
            )
            return
        if action == "limit":
            yield ProviderErrorEvent(
                message="TokenRhythm chat request failed (HTTP 400): BAD_REQUEST; too many",
                code="400",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages,
                    limit=100,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind=projection.provider_kind,
                    model=projection.model,
                    base_host=projection.base_host,
                ),
            )
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(
            stop_reason="stop",
            input_tokens=1,
            output_tokens=1,
            model="glm-5.2",
        )

    async def list_models(self) -> list[Any]:
        return []


class _EnsembleBudgetCatalog:
    _WINDOWS = {
        "deepseek-v4-pro": 1_000_000,
        "glm-5.2": 1_000_000,
        "kimi-k2.7-code": 256_000,
        "qwen3.7-max": 1_000_000,
    }

    def resolve_context_window_with_source(
        self,
        model_id: str,
        provider: str = "",  # noqa: ARG002
    ) -> tuple[int, str]:
        return self._WINDOWS[model_id], "catalog"

    def resolve_context_window(
        self,
        model_id: str,
        provider: str = "",  # noqa: ARG002
    ) -> int:
        return self._WINDOWS[model_id]


class _CountAwareEnsembleMemberProvider:
    provider_name = "tokenrhythm"

    def __init__(self, config: ProviderConfig, calls: list[dict[str, Any]]) -> None:
        self._config = config
        self._calls = calls
        self._projector = OpenAIProvider(
            api_key="test",
            model=config.model,
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        return self._projector.project_message_count(
            messages,
            config,
            additional_messages=additional_messages,
        )

    def project_final_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> Any:
        return self._projector.project_final_request(
            messages,
            tools,
            config,
            message_limit=message_limit,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        projection = self.project_message_count(messages, config)
        is_aggregator = bool(
            messages
            and "Candidate drafts:" in str(messages[-1].content)
        )
        self._calls.append(
            {
                "model": self._config.model,
                "wire_messages": projection.actual_wire_messages,
                "request_cap": int(
                    getattr(config, "provider_request_max_chars", 0) or 0
                ),
                "aggregator": is_aggregator,
            }
        )
        return self._stream(projection, is_aggregator)

    async def _stream(
        self,
        projection: ProviderMessageCountProjection,
        is_aggregator: bool,
    ) -> AsyncIterator[Any]:
        if projection.actual_wire_messages > 100:
            yield ProviderErrorEvent(
                message="TokenRhythm chat request failed (HTTP 400): BAD_REQUEST; too many",
                code="400",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages,
                    limit=100,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind="tokenrhythm",
                    model=self._config.model,
                    base_host="tokenrhythm.studio",
                ),
            )
            return
        yield TextDeltaEvent(text="final" if is_aggregator else "draft")
        yield ProviderDoneEvent(
            stop_reason="stop",
            input_tokens=1,
            output_tokens=1,
            model=self._config.model,
        )

    async def list_models(self) -> list[Any]:
        return []


def _plain_history(message_count: int = 104) -> list[Message]:
    assert message_count % 2 == 0
    history: list[Message] = []
    for index in range(message_count // 2):
        history.append(Message(role="user", content=f"historical request {index}"))
        history.append(Message(role="assistant", content=f"historical answer {index}"))
    return history


def _history_with_parallel_tool_group() -> list[Message]:
    history = _plain_history(16)
    history.extend(
        [
            Message(role="user", content="run both checks"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(id="call-a", name="check", input={"part": "a"}),
                    ContentBlockToolUse(id="call-b", name="check", input={"part": "b"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="call-a", content="result-a"),
                    ContentBlockToolResult(tool_use_id="call-b", content="result-b"),
                ],
            ),
            Message(role="assistant", content="both checks complete"),
        ]
    )
    history.extend(_plain_history(84))
    assert len(history) == 104
    return history


def _install_exact_compactor(
    monkeypatch: pytest.MonkeyPatch,
    requests: list[Any],
    *,
    fail: bool = False,
) -> None:
    async def _compact(request: Any) -> CompactionResult:
        requests.append(request)
        if fail:
            raise RuntimeError("synthetic summary failure")
        cut = request.forced_prefix_cut
        assert isinstance(cut, int)
        return CompactionResult(
            summary="bounded historical summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            chunks_processed=1,
            kept_start_index=cut,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _compact)


@pytest.mark.asyncio
async def test_message_limit_recovery_retries_once_below_headroom_without_rewriting_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ExactMessageLimitProvider([100, None])
    history = _plain_history()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            request_context_prompt="request-scoped evidence",
            flush_enabled=False,
        ),
    )
    agent.set_history(history)

    events = [event async for event in agent.run_turn("current user request")]

    assert len(provider.calls) == 2
    assert provider.projections[0].actual_wire_messages > 100
    assert provider.projections[1].actual_wire_messages <= 90
    assert len(compact_requests) == 1
    request = compact_requests[0]
    assert request.trigger == "message_count"
    assert request.reason == "provider_request_message_limit"
    assert request.forced_prefix_cut == 18
    assert sum(
        isinstance(message.content, str)
        and message.content.startswith("current user request\n\n[Runtime context")
        for message in provider.calls[1]
    ) == 1
    assert sum(
        "[Request context for this turn]" in str(message.content)
        for message in provider.calls[1]
    ) == 1
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "provider_request_message_limit_recovery_success"
        for event in events
    )
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent._history[: len(history)] == history
    assert not any("[Context summary]" in str(message.content) for message in agent._history)
    assert not any(
        "[Request context for this turn]" in str(message.content)
        for message in agent._history
    )


@pytest.mark.asyncio
async def test_message_limit_cut_never_splits_parallel_tool_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ExactMessageLimitProvider([100, None])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0, flush_enabled=False),
    )
    agent.set_history(_history_with_parallel_tool_group())

    events = [event async for event in agent.run_turn("current user request")]

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(compact_requests) == 1
    # The raw count target falls inside the parallel tool segment.  Recovery
    # advances to the next complete user-turn boundary instead.
    assert compact_requests[0].forced_prefix_cut == 20
    second_payload = provider.calls[1]
    assert not any("call-a" in str(message.content) for message in second_payload)
    assert not any("call-b" in str(message.content) for message in second_payload)


@pytest.mark.asyncio
async def test_second_exact_message_limit_error_is_terminal_without_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ExactMessageLimitProvider([100, 80])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3, flush_enabled=False),
    )
    agent.set_history(_plain_history())

    events = [event async for event in agent.run_turn("current user request")]

    assert len(provider.calls) == 2
    assert len(compact_requests) == 1
    terminal = next(
        event
        for event in events
        if isinstance(event, ErrorEvent)
        and event.code == "provider_request_message_limit_exhausted"
    )
    assert "again" in terminal.message


@pytest.mark.asyncio
async def test_message_limit_summary_failure_is_terminal_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests, fail=True)
    provider = _ExactMessageLimitProvider([100])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3, flush_enabled=False),
    )
    agent.set_history(_plain_history())

    events = [event async for event in agent.run_turn("current user request")]

    assert len(provider.calls) == 1
    assert len(compact_requests) == 1
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_message_limit_exhausted"
        for event in events
    )


@pytest.mark.asyncio
async def test_protected_current_turn_over_limit_refuses_without_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ExactMessageLimitProvider([10])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3, flush_enabled=False),
    )
    current_inputs = [Message(role="user", content=f"current-{index}") for index in range(11)]

    events = [
        event
        async for event in agent.run_turn(
            "",
            extra_messages=current_inputs,
        )
    ]

    assert len(provider.calls) == 1
    assert compact_requests == []
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_message_limit_exhausted"
        for event in events
    )


@pytest.mark.asyncio
async def test_message_limit_projects_completed_live_rounds_when_durable_prefix_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ExactMessageLimitProvider([None])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0, flush_enabled=False),
    )
    active_text = "finish this active request byte-for-byte: 你好 🦑"
    active_user = Message(role="user", content=active_text)
    rounds: list[tuple[Message, Message]] = []
    for index in range(10):
        tool_use = Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id=f"live-{index}",
                    name="check",
                    input={"part": index},
                )
            ],
        )
        result_content = f"result-{index}"
        is_error = index == 4
        if index == 5:
            result_content = '{"status":"awaiting_approval","approval_id":"approval-live-5"}'
        tool_result = Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id=f"live-{index}",
                    content=result_content,
                    is_error=is_error,
                )
            ],
        )
        rounds.append((tool_use, tool_result))
    messages = [active_user, *(message for pair in rounds for message in pair)]
    canonical_snapshot = list(messages)
    agent._current_turn_message = active_text
    runtime_context = Message(role="user", content="[Runtime context]\ncontinue")
    chat_config = ChatConfig()
    initial_projection = agent._project_provider_request_message_count(
        messages,
        config=chat_config,
        identical_request_perturbed=False,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=runtime_context,
        runtime_context_insert_index=0,
        turn_objective_message=None,
    )
    assert initial_projection is not None
    limit = 20
    assert initial_projection.actual_wire_messages > limit
    proof = ProviderMessageLimitProof(
        actual_wire_messages=initial_projection.actual_wire_messages,
        limit=limit,
        logical_messages=initial_projection.logical_messages,
        system_messages=initial_projection.system_messages,
        tool_result_messages=initial_projection.tool_result_messages,
        provider_kind=initial_projection.provider_kind,
        model=initial_projection.model,
        base_host=initial_projection.base_host,
    )

    outcome, reason = await agent._recover_provider_message_count_limit(
        messages,
        request_suffix_messages=[],
        proof=proof,
        config=chat_config,
        identical_request_perturbed=False,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=runtime_context,
        runtime_context_insert_index=0,
        turn_objective_message=None,
        protected_turn_start_index=0,
    )

    assert reason == "recovered_live_turn"
    assert outcome is not None
    assert outcome.projected_wire_messages <= (
        limit - agent._message_count_headroom(limit)
    )
    assert outcome.messages[2] is active_user
    assert outcome.messages[2].content == active_text
    # The error and pending-approval rounds force the raw tail to begin at
    # round four; the newest two rounds therefore remain raw as well.
    assert outcome.messages[-12:] == [
        message for pair in rounds[4:] for message in pair
    ]
    assert outcome.messages[-2] is rounds[-1][0]
    assert outcome.messages[-1] is rounds[-1][1]
    assert "approval-live-5" in str(outcome.messages)
    assert "result-4" in str(outcome.messages)
    assert messages == canonical_snapshot
    assert all(
        entry["content"] != active_text
        for entry in compact_requests[0].entries
    )
    assert compact_requests[0].forced_prefix_cut == 8


@pytest.mark.asyncio
async def test_long_tool_loop_continues_after_live_turn_message_count_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _LiveTurnMessageLimitProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=f"raw-result-for-{call.tool_use_id}",
        )

    active_text = "complete every tool round and keep this exact request"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=20,
            max_provider_retries=0,
            flush_enabled=False,
            model_capabilities=ModelCapabilities(supports_tools=True),
        ),
        tool_definitions=[
            ToolDefinition(
                name="check",
                description="Run one synthetic offline check.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn(active_text)]

    assert provider.limit_rejected is True
    assert provider.rounds_started == provider.completed_rounds
    assert provider.projections[-2].actual_wire_messages > provider.limit
    assert provider.projections[-1].actual_wire_messages <= (
        provider.limit - agent._message_count_headroom(provider.limit)
    )
    assert len(compact_requests) == 1
    assert compact_requests[0].forced_prefix_cut == 16
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "provider_request_message_limit_recovery_success"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(getattr(event, "kind", None) == "done" for event in events)
    # The provider request is ephemeral; the canonical transcript keeps the
    # original prompt and every raw tool pair without a summary marker.
    assert sum(
        message.role == "user" and message.content == active_text
        for message in agent._history
    ) == 1
    assert not any("[Context summary]" in str(message.content) for message in agent._history)
    for index in range(provider.completed_rounds):
        tool_id = f"live-provider-{index}"
        assert sum(tool_id in str(message.content) for message in agent._history) == 2


@pytest.mark.asyncio
async def test_reasoning_scaffold_cleanup_preserves_recovered_tool_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _ScaffoldLimitToolProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool result",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            max_provider_retries=0,
            flush_enabled=False,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo a value.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )
    agent.set_history(_plain_history())

    events = [event async for event in agent.run_turn("current user request")]

    final_call = next(call for call in provider.calls if call["action"] == "final")
    final_messages = final_call["messages"]
    tool_use_ids = [
        block.id
        for message in final_messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse)
    ]
    tool_result_ids = [
        block.tool_use_id
        for message in final_messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    ]
    assert tool_use_ids.count("call-1") == 1
    assert tool_result_ids.count("call-1") == 1
    assert not any(
        message.reasoning_content == "one-shot internal reasoning"
        for message in final_messages
    )
    assert compact_requests
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "provider_request_message_limit_recovery_success"
        for event in events
    )
    assert any(getattr(event, "kind", None) == "done" for event in events)


@pytest.mark.asyncio
async def test_assistant_tail_loop_perturbation_uses_actual_count_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    provider = _PerturbedAssistantTailLimitProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            identical_request_loop_break_threshold=1,
            max_provider_retries=0,
            flush_enabled=False,
        ),
    )
    # 98 historical messages + current user + reasoning prefill = 100 in the
    # pure request view.  The opt-in loop perturbation sees an assistant tail
    # and appends one user message, producing the exact rejected count of 101.
    agent.set_history(_plain_history(98))

    events = [event async for event in agent.run_turn("current user request")]

    limit_call = next(call for call in provider.calls if call["action"] == "limit")
    final_call = next(call for call in provider.calls if call["action"] == "final")
    assert limit_call["projection"].actual_wire_messages == 101
    assert final_call["projection"].actual_wire_messages <= 90
    assert len(compact_requests) == 1
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "provider_request_message_limit_recovery_success"
        for event in events
    )
    assert not any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_message_limit_exhausted"
        for event in events
    )
    assert any(getattr(event, "kind", None) == "done" for event in events)


@pytest.mark.asyncio
async def test_token_compaction_maps_duplicate_content_boundaries_by_prefix_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        Message(role="user", content="duplicate"),
        Message(role="assistant", content="historical answer"),
        Message(role="user", content="duplicate"),
        Message(role="assistant", content="historical answer"),
        Message(role="user", content="duplicate"),
        Message(role="user", content="second protected current input"),
    ]

    async def _compact(request: Any) -> CompactionResult:
        cut = 4
        return CompactionResult(
            summary="historical summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            chunks_processed=1,
            kept_start_index=cut,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _compact)
    agent = Agent(
        provider=_ExactMessageLimitProvider([None]),
        config=AgentConfig(flush_enabled=False),
    )

    outcome = await agent._check_context_overflow(
        messages,
        agent.config.context_window_tokens + 1,
        request_context_insert_index=4,
        runtime_context_insert_index=4,
        protected_turn_start_index=4,
    )

    assert outcome is not None
    assert outcome.compacted is True
    # The exact prefix cut removes four originals and inserts summary+ack, so
    # every boundary at the first kept message maps to index two regardless of
    # duplicate role/content values elsewhere in the transcript.
    assert outcome.request_context_insert_index == 2
    assert outcome.runtime_context_insert_index == 2
    assert outcome.protected_turn_start_index == 2
    assert agent._message_count_safe_prefix_cuts(
        outcome.messages,
        protected_turn_start_index=outcome.protected_turn_start_index,
    ) == [2]


def test_message_limit_safe_cuts_keep_consecutive_user_side_history_together() -> None:
    messages = [
        Message(role="user", content="historical request 0"),
        Message(role="assistant", content="historical answer 0"),
        Message(role="user", content="[Available skills for this turn]\nskill context"),
        Message(role="user", content="historical multimodal input"),
        Message(role="user", content="historical request 1"),
        Message(role="assistant", content="historical answer 1"),
        Message(role="user", content="protected current request"),
    ]

    cuts = Agent._message_count_safe_prefix_cuts(
        messages,
        protected_turn_start_index=6,
    )

    # The three consecutive user-side rows are one historical turn. Recovery
    # may summarize the whole turn or keep it whole, but must never cut between
    # its skills/multimodal/request components.
    assert cuts == [2, 6]


@pytest.mark.asyncio
async def test_member_budget_rebinding_and_exact_count_recovery_compose_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []
    _install_exact_compactor(monkeypatch, compact_requests)
    member_calls: list[dict[str, Any]] = []

    def _build_member(config: ProviderConfig) -> _CountAwareEnsembleMemberProvider:
        return _CountAwareEnsembleMemberProvider(config, member_calls)

    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", _build_member)
    gateway_config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "kimi-k2.7-code",
            "api_key": "test",
            "base_url": "https://tokenrhythm.studio/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_tokenrhythm_b5",
            "all_failed_policy": "error",
        },
    )
    ensemble = build_ensemble_provider_from_config(
        config=gateway_config,
        inherited_provider_config=ProviderConfig(
            provider="tokenrhythm",
            model="kimi-k2.7-code",
            api_key="test",
            base_url="https://tokenrhythm.studio/v1",
        ),
        fallback_provider=None,
        _enable_member_request_budget_rebinding=True,
        _model_catalog=_EnsembleBudgetCatalog(),
        _context_overflow_threshold=0.85,
    )
    agent = Agent(
        provider=ensemble,
        config=AgentConfig(
            max_tokens=128_000,
            context_window_tokens=256_000,
            max_provider_retries=0,
            flush_enabled=False,
        ),
    )
    agent.set_history(_plain_history())

    events = [event async for event in agent.run_turn("current user request")]

    assert len(compact_requests) == 1
    assert sum(call["wire_messages"] > 100 for call in member_calls) == 4
    assert sum(call["wire_messages"] <= 90 for call in member_calls) == 5
    assert sum(call["aggregator"] for call in member_calls) == 1
    assert any(
        call["model"] == "kimi-k2.7-code" and call["request_cap"] == 367_200
        for call in member_calls
    )
    assert any(
        call["model"] == "glm-5.2" and call["request_cap"] == 2_896_800
        for call in member_calls
    )
    assert any(getattr(event, "kind", None) == "done" for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_message_limit_exhausted"
        for event in events
    )


def test_message_count_request_preview_has_no_metadata_side_effects() -> None:
    provider = _ExactMessageLimitProvider([None])
    agent = Agent(
        provider=provider,
        config=AgentConfig(runtime_state_capsule_mode="inject"),
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call-1",
                    name="check",
                    input={"path": "[tool_use_argument_projection]\nprovider-only"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call-1", content="ok")],
        ),
    ]
    metadata_before = dict(agent.config.metadata)

    projected = agent._provider_request_messages_for_count_projection(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="runtime"),
        runtime_context_insert_index=0,
    )

    assert projected
    assert agent.config.metadata == metadata_before
    assert not agent._provider_tool_result_frozen_full_ids
