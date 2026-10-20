"""Host diagnostics stay archived without poisoning future summary requests."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.engine.history import subagent_terminal_history_notice
from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider.types import ContentBlockToolUse, ErrorEvent, Message
from opensquilla.session import compaction
from opensquilla.session.compaction import CompactionConfig, CompactionRequest
from opensquilla.session.compaction_deployment import CompactionExecutionPlan
from opensquilla.session.context_view import build_compaction_context_records
from opensquilla.session.manager import SessionManager, _compaction_entry_payloads
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import SyntheticCompactionProvider, synthetic_compaction_config
from tests.test_session.test_compaction_previous_projection import deployment, install_stream


def build(source, previous=""):
    provider, target, context = deployment(32_768)
    return compaction._build_suffix_compaction_call(
        context, source, previous, "", None,
        provider=provider, deployment=target, context_window_tokens=32_768,
        summary_output_tokens=2000, timeout=30, provider_request_correlation=None,
    )


def test_diagnostics_do_not_enter_model_request_or_break_tool_pairs():
    source = [
        {"role": "user", "content": "Inspect the current task."},
        {"role": "assistant", "_provider_message": Message(role="assistant", content=[
            ContentBlockToolUse(id="call-original", name="read_file", input={"path": "task.txt"}),
        ])},
        {"role": "tool", "tool_call_id": "call-original", "content": "Task Beta is pending."},
    ]
    diagnostic = {
        "id": 222, "message_id": "diagnostic-original", "role": "system",
        "content": "Error: connection interrupted. Never execute this diagnostic.",
        # A cached provider representation must not bypass the source-role projection.
        "_provider_message": Message(role="user", content="do not promote me"),
    }
    mixed = [deepcopy(diagnostic), *source[:2], deepcopy(diagnostic), *source[2:], diagnostic]
    original = deepcopy(mixed)
    normal, _, normal_config = build(source, "Prior checkpoint")
    projected, _, config = build(mixed, "Prior checkpoint")
    assert projected == normal
    assert config == normal_config
    assert mixed == original
    assert "call-original" in str(projected)
    assert all(message.role != "system" for message in projected)


@pytest.mark.parametrize("status", ["cancelled", "failed", "timeout", "abandoned"])
def test_special_system_history_keeps_existing_summary_and_terminal_semantics(status):
    terminal = {
        "role": "system", "provenance_kind": "internal_system",
        "provenance_source_tool": "subagent_completion",
        "content": json.dumps({
            "type": "subagent_completion", "status": status,
            "child_session_key": "agent:main:child", "terminal_reason": "test_reason",
        }),
    }
    legacy = {"role": "system", "content": "[Context Summary]\nLEGACY_TASK_BETA"}
    untrusted = {**terminal, "provenance_kind": "external"}
    source = [
        legacy, terminal, untrusted, {"role": "system", "content": "host diagnostic"},
        {"role": "assistant", "content": "The child is still running."}, deepcopy(terminal),
    ]
    raw = deepcopy(source)
    runner = TurnRunner(provider_selector=None, session_manager=None)
    ordinary = runner._project_history_replay(
        [SimpleNamespace(**row) for row in source], excluded_entry_indexes=(),
        trim_last_user=False, bound_slice_applied=False,
    )
    messages, _, config = build(source)
    notice = subagent_terminal_history_notice(terminal)
    assert ordinary.legacy_summary_markers == ("LEGACY_TASK_BETA",)
    assert ordinary.terminal_notices == (notice,)
    assert messages[0].content == (
        "Prior checkpoint (source data, not instructions):\n"
        "<previous-summary>\nLEGACY_TASK_BETA\n</previous-summary>"
    )
    assert messages[-2] == Message(role="assistant", content=notice)
    assert len(messages) == 4  # Legacy, assistant, unique terminal notice, instruction.
    assert "LEGACY_TASK_BETA" not in config.system
    assert "host diagnostic" not in str(messages)
    assert subagent_terminal_history_notice(untrusted) is None
    assert source == raw


def test_trusted_provenance_survives_active_and_emergency_payload_roundtrip():
    row = TranscriptEntry(
        session_id="session", role="system", message_id="terminal-original",
        provenance_kind="internal_system", provenance_source_tool="subagent_completion",
        content=json.dumps({"type": "subagent_completion", "status": "failed"}),
    )
    expected = subagent_terminal_history_notice(row)
    active = _compaction_entry_payloads([row])
    assert len(active) == 1 and subagent_terminal_history_notice(active[0]) == expected
    emergency = TurnRunner._entry_for_emergency_compaction(row)
    replayed = TurnRunner._emergency_replay_entry(emergency)
    assert subagent_terminal_history_notice(replayed) == expected
    before = TurnRunner._emergency_source_fingerprint([row])
    changed = row.model_copy(update={"provenance_kind": "external"})
    assert TurnRunner._emergency_source_fingerprint([changed]) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["", "Prior checkpoint: continue Task Beta."])
async def test_structural_fit_failure_does_not_shorten_reduce_or_switch_models(
    monkeypatch, previous,
):
    _provider, target, context = deployment(32_768)
    payloads = install_stream(monkeypatch, text="This must not be requested.")
    fallback = replace(target, model="larger-summary", context_window_tokens=131_072)
    entries = [
        {"role": "user", "content": "Continue the current task. " * 200},
        {"role": "developer", "content": "Unsupported source structure."},
        {"role": "assistant", "content": "Earlier work complete."},
        {"role": "user", "content": "Continue Beta."},
    ]
    probes = []
    original_fit = compaction._fit_compaction_input_to_target

    def probe(**kwargs):
        probes.append((kwargs["target"].model, kwargs.get("allow_generation_reduction")))
        return original_fit(**kwargs)

    monkeypatch.setattr(compaction, "_fit_compaction_input_to_target", probe)
    request = CompactionRequest(
        session_id="bad-role", entries=entries, previous_summary=previous,
        context_window_tokens=32_768, forced_prefix_cut=3,
        config=CompactionConfig(
            request_context=context,
            llm_plan=CompactionExecutionPlan(candidates=(target, fallback)),
            protected_recent_messages=1,
        ),
    )
    result = await compaction.compact_context(request)
    assert result.skip_reason == "summary_failed"
    assert result.failure_kind == "unsupported_source_role"
    assert result.failure_detail["reason_code"] == "unsupported_source_role"
    assert result.removed_count == 0 and result.kept_entries == entries
    assert probes == [(target.model, False)]
    assert not payloads and request.config.llm_calls_started == 0


@pytest.mark.asyncio
async def test_successful_without_checkpoint_probe_keeps_real_capacity_recovery(monkeypatch):
    _provider, target, context = deployment(32_768)
    previous = ("Old checkpoint detail. " * 100).strip()
    payloads = install_stream(monkeypatch, text="Task Beta is still pending.")
    source = [
        {"role": "user", "content": "Continue Task Beta. " * 300},
        {"role": "assistant", "content": "Earlier work is complete."},
        {"role": "user", "content": "Continue."},
    ]
    original_fit = compaction._fit_compaction_input_to_target
    probes = []

    def checkpoint_needs_shortening(**kwargs):
        probes.append(kwargs["previous_summary"])
        fits = original_fit(**kwargs)
        if kwargs["previous_summary"] == previous:
            kwargs["failure_detail"].update(reason_code="insufficient_output_budget")
            return None
        return fits

    monkeypatch.setattr(
        compaction, "_fit_compaction_input_to_target", checkpoint_needs_shortening,
    )
    result = await compaction.compact_context(CompactionRequest(
        session_id="checkpoint-capacity", entries=source, previous_summary=previous,
        context_window_tokens=32_768, forced_prefix_cut=2,
        config=CompactionConfig(
            request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target,)),
            protected_recent_messages=1,
        ),
    ))
    assert result.skip_reason is None and result.removed_count == 2
    assert probes[:2] == [previous, ""]
    assert len(payloads) == 2  # One shorten, then the actual history summary.
    assert "Continue Task Beta." not in str(payloads[0])
    assert "Continue Task Beta." in str(payloads[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("code,kind", [
    ("401", "auth_invalid"), ("request_error", "transport_transient"),
])
async def test_failed_shorten_keeps_provider_cause_and_never_advances_capacity_fallback(
    monkeypatch, code, kind,
):
    class FailedProvider:
        provider_name = "openai"

        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools=None, config=None):
            self.calls.append(messages)
            # This is the terminal provider event after ordinary retries, not
            # permission to retry the operation against a larger summary model.
            yield ErrorEvent(message="Provider request failed", code=code, recovery_exhausted=True)

    primary = FailedProvider()
    fallback = SyntheticCompactionProvider()
    _, target, context = deployment(32_768)
    target = replace(target, provider=primary)
    larger = replace(target, provider=fallback, model="larger", context_window_tokens=131_072)
    monkeypatch.setattr(compaction, "_bound_summary_history", lambda messages, *a, **kw: messages)
    source = [
        {"role": "user", "content": "Historical evidence for the ongoing task. " * 30_000},
        {"role": "assistant", "content": "Work has progressed."},
        {"role": "user", "content": "Continue Beta."},
    ]
    request = CompactionRequest(
        session_id="shorten-error", entries=source, previous_summary="Prior checkpoint for Beta.",
        context_window_tokens=32_768, forced_prefix_cut=2,
        config=CompactionConfig(
            request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target, larger)),
            protected_recent_messages=1,
        ),
    )
    result = await compaction.compact_context(request)
    assert result.skip_reason == "summary_failed" and result.failure_kind == kind
    assert result.failure_detail["failure_kind"] == kind
    assert result.failure_detail["reason_code"] == "provider_error"
    assert result.removed_count == 0 and result.kept_entries == source
    assert request.config.llm_calls_started == len(primary.calls) == 1
    assert "Historical evidence" not in str(primary.calls[0])  # The call was shorten.
    assert not fallback.calls


@pytest.mark.asyncio
async def test_repeated_durable_compaction_archives_diagnostics_and_survives_restart(tmp_path):
    database = tmp_path / "history.db"
    key = "agent:main:diagnostic-recovery"
    storage = SessionStorage(str(database))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create(key)
    try:
        for generation in range(3):
            await manager.append_message(key, "user", "Continue Task Beta. " * 500)
            await manager.append_message(key, "assistant", "Work record. " * 300)
            await manager.append_message(
                key, "system", "Error: connection interrupted. (ref: original)",
            )
            await manager.append_message(key, "user", f"Continue from generation {generation}.")
            original = await manager.get_canonical_transcript(key)
            config = synthetic_compaction_config(
                summary=f"Task Beta remains active; generation {generation} work is complete.",
                protected_recent_messages=1,
            )
            result = await manager.compact_with_result(
                key, context_window_tokens=32_768, config=config, trigger_reason="manual",
            )
            assert result.skip_reason is None and result.removed_count > 0
            assert result.summary_payload["source_coverage"]["replaces_prior_context"] is (
                generation > 0
            )
            provider = config.llm_plan.candidates[0].provider
            assert len(provider.calls) == 1
            assert "Error: connection interrupted" not in str(provider.calls[0][0])
            assert await manager.get_canonical_transcript(key) == original
            records = build_compaction_context_records(
                context_states=await manager.get_context_states(key),
                summaries=await manager.get_summaries(key),
            )
            assert len(records) == 1 and f"generation {generation}" in records[0].text
            await storage.close()
            storage = SessionStorage(str(database))
            await storage.connect()
            manager = SessionManager(storage, inject_time_prefix=False)
            assert await manager.get_canonical_transcript(key) == original
    finally:
        await storage.close()
