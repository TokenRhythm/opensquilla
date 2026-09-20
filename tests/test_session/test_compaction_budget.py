"""Budget contracts shared by idle/manual and active/automatic compaction."""

from __future__ import annotations

from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.gateway import compaction_target
from opensquilla.gateway.compaction_target import GatewayConsumerBudget
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.session.compaction_budget import resolve_compaction_budget
from opensquilla.session.compaction_lifecycle import ConsumerAdmissionStaleError

MODEL = "synthetic-budget-model"
TEMPLATE = "[candidate checkpoint]"


@pytest.fixture(autouse=True)
def isolated_character_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS", raising=False)


def _envelope(window: int = 262_144, output: int = 131_072, *, char_cap: int = 0):
    provider = OpenAIProvider(api_key="synthetic-key", model=MODEL)
    config = ChatConfig(
        max_tokens=output,
        provider_context_window_tokens=window,
        provider_request_max_chars=char_cap,
        provider_request_max_chars_explicit_cap=char_cap,
        system="Preserve completed decisions and pending obligations.",
        thinking=False,
    )
    tools: list[ToolDefinition] = []
    media: list[Any] = []

    def messages(summary: str, kept: list[dict[str, Any]]) -> list[Message]:
        return [
            *(Message(role=entry["role"], content=entry["content"]) for entry in kept),
            Message(role="user", content=f"Checkpoint: {summary}"),
            Message(role="user", content=[ContentBlockText(text="Continue the task."), *media]),
        ]

    def project(summary: str, kept: list[dict[str, Any]]):
        return provider.project_final_request(messages(summary, kept), tools, config)

    return provider, config, tools, media, messages, project


def _resolve(project, *, window: int = 262_144, output: int = 131_072, **kwargs):
    return resolve_compaction_budget(
        project=project,
        physical_context_window_tokens=window,
        generation_reserve_tokens=output,
        provider_identity=f"openai/{MODEL}:",
        **kwargs,
    )


@pytest.mark.parametrize("window,output", [
    (262_144, 131_072), (256_000, 16_000), (1_000_000, 384_000),
])
@pytest.mark.parametrize("persisted", [False, True])
def test_manual_and_automatic_entrypoints_share_identical_envelope_budget(
    monkeypatch: pytest.MonkeyPatch, window: int, output: int, persisted: bool,
) -> None:
    provider, config, tools, _media, messages, project = _envelope(window, output)
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=window, max_tokens=output,
    ))
    # Each entrypoint normally assembles a different request. Normalize only
    # that envelope here, so any divergence in their budget policy is visible.
    monkeypatch.setattr(
        agent, "_project_compaction_consumer_request",
        lambda **kwargs: project(kwargs["replay_summary"], kwargs["kept_entries"]),
    )
    monkeypatch.setattr(compaction_target, "_manual_consumer_messages", messages)
    monkeypatch.setattr(
        compaction_target, "project_provider_final_request",
        lambda target, request, _tools, _config: target.project_final_request(
            request, tools, config,
        ),
    )

    automatic = agent.resolve_compaction_budget(
        consumer_provider=provider,
        active_user_message="Continue the task.", active_user_in_history=persisted,
        bound_user_message_id=None, attachment_messages=None,
        context_window_tokens=window, max_output_tokens=output,
        consumer_model_id=MODEL,
    )
    manual = compaction_target.build_gateway_compaction_budget(GatewayConsumerBudget(
        provider=provider, provider_id="openai", model=MODEL,
        context_window_tokens=window, physical_context_window_tokens=window,
        max_output_tokens=output, provider_request_max_chars=window * 4,
        provider_request_max_chars_explicit_cap=0,
        next_request_reserve_tokens=0, next_request_reserve_chars=0,
    ))

    assert manual == automatic
    assert manual.consumer_admission_fingerprint == automatic.consumer_admission_fingerprint
    assert manual.consumer_admission("Completed initial work.", []) is True
    assert automatic.consumer_admission("Completed initial work.", []) is True
    assert manual.history_capacity_tokens > 0


@pytest.mark.parametrize("window,output", [
    (262_144, 131_072), (256_000, 16_000), (1_000_000, 384_000),
])
def test_provider_generation_reserve_is_subtracted_once(window: int, output: int) -> None:
    *_rest, project = _envelope(window, output)
    projection = project(TEMPLATE, [])
    budget = _resolve(project, window=window, output=output)
    # The provider proof has already removed generation plus its 20k physical
    # reserve and 4096 token proof margin for these large windows.
    expected = window - output - 20_000 - 4_096 - projection.proof["estimated_tokens"]
    assert projection.proof["raw_proof_token_budget"] == window - output - 20_000
    assert budget.generation_reserve_tokens == output
    assert budget.history_capacity_tokens == expected
    assert budget.auto_trigger_tokens == int(expected * 0.85)
    assert budget.retained_tail_tokens == expected // 5


def test_adapter_reasoning_cap_is_reserved_once() -> None:
    provider = AnthropicProvider(api_key="synthetic-key", model="claude-3-7-sonnet-latest")
    config = ChatConfig(
        max_tokens=1_024, thinking=True, thinking_budget_tokens=10_000,
        provider_context_window_tokens=64_000,
        provider_request_max_chars_explicit_cap=0,
    )

    def project(summary, kept):
        return provider.project_final_request([Message(role="user", content=summary)], [], config)

    projection = project(TEMPLATE, [])
    budget = _resolve(project, window=64_000, output=1_024)
    assert projection.payload["max_tokens"] == 14_096
    assert budget.generation_reserve_tokens == 14_096
    assert projection.proof["raw_proof_token_budget"] == 64_000 - 20_000 - 14_096
    assert budget.history_capacity_tokens == (
        projection.proof["effective_proof_token_budget"] - projection.proof["estimated_tokens"]
    )
    assert budget.consumer_admission("Complete portable checkpoint.", []) is True


@pytest.mark.parametrize("extra", ["tools", "media"])
def test_actual_tools_and_media_reduce_capacity_and_change_fingerprint(extra: str) -> None:
    _provider, _config, tools, media, _messages, project = _envelope()
    baseline = _resolve(project)
    if extra == "tools":
        tools.append(ToolDefinition(
            name="inspect", description="Detailed tool contract. " * 500,
            input_schema=ToolInputSchema(),
        ))
    else:
        media.append(ContentBlockImage(
            source_type="url", media_type="image/png", data="https://example.test/image.png",
        ))
    expanded = _resolve(project)

    assert expanded.history_capacity_tokens < baseline.history_capacity_tokens
    assert expanded.history_capacity_chars < baseline.history_capacity_chars
    assert expanded.consumer_admission_fingerprint != baseline.consumer_admission_fingerprint
    with pytest.raises(ConsumerAdmissionStaleError):
        baseline.consumer_admission("Checkpoint", [])


def test_explicit_character_cap_remains_independent_of_large_physical_window() -> None:
    *_rest, project = _envelope(1_000_000, 384_000, char_cap=8_000)
    budget = _resolve(project, window=1_000_000, output=384_000)

    assert budget.history_capacity_tokens > 500_000
    assert 0 < budget.history_capacity_chars < 8_000
    assert budget.consumer_admission("Small complete checkpoint", []) is True
    assert budget.consumer_admission("Oversized checkpoint. " * 1_000, []) is False


def test_explicit_history_limit_only_narrows_available_capacity() -> None:
    *_rest, project = _envelope()
    baseline = _resolve(project)
    narrow = _resolve(project, history_limit_tokens=1_000)
    larger = _resolve(project, history_limit_tokens=1_000_000)

    assert narrow.history_capacity_tokens == 1_000
    assert narrow.history_capacity_chars == 4_000
    assert narrow.consumer_admission("Oversized retained checkpoint. " * 500, []) is False
    assert larger.history_capacity_tokens == baseline.history_capacity_tokens
    assert larger.history_capacity_chars == baseline.history_capacity_chars
    assert larger.physical_context_window_tokens == baseline.physical_context_window_tokens


def test_exhausted_physical_window_never_manufactures_history_capacity() -> None:
    *_rest, project = _envelope(64_000, 128_000)
    budget = _resolve(project, window=64_000, output=128_000)

    assert budget.history_capacity_tokens == 0
    assert budget.auto_trigger_tokens == 0
    assert budget.retained_tail_tokens == 0
    assert budget.consumer_admission("Checkpoint", []) is False


def test_missing_projection_fails_closed() -> None:
    budget = _resolve(lambda summary, kept: None)

    assert budget.history_capacity_tokens == 0
    assert budget.history_capacity_chars == 0
    assert budget.consumer_admission("Checkpoint", []) is False


def test_changed_system_prompt_invalidates_the_frozen_envelope() -> None:
    _provider, config, _tools, _media, _messages, project = _envelope()
    budget = _resolve(project)
    config.system = "Different instructions with the same deployment."

    with pytest.raises(ConsumerAdmissionStaleError):
        budget.consumer_admission("Checkpoint", [])


def test_changed_proof_cap_invalidates_an_unchanged_wire_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_rest, project = _envelope()
    budget = _resolve(project)
    payload = project(TEMPLATE, []).payload
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS", "8000")
    assert project(TEMPLATE, []).payload == payload

    with pytest.raises(ConsumerAdmissionStaleError):
        budget.consumer_admission("Checkpoint", [])


def test_unknown_next_envelope_reserve_is_applied_once() -> None:
    *_rest, project = _envelope()
    baseline = _resolve(project)
    reserved = _resolve(project, envelope_reserve_tokens=8_000, envelope_reserve_chars=32_000)

    assert reserved.history_capacity_tokens == baseline.history_capacity_tokens - 8_000
    assert reserved.history_capacity_chars == baseline.history_capacity_chars - 32_000
    assert reserved.generation_reserve_tokens == baseline.generation_reserve_tokens
    assert reserved.consumer_admission_fingerprint != baseline.consumer_admission_fingerprint


def test_history_cap_includes_the_persisted_current_prompt() -> None:
    provider = OpenAIProvider(api_key="synthetic-key", model=MODEL)
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=64_000, max_tokens=1_024,
    ))
    prompt = "a b c d e f g h " * 800
    active = {"role": "user", "content": prompt, "message_id": "active"}

    def budget(persisted: bool, limit: int | None = None):
        return agent.resolve_compaction_budget(
            consumer_provider=provider, active_user_message=prompt,
            active_user_in_history=persisted,
            bound_user_message_id="active" if persisted else None,
            attachment_messages=None,
            context_window_tokens=64_000, max_output_tokens=1_024,
            history_limit_tokens=limit,
        )

    persisted = budget(True)
    pending = budget(False)
    assert agent.preflight_history_capacity(
        active_user_message=prompt, active_user_in_history=True,
        consumer_provider=provider, context_window_tokens=64_000,
        consumer_max_output_tokens=1_024,
    ) == (persisted.history_capacity_tokens, persisted.history_capacity_chars)
    assert persisted.history_capacity_tokens > pending.history_capacity_tokens + 6_000
    # Identical provider requests fit both allocations of the active row.
    assert persisted.consumer_admission("Complete checkpoint.", [active])
    assert pending.consumer_admission("Complete checkpoint.", [])
    # An explicit source cap includes the active row only when it is in source.
    assert not budget(True, 1_000).consumer_admission("Complete checkpoint.", [active])
    assert budget(False, 1_000).consumer_admission("Complete checkpoint.", [])
    assert budget(True, 8_000).consumer_admission("Complete checkpoint.", [active])


def test_persisted_native_media_stays_in_complete_admission_and_stale_identity() -> None:
    provider = OpenAIProvider(api_key="synthetic-key", model=MODEL)
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=64_000, max_tokens=1_024,
    ))
    media = ContentBlockImage(
        source_type="url", media_type="image/png", data="https://example.test/active.png",
    )
    attachment_messages = [Message(role="user", content=[
        ContentBlockText(text="Inspect this image."), media,
    ])]
    active = {"role": "user", "content": "Inspect this image.", "message_id": "active"}

    def budget(limit: int | None = None):
        return agent.resolve_compaction_budget(
            consumer_provider=provider, active_user_message="Inspect this image.",
            active_user_in_history=True, bound_user_message_id="active",
            attachment_messages=attachment_messages,
            context_window_tokens=64_000, max_output_tokens=1_024,
            history_limit_tokens=limit,
        )

    complete = budget()
    assert complete.consumer_admission("Complete checkpoint.", [active])
    # Native image occupancy remains in the source budget, even though the
    # durable row's text alone would easily fit this explicit narrow cap.
    assert not budget(100).consumer_admission("Complete checkpoint.", [active])
    media.data = "https://example.test/replaced.png"
    with pytest.raises(ConsumerAdmissionStaleError):
        complete.consumer_admission("Complete checkpoint.", [active])
