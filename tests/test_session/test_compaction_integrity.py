"""Durable compaction validates the complete frozen source and final replay."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from opensquilla.compaction_status import compaction_failure_status
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    ProviderFinalRequestProjection,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
)
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionRequestContext,
    _compaction_quality_report,
    _compaction_target_input_budget,
    _fit_compaction_input_to_target,
    _fit_structured_summary_current_status,
    _rolling_chunk_text,
    call_compaction_provider,
    compact_context,
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
    validate_compaction_artifact,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from opensquilla.session.compaction_lifecycle import ConsumerAdmissionStaleError
from opensquilla.session.compaction_state import (
    CompactionObligation,
    StructuredCompactionSummary,
    build_structured_summary_from_text,
    render_structured_summary,
)
from opensquilla.session.context_view import (
    compaction_replay_is_complete,
    format_compaction_summary_context,
)
from opensquilla.session.tokenizer import estimate_tokens
from tests.helpers.compaction import SyntheticCompactionProvider, synthetic_compaction_config


def source_entries():
    return [
        {"role": "user", "content": "Old task " + "background " * 200, "token_count": 500},
        {"role": "assistant", "content": "Earlier answer", "token_count": 100},
        {"role": "user", "content": "Continue the task", "token_count": 10},
        {"role": "assistant", "content": "Current answer", "token_count": 10},
    ]


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
@pytest.mark.parametrize("failure", ["empty", "whitespace", "length", "error", "oversized"])
async def test_failed_summary_never_replaces_source(monkeypatch, layout, failure):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)

    class Provider:
        async def chat(self, messages, tools=None, config=None):
            if failure == "error":
                yield ErrorEvent(message="synthetic unavailable", code="429")
            elif failure == "oversized":
                yield TextDeltaEvent(text="long " * 2000)
                yield DoneEvent()
            else:
                if failure == "length":
                    yield TextDeltaEvent(text="unfinished")
                elif failure == "whitespace":
                    yield TextDeltaEvent(text=" \n\t")
                yield DoneEvent(stop_reason="length" if failure == "length" else "end_turn")

    cfg = synthetic_compaction_config()
    cfg.request_context = CompactionRequestContext(chat_config=ChatConfig(max_tokens=4096))
    cfg.llm_plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
        provider=Provider(), provider_id="synthetic", model="summary", context_window_tokens=8000,
    ),))
    entries = source_entries()
    original = deepcopy(entries)
    result = await compact_context(CompactionRequest(
        session_id="failed-prefix", entries=entries, context_window_tokens=4000,
        forced_prefix_cut=2, trigger="message_count", config=cfg,
    ))
    assert result.removed_count == 0
    assert result.kept_entries == original
    assert result.summary == ""
    assert result.skip_reason == (
        "suffix_summary_failed" if layout == "suffix" else "summary_failed"
    )


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
@pytest.mark.parametrize("explicit_cap", [0, 18_000])
async def test_summary_request_rebinds_window_without_freezing_derived_character_cap(
    monkeypatch, layout, explicit_cap,
):
    from opensquilla.provider.request_proof import (
        provider_request_character_budget,
        provider_request_token_budget,
    )

    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    seen = []

    class Provider:
        def project_final_request(self, messages, tools=None, config=None, *, message_limit=None):
            # The physical adapter applies a different output cap from the
            # logical request. Both summary acceptance and input proof use it.
            payload = {"max_completion_tokens": 6000}
            seen.append((config, provider_request_character_budget(payload, config)))
            assert config.provider_context_window_tokens == 12_000
            assert provider_request_token_budget(payload, config) < 12_000 - 4096
            return ProviderFinalRequestProjection(
                payload=payload, proof={"estimated_tokens": 100},
                wire_message_count=len(messages), message_limit=None,
                fits_message_count=None, fits=True,
            )

        async def chat(self, messages, tools=None, config=None):
            yield TextDeltaEvent(text="Completed work is complete; continue the current task.")
            yield DoneEvent(output_tokens=5000, reasoning_tokens=4980)

    target = CompactionExecutionTarget(
        provider=Provider(), provider_id="synthetic", model="current-summary-model",
        context_window_tokens=12_000, provider_request_max_chars=32_000,
        provider_request_max_chars_explicit_cap=explicit_cap,
    )
    context = CompactionRequestContext(chat_config=ChatConfig(
        max_tokens=4096, provider_context_window_tokens=64_000,
        provider_request_max_chars=32_000,
        provider_request_max_chars_explicit_cap=explicit_cap,
    ))
    result = await call_compaction_provider(
        "Earlier completed work", "", CompactionExecutionPlan(candidates=(target,)),
        request_context=context, source_entries=source_entries()[:2],
    )

    assert result == "Completed work is complete; continue the current task."
    assert seen
    for config, char_cap in seen:
        assert config.max_tokens == 4096
        assert config.provider_request_max_chars_explicit_cap == explicit_cap
        if explicit_cap:
            assert char_cap == explicit_cap
        else:
            assert 0 < char_cap < 32_000
    assert context.chat_config.provider_context_window_tokens == 64_000


async def test_suffix_unknown_window_does_not_inherit_parent_physical_window(monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", "suffix")
    provider = SyntheticCompactionProvider()
    target = CompactionExecutionTarget(
        provider=provider, provider_id="synthetic", model="unknown-summary-model",
        context_window_tokens=16_000, context_window_source="bounded_fallback",
    )
    context = CompactionRequestContext(chat_config=ChatConfig(
        max_tokens=4096, provider_context_window_tokens=64_000,
    ))

    result = await call_compaction_provider(
        "Earlier completed work", "", CompactionExecutionPlan(candidates=(target,)),
        request_context=context, source_entries=source_entries()[:2],
    )

    assert result == provider.summary
    assert len(provider.calls) == 1
    assert provider.calls[0][2].provider_context_window_tokens == 0
    assert context.chat_config.provider_context_window_tokens == 64_000


async def test_repeated_unavailable_provider_does_not_grow_previous_checkpoint():
    entries = source_entries()
    previous = "Existing complete checkpoint"
    for _ in range(10):
        result = await compact_context(CompactionRequest(
            session_id="repeated-failure", entries=entries, previous_summary=previous,
            context_window_tokens=4000, forced_prefix_cut=2, trigger="message_count",
            config=CompactionConfig(),
        ))
        assert result.kept_entries == entries
        assert result.removed_count == 0
        assert result.replaced_previous_summary is False
        assert result.summary == ""
        assert result.skip_reason == "summary_target_unavailable"


def test_smaller_target_refuses_unread_middle_instead_of_preprojection():
    config = synthetic_compaction_config()
    target = replace(config.llm_plan.primary, context_window_tokens=4000)
    entries = [{"role": "user", "content": "start " * 3000 + "middle fact" + "end " * 3000}]
    request = CompactionRequest(
        session_id="small-target", entries=entries, context_window_tokens=4000, config=config,
    )
    assert _fit_compaction_input_to_target(
        request=request, target=target, previous_summary="", chunk=entries,
    ) is None
    assert "middle fact" in entries[0]["content"]


def test_final_fit_cannot_hide_previously_covered_goals():
    obligations = [
        CompactionObligation(kind="user_goal", value=f"unique goal {i}") for i in range(15)
    ]
    summary, before = build_structured_summary_from_text(
        "\n".join(item.value for item in obligations), obligations, block_missing_critical=True,
    )
    assert before.status == "pass"
    original = summary.model_dump()
    assert not _fit_structured_summary_current_status(summary, max_tokens=80, max_chars=130)
    assert summary.model_dump() == original
    coverage, reason = validate_compaction_artifact(render_structured_summary(summary), obligations)
    assert reason is None
    assert coverage.status == "pass"


async def test_fitting_refuses_to_delete_unextracted_fact_from_nonempty_summary():
    fact = "The retired route uses copper while the current route uses silver."
    text = (
        "Keep src/synthetic/main.py. "
        + "Earlier ordinary context remains useful. " * 25
        + fact
        + " Recent ordinary context remains useful." * 25
    )
    assert estimate_tokens(text) < 1024
    entries = source_entries()
    entries[0]["content"] += "\nFile src/synthetic/main.py.\n" + fact
    original = deepcopy(entries)
    result = await compact_context(CompactionRequest(
        session_id="whole-checkpoint", entries=entries, context_window_tokens=200,
        forced_prefix_cut=2, trigger="message_count",
        config=synthetic_compaction_config(summary=text),
    ))

    assert result.skip_reason == "summary_does_not_fit"
    assert result.removed_count == 0
    assert result.summary == ""
    assert result.kept_entries == original
    assert entries == original


@pytest.mark.parametrize("window", [200_000, 1_000_000])
@pytest.mark.parametrize("character_limit", [None, 18_000])
async def test_proportional_tail_scales_with_capacity_without_fixed_cap(window, character_limit):
    entries = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Record {index}: " + "harmless background " * 30,
            "token_count": window // 100,
        }
        for index in range(90)
    ]
    cfg = synthetic_compaction_config(safety_margin=1.2)
    cfg.llm_plan = CompactionExecutionPlan(candidates=(replace(
        cfg.llm_plan.primary, context_window_tokens=2_000_000,
    ),))
    result = await compact_context(CompactionRequest(
        session_id="proportional-tail", entries=entries, context_window_tokens=window,
        context_window_chars=character_limit, config=cfg,
    ))

    assert result.summary_source == "llm"
    assert result.kept_entries == entries[result.removed_count:]
    assert result.removed_count % 2 == 0
    kept_tokens = sum(estimate_entry_model_replay_tokens(entry) for entry in result.kept_entries)
    assert kept_tokens <= window // 5
    if character_limit is None:
        # The shared policy retains one fifth of available history, capped by
        # consumer capacity, so manual compaction of a short transcript has a
        # useful prefix without sacrificing the same recent tail as automatic.
        history_tokens = sum(estimate_entry_model_replay_tokens(entry) for entry in entries)
        assert kept_tokens == min(window, history_tokens) // 5
        assert kept_tokens > 20_000
    else:
        assert estimate_entries_model_replay_chars(result.kept_entries) <= character_limit // 5
        preceding_round_and_tail = entries[result.removed_count - 2:]
        assert estimate_entries_model_replay_chars(preceding_round_and_tail) > character_limit // 5
    assert result.quality_report["pressure_released"] is True


@pytest.mark.parametrize("dimension", ["tokens", "chars"])
@pytest.mark.parametrize("pressure", [8400, 8500])
@pytest.mark.parametrize("explicit_margin", [None, 1.0])
async def test_default_soft_trigger_is_85_percent_for_tokens_and_characters(
    dimension, pressure, explicit_margin,
):
    entries = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": ""}
        for index in range(4)
    ]
    if dimension == "tokens":
        for entry in entries:
            entry.update(content="Ordinary completed history", token_count=pressure // 4)
        window, char_window = 10_000, None
    else:
        entries[0]["content"] = "b" * (
            pressure - estimate_entries_model_replay_chars(entries)
        )
        assert estimate_entries_model_replay_chars(entries) == pressure
        window, char_window = 100_000, 10_000
    cfg = synthetic_compaction_config()
    if explicit_margin is not None:
        cfg.safety_margin = explicit_margin
    provider = cfg.llm_plan.primary.provider
    result = await compact_context(CompactionRequest(
        session_id="soft-trigger", entries=entries, context_window_tokens=window,
        context_window_chars=char_window, config=cfg,
    ))

    triggered = pressure == 8500 and explicit_margin is None
    if triggered:
        assert result.removed_count > 0
        assert result.summary_source == "llm"
        assert len(provider.calls) == 1
    else:
        assert result.skip_reason == "within_compaction_budget"
        assert result.kept_entries == entries
        assert provider.calls == []


@pytest.mark.parametrize(
    ("tokens_after", "chars_after", "released"),
    [(849, 1699, True), (850, 1600, False), (800, 1700, False)],
)
def test_pressure_diagnostic_does_not_reject_a_valid_smaller_checkpoint(
    tokens_after, chars_after, released,
):
    report = _compaction_quality_report(
        cfg=CompactionConfig(), entries=[], kept=[], tokens_before=950,
        tokens_after=tokens_after, removed_count=2, context_window_tokens=1000,
        chars_after=chars_after, context_window_chars=2000,
    )
    assert report["passes_structural_gate"] is True
    assert report["pressure_released"] is released


@pytest.mark.parametrize("bounded", [False, True])
def test_final_wrapper_preserves_all_paths_or_rejects_explicit_bounded_replay(bounded):
    paths = ["synthetic/" + str(i) + "x" * 460 + ".txt" for i in range(40)]
    summary = StructuredCompactionSummary(files_and_artifacts=[{"path": path} for path in paths])
    text = render_structured_summary(summary)
    obligations = [CompactionObligation(kind="file_path", value=path) for path in paths]
    def replay(summary):
        return format_compaction_summary_context([summary], max_chars=16_000 if bounded else None)

    coverage, reason = validate_compaction_artifact(
        text, obligations, summary_replay_renderer=replay,
    )
    assert coverage.status == "pass"
    assert reason == ("summary_replay_incomplete" if bounded else None)
    rendered = replay(text)
    assert rendered
    if bounded:
        assert len(rendered) <= 16000
        assert not compaction_replay_is_complete([text], rendered)
    else:
        assert len(rendered) > 16000
        assert all(path in rendered for path in paths)
        assert compaction_replay_is_complete([text], rendered)


def test_model_prose_heading_does_not_make_complete_checkpoint_invalid():
    summary, _ = build_structured_summary_from_text(
        "Completed work.\n\nGoal:\nBuild the requested app.", [],
    )
    text = render_structured_summary(summary)
    assert len(text) < 200
    coverage, reason = validate_compaction_artifact(text, [])
    assert coverage.status == "unknown"
    assert reason is None
    assert compaction_replay_is_complete([text], format_compaction_summary_context([text]))


def test_actual_renderer_must_replay_the_whole_artifact():
    coverage, reason = validate_compaction_artifact(
        "complete checkpoint", [], summary_replay_renderer=lambda text: text[:5],
    )
    assert coverage.status == "unknown"
    assert reason == "summary_replay_incomplete"


async def test_stale_consumer_returns_unchanged_source():
    def stale(summary, kept):
        raise ConsumerAdmissionStaleError("synthetic deployment changed")

    entries = source_entries()
    result = await compact_context(CompactionRequest(
        session_id="stale-consumer", entries=entries, context_window_tokens=4000,
        config=synthetic_compaction_config(), forced_prefix_cut=2,
        trigger="message_count", consumer_admission=stale,
    ))
    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "consumer_admission_stale"


@pytest.mark.parametrize("rejection", ["consumer", "stale", "replay"])
async def test_rejected_candidate_does_not_report_pressure_released(rejection):
    def admit(summary, kept):
        if rejection == "stale":
            raise ConsumerAdmissionStaleError("synthetic deployment changed")
        return rejection != "consumer"

    entries = source_entries()
    entries[0]["token_count"] = 3700
    result = await compact_context(CompactionRequest(
        session_id="rejected-pressure", entries=entries, context_window_tokens=4000,
        config=synthetic_compaction_config(), forced_prefix_cut=2,
        trigger="message_count", consumer_admission=admit,
        summary_replay_renderer=(lambda text: text[:5]) if rejection == "replay" else None,
    ))

    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.tokens_after == result.tokens_before > 4000 * 0.85
    assert result.skip_reason == {
        "consumer": "consumer_admission_failed",
        "stale": "consumer_admission_stale",
        "replay": "summary_replay_incomplete",
    }[rejection]
    assert result.quality_report["pressure_released"] is False


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("rejection", [None, "consumer", "replay"])
async def test_nonshrinking_candidate_does_not_report_pressure_released(manual, rejection):
    entries = [
        {"role": "user", "content": "Preserve the source file src/main.py."},
        {"role": "assistant", "content": "Brief old answer"},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "Current reply"},
    ]
    result = await compact_context(CompactionRequest(
        session_id="nonshrinking-pressure", entries=entries, context_window_tokens=4000,
        config=synthetic_compaction_config(summary="Completed ordinary background. " * 20),
        forced_prefix_cut=2,
        force=manual,
        reason="manual" if manual else "automatic",
        consumer_admission=lambda _summary, _kept: rejection != "consumer",
        summary_replay_renderer=(lambda text: text[:5]) if rejection == "replay" else None,
    ))

    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.skip_reason == {
        None: "no_compression_benefit",
        "consumer": "consumer_admission_failed",
        "replay": "summary_replay_incomplete",
    }[rejection]
    assert compaction_failure_status(result.skip_reason) == (
        "skipped" if rejection is None else "failed"
    )
    assert result.tokens_after == result.tokens_before
    assert result.summary == ""
    assert result.quality_report["pressure_released"] is False
    if rejection is None:
        assert result.coverage_status == "pass"
        assert result.quality_report["compression_ratio"] > 1
        assert result.quality_report["protected_tail_preserved"] is True
        assert result.quality_report["fits_context_window"] is True
        assert result.quality_report["fits_character_window"] is True
        assert result.quality_report["consumer_admission_fits"] is True
        assert result.quality_report["passes_structural_gate"] is False


@pytest.mark.parametrize("reasoning_control", [True, False])
@pytest.mark.parametrize("failure", [None, "body_cap", "input_reserve"])
async def test_prefix_reasoning_uses_current_generation_budget_without_control_metadata(
    monkeypatch, reasoning_control, failure,
):
    from opensquilla.provider.types import ProviderFinalRequestProjection
    from opensquilla.session.tokenizer import estimate_tokens

    provider = SyntheticCompactionProvider("short body")

    def project(messages, tools=None, config=None, *, message_limit=None):
        return ProviderFinalRequestProjection(
            payload={
                "max_tokens": config.max_tokens,
                **({"enable_thinking": True} if reasoning_control else {}),
            },
            proof={"estimated_tokens": 100}, wire_message_count=len(messages),
            message_limit=None, fits_message_count=None, fits=True,
        )

    async def chat(messages, tools=None, config=None):
        provider.calls.append((messages, tools, config))
        reasoning = "synthetic reasoning " * 600
        assert 1024 < estimate_tokens(reasoning) < 4096
        yield ReasoningDeltaEvent(text=reasoning)
        yield TextDeltaEvent(text="body " * 2000 if failure == "body_cap" else "short body")
        yield DoneEvent(output_tokens=3000, reasoning_tokens=2995)

    monkeypatch.setattr(provider, "project_final_request", project, raising=False)
    monkeypatch.setattr(provider, "chat", chat)
    plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
        provider=provider, provider_id="synthetic", model="reasoning-only",
        context_window_tokens=4000 if failure == "input_reserve" else 8000,
    ),))
    result = await call_compaction_provider(
        "history", "", plan,
        request_context=CompactionRequestContext(chat_config=ChatConfig(max_tokens=4096)),
    )
    assert result == (None if failure else "short body")
    if failure == "input_reserve":
        assert provider.calls == []
        return
    assert provider.calls[0][2].max_tokens == 4096
    assert provider.calls[0][2].thinking is False


@pytest.mark.parametrize("forced", [False, True])
async def test_call_budget_keeps_unprocessed_rounds_raw(forced):
    cfg = synthetic_compaction_config(safety_margin=1.0, protected_recent_messages=2)
    provider = cfg.llm_plan.primary.provider
    cfg.llm_plan = CompactionExecutionPlan(candidates=(replace(
        cfg.llm_plan.primary, context_window_tokens=2500, max_output_tokens=128,
    ),))
    entries = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"item-{index} " + "unique long background " * 140,
            "token_count": 600,
        }
        for index in range(20)
    ]
    result = await compact_context(CompactionRequest(
        session_id="bounded-calls", entries=entries, context_window_tokens=8000,
        config=cfg, forced_prefix_cut=18 if forced else None,
    ))
    if forced:
        assert result.removed_count == 0
        assert result.kept_entries == entries
        assert result.skip_reason == "summary_call_budget_exceeded"
        assert provider.calls == []
        return
    assert 0 < result.removed_count < 18
    assert result.removed_count % 2 == 0
    assert result.kept_entries == entries[result.removed_count:]
    assert len(provider.calls) == 2
    prompts = "\n".join(call[0][0].content for call in provider.calls)
    for entry in entries[:result.removed_count]:
        assert entry["content"] in prompts
    for entry in result.kept_entries:
        assert entry["content"] not in prompts
    assert "preprojection" not in prompts


class CharacterBoundedCompactionProvider(SyntheticCompactionProvider):
    """Synthetic wire proof whose character cap binds before its token cap."""

    def project_final_request(self, messages, tools=None, config=None, *, message_limit=None):
        payload = {
            "messages": [message.model_dump(mode="json") for message in messages],
            "tools": [tool.model_dump(mode="json") for tool in tools or []],
            "system": config.system,
            "max_tokens": config.max_tokens,
        }
        wire = json.dumps(payload, ensure_ascii=False)
        return ProviderFinalRequestProjection(
            payload=payload,
            proof={"estimated_tokens": estimate_tokens(wire), "estimated_chars": len(wire)},
            wire_message_count=len(messages), message_limit=None, fits_message_count=None,
            fits=len(wire) <= 9000,
        )


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
@pytest.mark.parametrize("forced", [False, True])
async def test_dense_text_character_cap_does_not_become_a_token_chunk_cap(
    monkeypatch, layout, forced,
):
    from opensquilla.provider.openai import OpenAIProvider

    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    provider = OpenAIProvider(api_key="synthetic-key")
    capture = SyntheticCompactionProvider()
    monkeypatch.setattr(provider, "chat", capture.chat)
    char_cap = 10_000
    target = CompactionExecutionTarget(
        provider=provider, provider_id="synthetic", model="synthetic-summary",
        context_window_tokens=32_000, provider_request_max_chars=char_cap,
        provider_request_max_chars_explicit_cap=char_cap,
    )
    entries = [
        entry
        for index in range(3)
        for entry in [
            {
                "role": "user", "content": f"记录{index}：" + "甲乙丙丁戊己庚辛壬癸" * 200,
                "token_count": 3000,
            },
            {"role": "assistant", "content": f"已完成记录{index}", "token_count": 1000},
        ]
    ] + [
        {"role": "user", "content": "Continue current task", "token_count": 10},
        {"role": "assistant", "content": "Current response", "token_count": 10},
    ]
    request = CompactionRequest(
        session_id="dense-character-budget", entries=entries, context_window_tokens=8000,
        forced_prefix_cut=6 if forced else None,
        config=CompactionConfig(
            llm_plan=CompactionExecutionPlan(candidates=(target,)),
            request_context=CompactionRequestContext(chat_config=ChatConfig(
                max_tokens=4096, provider_request_max_chars=char_cap,
                provider_request_max_chars_explicit_cap=char_cap,
            )),
            protected_recent_messages=2,
        ),
    )
    source = deepcopy(entries)
    source_tokens = estimate_tokens(_rolling_chunk_text("", entries[:6]))
    assert source_tokens > char_cap // 4
    assert _compaction_target_input_budget(request) > source_tokens

    result = await compact_context(request)

    assert result.removed_count == 6
    assert result.kept_entries == source[6:]
    assert request.entries == source
    assert result.summary_source == "llm"
    assert len(capture.calls) == 1
    messages, tools, config = capture.calls[0]
    projection = provider.project_final_request(messages, tools, config)
    assert projection.fits
    assert projection.proof["token_budget_source"] == "physical_context_window"
    assert projection.proof["estimated_tokens"] > char_cap // 4
    assert projection.proof["estimated_chars"] < char_cap
    assert config.max_tokens == 4096
    wire = json.dumps(projection.payload, ensure_ascii=False)
    for entry in source[:6]:
        assert entry["content"] in wire
    for entry in source[6:]:
        assert entry["content"] not in wire


def character_bounded_compaction_case(rounds=2, summary="Complete portable checkpoint"):
    provider = CharacterBoundedCompactionProvider(summary)
    context = CompactionRequestContext(
        chat_config=ChatConfig(max_tokens=4096, system="Synthetic current system", thinking=True),
        tools=(ToolDefinition(
            name="synthetic_lookup", description="Look up synthetic records",
            input_schema={"type": "object", "properties": {}},
        ),),
    )
    target = CompactionExecutionTarget(
        provider=provider, provider_id="synthetic", model="summary",
        context_window_tokens=32000, provider_request_max_chars=20000,
    )
    entries = [
        entry
        for index in range(rounds)
        for entry in [
            {"role": "user", "content": f"Round {index}: " + "layout " * 700},
            {"role": "assistant", "content": f"Acknowledged round {index}"},
        ]
    ] + [
        {"role": "user", "content": "Continue the current task"},
        {"role": "assistant", "content": "Current response"},
    ]
    config = CompactionConfig(
        llm_plan=CompactionExecutionPlan(candidates=(target,)), request_context=context,
    )
    request = CompactionRequest(
        session_id="character-bounded", entries=entries, context_window_tokens=16000,
        config=config, forced_prefix_cut=rounds * 2, trigger="message_count",
    )
    return provider, request


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
async def test_character_admission_splits_complete_token_fitting_rounds(monkeypatch, layout):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    provider, request = character_bounded_compaction_case()
    source = deepcopy(request.entries)
    # The old token-only packing admitted both rounds as one oversized wire request.
    assert estimate_tokens(_rolling_chunk_text("", source[:4])) < (
        _compaction_target_input_budget(request)
    )
    result = await compact_context(request)
    assert result.removed_count == 4
    assert result.kept_entries == source[4:]
    assert request.entries == source
    assert len(provider.calls) == 2
    for index, (messages, tools, config) in enumerate(provider.calls):
        projection = provider.project_final_request(messages, tools, config)
        assert projection.fits
        prompt = json.dumps(projection.payload)
        assert source[index * 2]["content"] in prompt
        assert source[index * 2 + 1]["content"] in prompt
        assert source[(1 - index) * 2]["content"] not in prompt
        assert config.max_tokens == 4096
        if layout == "suffix":
            assert tools == list(request.config.request_context.tools)
            assert config.system == request.config.request_context.chat_config.system
            assert config.thinking is True
            assert messages[-1].content.startswith("Summarize the preceding conversation")


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
async def test_character_chunks_cannot_shrink_a_forced_cut_to_meet_call_limit(monkeypatch, layout):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    provider, request = character_bounded_compaction_case(rounds=3)
    result = await compact_context(request)
    assert result.removed_count == 0
    assert result.kept_entries == request.entries
    assert result.summary == ""
    assert result.skip_reason == (
        "suffix_call_budget_exceeded" if layout == "suffix" else "summary_call_budget_exceeded"
    )
    assert provider.calls == []


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
async def test_automatic_character_chunks_keep_unprocessed_rounds_raw(monkeypatch, layout):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    provider, request = character_bounded_compaction_case(rounds=4)
    request = replace(request, forced_prefix_cut=None, context_window_chars=15000)
    result = await compact_context(request)
    assert len(provider.calls) == 2
    assert result.removed_count == 4
    assert result.kept_entries == request.entries[4:]
    prompts = json.dumps([
        provider.project_final_request(messages, tools, config).payload
        for messages, tools, config in provider.calls
    ])
    for entry in request.entries[:4]:
        assert entry["content"] in prompts
    for entry in request.entries[4:]:
        assert entry["content"] not in prompts


@pytest.mark.parametrize("layout", ["prefix", "suffix"])
async def test_larger_rolling_checkpoint_must_pass_actual_character_admission(monkeypatch, layout):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    provider, request = character_bounded_compaction_case(summary="checkpoint " * 700)
    result = await compact_context(request)
    assert len(provider.calls) == 1
    assert result.removed_count == 0
    assert result.kept_entries == request.entries
    assert result.summary == ""
    assert result.skip_reason == (
        "suffix_summary_failed" if layout == "suffix" else "summary_failed"
    )


async def test_broken_final_projection_never_falls_back_to_estimates(monkeypatch):
    provider, request = character_bounded_compaction_case()
    monkeypatch.setattr(provider, "project_final_request", lambda *args, **kwargs: None)
    result = await compact_context(request)
    assert result.removed_count == 0
    assert result.kept_entries == request.entries
    assert provider.calls == []


@pytest.mark.parametrize("finish_reason,content", [
    ("length", "unfinished summary"), ("stop", ""), ("tool_calls", "not a summary"),
    ("stop", "over budget " * 2000),
])
async def test_legacy_http_rejects_incomplete_output(monkeypatch, finish_reason, content):
    from unittest.mock import AsyncMock

    from opensquilla.session.compaction import call_compaction_llm

    class Response:
        text = "synthetic response"

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{
                "finish_reason": finish_reason, "message": {"content": content},
            }]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient", lambda **kwargs: Client(),
    )
    monkeypatch.setattr(
        "opensquilla.engine.usage_http.reserve_direct_usage_call", AsyncMock(),
    )
    result = await call_compaction_llm("synthetic history", "", "synthetic", "unused-synthetic")
    assert result is None
