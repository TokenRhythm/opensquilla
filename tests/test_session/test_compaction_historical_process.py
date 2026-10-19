"""Historical process observations must not become permanent sampling vetoes."""

import json
from copy import deepcopy

import pytest

from opensquilla import token_estimation
from opensquilla.engine.history import reconstruct_messages_from_entry
from opensquilla.execution_status import execution_status_for_tool_result
from opensquilla.provider.types import ChatConfig, Message, ToolDefinition
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionRequestContext,
    _build_suffix_compaction_call,
    _compaction_source_size,
    _semantic_protected_tail_start,
    compact_context,
)
from opensquilla.session.compaction_budget import resolve_compaction_budget
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from opensquilla.session.context_view import format_compaction_summary_context
from tests.test_session.test_compaction_bounded_input import CapturingProvider


@pytest.fixture(params=["default-tokenizer", "fallback"], autouse=True)
def tokenizer_mode(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)


def _process_round(call_id, action, status):
    session = {
        "session_id": "build-job", "execution_id": "build-job",
        "status": status, "returncode": None if status == "running" else 0,
        "timed_out": False, "killed": False,
    }
    payload = {
        "status": "ok", "action": action, "execution_id": "build-job",
        "session": session, "output": "",
    }
    if action == "wait":
        payload.update(wait_mode="one", sessions=[session], exited=status != "running")
    result = json.dumps(payload)
    return [
        {"role": "user", "content": f"{action} the managed build."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"type": "tool_use", "id": call_id, "name": "process",
             "input": {"action": action, "session_id": "build-job"}},
            {"type": "tool_result", "tool_use_id": call_id, "result": result,
             "execution_status": execution_status_for_tool_result("process", result),
             "is_error": False},
            {"type": "text", "text": f"The managed build is {status}."},
        ]},
    ]


def _history():
    entries = _process_round("old-poll", "poll", "running")
    entries += _process_round("later-wait", "wait", "done")
    # Keep each round below 32K's planning guard in both tokenizer modes,
    # while the full real source remains larger than the 200K model window.
    for index in range(48):
        entries.extend([
            {"role": "user", "content": f"Earlier request {index}. "
             + "Background facts were collected and verified carefully. " * 600},
            {"role": "assistant", "content": f"Batch {index} completed."},
        ])
    return entries


def _configuration(window):
    provider = CapturingProvider()
    generation = 128_000 if window == 200_000 else 2048
    tools = [ToolDefinition(
        name="process", description="Inspect or wait for a managed shell job.",
        input_schema={"type": "object", "properties": {
            "action": {"type": "string"}, "session_id": {"type": "string"},
        }, "required": ["action"]},
    )]
    config = ChatConfig(
        system="Continue the task using the available tools.", max_tokens=generation,
        provider_context_window_tokens=window, provider_request_max_chars_explicit_cap=0,
    )
    target = CompactionExecutionTarget(
        provider=provider, provider_id="openai", model="synthetic-bounded-summary",
        context_window_tokens=window, max_output_tokens=generation,
        max_generation_tokens=generation, provider_request_max_chars_explicit_cap=0,
    )

    def project(summary, kept):
        messages = []
        if summary:
            messages.extend([
                Message(role="user", content=format_compaction_summary_context([summary])),
                Message(role="assistant", content="Continue using the recorded context."),
            ])
        for entry in kept:
            messages.extend(reconstruct_messages_from_entry(
                entry["role"], entry.get("content", ""), entry.get("tool_calls"),
            ))
        return provider.project_final_request(messages, tools, config)

    budget = resolve_compaction_budget(
        project=project, physical_context_window_tokens=window,
        generation_reserve_tokens=generation, summary_output_tokens=generation,
    )
    return provider, project, CompactionConfig(
        llm_plan=CompactionExecutionPlan(candidates=(target,)),
        request_context=CompactionRequestContext(chat_config=config, tools=tuple(tools)),
        budget=budget,
    )


async def _compact(entries, cfg):
    return await compact_context(CompactionRequest(
        session_id="historical-process", entries=entries, config=cfg,
        context_window_tokens=cfg.budget.history_capacity_tokens,
        context_window_chars=cfg.budget.history_capacity_chars,
        consumer_admission=cfg.budget.consumer_admission,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [32_768, 200_000])
async def test_old_running_then_completed_process_uses_bounded_summary(window):
    provider, project, cfg = _configuration(window)
    entries = _history() + [{"role": "user", "content": "Continue the latest task."}]
    original = deepcopy(entries)
    assert entries[1]["tool_calls"][1]["execution_status"]["reason"] == "background_running"
    assert entries[3]["tool_calls"][1]["execution_status"]["status"] == "success"
    assert _compaction_source_size(entries)[0] > 200_000
    assert _compaction_source_size(entries[-3:-1])[0] < cfg.budget.auto_trigger_tokens
    assert project("Tiny checkpoint.", entries[-3:]).fits

    result = await _compact(entries, cfg)

    assert result.summary and result.skip_reason is None
    assert len(provider.calls) == cfg.llm_calls_started == 1
    messages, _, _, proof = provider.calls[0]
    assert proof.fits and "<historical-excerpts>" in messages[0].content
    assert result.removed_count > 4 and result.kept_entries == entries[result.removed_count:]
    assert entries == original and result.kept_entries[-1] == entries[-1]
    raw_tokens, _ = _compaction_source_size(result.kept_entries)
    previous_round_tokens, _ = _compaction_source_size(
        entries[result.removed_count - 2:result.removed_count],
    )
    assert window * 16 // 100 <= raw_tokens < window * 16 // 100 + previous_round_tokens
    assert project(result.summary, result.kept_entries).fits


@pytest.mark.asyncio
async def test_latest_running_round_remains_raw_after_historical_sampling():
    provider, project, cfg = _configuration(32_768)
    live_round = _process_round("current-poll", "poll", "running")
    entries = _history() + live_round
    original = deepcopy(entries)
    assert _semantic_protected_tail_start(entries) == len(entries) - len(live_round)

    result = await _compact(entries, cfg)

    assert result.summary and len(provider.calls) == 1
    assert result.kept_entries[-len(live_round):] == live_round
    assert result.kept_entries == entries[result.removed_count:]
    assert entries == original and project(result.summary, result.kept_entries).fits


@pytest.mark.parametrize("live_kind", ["running_with_text", "missing_result"])
def test_direct_builder_does_not_sample_latest_incomplete_round(live_kind):
    provider, _, cfg = _configuration(32_768)
    source = _process_round("current-poll", "poll", "running")
    source[0]["content"] += "Protected current input. " * 50_000
    if live_kind == "missing_result":
        source[1]["tool_calls"] = [source[1]["tool_calls"][0]]
    original = deepcopy(source)
    messages, tools, config = _build_suffix_compaction_call(
        cfg.request_context, source, "Previous checkpoint.", "", None,
        provider=provider, deployment=cfg.llm_plan.primary,
        context_window_tokens=32_768, summary_output_tokens=2048,
        timeout=30, provider_request_correlation=None,
    )

    assert not provider.project_final_request(messages, tools, config).fits
    assert not any("<historical-excerpts>" in message.content
                   for message in messages if isinstance(message.content, str))
    assert source == original


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_kind", ["running", "user"])
async def test_oversized_protected_round_cannot_be_hidden_by_successful_sampling(tail_kind):
    provider, project, cfg = _configuration(32_768)
    live_round = _process_round("current-poll", "poll", "running")
    live_round[0]["content"] += "Protected current input. " * 50_000
    if tail_kind == "user":
        live_round = live_round[:1]
    entries = _history() + live_round
    original = deepcopy(entries)
    assert not project("Tiny checkpoint.", live_round).fits

    result = await _compact(entries, cfg)

    assert not result.summary and result.skip_reason is not None
    assert result.removed_count == 0 and result.kept_entries == entries == original
    assert result.failure_kind == "summary_body_capacity_exhausted"
    assert not provider.calls  # No summary can make this protected tail fit.
