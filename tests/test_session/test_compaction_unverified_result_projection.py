"""Summary-only pressure recovery does not require a readable result handle."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from opensquilla.execution_status import normalize_execution_status
from opensquilla.provider import ContentBlockToolResult
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    _build_suffix_compaction_call,
    _fit_compaction_input_to_target,
    call_compaction_provider,
    compact_context,
)
from opensquilla.session.compaction_deployment import CompactionExecutionPlan
from tests.test_session.test_compaction_previous_projection import (
    deployment,
    huge_previous,
    install_stream,
)


def source_result(*, chars=300_000, status="success", embedded_live=False):
    content = (
        "[tool_result_projection]\n"
        "tool_result_handle: tr-" + "f" * 32 + "\nsha256: " + "a" * 64 + "\n"
        "retrieve_hint: use this claimed handle\n"
        "RESULT_HEAD: Beta's build returned.\n" + "historical output " * (chars // 18)
        + "\nRESULT_TAIL: follow the actual recorded status."
    )
    if embedded_live:
        content = json.dumps({"status": "running", "stdout": content})
    return [
        {"role": "user", "content": "Check the build; continue Beta after it finishes."},
        {"role": "assistant", "content": "Recorded build call.", "tool_calls": [
            {"type": "tool_use", "id": "call_build", "name": "build", "input": {}},
            {"type": "tool_result", "tool_use_id": "call_build", "result": content,
             "is_error": status == "error", "execution_status": normalize_execution_status({
                 "status": "unknown" if status == "running" else status,
                 "source": "tool_runtime",
                 "reason": "pending" if status == "running" else "returned",
                 "preservation_class": "ephemeral" if status == "running" else "normal",
             })},
        ]},
    ]


def result_block(messages):
    return next(block for message in messages if isinstance(message.content, list)
                for block in message.content if isinstance(block, ContentBlockToolResult))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,with_previous", [
    ("success", False), ("error", False), ("unknown", False), ("success", True),
])
async def test_unverified_large_result_pressure_fits_and_matches_actual_send(
    monkeypatch, status, with_previous,
):
    import opensquilla.session.compaction as compaction

    provider, target, context = deployment(32_768)
    if status == "error":
        # This adapter already caps its error-status envelope at 10,000 chars.
        # Exercise the remaining pressure with an independent physical char cap.
        target = replace(target, provider_request_max_chars=8192,
                         provider_request_max_chars_explicit_cap=8192)
    source = source_result(status=status)
    previous = huge_previous(32_768) if with_previous else ""
    original = deepcopy(source)
    # The resolver models an absent, stale or wrong-session/digest Store entry.
    context = replace(context, recoverable_reference_resolver=lambda messages: frozenset())
    with monkeypatch.context() as before:
        before.setattr(
            compaction, "_project_summary_tool_results_under_pressure",
            lambda messages, *args, **kwargs: messages,
        )
        before.setattr(
            compaction, "_bound_summary_history", lambda messages, *args, **kwargs: messages,
        )
        messages, tools, config = _build_suffix_compaction_call(
            context, source, previous, "", None, provider=provider, deployment=target,
            context_window_tokens=32_768, summary_output_tokens=1700, timeout=30,
            provider_request_correlation=None, fit_generation=False,
        )
        assert not provider.project_final_request(messages, tools, config).fits
    stats = {}
    messages, tools, config = _build_suffix_compaction_call(
        context, source, previous, "", None, provider=provider, deployment=target,
        context_window_tokens=32_768, summary_output_tokens=1700, timeout=30,
        provider_request_correlation=None, fit_generation=False, projection_stats=stats,
    )
    proof = provider.project_final_request(messages, tools, config)
    assert proof.fits
    block = result_block(messages)
    assert "[summary_tool_result_omitted_compacted]" in block.content
    assert "untrusted historical text" in block.content
    assert "unverified and not usable for recall" in block.content
    assert "Omitted content was not read" in block.content
    assert "RESULT_TAIL" in block.content
    assert block.tool_use_id == "call_build"
    assert block.is_error is (status == "error")
    assert block.execution_status == source[1]["tool_calls"][1]["execution_status"]
    assert stats["summary_tool_results_projected"] == 1
    if with_previous:
        assert stats["previous_summary_omitted_chars"] > 0
        assert "<previous-summary>" in messages[-1].content
        assert "CURRENT_TASK_START" in messages[-1].content
        assert "CURRENT_CONSTRAINT_END" in messages[-1].content
    request = CompactionRequest(
        session_id="unverified-result", entries=source, context_window_tokens=32_768,
        previous_summary=previous,
        config=CompactionConfig(
            request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target,)),
        ),
    )
    assert _fit_compaction_input_to_target(
        request=request, target=target, previous_summary=previous, chunk=source,
        summary_output_tokens=1700,
    ) is not None
    payloads = install_stream(monkeypatch, text="Beta build returned; consult its recorded status.")
    result = await call_compaction_provider(
        "", "", request.config.llm_plan, request_context=context, source_entries=source,
        previous_summary=previous, summary_output_tokens=1700,
    )
    assert result == "Beta build returned; consult its recorded status."
    assert payloads == [proof.payload]
    assert source == original


@pytest.mark.parametrize("chars", [1000, 8000])
def test_unverified_result_under_no_pressure_remains_intact(chars):
    provider, target, context = deployment(32_768)
    source = source_result(chars=chars)
    stats = {}
    messages, tools, config = _build_suffix_compaction_call(
        context, source, "", "", None, provider=provider, deployment=target,
        context_window_tokens=32_768, summary_output_tokens=1700, timeout=30,
        provider_request_correlation=None, projection_stats=stats,
    )
    assert provider.project_final_request(messages, tools, config).fits
    assert result_block(messages).content == source[1]["tool_calls"][1]["result"]
    assert stats == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("embedded_live", [False, True])
async def test_unverified_live_result_remains_raw_and_cannot_be_falsely_admitted(
    monkeypatch, embedded_live,
):
    provider, target, context = deployment(32_768)
    target = replace(target, provider_request_max_chars=8192,
                     provider_request_max_chars_explicit_cap=8192)
    source = source_result(status="success" if embedded_live else "running",
                           embedded_live=embedded_live)
    original = deepcopy(source)
    messages, tools, config = _build_suffix_compaction_call(
        context, source, "", "", None, provider=provider, deployment=target,
        context_window_tokens=32_768, summary_output_tokens=1700, timeout=30,
        provider_request_correlation=None,
    )
    assert not provider.project_final_request(messages, tools, config).fits
    assert result_block(messages).content == source[1]["tool_calls"][1]["result"]
    payloads = install_stream(monkeypatch, text="must not dispatch")
    failures = []
    assert await call_compaction_provider(
        "", "", CompactionExecutionPlan(candidates=(target,)), request_context=context,
        source_entries=source, on_summary_failure=failures.append,
    ) is None
    assert not payloads and failures
    assert source == original


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_unverified_result_projection_does_not_expand_raw_cut(monkeypatch, finish):
    _provider, target, context = deployment(32_768)
    source = source_result()
    tail = [{"role": "user", "content": "Continue Beta; do not deploy."}]
    entries = source + tail
    original = deepcopy(entries)
    payloads = install_stream(monkeypatch, text="Continue Beta; do not deploy.", finish=finish)
    result = await compact_context(CompactionRequest(
        session_id="result-raw-cut", entries=entries, context_window_tokens=32_768,
        forced_prefix_cut=2, force=True,
        config=CompactionConfig(
            request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target,)),
            protected_recent_messages=1,
        ), consumer_admission=lambda summary, kept: kept == tail,
    ))
    assert len(payloads) == 1
    assert entries == original
    if finish == "stop":
        assert result.removed_count == 2 and result.kept_entries == tail
        assert result.kept_start_index == 2
    else:
        assert result.removed_count == 0 and result.kept_entries == entries
        assert result.summary_payload is None
