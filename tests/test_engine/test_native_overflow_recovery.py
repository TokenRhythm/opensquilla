"""A real adapter's admitted request can still be rejected by the service."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent
from opensquilla.engine.agent import _CompactionParentDeadlineError
from opensquilla.engine.history import repair_tool_pairing
from opensquilla.engine.types import CompactionEvent
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    OpenAIProvider,
    ToolDefinition,
)
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText

CURRENT = "Continue the corrected west-only task; do not repeat completed work."


class NativeOverflowProvider(OpenAIProvider):
    def __init__(self, *, partial=False, provider_kind="openai"):
        super().__init__(api_key="synthetic-unused", model="synthetic-native-overflow",
                         provider_kind=provider_kind)
        self.calls = []
        self.partial = partial

    async def chat(self, messages, tools=None, config=None):
        projection = self.project_final_request(messages, tools, config)
        assert projection.fits
        self.calls.append((deepcopy(messages), projection, config))
        if self.partial:
            yield ProviderText(text="Already visible response.")
            yield ProviderError(message="context length exceeded", code="context_length_exceeded")
            return
        async for event in super().chat(messages, tools=tools, config=config):
            yield event


def setup_case(
    monkeypatch, *, retries=2, reject_count=1, tool_history=False, partial=False,
    context_window=32_768, history_override=None, minimum_reduction_chars=0, system_prompt="",
    error_body=None, provider_kind="openai",
):
    provider = NativeOverflowProvider(partial=partial, provider_kind=provider_kind)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=context_window,
            context_window_tokens_global_override=context_window,
            system_prompt=system_prompt,
            context_window_known=True, max_tokens=2048, max_provider_retries=0,
            max_overflow_retries=retries, timeout=10, iteration_timeout=10,
            model_capabilities=ModelCapabilities(supports_tools=True),
            tool_result_compression_enabled=False,
            tool_result_projection_max_inline_chars=2_000_000,
        ),
        tool_definitions=[ToolDefinition(
            name="lookup", description="Read a record", input_schema={},
        )]
        if tool_history else None,
    )
    history = [
        Message(role="user", content="Original task: report deployment status; owner Mira."),
        Message(role="assistant", content="Earlier analysis. " + "a" * 10_000),
        Message(role="user", content="Cancel east deployment; only west remains authorized."),
        Message(role="assistant", content="Review recorded. " + "b" * 10_000),
        Message(role="user", content="Continue checking the recorded status."),
        Message(role="assistant", content="West deployment already completed and verified."),
    ]
    if tool_history:
        history[1:2] = [
            Message(role="assistant", content=[ContentBlockToolUse(
                id="historic-call", name="lookup", input={"target": "west"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id="historic-call", content="Earlier analysis. " + "a" * 10_000,
            )]),
            Message(role="assistant", content="Recorded lookup completed."),
        ]
    if history_override is not None:
        history = history_override
    agent.set_history(history)
    physical = []

    def handle(request):
        physical.append(json.loads(request.content))
        insufficient_reduction = minimum_reduction_chars and (
            len(json.dumps(physical[-1], ensure_ascii=False))
            > len(json.dumps(physical[0], ensure_ascii=False)) - minimum_reduction_chars
        )
        if len(physical) <= reject_count or insufficient_reduction:
            return httpx.Response(400, json=error_body or {"error": {
                "message": "maximum context length exceeded", "code": "context_length_exceeded",
                "type": "invalid_request_error",
            }})
        chunks = [
            {"choices": [{"delta": {"content": "Continued safely."}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=(body + "data: [DONE]\n\n").encode())

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}),
    )

    async def unexpected_summary(*args, **kwargs):
        raise AssertionError("A service-only rejection must not generate or persist a summary")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", unexpected_summary)
    return agent, provider, history, physical


async def test_wrapped_input_range_http400_enters_existing_native_recovery(monkeypatch):
    history = large_user_history(32_768)
    wrapped_error = {
        "code": "LITELLM_ERROR",
        "message": "litellm.BadRequestError: OpenAIException - data: " + json.dumps({
            "error": {"code": "invalid_parameter_error", "param": None,
                      "message": "Range of input length should be [1, 983616]",
                      "type": "invalid_request_error"},
        }) + ". Received Model Group=qwen3.8-flash\nAvailable Model Group Fallbacks=None",
    }
    agent, provider, history, physical = setup_case(
        monkeypatch, history_override=history, error_body=wrapped_error,
        provider_kind="tokenrhythm", minimum_reduction_chars=len(history[2].content) // 2,
    )
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == 2
    assert all(call[1].fits for call in provider.calls)
    assert physical[1] == provider.calls[1][1].payload
    assert CURRENT in json.dumps(physical[1], ensure_ascii=False)
    assert len(json.dumps(physical[1])) < len(json.dumps(physical[0]))
    assert agent.history_snapshot()[:len(history)] == history
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, (CompactionEvent, ErrorEvent)) for event in events)


def large_user_history(window, *, with_tool=False):
    history = []
    for index in range(5):
        history.extend([
            Message(role="user", content=f"Historical request {index}: " + "x " * (window // 8)),
            Message(role="assistant", content="Acknowledged. " * 12),
        ])
    if with_tool:
        history[-1:] = [
            Message(role="assistant", content=[ContentBlockToolUse(
                id="recent-completed-call", name="lookup", input={"target": "west"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id="recent-completed-call", content="Verified west; do not repeat.",
            )]),
            Message(role="assistant", content="The west lookup is complete."),
        ]
    return history


@pytest.mark.parametrize("window", [32_768, 262_144])
@pytest.mark.parametrize("with_tool", [False, True])
async def test_native_overflow_releases_large_history_not_only_small_wrappers(
    monkeypatch, window, with_tool,
):
    history = large_user_history(window, with_tool=with_tool)
    # The hidden service limit lies between removing only small acknowledgements
    # and removing one complete old user request. It is a controlled mismatch,
    # not a claim about the real backend's tokenizer or physical context window.
    minimum_reduction = len(history[2].content) // 2
    agent, provider, history, physical = setup_case(
        monkeypatch, context_window=window, history_override=history,
        tool_history=with_tool, minimum_reduction_chars=minimum_reduction,
        system_prompt="Keep the current deployment authorization and safety rules.",
    )
    frozen = deepcopy(history)
    events = [event async for event in agent.run_turn(CURRENT)]

    assert len(physical) == len(provider.calls) == 2
    assert all(call[1].fits for call in provider.calls)
    assert len(json.dumps(physical[0], ensure_ascii=False)) - len(
        json.dumps(physical[1], ensure_ascii=False),
    ) >= minimum_reduction
    assert physical[1] == provider.calls[1][1].payload
    assert physical[1]["messages"][0] == physical[0]["messages"][0]
    assert physical[1].get("tools") == physical[0].get("tools")
    current = next(message for message in physical[0]["messages"]
                   if message["role"] == "user" and CURRENT in str(message["content"]))
    assert current in physical[1]["messages"]
    retry = provider.calls[1][0]
    assert repair_tool_pairing(retry) == retry
    if with_tool:
        assert all(message in retry for message in history[-3:])
    assert not any(isinstance(event, (CompactionEvent, ErrorEvent)) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)
    assert agent.history_snapshot()[:len(history)] == history == frozen


async def test_native_overflow_rechecks_headroom_after_async_rebuild(monkeypatch):
    history = large_user_history(32_768)
    agent, provider, history, physical = setup_case(monkeypatch, history_override=history)
    original = agent._provider_request_messages_async

    async def rebuild(messages, **kwargs):
        if provider.calls:
            # The async boundary restores almost all rejected history, while
            # still reducing both estimates. Strictly smaller alone is not fit.
            return [message for message in provider.calls[0][0] if message not in history[1::2]]
        return await original(messages, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == 1
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, (CompactionEvent, DoneEvent)) for event in events)
    assert agent.history_snapshot() == history


@pytest.mark.parametrize("missing", [
    ("estimated_tokens",), ("estimated_chars",), ("estimated_tokens", "estimated_chars"),
])
async def test_native_overflow_unknown_estimates_do_not_allow_blind_retries(monkeypatch, missing):
    history = large_user_history(32_768)
    agent, provider, history, physical = setup_case(monkeypatch, history_override=history)
    original = provider.project_final_request

    def project(*args, **kwargs):
        projection = original(*args, **kwargs)
        return replace(projection, proof={
            name: value for name, value in projection.proof.items() if name not in missing
        })

    monkeypatch.setattr(provider, "project_final_request", project)
    events = [event async for event in agent.run_turn(CURRENT)]
    if len(missing) == 2:
        assert len(physical) == len(provider.calls) == 1
        assert any(isinstance(event, ErrorEvent) for event in events)
        assert not any(isinstance(event, DoneEvent) for event in events)
        assert agent.history_snapshot() == history
    else:
        assert len(physical) == len(provider.calls) == 2
        assert any(isinstance(event, DoneEvent) for event in events)
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert agent.history_snapshot()[:len(history)] == history
    assert not any(isinstance(event, CompactionEvent) for event in events)


async def test_native_overflow_keeps_pending_protocol_state_while_releasing_other_history(
    monkeypatch,
):
    from opensquilla.execution_status import normalize_execution_status

    history = large_user_history(32_768)
    pending = [
        Message(role="assistant", content=[ContentBlockToolUse(
            id="pending-check", name="lookup", input={"target": "west"},
        )]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="pending-check", content="Still checking the deployment.",
            execution_status=normalize_execution_status({
                "status": "unknown", "reason": "pending", "source": "tool_runtime",
                "preservation_class": "ephemeral",
            }),
        )]),
        Message(role="assistant", content="Await the pending check before acting."),
    ]
    history[1:2] = pending
    agent, provider, history, physical = setup_case(
        monkeypatch, history_override=history, tool_history=True,
        minimum_reduction_chars=4000,
    )
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == 2
    retry = provider.calls[1][0]
    assert all(message in retry for message in pending)
    assert repair_tool_pairing(retry) == retry
    assert physical[1] == provider.calls[1][1].payload
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, (CompactionEvent, ErrorEvent)) for event in events)
    assert agent.history_snapshot()[:len(history)] == history


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_history", [False, True])
async def test_native_overflow_retries_a_smaller_final_payload_without_durable_write(
    monkeypatch, tool_history,
):
    agent, provider, history, physical = setup_case(monkeypatch, tool_history=tool_history)
    frozen = deepcopy(history)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == 2
    assert all(call[1].fits for call in provider.calls)
    assert len(json.dumps(physical[1], ensure_ascii=False)) < len(
        json.dumps(physical[0], ensure_ascii=False)
    )
    for metric in ("estimated_tokens", "estimated_chars"):
        assert provider.calls[1][1].proof[metric] < provider.calls[0][1].proof[metric]
    assert physical[1] == provider.calls[1][1].payload
    retry = provider.calls[1][0]
    assert repair_tool_pairing(retry) == retry
    retry_text = json.dumps(physical[1], ensure_ascii=False)
    assert CURRENT in retry_text and history[0].content in retry_text
    assert "Cancel east deployment; only west remains authorized." in retry_text
    assert not any(isinstance(event, (CompactionEvent, ErrorEvent)) for event in events)
    assert any(isinstance(event, DoneEvent) and event.text == "Continued safely."
               for event in events)
    assert agent.history_snapshot()[:len(history)] == frozen
    assert history == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected_calls", [("repeat", 3), ("disabled", 1), ("minimal", 1)])
async def test_native_overflow_has_no_unchanged_or_unbounded_retry(
    monkeypatch, mode, expected_calls,
):
    agent, provider, history, physical = setup_case(
        monkeypatch, retries=0 if mode == "disabled" else 2, reject_count=100,
    )
    if mode == "minimal":
        history = []
        agent.set_history(history)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == expected_calls
    assert not any(isinstance(event, CompactionEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors and errors[-1].code == "provider_request_too_large"
    assert agent.history_snapshot() == history


@pytest.mark.asyncio
async def test_native_overflow_uses_configured_second_reduction_and_next_turn(monkeypatch):
    agent, provider, history, physical = setup_case(monkeypatch, retries=2, reject_count=2)
    frozen = deepcopy(history)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == 3
    sizes = [len(json.dumps(payload, ensure_ascii=False)) for payload in physical]
    assert sizes[0] > sizes[1] > sizes[2]
    assert all(CURRENT in json.dumps(payload) for payload in physical)
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, (ErrorEvent, CompactionEvent)) for event in events)
    assert agent.history_snapshot()[:len(history)] == frozen
    next_prompt = "Continue the same west-only task on the next turn."
    next_events = [event async for event in agent.run_turn(next_prompt)]
    assert any(isinstance(event, DoneEvent) for event in next_events)
    assert not any(isinstance(event, ErrorEvent) for event in next_events)
    assert next_prompt in json.dumps(physical[-1])


@pytest.mark.asyncio
async def test_native_overflow_does_not_retry_after_visible_output(monkeypatch):
    agent, provider, _history, physical = setup_case(monkeypatch, partial=True)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(provider.calls) == 1 and not physical
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, CompactionEvent) for event in events)


@pytest.mark.asyncio
async def test_native_overflow_rechecks_rebuilt_payload_before_retry(monkeypatch):
    agent, provider, history, physical = setup_case(monkeypatch)
    original = agent._provider_request_messages_async

    async def rebuild(messages, **kwargs):
        if provider.calls:
            return provider.calls[0][0]
        return await original(messages, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == len(provider.calls) == 1
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent.history_snapshot() == history


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "deadline"])
async def test_forced_shrink_preserves_cancellation_and_parent_deadline(monkeypatch, failure):
    agent, provider, history, physical = setup_case(monkeypatch)
    frozen = deepcopy(history)
    agent._current_turn_message = CURRENT
    messages = [*history, Message(role="user", content=CURRENT)]
    runtime = agent._freeze_preflight_runtime_context_message()
    config = agent._provider_admission_chat_config(
        CURRENT, context_window_tokens=32_768, max_output_tokens=2048,
    ).model_copy(update={"turn_deadline_at_monotonic": (
        asyncio.get_running_loop().time() + (0.15 if failure == "deadline" else 5)
    )})
    rejected = agent._provider_request_messages_for_count_projection(
        messages, request_context_message=None, request_context_insert_index=len(history),
        runtime_context_message=runtime, runtime_context_insert_index=len(history),
    )
    assert provider.project_final_request(rejected, [], config).fits
    started = asyncio.Event()
    closed = []

    async def wait(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(agent, "_provider_request_messages_async", wait)
    task = asyncio.create_task(agent._recover_progressed_request_window(
        messages, rejected_messages=rejected, protected_turn_start_index=len(history),
        request_context_insert_index=len(history), runtime_context_insert_index=len(history),
        chat_config=config, tools=[], request_context_message=None,
        runtime_context_message=runtime, request_suffix_messages=[], force_shrink=True,
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    if failure == "cancel":
        task.cancel()
    expected_error = (
        asyncio.CancelledError if failure == "cancel" else _CompactionParentDeadlineError
    )
    with pytest.raises(expected_error):
        await task
    assert closed == [True] and not physical
    assert agent.history_snapshot() == history == frozen
