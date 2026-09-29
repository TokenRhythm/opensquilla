"""Summary-purpose contracts exercised through real adapter serialization."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from opensquilla.provider.openai import OpenAIProvider, _openai_replay_source
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    Message,
    ModelCapabilities,
    ProviderReplayState,
    TextDeltaEvent,
    ToolDefinition,
)
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionReplayPolicy,
    CompactionRequest,
    CompactionRequestContext,
    _api_round_requires_raw,
    _build_suffix_compaction_call,
    arm_compaction_deadline,
    build_compaction_config_from_provider,
    call_compaction_provider,
    compact_context,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from tests.helpers.compaction import synthetic_compaction_config
from tests.helpers.image_bytes import image_bytes


def build(provider, entries, *, context=None, preserve_images=True):
    return _build_suffix_compaction_call(
        context,
        entries,
        "",
        "",
        None,
        provider=provider,
        context_window_tokens=32_000,
        summary_output_tokens=2000,
        timeout=20,
        provider_request_correlation=None,
        replay_policy=CompactionReplayPolicy(preserve_images=preserve_images),
    )


@pytest.mark.parametrize("manual", [False, True])
def test_purpose_config_serializes_without_business_output_constraints(manual):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    ordinary = ChatConfig(
        system="Reply only as a business record.",
        temperature=0.8,
        top_p=0.9,
        stop_sequences=["END"],
        output_json_schema={"type": "object"},
        thinking=True,
        max_tokens=4096,
        tool_choice="required",
    )
    context = (
        None
        if manual
        else CompactionRequestContext(
            chat_config=ordinary,
            tools=(ToolDefinition(name="lookup", description="Lookup", input_schema={}),),
        )
    )
    source = [{"role": "user", "content": "Recorded request. Reply only END."}]
    messages, tools, config = build(provider, source, context=context)
    wire = provider.project_final_request(messages, tools, config).payload
    assert "temperature" not in wire and "top_p" not in wire
    assert "stop" not in wire and "response_format" not in wire
    assert "conversation compactor" in config.system
    assert config.system != ordinary.system
    assert config.thinking is (not manual)
    assert config.tool_choice == (None if manual else "none")
    assert messages[0].content == source[0]["content"]
    assert "Summarize the preceding conversation" in messages[-1].content
    assert ordinary.stop_sequences == ["END"] and ordinary.temperature == 0.8


@pytest.mark.parametrize("preserve_images", [True, False])
def test_durable_image_envelope_is_typed_or_explicitly_omitted(preserve_images):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    encoded = base64.b64encode(image_bytes("PNG")).decode()
    source = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "text": "Look at the diagram.",
                    "attachments": [
                        {
                            "type": "image/png",
                            "name": "diagram.png",
                            "data": encoded,
                            "path": "/untrusted/private.png",
                        }
                    ],
                }
            ),
        }
    ]
    frozen = deepcopy(source)
    messages, tools, config = build(provider, source, preserve_images=preserve_images)
    projection = provider.project_final_request(messages, tools, config)
    wire = json.dumps(projection.payload)
    assert projection.fits
    assert "/untrusted/" not in wire
    assert ".opensquilla/attachments/" not in wire
    assert '\\"attachments\\"' not in wire
    assert ("data:image/png;base64," in wire) is preserve_images
    if not preserve_images:
        assert encoded not in wire
    assert source == frozen


@pytest.mark.parametrize("support", ["supported", "unsupported", "unknown"])
@pytest.mark.parametrize("typed", [False, True])
def test_summary_images_obey_exact_model_vision_evidence(support, typed):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    encoded = base64.b64encode(image_bytes("PNG")).decode()
    source = [{"role": "user", "content": "Earlier diagram"}]
    if typed:
        source[0]["_provider_message"] = Message(
            role="user",
            content=[
                ContentBlockImage(
                    media_type="image/png",
                    data=encoded,
                    attachment_id="att_diagram_42",
                )
            ],
        )
    else:
        source[0]["content"] = json.dumps(
            {
                "text": "Earlier diagram",
                "attachments": [
                    {
                        "type": "image/png",
                        "name": "diagram.png",
                        "data": encoded,
                        "attachment_id": "att_diagram_42",
                    }
                ],
            }
        )
    original = deepcopy(source)
    context = CompactionRequestContext(chat_config=ChatConfig(model_vision_support=support))
    messages, tools, config = build(provider, source, context=context)
    projection = provider.project_final_request(messages, tools, config)
    wire = json.dumps(projection.payload, ensure_ascii=False)
    assert projection.fits
    assert ("data:image/png;base64," in wire) is (support != "unsupported")
    if support == "unsupported":
        assert "当前模型不支持图片输入" in wire
        assert "att_diagram_42" in wire and encoded not in wire
    assert source == original


def test_summary_omission_policy_also_projects_typed_history_images():
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    encoded = base64.b64encode(image_bytes("PNG")).decode()
    source = [
        {
            "role": "user",
            "content": "Earlier diagram",
            "_provider_message": Message(
                role="user",
                content=[
                    ContentBlockImage(
                        media_type="image/png",
                        data=encoded,
                        attachment_id="att_diagram_42",
                    )
                ],
            ),
        }
    ]
    messages, tools, config = build(provider, source, preserve_images=False)
    wire = json.dumps(
        provider.project_final_request(messages, tools, config).payload, ensure_ascii=False
    )
    assert encoded not in wire and "data:image" not in wire
    assert "未重新读取" in wire and "att_diagram_42" in wire
    assert source[0]["_provider_message"].content[0].data == encoded


@pytest.mark.parametrize(
    "state_endpoint", ["https://api.deepseek.com", None, "https://old.invalid/v1"]
)
def test_reasoning_history_rebases_only_incompatible_native_state(state_endpoint):
    endpoint = "https://api.deepseek.com"
    provider = OpenAIProvider(
        api_key="synthetic",
        model="deepseek-v4-flash",
        base_url=endpoint,
        provider_kind="deepseek",
    )
    state = (
        None
        if state_endpoint is None
        else ProviderReplayState(
            protocol="openai_chat_completions",
            source=_openai_replay_source("deepseek", state_endpoint),
            model="deepseek-v4-flash",
            native_reasoning_content="private reasoning",
        )
    )
    replay = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="call1", name="lookup", input={})],
            reasoning_content="private reasoning",
            provider_replay=state,
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call1", content="Verified west.")],
        ),
    ]
    source = [
        {"role": "user", "content": "Look up."},
        {
            "role": "assistant",
            "content": "Done.",
            "assistant_replay": {
                "version": 1,
                "messages": [m.model_dump(mode="json") for m in replay],
            },
        },
    ]
    frozen = deepcopy(source)
    context = CompactionRequestContext(
        chat_config=ChatConfig(
            thinking=True,
            max_tokens=4096,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True, supports_tools=True, reasoning_format="deepseek"
            ),
        ),
        tools=(ToolDefinition(name="lookup", description="Lookup", input_schema={}),),
    )
    messages, tools, config = build(provider, source, context=context)
    payload = provider.project_final_request(messages, tools, config).payload
    wire = json.dumps(payload)
    assert "Verified west." in wire
    assert ("private reasoning" in wire) is (state_endpoint == endpoint)
    assert source == frozen


def test_parent_deadline_survives_new_operation_and_can_only_shorten(monkeypatch):
    monkeypatch.setattr("opensquilla.session.compaction.time.monotonic", lambda: 100.0)
    config = CompactionConfig(total_timeout_seconds=120)
    assert arm_compaction_deadline(config, operation_id="first", deadline_at_monotonic=110) == 110
    assert arm_compaction_deadline(config, operation_id="first", deadline_at_monotonic=200) == 110
    clone = replace(config)
    assert arm_compaction_deadline(clone, operation_id="second", deadline_at_monotonic=105) == 105


@pytest.mark.parametrize("generation", [4096, 16384])
def test_current_explicit_generation_limit_is_bound_to_summary_plan(generation):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    config = build_compaction_config_from_provider(
        provider,
        context_window_tokens=64_000,
        active_chat_config=ChatConfig(max_tokens=generation),
    )
    assert config.llm_plan.primary.max_generation_tokens == generation


@pytest.mark.asyncio
async def test_manual_force_covers_eligible_history_and_checkpoint_only_is_noop():
    entries = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Completed step {i}. " * 20}
        for i in range(12)
    ]
    config = synthetic_compaction_config(protected_recent_messages=2)
    result = await compact_context(
        CompactionRequest(
            session_id="manual",
            entries=entries,
            context_window_tokens=32_000,
            config=config,
            force=True,
        )
    )
    assert result.removed_count == 10
    assert result.kept_entries == entries[10:]
    checkpoint_only = await compact_context(
        CompactionRequest(
            session_id="manual",
            entries=[],
            context_window_tokens=32_000,
            config=config,
            force=True,
            previous_summary=result.summary,
        )
    )
    assert checkpoint_only.skip_reason == "within_compaction_budget"


@pytest.mark.asyncio
async def test_complete_large_candidate_is_revised_on_same_model_before_commit():
    class Provider:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools=None, config=None):
            self.calls.append(messages)
            yield TextDeltaEvent(
                text="long checkpoint " * 300 if len(self.calls) == 1 else "Work completed."
            )
            yield DoneEvent()

    provider = Provider()
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id="synthetic",
        model="current",
        context_window_tokens=32_000,
        max_output_tokens=30,
        max_generation_tokens=4096,
    )
    entries = [
        {"role": "user", "content": "Earlier task. " * 200},
        {"role": "assistant", "content": "Task finished. " * 200},
    ]
    result = await compact_context(
        CompactionRequest(
            session_id="revision",
            entries=entries,
            context_window_tokens=100,
            config=CompactionConfig(
                identifier_policy="off", llm_plan=CompactionExecutionPlan(candidates=(target,))
            ),
            forced_prefix_cut=2,
            custom_instructions="f" * 2000,
        )
    )
    assert len(provider.calls) == 2
    assert "long checkpoint" in provider.calls[1][-1].content
    assert all(entry["content"] not in str(provider.calls[1]) for entry in entries)
    assert result.summary == "Work completed." and result.removed_count == 2


@pytest.mark.asyncio
async def test_attempt_callback_is_after_admission_and_survives_config_clone():
    calls = []
    config = synthetic_compaction_config()
    config.on_summary_call_started = lambda: calls.append("started")
    copied = replace(config)
    await call_compaction_provider(
        "source", "", copied.llm_plan, on_summary_call_started=copied.on_summary_call_started
    )
    assert calls == ["started"]


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_adapter_accounting_failure_remains_failure_through_compaction(busy):
    from opensquilla.engine.usage_accounting import (
        UsageAccountingBusyError,
        UsageAccountingUnavailableError,
    )

    error_type = UsageAccountingBusyError if busy else UsageAccountingUnavailableError
    failure = error_type("Ledger rejected start", no_prior_provider_dispatch=True)

    class Provider:
        accounts_physical_usage = True

        async def chat(self, messages, tools=None, config=None):
            raise failure
            yield  # Keep the ordinary async-iterator provider contract.

    config = synthetic_compaction_config()
    config.llm_plan = CompactionExecutionPlan(
        candidates=(replace(config.llm_plan.primary, provider=Provider()),)
    )
    entries = [
        {"role": "user", "content": "Completed old work. " * 100},
        {"role": "assistant", "content": "Old work accepted. " * 100},
    ]
    original = deepcopy(entries)
    with pytest.raises(error_type) as caught:
        await compact_context(
            CompactionRequest(
                session_id="accounting-failure",
                entries=entries,
                context_window_tokens=1000,
                config=config,
                force=True,
            )
        )
    assert caught.value is failure
    assert caught.value.no_prior_provider_dispatch
    assert entries == original


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "pending", "completed", "error"])
@pytest.mark.parametrize("shape", ["nested-json", "nested-mapping", "top-level-json"])
async def test_legacy_tool_body_status_controls_raw_protection(status, shape):
    payload = {"execution_status": {"status": status}}
    tool_round = [
        {"role": "user", "content": "Run current command."},
        {
            "role": "assistant",
            "content": "Command result",
            "tool_calls": [
                {"type": "tool_use", "tool_use_id": "call-live", "name": "exec"},
            ],
        },
    ]
    if shape == "top-level-json":
        tool_round.append(
            {
                "role": "tool",
                "content": json.dumps({"status": status}),
                "tool_call_id": "call-live",
            }
        )
    else:
        tool_round[-1]["tool_calls"].append(
            {
                "type": "tool_result",
                "tool_use_id": "call-live",
                "result": json.dumps(payload) if shape == "nested-json" else payload,
            }
        )
    live = status in {"running", "pending"}
    # The runtime window planner uses this same group gate.
    assert _api_round_requires_raw(tool_round) is live
    completed_prefix = [
        {"role": "user", "content": "Earlier work. " * 100},
        {"role": "assistant", "content": "Earlier work completed. " * 100},
    ]
    result = await compact_context(
        CompactionRequest(
            session_id="legacy-live",
            entries=completed_prefix + tool_round,
            context_window_tokens=1000,
            config=synthetic_compaction_config(),
            force=True,
        )
    )
    assert result.removed_count == (2 if live else 2 + len(tool_round))
    assert result.kept_entries == (tool_round if live else [])


@pytest.mark.asyncio
async def test_final_kept_image_uses_same_omission_policy_as_initial_count():
    encoded = base64.b64encode(image_bytes("PNG")).decode()
    entries = [
        {"role": "user", "content": "Completed old work. " * 200},
        {"role": "assistant", "content": "Old work accepted. " * 200},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "text": "Keep this recent diagram.",
                    "attachments": [{"type": "image/png", "name": "diagram.png", "data": encoded}],
                }
            ),
        },
        {"role": "assistant", "content": "Recent image acknowledged."},
    ]
    frozen = deepcopy(entries)
    result = await compact_context(
        CompactionRequest(
            session_id="image-policy",
            entries=entries,
            context_window_tokens=500,
            config=synthetic_compaction_config(preserve_historical_images=False),
            forced_prefix_cut=2,
        )
    )
    assert result.removed_count == 2
    assert result.tokens_after < 500
    assert result.kept_entries == entries[2:]
    assert entries == frozen


@pytest.mark.asyncio
async def test_required_chunks_do_not_consume_an_implicit_two_call_retry_budget(monkeypatch):
    monkeypatch.setattr(
        "opensquilla.session.compaction._compaction_target_input_budget", lambda *args: 200
    )
    entries = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Step {i}. " * 100}
        for i in range(12)
    ]
    config = synthetic_compaction_config()
    result = await compact_context(
        CompactionRequest(
            session_id="many-chunks",
            entries=entries,
            context_window_tokens=500,
            config=config,
            forced_prefix_cut=12,
        )
    )
    assert result.removed_count == 12
    assert result.chunks_processed == 6
    assert config.llm_calls_started == 6
    calls = config.llm_plan.primary.provider.calls
    seen = [message.content for messages, _, _ in calls for message in messages[:-1]]
    assert seen == [entry["content"] for entry in entries]


def test_missing_image_reference_is_described_without_fake_media():
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    entries = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "text": "Earlier diagram",
                    "attachments": [
                        {
                            "type": "image/png",
                            "name": "missing.png",
                            "sha256_ref": "a" * 64,
                            "path": "/untrusted/file.png",
                        }
                    ],
                }
            ),
        }
    ]
    messages, tools, config = build(provider, entries)
    wire = json.dumps(provider.project_final_request(messages, tools, config).payload)
    assert "not reread" in wire
    assert "data:image" not in wire and "/untrusted/" not in wire


def test_legacy_attachment_without_text_field_never_sends_storage_json():
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    encoded = base64.b64encode(image_bytes("PNG")).decode()
    entries = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "attachments": [
                        {"type": "image/png", "name": "legacy.png", "data": encoded},
                    ]
                }
            ),
        }
    ]
    messages, tools, config = build(provider, entries)
    wire = json.dumps(provider.project_final_request(messages, tools, config).payload)
    assert encoded not in wire and '\\"attachments\\"' not in wire
    assert "not reread" in wire


@pytest.mark.asyncio
async def test_chunk_packing_preserves_generation_for_character_heavy_history(monkeypatch):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    calls = []

    async def chat(messages, tools=None, config=None):
        calls.append((messages, tools, config))
        yield TextDeltaEvent(text="Earlier steps completed.")
        yield DoneEvent()

    monkeypatch.setattr(provider, "chat", chat)
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id="openai",
        model="synthetic-model",
        context_window_tokens=16_000,
        max_output_tokens=4096,
        max_generation_tokens=4096,
        provider_request_max_chars_explicit_cap=0,
    )
    entries = [
        entry
        for i in range(10)
        for entry in (
            {"role": "user", "content": f"Recorded step {i}: " + "a" * 6000},
            {"role": "assistant", "content": f"Completed step {i}."},
        )
    ]
    result = await compact_context(
        CompactionRequest(
            session_id="character-heavy",
            entries=entries,
            context_window_tokens=4000,
            forced_prefix_cut=len(entries),
            config=CompactionConfig(
                identifier_policy="off",
                llm_plan=CompactionExecutionPlan(candidates=(target,)),
                request_context=CompactionRequestContext(chat_config=ChatConfig(max_tokens=4096)),
            ),
        )
    )
    assert result.removed_count == len(entries)
    assert len(calls) > 1
    for messages, tools, config in calls:
        assert config.max_tokens == 4096
        assert provider.project_final_request(messages, tools, config).fits
    seen = [m.content for messages, _, _ in calls for m in messages[:-1]]
    assert seen == [entry["content"] for entry in entries]


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_checkpoint", [False, True])
async def test_indivisible_round_revises_only_when_checkpoint_causes_pressure(
    monkeypatch,
    oversized_checkpoint,
):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    calls = []

    async def chat(messages, tools=None, config=None):
        calls.append((messages, tools, config))
        yield TextDeltaEvent(
            text="Checkpoint. " * 100
            if oversized_checkpoint and len(calls) == 1
            else "Recorded work completed; continue remaining tasks."
        )
        yield DoneEvent()

    monkeypatch.setattr(provider, "chat", chat)
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id="openai",
        model="synthetic-model",
        context_window_tokens=6000,
        max_output_tokens=4096,
        max_generation_tokens=4096,
        provider_request_max_chars_explicit_cap=0,
    )
    round_count = 2 if oversized_checkpoint else 3
    source_chars = 500 if oversized_checkpoint else 1500
    entries = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Step {i}: " + "a" * source_chars,
        }
        for i in range(2 * round_count)
    ]
    frozen = deepcopy(entries)
    config = CompactionConfig(
        identifier_policy="off",
        llm_plan=CompactionExecutionPlan(candidates=(target,)),
        request_context=CompactionRequestContext(chat_config=ChatConfig(max_tokens=4096)),
    )
    result = await compact_context(
        CompactionRequest(
            session_id="indivisible-round",
            entries=entries,
            context_window_tokens=4000,
            forced_prefix_cut=len(entries),
            config=config,
        )
    )
    assert result.chunks_processed == round_count
    if oversized_checkpoint:
        # Source alone admits the full allowance: revise the actual oversized
        # checkpoint once instead of letting it starve the next generation.
        assert [len(messages) for messages, _, _ in calls] == [3, 1, 3]
        assert all(chat_config.max_tokens == 4096 for _, _, chat_config in calls)
    else:
        # Three indivisible rounds require three calls. Even an empty checkpoint
        # cannot reserve 4096 output tokens; shortening a tiny one cannot fix it.
        assert [len(messages) for messages, _, _ in calls] == [3, 3, 3]
        assert all(0 < chat_config.max_tokens < 4096 for _, _, chat_config in calls)
    assert all(
        provider.project_final_request(messages, tools, chat_config).fits
        for messages, tools, chat_config in calls
    )
    assert [message.content for messages, _, _ in calls for message in messages[:-1]] == [
        entry["content"] for entry in entries
    ]
    assert result.removed_count == len(entries) and result.kept_entries == []
    assert entries == frozen


@pytest.mark.asyncio
async def test_expired_context_parent_prevents_summary_dispatch():
    from opensquilla.compaction_timing import CompactionOperationTimeoutError

    config = synthetic_compaction_config()
    config.request_context = CompactionRequestContext(
        chat_config=ChatConfig(
            turn_deadline_at_monotonic=0,
        )
    )
    assert arm_compaction_deadline(config, operation_id="parent-expired") == 0
    attempted = []
    with pytest.raises(CompactionOperationTimeoutError):
        await call_compaction_provider(
            "source",
            "",
            config.llm_plan,
            request_context=config.request_context,
            on_summary_call_started=lambda: attempted.append(True),
        )
    assert attempted == []
    assert config.llm_plan.primary.provider.calls == []


@pytest.mark.parametrize(
    "mime", ["application/pdf", "audio/wav", "application/octet-stream", "text/plain"]
)
@pytest.mark.parametrize("material", ["inline", "missing", "reference", "invalid"])
def test_summary_non_image_attachments_never_claim_hypothetical_materialization(mime, material):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    encoded = base64.b64encode(b"OPAQUE_ARCHIVED_CONTENT_42").decode()
    attachment = {"type": mime, "name": "material.bin", "path": "/private/untrusted.bin"}
    if material == "inline":
        attachment["data"] = encoded
    elif material == "reference":
        attachment["sha256_ref"] = "a" * 64
    elif material == "invalid":
        attachment["data"] = "%%%invalid-base64%%%"
    else:
        attachment["missing_reason"] = "not retained"
    entries = [
        {
            "role": "user",
            "content": json.dumps({"text": "Earlier attachment.", "attachments": [attachment]}),
        }
    ]
    messages, tools, config = build(provider, entries)
    wire = json.dumps(provider.project_final_request(messages, tools, config).payload)
    assert encoded not in wire and "OPAQUE_ARCHIVED_CONTENT_42" not in wire
    assert "%%%invalid-base64%%%" not in wire and "/private/" not in wire
    assert "available:" not in wire.replace("unavailable:", "")
    assert ".opensquilla/attachments/" not in wire
    assert "not reread" in wire and "attachment_id=" in wire
