"""Tests for context window compaction logic."""

import asyncio
import base64
import json
from copy import deepcopy

import pytest

from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.attachment_manifest import (
    extract_attachment_occurrences_from_envelope,
)
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    _api_round_groups,
    _format_chunk_for_llm,
    _summarize_chunk_fallback,
    arm_compaction_deadline,
    await_compaction_phase,
    build_compaction_config_from_provider,
    call_compaction_llm,
    compact_context,
    compaction_remaining_seconds,
    compaction_replay_summary,
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
    estimate_entry_replay_tokens,
)
from opensquilla.session.compaction_lifecycle import (
    CompactionTimeoutError,
    compaction_effect_payload,
    compaction_result_payload,
)


def _native_replay_budget_message():
    from opensquilla.provider.types import Message

    thinking = "Synthetic reasoning step. " * 500
    native = [
        {"type": "thinking", "thinking": thinking, "signature": "synthetic-signature"},
        {"type": "redacted_thinking", "data": "synthetic-opaque-data"},
        {"type": "text", "text": "Synthetic answer", "citations": [{"title": "Synthetic source"}]},
        {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"count": 1}},
    ]
    return Message.model_validate({
        "role": "assistant", "content": native, "reasoning_content": thinking,
        "provider_replay": {
            "protocol": "anthropic_messages", "source": "synthetic-route", "model": "synthetic",
            "native_content": deepcopy(native),
        },
    })


@pytest.mark.parametrize("opaque_kind", ["signature", "redacted"])
def test_replay_budget_counts_one_native_copy_across_all_estimators(opaque_kind):
    from opensquilla.engine.history import HistoryReplayProjection, project_history_replay_capacity
    from opensquilla.provider.anthropic import _build_message_payload
    from opensquilla.provider.replay_budget import project_message_replay_budget
    from opensquilla.session.tokenizer import estimate_tokens

    message = _native_replay_budget_message()
    # This estimate covers the final assistant message without a pending tool.
    message.content.pop()
    message.provider_replay.native_content.pop()
    small_entry = {
        "assistant_replay": {"version": 1, "messages": [message.model_dump(mode="json")]},
    }
    small_tokens = estimate_entry_model_replay_tokens(small_entry)
    small_chars = estimate_entries_model_replay_chars([small_entry])
    index, field = (0, "signature") if opaque_kind == "signature" else (1, "data")
    opaque = "synthetic opaque state " * 1_000
    setattr(message.content[index], field, opaque)
    message.provider_replay.native_content[index][field] = opaque
    before = message.model_dump(mode="json")
    entry = {"role": "assistant", "assistant_replay": {"version": 1, "messages": [before]}}
    expected_wire = _build_message_payload(message, model="synthetic")
    wire_json = json.dumps(expected_wire, ensure_ascii=False, sort_keys=True)
    wire_tokens = estimate_tokens(wire_json)
    budget = project_message_replay_budget(message)

    assert budget == project_message_replay_budget(before)
    assert budget["content"] == expected_wire["content"]  # Includes citations and opaque blocks.
    assert "reasoning_content" not in budget
    assert "native_content" not in budget["provider_replay"]
    capacity = project_history_replay_capacity(HistoryReplayProjection(messages=(message,)))
    for measured in (capacity.estimated_tokens, estimate_entry_model_replay_tokens(entry)):
        assert wire_tokens <= measured < wire_tokens + 200
    assert len(wire_json) <= estimate_entries_model_replay_chars([entry]) < len(wire_json) + 500
    assert estimate_entry_model_replay_tokens(entry) > small_tokens + 1_000
    assert estimate_entries_model_replay_chars([entry]) > small_chars + 10_000
    assert message.model_dump(mode="json") == before
    assert entry["assistant_replay"]["messages"] == [before]


def test_anthropic_budget_preserves_reasoning_that_differs_from_native_thinking():
    from opensquilla.provider.replay_budget import project_message_replay_budget

    message = _native_replay_budget_message()
    message.reasoning_content = "Different display reasoning " * 1_000
    before = message.model_dump(mode="json")
    budget = project_message_replay_budget(before)
    assert budget["reasoning_content"] == message.reasoning_content
    assert budget["content"] == message.provider_replay.native_content
    entry = {"assistant_replay": {"version": 1, "messages": [before]}}
    without_display = deepcopy(entry)
    without_display["assistant_replay"]["messages"][0].pop("reasoning_content")
    assert estimate_entry_model_replay_tokens(entry) > (
        estimate_entry_model_replay_tokens(without_display) + 1_000
    )
    assert message.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "change", ["text", "bool_input", "float_input", "protocol", "unknown_block"],
)
def test_replay_budget_keeps_both_representations_when_native_equivalence_is_unproven(change):
    from opensquilla.provider.replay_budget import project_message_replay_budget

    message = _native_replay_budget_message()
    # A changed endpoint cannot justify discarding either current content or captured data.
    message.provider_replay.source = "different-synthetic-route"
    if change == "text":
        message.content[2].text = "Changed accepted answer"
    elif change in {"bool_input", "float_input"}:
        message.content[3].input["count"] = True if change == "bool_input" else 1.0
    elif change == "protocol":
        message.provider_replay.protocol = "future-protocol"
    else:
        message.provider_replay.native_content.append({"type": "future-block", "data": "opaque"})
    before = message.model_dump(mode="json")
    budget = project_message_replay_budget(before)
    assert budget["content"] == before["content"]
    assert budget["provider_replay"]["native_content"] == (
        before["provider_replay"]["native_content"]
    )
    assert budget["reasoning_content"] == before["reasoning_content"]
    assert message.model_dump(mode="json") == before


@pytest.mark.parametrize("display_matches", [True, False])
def test_openai_budget_deduplicates_only_display_alias_not_distinct_native_fields(display_matches):
    from opensquilla.provider.replay_budget import project_message_replay_budget

    message = {
        "role": "assistant", "content": "answer",
        "reasoning_content": "native text" if display_matches else "different display text",
        "provider_replay": {
            "protocol": "openai_chat_completions", "source": "synthetic-route",
            "model": "synthetic",
            "native_reasoning_content": "native text",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "native text"},
                {"type": "reasoning.encrypted", "data": "opaque " * 200},
            ],
        },
    }
    before = deepcopy(message)
    budget = project_message_replay_budget(message)
    assert ("reasoning_content" in budget) is not display_matches
    assert budget["provider_replay"] == message["provider_replay"]
    assert message == before


def test_replay_budget_preserves_typed_media_reserves_and_counts_raw_tool_json_as_text():
    from opensquilla.engine.history import HistoryReplayProjection, project_history_replay_capacity
    from opensquilla.provider.request_proof import estimate_provider_media_tokens
    from opensquilla.provider.types import ContentBlockDocument, ContentBlockImage, Message

    data = base64.b64encode(b"synthetic media" * 20).decode("ascii")
    message = Message(role="user", content=[
        ContentBlockImage(media_type="image/png", data=data),
        ContentBlockDocument(media_type="application/pdf", data=data),
    ])
    capacity = project_history_replay_capacity(HistoryReplayProjection(messages=(message,)))
    decoded_bytes = len(base64.b64decode(data))
    assert capacity.media_block_count == 2
    assert capacity.media_reserve_tokens == (
        estimate_provider_media_tokens("image", decoded_bytes)
        + estimate_provider_media_tokens("pdf", decoded_bytes)
    )
    assert capacity.estimate_complete
    raw = Message.model_validate({"role": "assistant", "content": [{
        "type": "tool_use", "id": "raw", "name": "lookup",
        "input": {"type": "image", "data": data * 100},
    }]})
    result = Message.model_validate({"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "raw", "content": "done",
    }]})
    raw_capacity = project_history_replay_capacity(HistoryReplayProjection(messages=(raw, result)))
    assert raw_capacity.media_block_count == 0
    assert raw_capacity.media_reserve_tokens == 0
    assert raw_capacity.estimated_tokens > capacity.estimated_tokens


def _make_entries(n: int, tokens_each: int = 100) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message {i} " + "x" * 50,
            "token_count": tokens_each,
        }
        for i in range(n)
    ]


def test_compaction_attachment_descriptor_is_safe_stable_and_not_truncated() -> None:
    image_data = base64.b64encode(b"image-bytes").decode("ascii")
    content = json.dumps(
        {
            # Deliberately put attachments first and pretty-print the object:
            # legacy detection must not depend on a compact ``{"text":`` prefix.
            "attachments": [
                {
                    "attachment_id": "/private/tmp/not-an-occurrence-id.png",
                    "path": "/private/tmp/material/image.png",
                    "name": "/private/tmp/upload/image.png",
                    "type": "image/png",
                    "data": image_data,
                }
            ],
            "text": "long prompt " + "x" * 500,
        },
        indent=2,
    )
    [occurrence] = extract_attachment_occurrences_from_envelope(
        content,
        session_id="session-image",
        source_message_id="message-image",
    )
    entry = {
        "id": 1,
        "session_id": "session-image",
        "message_id": "message-image",
        "role": "user",
        "content": content,
    }

    llm_input = _format_chunk_for_llm([entry])
    fallback = _summarize_chunk_fallback([entry], "strict")

    for rendered in (llm_input, fallback):
        assert occurrence.attachment_id in rendered
        assert "image.png (image/png" in rendered
        assert image_data not in rendered
        assert "/private/tmp" not in rendered
        assert '"path"' not in rendered
        assert "not-an-occurrence-id" not in rendered


@pytest.mark.asyncio
async def test_durable_attachment_summary_backfills_id_without_media_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_data = base64.b64encode(b"durable-image-bytes").decode("ascii")
    content = json.dumps(
        {
            "attachments": [
                {
                    "attachment_id": "/private/tmp/not-an-occurrence-id.png",
                    "path": "/private/tmp/material/image.png",
                    "name": "/private/tmp/upload/image.png",
                    "type": "image/png",
                    "data": image_data,
                }
            ],
            "text": "Inspect the archived image.",
        },
        indent=2,
    )
    [occurrence] = extract_attachment_occurrences_from_envelope(
        content,
        session_id="session-image",
        source_message_id="message-image",
    )
    entries = [
        {
            "id": 1,
            "session_id": "session-image",
            "message_id": "message-image",
            "role": "user",
            "content": content,
            "token_count": 5,
        },
        {
            "id": 2,
            "session_id": "session-image",
            "message_id": "message-answer",
            "role": "assistant",
            "content": "Earlier answer.",
            "token_count": 5,
        },
        {
            "id": 3,
            "session_id": "session-image",
            "message_id": "message-current",
            "role": "user",
            "content": "Continue.",
            "token_count": 5,
        },
        {
            "id": 4,
            "session_id": "session-image",
            "message_id": "message-current-answer",
            "role": "assistant",
            "content": "Current answer.",
            "token_count": 5,
        },
    ]

    async def summary_without_attachment(**kwargs):  # noqa: ANN003
        del kwargs
        return "Safe summary without an attachment reference."

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm",
        summary_without_attachment,
    )
    result = await compact_context(
        CompactionRequest(
            session_id="session-image",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
                protected_recent_messages=2,
            ),
            forced_prefix_cut=2,
            trigger="message_count",
        )
    )

    assert result.removed_count == 2
    assert result.summary_payload is not None
    assert result.summary_payload["files_and_artifacts"] == []
    assert occurrence.attachment_id in result.summary_payload["important_identifiers"]
    serialized_payload = json.dumps(result.summary_payload, sort_keys=True)
    replay = compaction_replay_summary(result)
    for rendered in (serialized_payload, replay):
        assert occurrence.attachment_id in rendered
        assert image_data not in rendered
        assert "/private/tmp" not in rendered
        assert "not-an-occurrence-id" not in rendered


@pytest.mark.asyncio
async def test_nested_tool_result_images_are_projected_out_of_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_data = base64.b64encode(b"nested-tool-image-bytes").decode("ascii")
    image_path = "/private/tmp/tool-results/private-image.png"
    nested_result = {
        "items": [
            {
                "type": "image",
                "path": image_path,
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_data,
                },
            },
            {
                "wrapper": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "path": image_path,
                    "data": image_data,
                }
            },
        ]
    }
    tool_calls = [
        {
            "type": "tool_use",
            "id": "tool-image-result-1",
            "name": "inspect_image",
            "input": json.dumps({"payload": nested_result}),
        },
        {
            "type": "tool_result",
            "tool_use_id": "tool-image-result-1",
            "result": json.dumps(nested_result),
        },
    ]
    entries = [
        {
            "id": 1,
            "session_id": "session-tool-image",
            "message_id": "message-request",
            "role": "user",
            "content": "Inspect the tool image.",
            "token_count": 5,
        },
        {
            "id": 2,
            "session_id": "session-tool-image",
            "message_id": "message-tool-result",
            "role": "assistant",
            "content": "Tool completed.",
            "tool_calls": tool_calls,
            "token_count": 5,
        },
        {
            "id": 3,
            "session_id": "session-tool-image",
            "message_id": "message-follow-up",
            "role": "user",
            "content": "Continue without replaying bytes.",
            "token_count": 5,
        },
        {
            "id": 4,
            "session_id": "session-tool-image",
            "message_id": "message-answer",
            "role": "assistant",
            "content": "Latest answer.",
            "token_count": 5,
        },
    ]
    captured_chunks: list[str] = []

    async def safe_summary(**kwargs):  # noqa: ANN003
        captured_chunks.append(kwargs["chunk_text"])
        return "The tool returned an image for inspection."

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm",
        safe_summary,
    )

    llm_projection = _format_chunk_for_llm(entries[:2])
    fallback_projection = _summarize_chunk_fallback(entries[:2], "strict")
    result = await compact_context(
        CompactionRequest(
            session_id="session-tool-image",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
                protected_recent_messages=2,
            ),
            forced_prefix_cut=2,
            trigger="message_count",
        )
    )

    assert captured_chunks
    assert result.removed_count == 2
    assert result.summary_payload is not None
    serialized_payload = json.dumps(result.summary_payload, sort_keys=True)
    replay = compaction_replay_summary(result)
    for rendered in (
        llm_projection,
        fallback_projection,
        *captured_chunks,
        serialized_payload,
        replay,
    ):
        assert "image omitted from compaction input" in rendered
        assert image_data not in rendered
        assert image_path not in rendered
        assert "/private/tmp" not in rendered
    assert tool_calls[1]["result"] == json.dumps(nested_result)


def test_api_round_groups_keep_user_role_tool_result_with_its_call() -> None:
    active_user = {"role": "user", "content": "inspect the file"}
    tool_call = {
        "role": "assistant",
        "content": "[tool_call: read_file]",
        "tool_calls": [{"id": "call-1", "name": "read_file"}],
    }
    tool_result = {
        "role": "user",
        "content": "[Tool result call-1]\ncontents",
    }
    next_assistant = {"role": "assistant", "content": "The file is valid."}

    groups = _api_round_groups(
        [active_user, tool_call, tool_result, next_assistant]
    )

    assert groups[0] == [active_user, tool_call, tool_result]
    assert groups[1] == [next_assistant]


def test_compaction_effect_payload_marks_automatic_noop_not_user_visible():
    payload = compaction_effect_payload(
        status="skipped",
        source="automatic",
        reason="within_compaction_budget",
    )

    assert payload == {
        "applied": False,
        "durability": "none",
        "skip_reason": "within_compaction_budget",
        "user_visible": False,
    }


def test_compaction_effect_payload_surfaces_non_benign_skip_reasons():
    for reason in ("coverage_blocked", "empty_summary", "no_safe_turn_boundary"):
        payload = compaction_effect_payload(
            status="skipped",
            source="automatic",
            reason=reason,
        )

        assert payload["applied"] is False
        assert payload["durability"] == "none"
        assert payload["skip_reason"] == reason
        assert payload["user_visible"] is True


def test_compaction_effect_payload_marks_durable_completion_applied():
    payload = compaction_effect_payload(status="completed", source="automatic")

    assert payload["applied"] is True
    assert payload["durability"] == "durable"
    assert payload["user_visible"] is True


def test_compaction_deadline_is_armed_only_once(monkeypatch):
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr("opensquilla.session.compaction.time", clock)
    config = CompactionConfig(total_timeout_seconds=10.0)

    first_deadline = arm_compaction_deadline(config, operation_id="cmp_deadline")
    clock.now = 106.0
    second_deadline = arm_compaction_deadline(config)

    assert first_deadline == 110.0
    assert second_deadline == first_deadline
    assert config.deadline_at_monotonic == first_deadline
    assert config.operation_id == "cmp_deadline"
    assert compaction_remaining_seconds(config) == 4.0


def test_reused_compaction_config_rearms_for_a_new_operation(monkeypatch):
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr("opensquilla.session.compaction.time", clock)
    config = CompactionConfig(total_timeout_seconds=10.0)

    first_deadline = arm_compaction_deadline(config, operation_id="cmp_first")
    clock.now = 106.0
    second_deadline = arm_compaction_deadline(config, operation_id="cmp_second")

    assert first_deadline == 110.0
    assert second_deadline == 116.0
    assert config.operation_id == "cmp_second"


@pytest.mark.asyncio
async def test_nested_compaction_deadline_keeps_precise_phase():
    config = CompactionConfig(total_timeout_seconds=10.0)

    async def validation_phase() -> None:
        raise CompactionTimeoutError("validating", 10.0)

    with pytest.raises(CompactionTimeoutError) as exc_info:
        await await_compaction_phase(
            validation_phase(),
            config,
            phase="summarizing",
        )

    assert exc_info.value.phase == "validating"


@pytest.mark.asyncio
async def test_no_compaction_needed_small_context():
    entries = _make_entries(5, tokens_each=10)  # 50 tokens total
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=10_000,  # huge window
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.summary_source == "skipped"
    assert result.skip_reason == "within_compaction_budget"
    assert result.kept_start_index == 0
    assert result.quality_report["pressure_kind"] == "token_budget"
    assert result.quality_report["physical_call_count"] == 0
    assert result.quality_report["consumer_window_source"] == "consumer_capacity"
    assert result.quality_report["consumer_window_tokens"] == 10_000
    assert result.quality_report["degraded_reason"] == "within_compaction_budget"


@pytest.mark.asyncio
async def test_message_count_compaction_uses_exact_forced_prefix_within_token_budget(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        # Deliberately larger than the removed entries. Message-count recovery
        # is still useful as long as the replacement fits the token window.
        return "count recovery summary " * 40

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = [
        {"role": "user", "content": "old user 0", "token_count": 5},
        {"role": "assistant", "content": "old assistant 1", "token_count": 5},
        {"role": "user", "content": "protected current request", "token_count": 5},
        {"role": "assistant", "content": "protected current answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="message-count",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
                protected_recent_messages=2,
            ),
            forced_prefix_cut=2,
            trigger="message_count",
            reason="provider_messages_limit",
        )
    )

    assert calls
    assert "old user 0" in "\n".join(calls)
    assert "old assistant 1" in "\n".join(calls)
    assert "protected current request" not in "\n".join(calls)
    assert result.removed_count == 2
    assert result.kept_start_index == 2
    assert result.kept_entries == entries[2:]
    assert result.kept_entries[0] is entries[2]
    assert result.kept_entries[1] is entries[3]
    assert result.tokens_after >= result.tokens_before
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is True
    assert result.quality_report["pressure_kind"] == "message_count"
    assert result.quality_report["physical_call_count"] == 1
    assert result.quality_report["target_source"] == "legacy_raw_compat"


@pytest.mark.asyncio
async def test_compaction_request_derives_unique_correlation_for_every_physical_call(
    monkeypatch,
) -> None:
    observed: list[ProviderRequestCorrelation | None] = []

    async def fake_llm(**kwargs):
        observed.append(kwargs.get("provider_request_correlation"))
        return "bounded historical summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="compaction-1",
        call_kind="auxiliary.compaction",
    )
    await compact_context(
        CompactionRequest(
            session_id="session-1",
            entries=_make_entries(12, tokens_each=20),
            context_window_tokens=100,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
            ),
            provider_request_correlation=correlation,
        )
    )

    assert len(observed) == 2
    assert all(item is not None for item in observed)
    physical = [item for item in observed if item is not None]
    assert all(item is not correlation for item in physical)
    assert all(item.session_id == correlation.session_id for item in physical)
    assert all(item.turn_id == correlation.turn_id for item in physical)
    assert all(item.call_kind == correlation.call_kind for item in physical)
    assert all(item.execution_id != correlation.execution_id for item in physical)
    assert len({item.execution_id for item in physical}) == len(physical)


@pytest.mark.asyncio
async def test_message_count_compaction_summarizes_large_prefix_with_one_llm_call(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        return "one bounded historical summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(104, tokens_each=1)

    result = await compact_context(
        CompactionRequest(
            session_id="message-count-large-prefix",
            entries=entries,
            context_window_tokens=128_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                protected_recent_messages=86,
            ),
            forced_prefix_cut=18,
            trigger="message_count",
            reason="provider_request_message_limit",
        )
    )

    assert len(calls) == 1
    assert "message 0" in calls[0]
    assert "message 17" in calls[0]
    assert "message 18" not in calls[0]
    assert result.chunks_processed == 1
    assert result.removed_count == 18
    assert result.kept_start_index == 18
    assert result.kept_entries == entries[18:]


@pytest.mark.asyncio
async def test_token_trigger_still_rejects_forced_summary_that_does_not_reduce_tokens(
    monkeypatch,
):
    async def fake_llm(**kwargs):
        return "larger replacement summary " * 40

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(4, tokens_each=5)

    result = await compact_context(
        CompactionRequest(
            session_id="token-trigger",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
            ),
            forced_prefix_cut=2,
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "quality_gate_failed"
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is False


@pytest.mark.asyncio
async def test_forced_prefix_cut_refuses_protected_tail_overlap():
    entries = _make_entries(4, tokens_each=5)

    result = await compact_context(
        CompactionRequest(
            session_id="protected-tail",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(protected_recent_messages=2),
            forced_prefix_cut=3,
            trigger="message_count",
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "forced_prefix_cut_overlaps_protected_tail"


@pytest.mark.asyncio
async def test_forced_prefix_cut_refuses_split_tool_segment():
    entries = [
        {"role": "user", "content": "old context", "token_count": 5},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function"}],
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "tool result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {"role": "user", "content": "current request", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="tool-boundary",
            entries=entries,
            context_window_tokens=2_000,
            forced_prefix_cut=2,
            trigger="message_count",
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "forced_prefix_cut_splits_tool_segment"


@pytest.mark.asyncio
async def test_compaction_occurs_when_over_budget():
    entries = _make_entries(20, tokens_each=200)  # 4000 tokens
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1600,  # tight enough to compact, large enough for the result
            config=CompactionConfig(safety_margin=1.0),
        )
    )
    assert result.removed_count > 0
    assert result.summary != ""
    assert result.chunks_processed >= 1
    assert result.summary_source == "fallback"
    assert result.tokens_before == 4000
    assert result.tokens_after < result.tokens_before
    assert result.remaining_budget_tokens >= 0


def _make_tool_heavy_entries(
    turns: int = 10, pairs: int = 4, result_chars: int = 2000
) -> list[dict]:
    line = "drwxr-xr-x staff 4096 synthetic/file.txt "
    result_text = (line * (result_chars // len(line) + 1))[:result_chars]
    entries: list[dict] = []
    for turn in range(turns):
        tool_calls: list[dict] = []
        for pair in range(pairs):
            tool_id = f"tool-{turn}-{pair}"
            tool_calls.append(
                {
                    "type": "tool_use",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "input": {"command": f"ls batch_{turn}/{pair}"},
                }
            )
            tool_calls.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "result": result_text,
                    "is_error": False,
                }
            )
        entries.append({"role": "user", "content": f"inspect batch {turn}", "token_count": None})
        entries.append(
            {
                "role": "assistant",
                "content": f"inspected batch {turn}",
                "token_count": 120,
                "tool_calls": tool_calls,
            }
        )
    return entries


@pytest.mark.asyncio
async def test_budget_check_counts_full_tool_call_replay_not_summarized_previews():
    entries = _make_tool_heavy_entries()
    window = 16_000
    summarized = sum(estimate_entry_replay_tokens(e) for e in entries)
    model_replay = sum(estimate_entry_model_replay_tokens(e) for e in entries)
    # The summarized estimate looks within budget while the model replay overflows.
    assert summarized * 1.2 <= window < model_replay

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=window,
            config=CompactionConfig(model=None, api_key=""),
        )
    )

    assert result.skip_reason != "within_compaction_budget"
    assert result.removed_count > 0


def test_replay_token_estimate_uses_tool_payload_summary_not_raw_arguments():
    large_content = "x" * 80_000
    entry = {
        "role": "assistant",
        "content": "wrote file",
        "token_count": 1,
        "tool_calls": [
            {
                "type": "tool_use",
                "tool_use_id": "write-large",
                "name": "write_file",
                "input": {"path": "index.html", "content": large_content},
            }
        ],
        "reasoning_content": "private reasoning " + ("r" * 20_000),
    }

    tokens = estimate_entry_replay_tokens(entry)

    assert tokens < 500


def test_model_replay_estimate_does_not_trust_underreported_persisted_count():
    content = "x" * 8_000
    entry = {
        "role": "user",
        "content": content,
        "token_count": 1,
    }

    tokens = estimate_entry_model_replay_tokens(entry)
    chars = estimate_entries_model_replay_chars([entry])

    assert tokens > 1
    assert chars >= len(content)


@pytest.mark.asyncio
async def test_character_pressure_triggers_compaction_when_token_window_fits():
    entries = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index} " + ("x" * 1_000),
            "token_count": 1,
        }
        for index in range(12)
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="character-pressure",
            entries=entries,
            context_window_tokens=20_000,
            context_window_chars=4_000,
            config=CompactionConfig(safety_margin=1.0),
            summary_replay_renderer=lambda summary: (
                f"[Compacted Session Summaries]\n[Summary 1]\n{summary}\n\n"
            ),
        )
    )

    assert result.removed_count > 0
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["fits_character_window"] is True
    assert result.quality_report["chars_after"] <= 4_000


@pytest.mark.asyncio
async def test_character_gate_counts_replay_wrapper_before_durable_install():
    entries = [
        {"role": "user", "content": "old " + ("x" * 2_000), "token_count": 1},
        {"role": "assistant", "content": "old answer", "token_count": 1},
        {"role": "user", "content": "latest request", "token_count": 1},
        {"role": "assistant", "content": "latest answer", "token_count": 1},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="wrapper-pressure",
            entries=entries,
            context_window_tokens=20_000,
            context_window_chars=350,
            config=CompactionConfig(safety_margin=1.0),
            summary_replay_renderer=lambda summary: ("W" * 300) + summary,
        )
    )

    assert result.removed_count == 0
    assert result.skip_reason == "quality_gate_failed"
    assert result.quality_report["fits_character_window"] is False
    assert result.quality_report["passes_structural_gate"] is False


def test_provider_config_preserves_profile_when_compaction_llm_disabled():
    cfg = build_compaction_config_from_provider(
        None,
        compaction_config=type(
            "CompactionSettings",
            (),
            {
                "enabled": False,
                "compaction_profile": "coding",
                "protected_recent_messages": 6,
            },
        )(),
    )

    assert cfg.model is None
    assert cfg.api_key == ""
    assert cfg.compaction_profile == "coding"
    assert cfg.protected_recent_messages == 6


@pytest.mark.asyncio
async def test_compaction_source_is_llm_when_all_chunks_use_llm(monkeypatch):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        return "LLM summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert calls
    assert result.removed_count > 0
    assert result.summary_source == "llm"


@pytest.mark.asyncio
async def test_multichunk_compaction_reuses_remaining_absolute_budget(monkeypatch):
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    request_timeouts: list[float] = []

    async def fake_llm(**kwargs):
        request_timeouts.append(kwargs["timeout"])
        clock.now += 6.0
        return f"summary {len(request_timeouts)}"

    monkeypatch.setattr("opensquilla.session.compaction.time", clock)
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    config = CompactionConfig(
        model="test/model",
        api_key="test-key",
        timeout_seconds=90.0,
        total_timeout_seconds=10.0,
        base_chunk_ratio=0.1,
        min_chunk_ratio=0.1,
    )

    with pytest.raises(CompactionTimeoutError) as exc_info:
        await compact_context(
            CompactionRequest(
                session_id="shared-deadline",
                entries=_make_entries(30, tokens_each=200),
                context_window_tokens=500,
                config=config,
            )
        )

    assert exc_info.value.phase == "summarizing"
    assert request_timeouts == pytest.approx([10.0, 4.0])
    assert config.deadline_at_monotonic == 110.0


@pytest.mark.asyncio
async def test_compaction_source_is_mixed_when_llm_partly_falls_back(monkeypatch):
    responses = ["LLM summary", None]

    async def fake_llm(**kwargs):
        return responses.pop(0) if responses else "LLM summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert result.removed_count > 0
    assert result.summary_source == "mixed"


@pytest.mark.asyncio
async def test_compaction_keeps_recent_entries():
    entries = _make_entries(20, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1500,
        )
    )
    # kept entries should be a tail of the original
    if result.kept_entries:
        last_kept = result.kept_entries[-1]
        assert last_kept in entries[-len(result.kept_entries) :]


@pytest.mark.asyncio
async def test_coding_profile_preserves_configured_recent_tail():
    entries = _make_entries(30, tokens_each=250)
    protected_tail = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"active task message {i}",
            "token_count": 5,
        }
        for i in range(4)
    ]
    entries.extend(protected_tail)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1500,
            config=CompactionConfig(
                safety_margin=1.0,
                compaction_profile="coding",
                protected_recent_messages=4,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[-4:] == protected_tail
    assert result.quality_report["profile"] == "coding"
    assert result.quality_report["protected_recent_messages"] == 4
    assert result.quality_report["protected_tail_preserved"] is True
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is True
    assert compaction_result_payload(result)["quality_report"][
        "passes_structural_gate"
    ] is True


@pytest.mark.asyncio
async def test_quality_report_marks_compaction_that_still_exceeds_window():
    entries = [
        {"role": "user", "content": "old context", "token_count": 10_000},
        *[
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"large protected tail {i}",
                "token_count": 500,
            }
            for i in range(5)
        ],
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=5,
            ),
        )
    )

    assert result.removed_count == 0
    assert result.skip_reason == "quality_gate_failed"
    assert result.quality_report["fits_context_window"] is False
    assert result.quality_report["passes_structural_gate"] is False
    assert compaction_result_payload(result)["quality_report"][
        "fits_context_window"
    ] is False


@pytest.mark.asyncio
async def test_latest_completed_assistant_can_compact_when_it_exceeds_window():
    entries = [
        {"role": "user", "content": "old question", "token_count": 400},
        {"role": "assistant", "content": "old answer", "token_count": 400},
        {
            "role": "assistant",
            "content": "LATEST_ASSISTANT_RAW",
            "token_count": 2_000,
        },
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="latest-assistant-protected",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count == len(entries)
    assert result.kept_entries == []
    assert "LATEST_ASSISTANT_RAW" in result.summary


@pytest.mark.asyncio
async def test_error_tool_result_and_its_call_remain_raw() -> None:
    call = {
        "role": "assistant",
        "content": "calling checker",
        "tool_calls": [{"id": "call_error", "type": "function"}],
        "token_count": 10,
    }
    error_result = {
        "role": "tool",
        "content": "Error: checker failed: exact diagnostic",
        "tool_call_id": "call_error",
        "is_error": True,
        "token_count": 100,
    }
    entries = [
        {"role": "user", "content": "ancient request", "token_count": 1_000},
        {"role": "assistant", "content": "ancient answer", "token_count": 1_000},
        {"role": "user", "content": "tool request", "token_count": 10},
        call,
        error_result,
        {"role": "user", "content": "continue", "token_count": 10},
        {"role": "assistant", "content": "latest answer", "token_count": 10},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="error-result-protected",
            entries=entries,
            context_window_tokens=600,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count == 2
    assert result.kept_entries[0]["content"] == "tool request"
    assert result.kept_entries[1] == call
    assert result.kept_entries[2] == error_result


@pytest.mark.asyncio
async def test_canonical_nested_error_tool_result_remains_raw() -> None:
    canonical_tool_round = {
        "role": "assistant",
        "content": "calling checker",
        "tool_calls": [
            {
                "type": "tool_use",
                "tool_use_id": "call_nested_error",
                "name": "exec_command",
                "input": {"command": "pytest -q"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call_nested_error",
                "result": "FAILED exact canonical diagnostic",
                "is_error": True,
                "execution_status": {
                    "status": "error",
                    "reason": "nonzero_exit",
                },
            },
        ],
        "token_count": 100,
    }
    entries = [
        {"role": "user", "content": "ancient request", "token_count": 1_000},
        {"role": "assistant", "content": "ancient answer", "token_count": 1_000},
        {"role": "user", "content": "run the checker", "token_count": 10},
        canonical_tool_round,
        {"role": "user", "content": "continue", "token_count": 10},
        {"role": "assistant", "content": "latest answer", "token_count": 10},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="nested-error-result-protected",
            entries=entries,
            context_window_tokens=600,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert canonical_tool_round in result.kept_entries
    assert result.kept_entries.index(canonical_tool_round) >= 1


@pytest.mark.asyncio
async def test_old_completed_error_does_not_permanently_anchor_semantic_tail() -> None:
    old_error_round = {
        "role": "assistant",
        "content": "old checker call",
        "tool_calls": [
            {"type": "tool_use", "tool_use_id": "old-error", "name": "exec"},
            {
                "type": "tool_result",
                "tool_use_id": "old-error",
                "result": "Error: old exact diagnostic",
                "is_error": True,
                "execution_status": {
                    "status": "error",
                    "reason": "nonzero_exit",
                    "preservation_class": "diagnostic",
                },
            },
        ],
        "token_count": 400,
    }
    entries = [
        {"role": "user", "content": "old request", "token_count": 400},
        old_error_round,
        *_make_entries(18, tokens_each=300),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="old-terminal-error",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert old_error_round not in result.kept_entries
    assert "status=error reason=nonzero_exit" in result.summary
    assert result.kept_start_index in {
        sum(len(group) for group in _api_round_groups(entries)[:index])
        for index in range(1, len(_api_round_groups(entries)) + 1)
    }


@pytest.mark.asyncio
async def test_historical_unmatched_call_does_not_anchor_later_user_rounds() -> None:
    old_unmatched_call = {
        "role": "assistant",
        "content": "old interrupted call",
        "tool_calls": [
            {"type": "tool_use", "tool_use_id": "stale-unmatched", "name": "exec"},
        ],
        "token_count": 400,
    }
    entries = [
        {"role": "user", "content": "old request", "token_count": 400},
        old_unmatched_call,
        *_make_entries(18, tokens_each=300),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="old-unmatched-call",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert old_unmatched_call not in result.kept_entries


@pytest.mark.asyncio
async def test_latest_completed_large_error_round_can_be_compacted() -> None:
    completed_error_round = {
        "role": "assistant",
        "content": "completed large terminal tool round",
        "tool_calls": [
            {"type": "tool_use", "tool_use_id": "latest-error", "name": "exec"},
            {
                "type": "tool_result",
                "tool_use_id": "latest-error",
                "result": "Error: " + ("large diagnostic " * 200),
                "is_error": True,
                "execution_status": {
                    "status": "error",
                    "reason": "nonzero_exit",
                },
            },
        ],
        "token_count": 4_000,
    }
    entries = [
        *_make_entries(18, tokens_each=100),
        {"role": "user", "content": "run the latest command", "token_count": 20},
        completed_error_round,
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="latest-completed-error",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count == len(entries)
    assert result.kept_entries == []
    assert "latest-error" in result.summary
    assert "status=error reason=nonzero_exit" in result.summary


@pytest.mark.asyncio
async def test_top_level_terminal_result_status_is_preserved_in_summary() -> None:
    top_level_error = {
        "role": "tool",
        "content": "command stopped",
        "tool_call_id": "top-level-timeout",
        "is_error": True,
        "execution_status": {
            "status": "timed_out",
            "reason": "deadline_exceeded",
        },
        "token_count": 300,
    }
    entries = [
        {"role": "user", "content": "old command", "token_count": 300},
        {
            "role": "assistant",
            "content": "calling command",
            "tool_calls": [{"id": "top-level-timeout", "type": "function"}],
            "token_count": 300,
        },
        top_level_error,
        *_make_entries(18, tokens_each=300),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="top-level-terminal-status",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert top_level_error not in result.kept_entries
    assert "tool_call_id=top-level-timeout" in result.summary
    assert "status=timed_out reason=deadline_exceeded" in result.summary


@pytest.mark.asyncio
async def test_current_live_tool_state_remains_raw() -> None:
    active_round = {
        "role": "assistant",
        "content": "long-running command",
        "tool_calls": [
            {"type": "tool_use", "tool_use_id": "active-call", "name": "exec"},
            {
                "type": "tool_result",
                "tool_use_id": "active-call",
                "result": "still running",
                "execution_status": {"status": "running"},
            },
        ],
        "token_count": 20,
    }
    entries = [
        *_make_entries(18, tokens_each=300),
        {"role": "user", "content": "run the current command", "token_count": 20},
        active_round,
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="current-live-state",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[-2:] == entries[-2:]


@pytest.mark.asyncio
async def test_latest_unmatched_tool_call_remains_raw() -> None:
    unmatched_call = {
        "role": "assistant",
        "content": "starting current command",
        "tool_calls": [
            {"type": "tool_use", "tool_use_id": "current-unmatched", "name": "exec"},
        ],
        "token_count": 20,
    }
    entries = [
        *_make_entries(18, tokens_each=300),
        {"role": "user", "content": "run current command", "token_count": 20},
        unmatched_call,
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="latest-unmatched-call",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[-2:] == entries[-2:]


@pytest.mark.asyncio
async def test_latest_legacy_untyped_tool_call_remains_raw() -> None:
    legacy_unmatched_call = {
        "role": "assistant",
        "content": "starting legacy current command",
        "tool_calls": [
            {
                "id": "legacy-current-unmatched",
                "name": "exec_command",
                "arguments": {"command": "pytest -q"},
            },
        ],
        "token_count": 20,
    }
    entries = [
        *_make_entries(18, tokens_each=300),
        {"role": "user", "content": "run legacy current command", "token_count": 20},
        legacy_unmatched_call,
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="latest-legacy-untyped-call",
            entries=entries,
            context_window_tokens=1_500,
            config=CompactionConfig(safety_margin=1.0),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[-2:] == entries[-2:]


@pytest.mark.asyncio
async def test_protected_tail_retreats_to_tool_boundary():
    entries = [
        {"role": "user", "content": "ancient request", "token_count": 1_000},
        {"role": "assistant", "content": "ancient answer", "token_count": 1_000},
        {"role": "user", "content": "tool request", "token_count": 5},
        {
            "role": "assistant",
            "content": "[Used tool: read_file]",
            "token_count": 5,
        },
        {
            "role": "user",
            "content": "[Tool result (toolu_1): file contents]",
            "token_count": 5,
        },
        {"role": "user", "content": "next question", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=600,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=3,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[0]["content"] == "tool request"
    assert result.kept_entries[1]["content"] == "[Used tool: read_file]"
    assert result.kept_entries[2]["content"].startswith("[Tool result ")
    assert result.quality_report["protected_tail_preserved"] is True


@pytest.mark.asyncio
async def test_protected_tail_retreats_over_multi_result_tool_segment():
    entries = [
        {"role": "user", "content": "ancient request", "token_count": 1_000},
        {"role": "assistant", "content": "ancient answer", "token_count": 1_000},
        {"role": "user", "content": "tool request", "token_count": 5},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function"}],
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "first result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "second result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {"role": "user", "content": "next question", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=600,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=3,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[0]["content"] == "tool request"
    assert result.kept_entries[1]["role"] == "assistant"
    assert result.kept_entries[2]["content"] == "first result"
    assert result.kept_entries[3]["content"] == "second result"
    assert result.quality_report["protected_tail_preserved"] is True


@pytest.mark.asyncio
async def test_empty_entries():
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=[],
            context_window_tokens=1000,
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == []
    assert result.summary == ""
    assert result.skip_reason == "no_entries"


@pytest.mark.asyncio
async def test_custom_config():
    entries = _make_entries(20, tokens_each=200)
    cfg = CompactionConfig(
        base_chunk_ratio=0.3,
        min_chunk_ratio=0.1,
        safety_margin=1.0,
        default_parts=3,
        identifier_policy="off",
    )
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,
            config=cfg,
        )
    )
    assert result.removed_count > 0


@pytest.mark.asyncio
async def test_strict_identifier_policy_in_summary():
    entries = _make_entries(10, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(identifier_policy="strict"),
        )
    )
    if result.summary:
        assert "identifier" in result.summary.lower() or "IMPORTANT" in result.summary


@pytest.mark.asyncio
async def test_chunks_processed_count():
    entries = _make_entries(30, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
        )
    )
    assert result.chunks_processed >= 1


@pytest.mark.asyncio
async def test_call_compaction_llm_adds_openrouter_app_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
    )

    assert result == "summary"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }


@pytest.mark.asyncio
async def test_call_compaction_llm_adds_tokenrhythm_app_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}
    install_id = "synthetic-install-id"

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": f"summary echoed {install_id}"}}
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(
        "opensquilla.session.compaction.tokenrhythm_install_id_headers",
        lambda _provider_kind, _base_url: {
            "X-OpenSquilla-Install-Id": install_id
        },
    )
    monkeypatch.setattr(
        "opensquilla.session.compaction.redact_tokenrhythm_install_ids",
        lambda text: text.replace(install_id, "***"),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://tokenrhythm.studio/v1",
        provider="tokenrhythm",
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="compaction-1",
            call_kind="auxiliary.compaction",
        ),
    )

    assert result == "summary echoed ***"
    assert captured["url"] == "https://tokenrhythm.studio/v1/chat/completions"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-OpenSquilla-Session-Id": "session-1",
        "X-OpenSquilla-Turn-Id": "turn-1",
        "X-OpenSquilla-Execution-Id": "compaction-1",
        "X-OpenSquilla-Call-Kind": "auxiliary.compaction",
        "X-OpenSquilla-Install-Id": install_id,
        "X-Title": "OpenSquilla",
    }
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload == {
        "model": "deepseek-v4-flash",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a conversation compactor. Summarize the conversation "
                    "concisely, preserving key facts, decisions, open questions, and "
                    "action items. Write in the same language as the conversation. "
                    "Focus on recent context over older history."
                ),
            },
            {
                "role": "user",
                "content": "Summarize this conversation:\n\nold conversation",
            },
        ],
        "max_tokens": 1024,
        "temperature": 0,
        "stream": False,
    }
    serialized_payload = json.dumps(payload, sort_keys=True)
    for internal_field in (
        "target_fingerprint",
        "request_proof",
        "quality_report",
        "provider_request_correlation",
    ):
        assert internal_field not in serialized_payload


@pytest.mark.asyncio
async def test_call_compaction_llm_privacy_switch_removes_correlation_on_wire(
    monkeypatch,
) -> None:
    captured_headers: dict[str, str] = {}

    class FakeResponse:
        text = ""
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured_headers.update(headers)
            return FakeResponse()

    monkeypatch.setenv(
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        "true",
    )
    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://tokenrhythm.studio/v1",
        provider="tokenrhythm",
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="compaction-1",
            call_kind="auxiliary.compaction",
        ),
    )

    assert result == "summary"
    assert not any(name.startswith("X-OpenSquilla-") for name in captured_headers)
    assert captured_headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }


@pytest.mark.asyncio
async def test_call_compaction_llm_cancellation_does_not_retain_install_id(
    monkeypatch,
) -> None:
    install_id = "synthetic-cancelled-compaction-install-id"
    sent_headers: dict[str, str] = {}
    usage_reasons: list[str] = []

    class RetainingResponse:
        text = ""

        def __init__(self, headers: dict[str, str]) -> None:
            self.request_headers = dict(headers)

        def __repr__(self) -> str:
            return f"RetainingResponse(headers={self.request_headers!r})"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "unused"}}],
                "echo": install_id,
            }

    class RetainingClient:
        def __init__(self) -> None:
            self.request_headers: dict[str, str] = {}

        def __repr__(self) -> str:
            return f"RetainingClient(headers={self.request_headers!r})"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            self.request_headers = dict(headers)
            sent_headers.update(headers)
            return RetainingResponse(headers)

    class CancellingUsage:
        async def finalize_openai_response(self, data, *, raw_json) -> None:
            raise asyncio.CancelledError

        async def mark_unknown(self, reason: str) -> None:
            usage_reasons.append(reason)

    async def reserve_direct_usage_call(**_kwargs):
        return CancellingUsage()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **_kwargs: RetainingClient(),
    )
    monkeypatch.setattr(
        "opensquilla.session.compaction.tokenrhythm_install_id_headers",
        lambda _provider_kind, _base_url: {
            "X-OpenSquilla-Install-Id": install_id
        },
    )
    monkeypatch.setattr(
        "opensquilla.engine.usage_http.reserve_direct_usage_call",
        reserve_direct_usage_call,
    )

    task = asyncio.create_task(
        call_compaction_llm(
            chunk_text="old conversation",
            identifier_instruction="",
            model="deepseek-v4-flash",
            api_key="test-key",
            base_url="https://tokenrhythm.studio/v1",
            provider="tokenrhythm",
        )
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert task.cancelled()
    assert usage_reasons == ["cancelled"]
    assert sent_headers["X-OpenSquilla-Install-Id"] == install_id
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    traceback = caught.value.__traceback__
    production_locals: list[str] = []
    while traceback is not None:
        frame = traceback.tb_frame
        if (
            frame.f_globals.get("__name__") == "opensquilla.session.compaction"
            and frame.f_code.co_name == "call_compaction_llm"
        ):
            production_locals.append(repr(frame.f_locals))
        traceback = traceback.tb_next
    assert len(production_locals) == 1
    assert install_id not in production_locals[0]


@pytest.mark.asyncio
async def test_call_compaction_llm_redacts_install_id_from_failure_log(monkeypatch) -> None:
    install_id = "i7"
    warnings: list[tuple[str, dict]] = []

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            raise RuntimeError(f"upstream echoed {install_id}")

    class CapturingLog:
        def info(self, event: str, **kwargs) -> None:
            return None

        def warning(self, event: str, **kwargs) -> None:
            warnings.append((event, kwargs))

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )
    monkeypatch.setattr("opensquilla.session.compaction.log", CapturingLog())
    monkeypatch.setattr(
        "opensquilla.session.compaction.redact_tokenrhythm_install_ids",
        lambda text: text.replace(install_id, "***"),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://tokenrhythm.studio/v1",
        provider="tokenrhythm",
    )

    assert result is None
    assert warnings == [
        (
            "compaction.llm_call_failed",
            {
                "compaction_id": None,
                "chunk_index": None,
                "model": "deepseek-v4-flash",
                "error": "upstream echoed ***",
            },
        )
    ]


@pytest.mark.asyncio
async def test_call_compaction_llm_timeout_returns_none(monkeypatch) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            raise TimeoutError("summary timed out")

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        timeout=0.01,
    )

    assert result is None


@pytest.mark.asyncio
async def test_custom_instructions_are_user_scoped_and_identifier_policy_stays_system(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="Preserve exact IDs.",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
        custom_instructions="Focus on deployment decisions.",
    )

    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert "Preserve exact IDs." in messages[0]["content"]
    assert "Focus on deployment decisions." not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Focus on deployment decisions." in messages[1]["content"]
