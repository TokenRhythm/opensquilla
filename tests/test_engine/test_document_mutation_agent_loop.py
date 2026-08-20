from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import ToolEffectOutcome
from opensquilla.provider import ModelCapabilities, ToolDefinition, ToolInputSchema
from opensquilla.provider.types import (
    DoneEvent as ProviderDoneEvent,
)
from opensquilla.provider.types import (
    ErrorEvent as ProviderErrorEvent,
)
from opensquilla.provider.types import (
    TextDeltaEvent as ProviderTextDeltaEvent,
)
from opensquilla.provider.types import (
    ToolUseEndEvent as ProviderToolUseEndEvent,
)
from opensquilla.provider.types import (
    ToolUseStartEvent as ProviderToolUseStartEvent,
)
from opensquilla.tools.types import ToolContext


class _MutationController:
    def __init__(self) -> None:
        self.active_id: str | None = None
        self.committed_ids: set[str] = set()
        self.observed: list[str] = []
        self.rejected: list[str] = []

    async def observe_intent(self, tool_use_id: str) -> SimpleNamespace:
        if self.active_id not in {None, tool_use_id}:
            raise RuntimeError("another writer is active")
        created = self.active_id is None
        self.active_id = tool_use_id
        self.observed.append(tool_use_id)
        return SimpleNamespace(created=created, attempt_number=len(self.observed))

    async def reject_proposal(self, tool_use_id: str) -> None:
        self.rejected.append(tool_use_id)
        if self.active_id == tool_use_id:
            self.active_id = None

    def owns_commit(self, tool_use_id: str) -> bool:
        return tool_use_id in self.committed_ids

    async def reconcile(self, tool_use_id: str) -> SimpleNamespace:
        status = "applied" if tool_use_id in self.committed_ids else "failed"
        return SimpleNamespace(status=SimpleNamespace(value=status))

    async def mark_ambiguous(self, *_args: object) -> None:
        return None


class _ScriptedMutationProvider:
    provider_name = "scripted-document-mutation"

    def __init__(self, scripts: list[list[object]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> Any:
        call_index = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools, "config": config})
        for event in self.scripts[call_index]:
            if isinstance(event, BaseException):
                raise event
            yield event


class _DelayedAnswerProvider:
    provider_name = "delayed-document-answer"

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> Any:
        self.calls.append({"messages": list(messages), "tools": tools, "config": config})
        await asyncio.sleep(self.delay)
        yield ProviderTextDeltaEvent(text="The selected text is clear.")
        yield ProviderDoneEvent(stop_reason="end_turn")


class _CloseAwareStream:
    def __init__(self, events: list[object]) -> None:
        self.events = iter(events)
        self.closed = False

    def __aiter__(self) -> _CloseAwareStream:
        return self

    async def __anext__(self) -> object:
        try:
            event = next(self.events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(event, BaseException):
            raise event
        return event

    async def aclose(self) -> None:
        self.closed = True


class _CloseAwareMutationProvider:
    provider_name = "close-aware-document-mutation"

    def __init__(self, scripts: list[list[object]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []
        self.streams: list[_CloseAwareStream] = []

    def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> _CloseAwareStream:
        call_index = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools, "config": config})
        stream = _CloseAwareStream(self.scripts[call_index])
        self.streams.append(stream)
        return stream


def _tool_call_events(tool_use_id: str, arguments: dict[str, Any]) -> list[object]:
    return [
        ProviderToolUseStartEvent(
            tool_use_id=tool_use_id,
            tool_name="document_apply",
        ),
        ProviderToolUseEndEvent(
            tool_use_id=tool_use_id,
            tool_name="document_apply",
            arguments=arguments,
        ),
        ProviderDoneEvent(stop_reason="tool_use"),
    ]


def _document_patch_call_events(
    tool_use_id: str,
    arguments: dict[str, Any],
) -> list[object]:
    return [
        ProviderToolUseStartEvent(
            tool_use_id=tool_use_id,
            tool_name="document_patch",
        ),
        ProviderToolUseEndEvent(
            tool_use_id=tool_use_id,
            tool_name="document_patch",
            arguments=arguments,
        ),
        ProviderDoneEvent(stop_reason="tool_use"),
    ]


def _effect_result(
    tool_use_id: str,
    *,
    status: str,
    effect_state: str,
    retry_policy: str,
    loop_action: str,
    code: str,
    is_error: bool,
    corrected: bool = False,
    tool_name: str = "document_apply",
) -> ToolResult:
    outcome: dict[str, Any] = {
        "status": status,
        "phase": "commit" if effect_state != "none" else "proposal",
        "retryPolicy": retry_policy,
        "code": code,
    }
    if corrected:
        outcome.update({"corrected": True, "proposalAttempts": 2})
    return ToolResult(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        content=json.dumps({"status": status, "code": code}),
        is_error=is_error,
        effect_outcome=ToolEffectOutcome(
            effect_state=effect_state,
            retry_policy=retry_policy,
            loop_action=loop_action,
            outcome_code=code,
            safe_details={"documentMutationOutcome": outcome},
        ),
    )


def _agent(
    provider: Any,
    controller: _MutationController,
    handler: Any,
    *,
    locale: str = "en",
    max_iterations: int = 0,
    max_provider_retries: int = 2,
    tools_verified: bool = True,
    iteration_timeout: float = 1800.0,
    timeout: float = 0.0,
    max_turn_llm_calls: int = 8,
    max_turn_input_tokens: int = 0,
    max_turn_billed_cost_usd: float = 0.0,
) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(
            metadata={"artifact_operation_class": "selection_edit", "locale": locale},
            model_capabilities=ModelCapabilities(supports_tools=True),
            model_tools_capability_verified=tools_verified,
            max_turn_llm_calls=max_turn_llm_calls,
            max_turn_input_tokens=max_turn_input_tokens,
            max_turn_billed_cost_usd=max_turn_billed_cost_usd,
            max_iterations=max_iterations,
            max_provider_retries=max_provider_retries,
            iteration_timeout=iteration_timeout,
            timeout=timeout,
        ),
        tool_definitions=[
            ToolDefinition(
                name="document_apply",
                description="Apply a prepared semantic document mutation.",
                input_schema=ToolInputSchema(
                    properties={"mutations": {"type": "array"}},
                    required=["mutations"],
                ),
            )
        ],
        tool_handler=handler,
        tool_context=ToolContext(
            is_owner=True,
            session_key="agent:main:webchat:document-loop-test",
            exclusive_tools={"document_apply"},
            allowed_tools={"document_apply"},
            surfaced_tools={"document_apply"},
            artifact_mutation_attempt_controller=controller,
        ),
    )


def _ordinary_document_agent(
    provider: Any,
    controller: _MutationController,
    handler: Any,
) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(
            metadata={"artifact_operation_class": "document_edit", "locale": "en"},
            model_capabilities=ModelCapabilities(supports_tools=True),
            model_tools_capability_verified=True,
            max_turn_llm_calls=8,
            max_iterations=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="document_patch",
                description="Patch the current bound document.",
                input_schema=ToolInputSchema(
                    properties={
                        "expectedSha256": {"type": "string"},
                        "edits": {"type": "array"},
                    },
                    required=["expectedSha256", "edits"],
                ),
            )
        ],
        tool_handler=handler,
        tool_context=ToolContext(
            is_owner=True,
            session_key="agent:main:webchat:ordinary-document-loop-test",
            surfaced_tools={"document_patch"},
            artifact_mutation_attempt_controller=controller,
        ),
    )


def _serialized_messages(call: dict[str, Any]) -> str:
    return json.dumps(call["messages"], default=lambda value: vars(value), sort_keys=True)


@pytest.mark.asyncio
async def test_annotation_context_can_answer_without_starting_mutation_lifecycle() -> None:
    provider = _ScriptedMutationProvider(
        [
            [
                ProviderTextDeltaEvent(text="The selected heading is already concise."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ]
        ]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("a direct answer must not dispatch document_apply")

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            max_turn_llm_calls=1,
        ).run_turn("Is this heading concise?")
    ]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 1
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[0]["config"].max_tokens == 16_384
    assert done.text == "The selected heading is already concise."
    assert done.document_mutation_outcome is None
    assert controller.observed == []
    assert controller.rejected == []
    assert controller.active_id is None


@pytest.mark.asyncio
async def test_pure_answer_does_not_inherit_mutation_summary_deadline() -> None:
    provider = _DelayedAnswerProvider(delay=0.02)
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("a direct answer must not dispatch document_apply")

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            timeout=0.5,
        ).run_turn("Explain the selected text")
    ]
    done = next(event for event in events if event.kind == "done")

    # The mutation summary reserve has a one-second minimum and would already
    # be expired for this synthetic turn if it were armed before document_apply.
    assert len(provider.calls) == 1
    assert done.text == "The selected text is clear."
    assert done.document_mutation_outcome is None
    assert controller.observed == []


@pytest.mark.asyncio
async def test_committed_mutation_gets_real_outcome_only_tools_disabled_finalization() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-1", {"mutations": [{"grant_token": "secret-grant"}]}),
            [
                ProviderTextDeltaEvent(text="I could not apply the change."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert provider.calls[0]["config"].max_tokens == 16_384
    assert provider.calls[1]["config"].max_tokens == 256
    assert provider.calls[1]["config"].thinking is False
    assert provider.calls[1]["config"].system
    assert "document_apply" not in provider.calls[1]["config"].system
    finalization_wire = _serialized_messages(provider.calls[1])
    for forbidden in ("secret-grant", "document_apply", "grant_token", "offset", "path"):
        assert forbidden not in finalization_wire
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text.endswith("The document changes were applied.")
    assert controller.observed == ["apply-1"]
    assert controller.rejected == []


@pytest.mark.asyncio
async def test_mutation_finalizer_cannot_stream_internal_protocol_echo() -> None:
    leaked_prefix = (
        'TheUserInstructions {"documentMutationOutcome": '
        '{"status": "applied"}} User(internal control text)'
    )
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-contained", {"mutations": []}),
            [
                ProviderTextDeltaEvent(text=leaked_prefix),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")
    streamed_text = "".join(
        event.text for event in events if event.kind == "text_delta"
    )
    finalization_payload = json.loads(provider.calls[1]["messages"][0].content)

    assert finalization_payload == {"language": "en", "status": "applied"}
    assert done.text == "The document changes were applied."
    assert streamed_text == done.text
    for forbidden in ("TheUserInstructions", "documentMutationOutcome", "internal control"):
        assert forbidden not in streamed_text
        assert forbidden not in done.text


@pytest.mark.asyncio
async def test_additive_document_patch_uses_same_guarded_writer_lifecycle(
    unavailable_git_runtime: SimpleNamespace,
) -> None:
    provider = _ScriptedMutationProvider(
        [
            _document_patch_call_events(
                "patch-1",
                {
                    "expectedSha256": "a" * 64,
                    "edits": [
                        {"expectedText": "Old heading", "replacement": "New heading"}
                    ],
                },
            ),
            [
                ProviderTextDeltaEvent(text="Updated."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
            tool_name="document_patch",
        )

    events = [
        event
        async for event in _ordinary_document_agent(provider, controller, handler).run_turn(
            "Edit the open document"
        )
    ]
    done = next(event for event in events if event.kind == "done")

    assert controller.observed == ["patch-1"]
    assert controller.rejected == []
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text.endswith("The document changes were applied.")
    assert unavailable_git_runtime.resolution_calls == []


@pytest.mark.asyncio
async def test_apply_on_last_llm_call_uses_reserved_tools_disabled_finalizer() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-last", {"mutations": []}),
            [
                ProviderTextDeltaEvent(text="Updated."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            max_turn_llm_calls=1,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is None
    assert provider.calls[1]["config"].max_tokens == 256
    assert done.document_mutation_outcome["status"] == "applied"
    assert not any(event.kind == "error" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_kwargs", "usage_kwargs"),
    [
        ({"max_turn_input_tokens": 1}, {"input_tokens": 2}),
        (
            {"max_turn_billed_cost_usd": 0.01},
            {"billed_cost": 0.02, "cost_source": "provider_billed"},
        ),
    ],
    ids=("input-token-boundary", "billed-cost-boundary"),
)
async def test_apply_response_over_budget_still_uses_reserved_finalizer(
    budget_kwargs: dict[str, Any],
    usage_kwargs: dict[str, Any],
) -> None:
    provider = _ScriptedMutationProvider(
        [
            [
                *_tool_call_events("apply-budget", {"mutations": []})[:-1],
                ProviderDoneEvent(stop_reason="tool_use", **usage_kwargs),
            ],
            [
                ProviderTextDeltaEvent(text="Updated."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()
    handler_calls = 0

    async def handler(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            **budget_kwargs,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert handler_calls == 1
    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert provider.calls[1]["config"].max_tokens == 256
    assert done.document_mutation_outcome["status"] == "applied"
    assert not any(event.kind == "error" for event in events)


@pytest.mark.asyncio
async def test_outcome_only_finalization_includes_bounded_response_locale() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-1", {"mutations": []}),
            [ProviderTextDeltaEvent(text="已完成。"), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    _events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            locale="zh-Hans",
        ).run_turn("edit")
    ]

    finalization_payload = json.loads(provider.calls[1]["messages"][0].content)
    assert finalization_payload["language"] == "zh-Hans"


@pytest.mark.asyncio
async def test_iteration_cap_uses_outcome_only_finalization_without_grant_history() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events(
                "apply-invalid",
                {"mutations": [{"grant_token": "secret-cap-grant"}]},
            ),
            [
                ProviderTextDeltaEvent(text="Please try again."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        await controller.reject_proposal(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="not_attempted",
            effect_state="none",
            retry_policy="same_turn",
            loop_action="continue",
            code="DOCUMENT_MUTATIONS_INVALID",
            is_error=True,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            max_iterations=1,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    finalization_wire = _serialized_messages(provider.calls[1])
    for forbidden in ("secret-cap-grant", "document_apply", "grant_token"):
        assert forbidden not in finalization_wire
    assert done.document_mutation_outcome["status"] == "not_attempted"
    assert done.document_mutation_outcome["retryPolicy"] == "new_turn"
    assert done.document_mutation_outcome["code"] == (
        "document_mutation_iteration_budget_exhausted"
    )


@pytest.mark.asyncio
async def test_unverified_tool_model_answers_without_starting_mutation_lifecycle() -> None:
    provider = _ScriptedMutationProvider(
        [[ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")]]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("an unverified model must never dispatch a document tool")

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            tools_verified=False,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 1
    assert provider.calls[0]["tools"] is None
    assert done.text == "No change."
    assert done.document_mutation_outcome is None
    assert controller.observed == []


@pytest.mark.asyncio
async def test_terminal_provider_error_before_apply_uses_ordinary_error_path() -> None:
    provider = _ScriptedMutationProvider(
        [
            [ProviderErrorEvent(message="provider unavailable", code="fatal")],
            [ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("the failed provider emitted no document tool")

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            max_provider_retries=0,
        ).run_turn("edit")
    ]
    assert len(provider.calls) == 1
    assert any(event.kind == "error" for event in events)
    assert not any(event.kind == "done" for event in events)
    assert controller.observed == []


@pytest.mark.asyncio
async def test_raised_provider_stream_before_apply_uses_ordinary_retry() -> None:
    provider = _ScriptedMutationProvider(
        [
            [RuntimeError("secret provider path /tmp/private-provider-state")],
            [ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("the failed provider emitted no document tool")

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert all(call["tools"] is not None for call in provider.calls)
    assert done.text == "No change."
    assert done.document_mutation_outcome is None
    assert controller.observed == []


@pytest.mark.asyncio
async def test_raised_provider_timeout_before_apply_uses_ordinary_retry() -> None:
    provider = _ScriptedMutationProvider(
        [
            [TimeoutError("private socket timeout /tmp/provider-state")],
            [ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("the timed-out provider emitted no document tool")

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert all(call["tools"] is not None for call in provider.calls)
    assert done.text == "No change."
    assert done.document_mutation_outcome is None
    assert controller.observed == []


@pytest.mark.asyncio
async def test_correctable_proposal_reenters_global_loop_then_reports_corrected_apply() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-bad", {"mutations": []}),
            _tool_call_events("apply-good", {"mutations": [{"grant_token": "opaque"}]}),
            [ProviderTextDeltaEvent(text="Updated."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()
    handler_calls = 0

    async def handler(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        if handler_calls == 1:
            await controller.reject_proposal(call.tool_use_id)
            return _effect_result(
                call.tool_use_id,
                status="not_attempted",
                effect_state="none",
                retry_policy="same_turn",
                loop_action="continue",
                code="DOCUMENT_MUTATIONS_INVALID",
                is_error=True,
            )
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
            corrected=True,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 3
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is not None
    assert provider.calls[2]["tools"] is None
    assert provider.calls[0]["config"].max_tokens == 16_384
    assert provider.calls[1]["config"].max_tokens == 8_192
    assert handler_calls == 2
    assert controller.observed == ["apply-bad", "apply-good"]
    assert controller.rejected == ["apply-bad"]
    assert done.document_mutation_outcome["corrected"] is True
    assert done.document_mutation_outcome["proposalAttempts"] == 2


@pytest.mark.asyncio
async def test_two_document_apply_calls_in_one_response_are_rejected_before_dispatch() -> None:
    provider = _ScriptedMutationProvider(
        [
            [
                *_tool_call_events("apply-a", {"mutations": []})[:-1],
                *_tool_call_events("apply-b", {"mutations": []}),
            ],
            [
                ProviderTextDeltaEvent(text="No mutation."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()
    handler_calls = 0

    async def handler(_call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        raise AssertionError("parallel writers must be rejected before dispatch")

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert handler_calls == 0
    assert done.document_mutation_outcome["code"] == "document_parallel_writers"
    assert done.document_mutation_outcome["status"] == "not_attempted"


@pytest.mark.asyncio
async def test_incomplete_streamed_apply_preserves_outcome_and_uses_finalization_call() -> None:
    provider = _ScriptedMutationProvider(
        [
            [
                ProviderToolUseStartEvent(
                    tool_use_id="apply-incomplete",
                    tool_name="document_apply",
                ),
                ProviderErrorEvent(
                    message="provider stream ended",
                    code="incomplete_tool_call",
                ),
            ],
            [ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(_call: Any) -> ToolResult:
        raise AssertionError("incomplete writer must not dispatch")

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome == {
        "version": 1,
        "status": "not_attempted",
        "phase": "proposal",
        "retryPolicy": "new_turn",
        "code": "document_mutation_proposal_incomplete",
    }


@pytest.mark.asyncio
async def test_global_tool_error_budget_closes_proposals_but_keeps_finalization_slot() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-invalid", {"mutations": []}),
            [
                ProviderTextDeltaEvent(text="Please try again."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        await controller.reject_proposal(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="not_attempted",
            effect_state="none",
            retry_policy="same_turn",
            loop_action="continue",
            code="DOCUMENT_MUTATIONS_INVALID",
            is_error=True,
        )

    agent = _agent(provider, controller, handler)
    agent.config.max_turn_tool_errors = 1
    events = [event async for event in agent.run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["code"] == "document_mutation_budget_exhausted"
    assert done.document_mutation_outcome["retryPolicy"] == "new_turn"


@pytest.mark.asyncio
async def test_finalization_provider_failure_keeps_outcome_and_local_fallback() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [ProviderErrorEvent(message="provider unavailable", code="503")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text == "The document changes were applied."
    assert any(
        event.kind == "warning"
        and event.code == "document_mutation_finalization_degraded"
        for event in events
    )


@pytest.mark.asyncio
async def test_finalization_provider_error_closes_the_underlying_stream() -> None:
    provider = _CloseAwareMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [ProviderErrorEvent(message="provider unavailable", code="503")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert done.document_mutation_outcome["status"] == "applied"
    assert len(provider.streams) == 2
    assert all(stream.closed for stream in provider.streams)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_finalization",
    [
        [ProviderDoneEvent(stop_reason="end_turn")],
        [ProviderTextDeltaEvent(text="partial finalizer output")],
    ],
)
async def test_invalid_finalization_is_one_shot_and_uses_local_fallback(
    invalid_finalization: list[object],
) -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            invalid_finalization,
            [
                ProviderTextDeltaEvent(text="A third provider call must not happen."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text == "The document changes were applied."
    assert any(
        event.kind == "warning"
        and event.code == "document_mutation_finalization_degraded"
        for event in events
    )


@pytest.mark.asyncio
async def test_silent_finalization_keeps_committed_outcome_and_uses_local_fallback() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [
                ProviderTextDeltaEvent(text="NO_REPLY"),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
            [
                ProviderTextDeltaEvent(text="A third provider call must not happen."),
                ProviderDoneEvent(stop_reason="end_turn"),
            ],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text == "The document changes were applied."
    assert any(
        event.kind == "warning"
        and event.code == "document_mutation_finalization_degraded"
        for event in events
    )


@pytest.mark.asyncio
async def test_post_tool_iteration_timeout_still_runs_outcome_finalization() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [ProviderTextDeltaEvent(text="Updated."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = []
    async for event in _agent(
        provider,
        controller,
        handler,
        iteration_timeout=1.0,
    ).run_turn("edit"):
        events.append(event)
        if event.kind == "tool_result":
            # Cross the iteration deadline only after the authoritative tool
            # result has been emitted. Sleeping inside the handler races the
            # tool timeout itself and makes the test scheduler-dependent.
            time.sleep(1.1)
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text.endswith("The document changes were applied.")
    assert not any(event.kind == "error" for event in events)


@pytest.mark.asyncio
async def test_terminal_provider_failure_closes_a_retryable_proposal_outcome() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-invalid", {"mutations": []}),
            [ProviderErrorEvent(message="provider unavailable", code="fatal")],
            [ProviderTextDeltaEvent(text="No change."), ProviderDoneEvent(stop_reason="end_turn")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        await controller.reject_proposal(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="not_attempted",
            effect_state="none",
            retry_policy="same_turn",
            loop_action="continue",
            code="DOCUMENT_MUTATIONS_INVALID",
            is_error=True,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            max_provider_retries=0,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 3
    assert provider.calls[2]["tools"] is None
    assert done.document_mutation_outcome["status"] == "not_attempted"
    assert done.document_mutation_outcome["retryPolicy"] == "new_turn"
    assert done.document_mutation_outcome["code"] == "document_mutation_provider_failed"


@pytest.mark.asyncio
async def test_raised_finalization_stream_keeps_committed_outcome_and_local_fallback() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [RuntimeError("secret finalizer failure /tmp/private-finalizer-state")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [event async for event in _agent(provider, controller, handler).run_turn("edit")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text == "The document changes were applied."
    assert "private-finalizer-state" not in done.text
    assert any(
        event.kind == "warning"
        and event.code == "document_mutation_finalization_degraded"
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh-Hans", "文档修改已成功应用。"),
        ("de", "Die Dokumentänderungen wurden angewendet."),
        ("es", "Se aplicaron los cambios del documento."),
        ("fr", "Les modifications du document ont été appliquées."),
        ("ja", "文書の変更を適用しました。"),
    ],
)
async def test_finalization_failure_fallback_uses_the_client_locale(
    locale: str,
    expected: str,
) -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [ProviderErrorEvent(message="provider unavailable", code="503")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            locale=locale,
        ).run_turn("edit")
    ]
    done = next(event for event in events if event.kind == "done")

    assert done.document_mutation_outcome["status"] == "applied"
    assert done.text == expected


@pytest.mark.asyncio
async def test_finalization_prefers_the_annotation_turn_language_over_ui_locale() -> None:
    provider = _ScriptedMutationProvider(
        [
            _tool_call_events("apply-committed", {"mutations": []}),
            [ProviderErrorEvent(message="provider unavailable", code="503")],
        ]
    )
    controller = _MutationController()

    async def handler(call: Any) -> ToolResult:
        controller.committed_ids.add(call.tool_use_id)
        return _effect_result(
            call.tool_use_id,
            status="applied",
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            code="document_mutation_applied",
            is_error=False,
        )

    events = [
        event
        async for event in _agent(
            provider,
            controller,
            handler,
            locale="en",
        ).run_turn("请按批注修改文档")
    ]
    done = next(event for event in events if event.kind == "done")

    assert provider.calls[1]["tools"] is None
    finalization_payload = json.loads(provider.calls[1]["messages"][0].content)
    assert finalization_payload["language"] == "zh-Hans"
    assert done.text == "文档修改已成功应用。"
