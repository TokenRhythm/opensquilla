from __future__ import annotations

from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import (
    ContentBlockImage,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    OpenAIProvider,
    ProviderMessageLimitProof,
)
from opensquilla.session.compaction import (
    CompactionResult,
    _validate_forced_prefix_cut,
    effective_protected_recent_messages,
)
from opensquilla.session.compaction_budget import resolve_compaction_budget
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


@pytest.mark.parametrize("window,output", [(262144, 131072), (256000, 16000), (1000000, 384000)])
@pytest.mark.parametrize("entry", ["inline", "live", "message_count"])
async def test_in_turn_entrypoints_bind_shared_physical_budget_and_raw_tail_admission(
    monkeypatch: pytest.MonkeyPatch, entry: str, window: int, output: int,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=window, max_tokens=output, model_id="synthetic",
    ))
    agent._current_turn_message = "continue the current task"
    messages = [
        Message(role="user" if index % 2 == 0 else "assistant", content=f"old turn {index}")
        for index in range(40)
    ]
    protected_start = len(messages)
    active = Message(role="user", content=agent._current_turn_message)
    messages.append(active)
    for index in range(6):
        messages.extend([
            Message(role="assistant", content=[ContentBlockToolUse(
                id=f"step-{index}", name="read_file", input={"path": f"file-{index}"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id=f"step-{index}", content=f"completed step {index}",
            )]),
        ])
    before = [message.model_copy(deep=True) for message in messages]
    requests: list[Any] = []

    async def capture(request: Any) -> CompactionResult:
        requests.append(request)
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0,
            chunks_processed=0, skip_reason="synthetic_summary_refused",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", capture)
    config = agent._provider_admission_chat_config(
        agent._current_turn_message, context_window_tokens=window, max_output_tokens=output,
    )
    runtime = agent._freeze_preflight_runtime_context_message()
    if entry == "inline":
        await agent._check_context_overflow(
            messages, window + 1, protected_turn_start_index=protected_start,
            request_context_insert_index=protected_start,
            runtime_context_insert_index=protected_start,
        )
    elif entry == "live":
        await agent._recover_live_turn_request_overflow(
            messages, protected_turn_start_index=protected_start,
            context_window_tokens=window - output,
            request_context_insert_index=protected_start,
            runtime_context_insert_index=protected_start,
            consumer_chat_config=config,
        )
    else:
        projection = agent._project_provider_request_message_count(
            messages, config=config, request_context_message=None,
            request_context_insert_index=protected_start,
            runtime_context_message=runtime, runtime_context_insert_index=protected_start,
        )
        assert projection is not None
        proof = ProviderMessageLimitProof(
            actual_wire_messages=projection.actual_wire_messages, limit=22,
            logical_messages=projection.logical_messages,
            system_messages=projection.system_messages,
            tool_result_messages=projection.tool_result_messages,
            provider_kind=projection.provider_kind, model=projection.model,
            base_host=projection.base_host,
        )
        await agent._recover_provider_message_count_limit(
            messages, request_suffix_messages=[], proof=proof, config=config,
            request_context_message=None, request_context_insert_index=protected_start,
            runtime_context_message=runtime, runtime_context_insert_index=protected_start,
            protected_turn_start_index=protected_start,
        )

    assert len(requests) == 1
    request = requests[0]
    budget = request.config.budget
    assert budget is not None
    assert budget.physical_context_window_tokens == window
    assert budget.generation_reserve_tokens == output
    assert 0 < budget.history_capacity_tokens < window - output
    assert request.context_window_tokens == budget.history_capacity_tokens
    assert request.context_window_chars == budget.history_capacity_chars
    assert request.consumer_admission is budget.consumer_admission
    assert messages == before
    kept = [] if entry == "live" else request.entries[protected_start:]
    assert request.consumer_admission("Completed older work", kept)
    assert not request.consumer_admission("huge summary " * window, kept)
    # Structured tool results are replayed from the original Message objects,
    # not their flattened summary input. A changed raw tail invalidates live
    # template identity or refuses the now-oversized candidate.
    messages[-1].content[0].content = "raw tool result " * window
    if entry == "live":
        from opensquilla.session.compaction_lifecycle import ConsumerAdmissionStaleError

        with pytest.raises(ConsumerAdmissionStaleError):
            request.consumer_admission("Completed older work", kept)
    else:
        assert not request.consumer_admission("Completed older work", kept)


async def test_live_prefix_budget_reserves_native_media_and_exhausted_raw_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=12_000, max_tokens=8192,
    ))
    agent._current_turn_message = "inspect the attached image"
    messages = [Message(role="user", content=agent._current_turn_message)]
    for index in range(4):
        messages.extend([
            Message(role="assistant", content=[ContentBlockToolUse(
                id=f"read-{index}", name="read_file", input={"path": "image.png"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id=f"read-{index}", content="result " * 8_000,
            )]),
        ])
    messages.append(Message(role="user", content=[ContentBlockImage(
        media_type="image/png", data="c3ludGhldGlj",
    )]))
    requests: list[Any] = []

    async def capture(request: Any) -> CompactionResult:
        requests.append(request)
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0,
            chunks_processed=0, skip_reason="non_history_envelope_exhausts_budget",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", capture)
    await agent._recover_live_turn_request_overflow(
        messages, protected_turn_start_index=0, context_window_tokens=3000,
        request_context_insert_index=0, runtime_context_insert_index=0,
    )
    assert len(requests) == 1
    budget = requests[0].config.budget
    assert budget.history_capacity_tokens == 0
    assert not budget.consumer_admission("small summary", [])


@pytest.mark.parametrize("failure", ["already_attempted", "exception", "rejected"])
async def test_failed_live_compaction_fallback_keeps_the_actual_request_suffix(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=16_000, max_tokens=1_024,
    ))
    agent._current_turn_message = "Continue the current task."
    agent._compaction_failed_this_turn = failure == "already_attempted"

    async def refuse(request):
        if failure == "exception":
            raise RuntimeError("Synthetic summary failure.")
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0,
            chunks_processed=0, skip_reason="summary_rejected",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", refuse)
    messages = [
        Message(role="user", content="Earlier request."),
        Message(role="assistant", content="Earlier request completed."),
        Message(role="user", content=agent._current_turn_message),
    ]
    for index in range(6):
        messages.extend([
            Message(role="assistant", content=[ContentBlockToolUse(
                id=f"done-{index}", name="read_file", input={"path": "done.txt"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id=f"done-{index}", content="Completed read.",
            )]),
        ])
    config = agent._provider_admission_chat_config(
        agent._current_turn_message, context_window_tokens=16_000, max_output_tokens=1_024,
    )
    outcome = await agent._recover_live_turn_request_overflow(
        messages, protected_turn_start_index=2, context_window_tokens=12_000,
        request_context_insert_index=2, runtime_context_insert_index=2,
        consumer_chat_config=config,
        request_suffix_messages=[Message(role="user", content="a b c d e f g h " * 5_000)],
    )
    # Removing old rows cannot fix an oversized fixed finalization suffix.
    assert outcome is None


@pytest.mark.parametrize("envelope", ["suffix", "request_context", "runtime_context"])
async def test_inline_overflow_admission_includes_actual_fixed_request_envelope(
    monkeypatch: pytest.MonkeyPatch, envelope: str,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=16_000, max_tokens=1_024,
    ))
    agent._current_turn_message = "Continue the current task."
    messages = [
        Message(role="user", content="Earlier request."),
        Message(role="assistant", content="Earlier request completed."),
        Message(role="user", content=agent._current_turn_message),
    ]
    config = agent._provider_admission_chat_config(
        agent._current_turn_message, context_window_tokens=16_000, max_output_tokens=1_024,
    )
    fixed = Message(role="user", content="a b c d e f g h " * 5_000)
    requests = []

    async def capture(request):
        requests.append(request)
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0,
            chunks_processed=0, skip_reason="non_history_envelope_exhausts_budget",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", capture)
    outcome = await agent._check_context_overflow(
        messages, estimated_context_tokens=40_000, protected_turn_start_index=2,
        request_context_insert_index=2, runtime_context_insert_index=2,
        consumer_chat_config=config, provider_overflow=True,
        request_window_tokens=12_000,
        request_suffix_messages=[fixed] if envelope == "suffix" else None,
        request_context_message=fixed if envelope == "request_context" else None,
        runtime_context_message=fixed if envelope == "runtime_context" else None,
    )
    assert len(requests) == 1
    assert requests[0].config.budget.history_capacity_tokens == 0
    assert not requests[0].consumer_admission("Small checkpoint.", requests[0].entries[-1:])
    assert outcome is None


async def test_inline_compaction_honors_configured_trigger_and_effective_summary_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
        provider=provider, provider_id="synthetic", model="synthetic-summary",
        context_window_tokens=64_000, max_output_tokens=512,
    ),))
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=64_000, max_tokens=4096,
        compaction_trigger_ratio=0.6, context_overflow_threshold=0.85,
        compaction_profile="coding", compaction_execution_plan=plan,
    ))
    requests: list[Any] = []

    async def capture(request: Any) -> CompactionResult:
        requests.append(request)
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0,
            chunks_processed=0, skip_reason="within_compaction_budget",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", capture)
    messages = [Message(role="user" if i % 2 == 0 else "assistant", content=f"turn {i}")
                for i in range(20)]
    await agent._check_context_overflow(messages, estimated_context_tokens=45_000)
    assert len(requests) == 1
    budget = requests[0].config.budget
    assert budget.auto_trigger_tokens == int(budget.history_capacity_tokens * 0.6)
    assert budget.auto_trigger_chars == int(budget.history_capacity_chars * 0.6)
    assert budget.summary_output_tokens == 512
    assert budget.retained_tail_messages == 12
    # Being called after a full-request overflow does not grant manual force
    # or a prefix cut: the core still checks actual durable history pressure.
    assert not requests[0].force
    assert requests[0].forced_prefix_cut is None


@pytest.mark.parametrize("profile", ["coding", "research", "support"])
@pytest.mark.parametrize("projection_fails", [False, True])
async def test_live_completed_prefix_keeps_profile_policy_and_external_native_tail(
    monkeypatch: pytest.MonkeyPatch, profile: str, projection_fails: bool,
) -> None:
    provider = OpenAIProvider(api_key="synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=64_000, max_tokens=4096, compaction_profile=profile,
    ))
    agent._current_turn_message = "continue exact current task"
    messages = [Message(role="user", content=agent._current_turn_message)]
    for index in range(6):
        messages.extend([
            Message(role="assistant", content=[ContentBlockToolUse(
                id=f"native-{index}", name="read_file", input={"path": f"file-{index}"},
            )]),
            Message(role="user", content=[ContentBlockToolResult(
                tool_use_id=f"native-{index}", content=f"completed native step {index}",
            )]),
        ])
    original = [message.model_copy(deep=True) for message in messages]
    config = agent._build_compaction_config()
    config.budget = resolve_compaction_budget(
        project=lambda _summary, _kept: None, physical_context_window_tokens=64_000,
        generation_reserve_tokens=4096, retained_tail_messages=12,
    )
    requests: list[Any] = []

    async def summarize(request: Any) -> CompactionResult:
        requests.append(request)
        assert request.config.compaction_profile == profile
        assert request.config.budget.retained_tail_messages == 0
        cut, error = _validate_forced_prefix_cut(
            request.entries, request.forced_prefix_cut, request.config,
        )
        assert error is None and cut == len(request.entries)
        assert request.consumer_admission("Completed earlier file reads", [])
        return CompactionResult(
            summary="Completed earlier file reads", kept_entries=[],
            removed_count=cut, kept_start_index=cut, chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    fallbacks: list[dict[str, Any]] = []
    if projection_fails:
        def fail_projection(**_kwargs: Any) -> Any:
            raise RuntimeError("consumer projection failed")

        def recover_locally(_messages: list[Message], **kwargs: Any) -> None:
            fallbacks.append(kwargs)

        monkeypatch.setattr(agent, "_resolve_in_turn_compaction_budget", fail_projection)
        monkeypatch.setattr(agent, "_recover_local_request_window", recover_locally)
    outcome = await agent._recover_live_turn_request_overflow(
        messages, protected_turn_start_index=0, context_window_tokens=40_000,
        request_context_insert_index=0, runtime_context_insert_index=0,
        shared_compaction_config=config,
    )
    if projection_fails:
        assert outcome is None and not requests
        assert len(fallbacks) == 1
        assert fallbacks[0]["input_budget_tokens"] == 40_000
        assert fallbacks[0]["compaction_config"] is config
        assert messages == original
        assert config.protect_profile_tail and config.protect_semantic_tail
        assert effective_protected_recent_messages(config) == 12
        return
    assert len(requests) == 1
    assert outcome is not None and outcome.ephemeral_only and outcome.compacted
    assert outcome.messages[2] is messages[0]
    assert all(left is right for left, right in zip(outcome.messages[-4:], messages[-4:]))
    assert messages == original
    assert config.protect_profile_tail and config.protect_semantic_tail
    assert effective_protected_recent_messages(config) == 12
    _, restored_error = _validate_forced_prefix_cut(
        requests[0].entries, len(requests[0].entries), config,
    )
    assert restored_error == "forced_prefix_cut_overlaps_protected_tail"
