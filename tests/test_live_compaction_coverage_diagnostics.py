"""A diagnostic coverage miss cannot hide source or task-quality evidence."""

from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.session import compaction_state
from scripts import live_reasoning_replay_e2e as harness


@pytest.mark.asyncio
@pytest.mark.parametrize("missing,window_matches", [
    (None, True), (None, False), ("summary", True), ("answer", True),
    ("duplicate_wrapper", True),
])
async def test_reported_coverage_miss_continues_to_real_quality_checks(
    tmp_path, monkeypatch, missing, window_matches,
):
    facts = harness.compaction_task_facts("coding")
    label = "qzvxjprt"
    requests = []
    summary_facts = dict(facts)
    answer_facts = dict(facts)
    if missing == "summary":
        summary_facts["owner"] = "unknown-owner"
    elif missing == "answer":
        answer_facts["owner"] = "unknown-owner"

    # This models a diagnostic string miss, not a writer/admission override.
    monkeypatch.setattr(
        compaction_state,
        "verify_summary_coverage",
        lambda *_args, **_kwargs: compaction_state.CoverageResult(
            status="fail_reported", checked_obligations=1, covered_obligations=0,
            missing_obligations=["synthetic diagnostic-only phrase"],
        ),
    )
    monkeypatch.setattr(harness._Catalog, "resolve_context_window", lambda *a, **k: 32768)
    # The HTTP deployment is synthetic too; do not claim a live catalog/model check.
    monkeypatch.setattr(harness, "deployment_window_evidence", lambda *a, **k: {
        "physical_window_known": True, "physical_context_window_tokens": 32768,
        "configured_window_matches_physical": window_matches,
    })
    if missing == "duplicate_wrapper":
        from opensquilla.engine import runtime

        formatter = runtime._format_compaction_summary_context
        monkeypatch.setattr(runtime, "_format_compaction_summary_context", lambda texts: (
            "\n\n".join([formatted, formatted]) if (formatted := formatter(texts)) else None
        ))

    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        is_summary = harness._is_compaction_wire_call(harness.WireCall(request=payload))
        content = (
            f"COMPACTION_LABEL={label}" if len(requests) == 1
            else f"Retain COMPACTION_LABEL={label}. " + json.dumps(summary_facts)
            if is_summary else f"{label} COMPACTION_RECALL_OK\n" + json.dumps(answer_facts)
        )
        frame = {"choices": [{"index": 0, "delta": {"content": content},
                              "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode(),
        )

    observer = harness.WireObserver(
        harness.registry_endpoint("openrouter"), httpx.MockTransport(respond), max_calls=8,
    )
    operation = harness._run_compaction_case(
        tmp_path, provider="openrouter", model="qwen/qwen-2.5-72b-instruct",
        api_key="synthetic-offline-key", observer=observer, thinking="off", variant="basic",
        options=harness.CompactionCaseOptions(
            context_window_tokens=32768, max_output_tokens=4096,
            native_pressure=True, task_profile="coding",
        ),
    )
    if missing is None:
        result = await operation
        # Coverage diagnostics do not bypass source/quality checks. Native
        # pressure additionally requires a verified physical deployment window.
        assert result["pressure_checks"][0]["before_parent"]["estimated_pressure_ratio"] >= 0.85
        assert result["status"] == (
            "passed" if window_matches else "compaction_variant_not_covered"
        )
        assert result["ok"] is window_matches
        assert {
            key for key, value in result["coverage"]["observed"].items() if not value
        } == (set() if window_matches else {"native_pressure"})
        assert result["coverage"]["observed"]["archive_integrity"]
        assert result["coverage"]["observed"]["source_range"]
    else:
        failure = (
            "persisted_summary_replayed_more_than_once" if missing == "duplicate_wrapper"
            else f"{missing}_critical_fact_missing"
        )
        with pytest.raises(harness.ReplayCheckError, match=f"^{failure}$"):
            await operation
    assert observer.replay_checks["summary_coverage_diagnostics"] == [
        {"turn": 1, "status": "fail_reported"},
    ]
    assert not observer.engine_error_codes
    assert observer.replay_checks["summary_replay_checks"] == [
        {"turn": 1, "exact_text_count": 1 if missing == "duplicate_wrapper" else 0,
         "formatted_wrapper_count": 2 if missing == "duplicate_wrapper" else 1},
    ]
    checks = observer.replay_checks["critical_fact_checks"]
    if missing == "duplicate_wrapper":
        assert checks == []
        assert any(
            "[Summary 1]" in str(message) and str(message).count("[Summary 1]") == 2
            for message in requests[-1]["messages"]
        )
        return
    assert len(checks) == 1
    assert checks[0]["summary"]["owner"] is (missing != "summary")
    assert checks[0]["answer"]["owner"] is (missing != "answer")
    assert any(harness._is_compaction_wire_call(call) for call in observer.calls)
    assert observer.calls[-1].completed and observer.calls[-1].finish_reason == "stop"
