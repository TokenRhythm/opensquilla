"""Local summary admission reports its actual cause without replacing API errors."""

from dataclasses import replace

import pytest

from opensquilla.session import compaction
from opensquilla.session.compaction import CompactionConfig, CompactionRequest
from opensquilla.session.compaction_deployment import CompactionExecutionPlan
from tests.test_session.test_compaction_previous_projection import deployment, raw_source
from tests.test_session.test_compaction_unverified_result_projection import source_result


def test_fit_failure_is_local_and_cleared_by_success():
    _provider, target, context = deployment(32_768)
    limited = replace(
        target, provider_request_max_chars=8192, provider_request_max_chars_explicit_cap=8192
    )
    request = CompactionRequest(
        session_id="local-fit-detail",
        entries=[],
        context_window_tokens=32_768,
        config=CompactionConfig(request_context=context),
    )
    # A failed optional shorten must retain its API cause. Exploratory fits do
    # not change operation state or authorize a different recovery category.
    request.config.last_failure_kind = "authentication"
    detail = {}
    assert (
        compaction._fit_compaction_input_to_target(
            request=request,
            target=limited,
            previous_summary="",
            chunk=source_result(status="running"),
            failure_detail=detail,
        )
        is None
    )
    assert detail == {
        "error_type": "_CompactionProviderError",
        "reason_code": "request_exceeds_provider_limits",
    }
    assert request.config.last_failure_kind == "authentication"
    assert (
        compaction._fit_compaction_input_to_target(
            request=request,
            target=target,
            previous_summary="",
            chunk=raw_source(),
            failure_detail=detail,
        )
        is not None
    )
    assert detail == {}
    assert request.config.last_failure_kind == "authentication"


@pytest.mark.asyncio
async def test_terminal_reports_failed_input_proof_without_a_provider_call(monkeypatch):
    _provider, target, context = deployment(32_768)
    target = replace(
        target, provider_request_max_chars=8192, provider_request_max_chars_explicit_cap=8192
    )
    source = source_result(status="running")
    tail = [{"role": "user", "content": "Continue Beta."}]
    entries = source + tail
    events = []
    monkeypatch.setattr(
        compaction.log, "info", lambda event, **fields: events.append((event, fields))
    )
    result = await compaction.compact_context(
        CompactionRequest(
            session_id="local-proof-failure",
            entries=entries,
            context_window_tokens=32_768,
            forced_prefix_cut=2,
            force=True,
            config=CompactionConfig(
                request_context=context,
                llm_plan=CompactionExecutionPlan(candidates=(target,)),
                protected_recent_messages=1,
            ),
        )
    )
    assert result.skip_reason == "summary_failed"
    assert result.failure_kind == "request_exceeds_provider_limits"
    assert result.failure_detail["reason_code"] == "request_exceeds_provider_limits"
    assert result.removed_count == 0 and result.kept_entries == entries
    terminal = next(fields for event, fields in events if event == "compaction.operation_terminal")
    assert terminal["failure_kind"] == "request_exceeds_provider_limits"
    assert terminal["summary_call_count"] == 0
    assert "content" not in terminal
