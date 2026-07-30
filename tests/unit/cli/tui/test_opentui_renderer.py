from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.cli.chat.turn import UsageSummary
from opensquilla.cli.tui.backend.render_summary import (
    summarize_args,
    summarize_result,
    tool_args_detail,
    tool_result_detail,
)
from opensquilla.cli.tui.opentui.renderer import (
    OpenTuiStreamRenderer,
    _format_tokens,
)
from opensquilla.engine.usage import SessionTotalsSnapshot


def test_web_search_args_render_query_summary() -> None:
    assert summarize_args("web_search", {"query": "OpenSquilla canonical search"}) == (
        "OpenSquilla canonical search"
    )
    assert summarize_args("web_discover", {"query": "OpenSquilla discover links"}) == (
        "OpenSquilla discover links"
    )


class _RecordingHandle:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, message_type: str, payload: dict) -> None:
        self.sent.append((message_type, payload))


class _ToolbarRecordingHandle(_RecordingHandle):
    def __init__(self) -> None:
        super().__init__()
        self.toolbar: dict[str, object] = {}
        self.toolbar_updates: list[tuple[str, object | None]] = []
        self.invalidated = 0

    def set_toolbar(self, key: str, value: object | None) -> None:
        self.toolbar_updates.append((key, value))
        if value is None:
            self.toolbar.pop(key, None)
            return
        self.toolbar[key] = value

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.mark.asyncio
async def test_active_turn_keeps_composer_enabled_for_commands_queue_and_steering() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aturn_started()

    composer_sets = [payload for kind, payload in handle.sent if kind == "composer.set"]
    assert not any(payload.get("disabled") is True for payload in composer_sets)
    assert any(kind == "turn.begin" for kind, _payload in handle.sent)


@pytest.mark.asyncio
async def test_intermediate_text_is_thinking_final_text_is_answer_card() -> None:
    """Intermediate narration before a tool (presentation="intermediate") opens
    a purple thinking block; the final answer (presentation="answer") opens a
    cyan answer card. Each is the right kind from its first delta — no retype."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("Let me check", presentation="intermediate")
    await r.atool_start("web_search", {"query": "x"}, "c1")
    await r.atool_finished("c1", success=True, result="result line")
    await r.aappend_text("Final answer", presentation="answer")
    await r.afinalize(None)

    assert not [t for t, _ in handle.sent if t == "block.retype"]
    begins = [
        (p["kind"], p["id"])
        for t, p in handle.sent
        if t == "block.begin" and p.get("kind") in {"thinking", "answer"}
    ]
    # intermediate -> thinking block, final -> answer card, in that order
    assert [kind for kind, _id in begins] == ["thinking", "answer"]
    tool_begins = [p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "tool"]
    assert tool_begins and tool_begins[0]["meta"]["name"] == "web_search"


@pytest.mark.asyncio
async def test_final_answer_is_a_card_from_the_first_delta() -> None:
    """A pure-answer turn opens an answer card on the very first delta and
    streams into it — never a thinking block, never a retype."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("The ", presentation="answer")
    await r.aappend_text("answer.", presentation="answer")
    await r.afinalize(None)

    assert not [t for t, _ in handle.sent if t == "block.retype"]
    answer_begins = [p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "answer"]
    assert len(answer_begins) == 1
    assert not [
        p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "thinking"
    ]


@pytest.mark.asyncio
async def test_conflicting_final_snapshot_clears_text_blocks_but_preserves_tool_blocks() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("stale narration", presentation="intermediate")
    await r.atool_start("lookup", {"query": "x"}, "tool-1")
    await r.atool_finished("tool-1", success=True, result="found")
    await r.aappend_text("stale answer", presentation="answer")

    stale_text_ids = {
        p["id"]
        for t, p in handle.sent
        if t == "block.begin" and p.get("kind") in {"answer", "thinking"}
    }
    await r.areconcile_final_text("canonical answer")

    cleared = {
        p["id"]
        for t, p in handle.sent
        if t == "block.update" and p.get("patch", {}).get("text") == ""
    }
    assert cleared == stale_text_ids
    assert "tool-1" not in cleared
    assert r.buffer == "canonical answer"
    assert any(
        t == "block.begin"
        and p.get("kind") == "status"
        and "superseded" in p.get("meta", {}).get("text", "")
        for t, p in handle.sent
    )
    canonical_appends = [
        p
        for t, p in handle.sent
        if t == "block.append" and p.get("delta") == "canonical answer"
    ]
    assert len(canonical_appends) == 1
    assert canonical_appends[0]["id"] not in stale_text_ids
    assert any(
        t == "block.update"
        and p.get("id") == "tool-1"
        and p.get("patch", {}).get("status") == "ok"
        for t, p in handle.sent
    )


@pytest.mark.asyncio
async def test_explicit_empty_snapshot_clears_preview_and_emits_visible_status() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("preview to withdraw")

    await r.areconcile_final_text("")

    assert r.buffer == ""
    assert any(
        t == "block.update" and p.get("patch", {}).get("text") == ""
        for t, p in handle.sent
    )
    assert any(
        t == "block.begin"
        and p.get("kind") == "status"
        and p.get("meta", {}).get("text")
        == "Streamed preview withdrawn; the final answer is empty."
        for t, p in handle.sent
    )


@pytest.mark.asyncio
async def test_strict_snapshot_extension_uses_normal_answer_append_without_correction() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("prefix")
    before = len(handle.sent)

    await r.areconcile_final_text("prefix suffix")

    assert r.buffer == "prefix suffix"
    new_messages = handle.sent[before:]
    assert any(t == "block.append" and p.get("delta") == " suffix" for t, p in new_messages)
    assert not any(t == "block.update" and "text" in p.get("patch", {}) for t, p in new_messages)
    assert not any(t == "block.begin" and p.get("kind") == "status" for t, p in new_messages)


@pytest.mark.asyncio
async def test_reasoning_streams_into_its_own_block_and_closes_before_text() -> None:
    """Reasoning (the model's extended-thinking process) streams live into a
    dedicated 'reasoning' block — the host shows a dim rolling peek and
    collapses it to 'Thought for Ns' on block.end — and the block closes
    before the answer text opens so the two never interleave."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_reasoning("let me think ")
    await r.aappend_reasoning("step by step about the internals")
    await r.aappend_text("the answer")
    await r.afinalize(None)

    # the reasoning block is its own kind, distinct from intermediate "thinking"
    # text and from the "answer" card
    reasoning_begins = [
        p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "reasoning"
    ]
    assert len(reasoning_begins) == 1
    reasoning_id = reasoning_begins[0]["id"]
    # every reasoning delta streams into that block (the host renders the peek)
    reasoning_appends = [
        p["delta"] for t, p in handle.sent if t == "block.append" and p["id"] == reasoning_id
    ]
    assert reasoning_appends == ["let me think ", "step by step about the internals"]
    # the reasoning block closes before the answer block opens
    events = [
        (t, p.get("id"), p.get("kind"))
        for t, p in handle.sent
        if t in ("block.begin", "block.end")
    ]
    end_reasoning = events.index(("block.end", reasoning_id, None))
    begin_answer = next(
        i for i, (t, _i, kind) in enumerate(events) if t == "block.begin" and kind == "answer"
    )
    assert end_reasoning < begin_answer
    assert not [t for t, _ in handle.sent if t == "block.retype"]
    # the reasoning marker is closed before the answer block opens
    ends = [p["id"] for t, p in handle.sent if t == "block.end"]
    assert reasoning_id in ends
    answer_begins = [
        p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "answer"
    ]
    assert len(answer_begins) == 1


@pytest.mark.asyncio
async def test_answer_only_turn_has_no_retype() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("Direct answer")
    await r.afinalize(None)
    assert not [t for t, _ in handle.sent if t == "block.retype"]
    answer_begins = [p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "answer"]
    assert len(answer_begins) == 1


@pytest.mark.asyncio
async def test_renderer_marks_tool_error_and_cancel() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.atool_start("grep", {"pattern": "x"}, "c2")
    await r.atool_finished("c2", success=False, error="boom")
    await r.aerror("turn-level failure")
    await r.afinalize(None, cancelled=True)
    updates = [p for t, p in handle.sent if t == "block.update"]
    assert any(p["patch"].get("status") == "error" for p in updates)
    error_begins = [p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "error"]
    assert error_begins and error_begins[0]["meta"]["text"] == "turn-level failure"
    end = [p for t, p in handle.sent if t == "turn.end"][0]
    assert end["cancelled"] is True
    # Errors retain their own semantic field rather than being flattened into
    # a result preview, so the host can disclose both when a provider sends both.
    tool_updates = [p for t, p in handle.sent if t == "block.update" and p["id"] == "c2"]
    assert tool_updates[-1]["patch"]["error"] == "boom"


@pytest.mark.asyncio
async def test_tool_protocol_retains_full_args_result_and_error_without_preview_clipping() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)
    long_tail = "tail-" + "x" * 400
    args = {
        "path": "/workspace/src/composer.py",
        "options": {"include": ["thinking", "tools", "results"]},
    }

    await renderer.atool_start("inspect", args, "tool-full")
    await renderer.atool_finished(
        "tool-full",
        success=False,
        result=f"line one\n  indented line\n{long_tail}",
        error="provider error\nsecond error line",
    )

    begin = next(
        payload
        for kind, payload in handle.sent
        if kind == "block.begin" and payload["id"] == "tool-full"
    )
    assert begin["meta"]["args_summary"] == "/workspace/src/composer.py"
    assert begin["meta"]["args_full"] == tool_args_detail(args)
    assert '"thinking"' in begin["meta"]["args_full"]

    append = next(
        payload
        for kind, payload in handle.sent
        if kind == "block.append" and payload["id"] == "tool-full"
    )
    assert append["delta"] == f"line one\n  indented line\n{long_tail}"
    assert long_tail in append["delta"]
    update = next(
        payload
        for kind, payload in handle.sent
        if kind == "block.update" and payload["id"] == "tool-full"
    )
    assert update["patch"]["error"] == "provider error\nsecond error line"


def test_full_tool_payload_helpers_strip_terminal_controls_without_truncating() -> None:
    long_value = "a" * 600
    assert tool_result_detail(f"\x1b[31m{long_value}\x1b[0m") == long_value
    assert long_value in tool_args_detail({"value": f"\x1b[31m{long_value}\x1b[0m"})
    safe_args = tool_args_detail({"value": "\x1b[31munsafe\x1b[0m"})
    assert "\x1b" not in safe_args
    assert "[31m" not in safe_args
    mixed = [{"type": "msg", "msg": "kept"}, {"type": "status", "value": "also kept"}]
    rendered = tool_result_detail(mixed)
    assert '"msg": "kept"' in rendered
    assert '"value": "also kept"' in rendered


@pytest.mark.asyncio
async def test_cancel_midtool_closes_open_tool_block() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.atool_start("grep", {"pattern": "x"}, "c9")
    # NO atool_finished — simulate cancellation
    await r.afinalize(None, cancelled=True)
    # the open tool must be force-closed: an error update + an end for its id
    updates = [p for t, p in handle.sent if t == "block.update" and p["id"] == "c9"]
    ends = [p for t, p in handle.sent if t == "block.end" and p["id"] == "c9"]
    assert updates and updates[-1]["patch"].get("status") == "error"
    assert ends, "cancelled in-flight tool block was never closed"


@pytest.mark.asyncio
async def test_aclose_without_finalize_tears_down_errored_turn() -> None:
    """Error paths end the turn without afinalize; the guaranteed aclose must
    still end the turn, idle transcript activity, and re-enable the composer
    so the UI never stays busy and the next turn never merges into the errored
    card."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("partial answer")
    await r.aerror("provider exploded")
    await r.aclose()

    types = [t for t, _ in handle.sent]
    assert "turn.end" in types
    statuses = [p for t, p in handle.sent if t == "turn.status"]
    assert statuses[-1]["phase"] == "idle"
    assert statuses[-1]["active"] is False
    composer_sets = [p for t, p in handle.sent if t == "composer.set"]
    assert composer_sets[-1] == {"disabled": False}
    # the open answer block was force-closed
    begins = {p["id"] for t, p in handle.sent if t == "block.begin" and p.get("kind") == "answer"}
    ends = {p["id"] for t, p in handle.sent if t == "block.end"}
    assert begins <= ends


@pytest.mark.asyncio
async def test_aclose_force_closes_inflight_tool_block() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.atool_start("grep", {"pattern": "x"}, "c7")
    # NO atool_finished, NO afinalize — e.g. a provider timeout mid-tool
    await r.aclose()

    updates = [p for t, p in handle.sent if t == "block.update" and p["id"] == "c7"]
    ends = [p for t, p in handle.sent if t == "block.end" and p["id"] == "c7"]
    assert updates and updates[-1]["patch"].get("status") == "error"
    assert ends
    assert [t for t, _ in handle.sent if t == "turn.end"]


@pytest.mark.asyncio
async def test_aclose_after_afinalize_emits_no_second_teardown() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(None)
    await r.aclose()

    assert len([t for t, _ in handle.sent if t == "turn.end"]) == 1


@pytest.mark.asyncio
async def test_aclose_before_any_output_is_a_noop() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aclose()
    assert handle.sent == []


@pytest.mark.asyncio
async def test_aclose_tolerates_dead_output_handle() -> None:
    class _DeadHandle:
        async def send_message(self, message_type: str, payload: dict) -> None:
            raise RuntimeError("OpenTUI bridge is not started")

    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("partial")
    # the bridge dies before teardown; aclose must not raise from its emits
    r.output_handle = _DeadHandle()
    await r.aclose()


@pytest.mark.asyncio
async def test_activity_phase_returns_to_output_when_text_resumes_after_tool() -> None:
    """In the narrate-then-act flow activity must not stay stuck on the
    finished tool while the final answer streams."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("Let me look", presentation="intermediate")
    await r.atool_start("grep", {"pattern": "x"}, "c1")
    await r.atool_finished("c1", success=True, result="hit")
    await r.aappend_text("Final answer", presentation="answer")
    await r.afinalize(None)

    phases = [p["phase"] for t, p in handle.sent if t == "turn.status"]
    assert phases == ["thinking", "output", "tool", "output", "idle"]


@pytest.mark.asyncio
async def test_astatus_updates_activity_and_renders_dim_status_line() -> None:
    """Status messages (artifact saved, task-group progress) must be visible:
    a transient activity label plus a dim in-card status line."""
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("working")
    await r.astatus("artifact written: report.md")
    await r.aappend_text(" more")
    await r.afinalize(None)

    status_blocks = [
        p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "status"
    ]
    assert status_blocks and status_blocks[0]["meta"]["text"] == "artifact written: report.md"
    assert status_blocks[0]["meta"]["style"] == "dim"
    ends = {p["id"] for t, p in handle.sent if t == "block.end"}
    assert status_blocks[0]["id"] in ends

    labels = [(p["phase"], p["label"]) for t, p in handle.sent if t == "turn.status"]
    assert ("output", "artifact written: report.md") in labels
    # The activity label is transient: the next text delta restores the phase label.
    status_index = labels.index(("output", "artifact written: report.md"))
    assert ("output", "output") in labels[status_index + 1 :]


@pytest.mark.asyncio
async def test_astatus_ignores_blank_messages() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.astatus("   ")
    assert not [p for t, p in handle.sent if t == "block.begin"]


@pytest.mark.asyncio
async def test_usage_block_emitted_before_turn_end() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(None)
    types = [t for t, _ in handle.sent]
    usage_begin = next(
        i
        for i, (t, p) in enumerate(handle.sent)
        if t == "block.begin" and p.get("kind") == "usage"
    )
    turn_end = types.index("turn.end")
    assert usage_begin < turn_end, "usage block must render in the active turn, before turn.end"
    # answer card still closes (its block.end) before usage
    answer_id = next(
        p["id"]
        for t, p in handle.sent
        if t == "block.begin" and p.get("kind") == "answer"
    )
    answer_end = next(
        i
        for i, (t, p) in enumerate(handle.sent)
        if t == "block.end" and p["id"] == answer_id
    )
    assert answer_end < usage_begin


@pytest.mark.asyncio
async def test_usage_receipt_includes_reported_reasoning_tokens() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)
    renderer.__enter__()
    await renderer.aappend_text("done")
    await renderer.afinalize(
        UsageSummary(
            input_tokens=36_060,
            output_tokens=1_047,
            reasoning_tokens=436,
            model="z-ai/glm-5.2",
        )
    )

    usage = next(
        payload
        for kind, payload in handle.sent
        if kind == "block.begin" and payload.get("kind") == "usage"
    )
    assert usage["meta"]["text"] == (
        "in 36,060 / out 1,047 / think 436 · z-ai/glm-5.2"
    )


@pytest.mark.asyncio
async def test_ensemble_progress_reuses_one_live_block_and_never_forwards_candidate_text() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aensemble_progress(
        {
            "event_type": "proposer_start",
            "proposer_index": 0,
            "sample_index": 0,
            "proposer_label": "fast",
            "proposer_provider": "openrouter",
            "proposer_model": "model-a",
            # Candidate bodies are private implementation data, even if a
            # future Gateway accidentally includes them on a progress frame.
            "content": "PRIVATE CANDIDATE BODY",
            "text": "PRIVATE FULL CANDIDATE",
        }
    )
    await renderer.aensemble_progress(
        {
            "event_type": "proposer_finish",
            "proposer_index": 0,
            "sample_index": 0,
            "proposer_label": "fast",
            "proposer_provider": "openrouter",
            "proposer_model": "model-a",
            "elapsed_ms": 1250,
            "input_tokens": 120,
            "output_tokens": 32,
            "cost_usd": 0.002,
        }
    )
    await renderer.afinalize(
        SimpleNamespace(
            input_tokens=120,
            output_tokens=32,
            reasoning_tokens=0,
            model="ensemble/model-a",
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "label": "fast",
                    "provider": "openrouter",
                    "model": "model-a",
                    "sample_index": 0,
                    "input_tokens": 120,
                    "output_tokens": 32,
                    "request_count": 1,
                }
            ],
            ensemble_trace={"total_candidates": 1, "successful_proposers": 1},
            session_totals=None,
        )
    )

    ensemble_begins = [
        payload
        for kind, payload in handle.sent
        if kind == "block.begin" and payload.get("kind") == "ensemble"
    ]
    assert len(ensemble_begins) == 1
    ensemble_id = ensemble_begins[0]["id"]
    updates = [
        payload["patch"]
        for kind, payload in handle.sent
        if kind == "block.update" and payload["id"] == ensemble_id
    ]
    assert updates[-1]["completed"] == 1
    assert updates[-1]["total"] == 1
    assert updates[-1]["members"][0]["status"] == "done"
    # Final usage has no duration field; enriching the member must not erase
    # the live lifecycle measurement.
    assert updates[-1]["members"][0]["elapsed_ms"] == 1250
    assert "PRIVATE CANDIDATE" not in repr(handle.sent)


@pytest.mark.asyncio
async def test_live_aggregator_progress_does_not_replace_first_proposer() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aensemble_progress(
        {
            "event_type": "proposer_start",
            "proposer_index": 0,
            "sample_index": 0,
            "proposer_label": "fast",
            "proposer_provider": "openrouter",
            "proposer_model": "candidate-model",
        }
    )
    await renderer.aensemble_progress(
        {
            "event_type": "aggregator_start",
            "proposer_index": -1,
            "sample_index": 0,
            "proposer_label": "aggregator",
            "proposer_provider": "openrouter",
            "proposer_model": "answer-model",
        }
    )
    await renderer.aensemble_progress(
        {
            "event_type": "aggregator_finish",
            "proposer_index": -1,
            "sample_index": 0,
            "proposer_label": "aggregator",
            "proposer_provider": "openrouter",
            "proposer_model": "answer-model",
            "elapsed_ms": 900,
            "input_tokens": 80,
            "output_tokens": 24,
        }
    )

    final_patch = [
        payload["patch"]
        for kind, payload in handle.sent
        if kind == "block.update" and "patch" in payload
    ][-1]
    members = {member["role"]: member for member in final_patch["members"]}
    assert final_patch["total"] == 1
    assert members["proposer"]["id"] == "proposer:0:0"
    assert members["proposer"]["label"] == "fast"
    assert members["aggregator"]["id"] == "aggregator:0:0"
    assert members["aggregator"]["status"] == "done"


@pytest.mark.asyncio
async def test_ensemble_finalize_adds_aggregator_receipt_before_usage() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)
    usage = SimpleNamespace(
        input_tokens=250,
        output_tokens=80,
        reasoning_tokens=0,
        model="ensemble/answer-model",
        model_usage_breakdown=[
            {
                "role": "proposer",
                "label": "fast",
                "provider": "openrouter",
                "model": "model-a",
                "sample_index": 0,
                "input_tokens": 120,
                "output_tokens": 32,
                "request_count": 1,
            },
            {
                "role": "aggregator",
                "label": "judge",
                "provider": "openrouter",
                "model": "answer-model",
                "input_tokens": 130,
                "output_tokens": 48,
                "request_count": 1,
            },
        ],
        ensemble_trace={
            "total_candidates": 1,
            "successful_proposers": 1,
            "fallback_used": False,
            "fallback_reason": "",
            "candidates": [{"content": "DO NOT RENDER THIS", "text": "NOR THIS"}],
        },
        session_totals=None,
    )

    await renderer.aappend_text("combined answer")
    await renderer.afinalize(usage)

    ensemble_begin_index = next(
        index
        for index, (kind, payload) in enumerate(handle.sent)
        if kind == "block.begin" and payload.get("kind") == "ensemble"
    )
    ensemble_id = handle.sent[ensemble_begin_index][1]["id"]
    ensemble_end_index = next(
        index
        for index, (kind, payload) in enumerate(handle.sent)
        if kind == "block.end" and payload["id"] == ensemble_id
    )
    usage_index = next(
        index
        for index, (kind, payload) in enumerate(handle.sent)
        if kind == "block.begin" and payload.get("kind") == "usage"
    )
    assert ensemble_begin_index < ensemble_end_index < usage_index
    final_patch = handle.sent[ensemble_begin_index][1]["meta"]
    assert final_patch["status"] == "done"
    assert final_patch["request_count"] == 2
    assert {member["role"] for member in final_patch["members"]} == {
        "proposer",
        "aggregator",
    }
    assert "DO NOT RENDER" not in repr(handle.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_tokens", [None, 0])
async def test_usage_receipt_omits_unreported_reasoning_tokens(
    reasoning_tokens: int | None,
) -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)
    renderer.__enter__()
    await renderer.aappend_text("done")
    await renderer.afinalize(
        UsageSummary(
            input_tokens=12,
            output_tokens=4,
            reasoning_tokens=reasoning_tokens,
            model="test-model",
        )
    )

    usage = next(
        payload
        for kind, payload in handle.sent
        if kind == "block.begin" and payload.get("kind") == "usage"
    )
    assert usage["meta"]["text"] == "in 12 / out 4 · test-model"
    assert "think" not in usage["meta"]["text"]


@pytest.mark.asyncio
async def test_anonymous_tools_each_close_independently() -> None:
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.atool_start("a", {}, None)
    await r.atool_finished(None, success=True, result="ra")
    await r.atool_start("b", {}, None)
    await r.atool_finished(None, success=True, result="rb")
    begins = [p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "tool"]
    ends = [p for t, p in handle.sent if t == "block.end"]
    assert len(begins) == 2
    # each tool block gets its own end (distinct ids), so neither overwrites
    # the other and no dangling block is left without a close.
    begin_ids = {p["id"] for p in begins}
    end_ids = {p["id"] for p in ends}
    assert len(begin_ids) == 2
    assert begin_ids <= end_ids


def test_format_tokens_abbreviates_thousands() -> None:
    assert _format_tokens(856) == "856"
    assert _format_tokens(1234) == "1.2k"
    assert _format_tokens(0) == "0"
    assert _format_tokens(None) == "0"


@pytest.mark.asyncio
async def test_afinalize_writes_usage_to_toolbar_and_invalidates() -> None:
    handle = _ToolbarRecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(UsageSummary(input_tokens=1234, output_tokens=856))
    assert handle.toolbar.get("router_usage") == "1.2k/856"
    assert handle.invalidated == 2


@pytest.mark.asyncio
async def test_afinalize_writes_session_input_to_toolbar() -> None:
    handle = _ToolbarRecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(
        UsageSummary(
            input_tokens=1,
            output_tokens=2,
            session_totals=SessionTotalsSnapshot(input_tokens=84_000),
        )
    )

    assert handle.toolbar.get("router_usage") == "1/2"
    assert handle.toolbar.get("router_session_input") == 84_000
    assert handle.invalidated == 2


@pytest.mark.asyncio
async def test_afinalize_clears_stale_session_input_when_snapshot_missing() -> None:
    handle = _ToolbarRecordingHandle()
    handle.toolbar["router_session_input"] = 84_000
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(UsageSummary(input_tokens=1, output_tokens=2))

    assert handle.toolbar.get("router_usage") == "1/2"
    assert "router_session_input" not in handle.toolbar
    assert handle.invalidated == 2


@pytest.mark.asyncio
async def test_turn_begin_clears_stale_router_usage_before_finalize_writes_current_usage() -> None:
    handle = _ToolbarRecordingHandle()
    handle.toolbar["router_usage"] = "999/888"
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.astatus("thinking")

    assert ("router_usage", None) in handle.toolbar_updates
    assert "router_usage" not in handle.toolbar
    assert handle.invalidated == 1

    await r.afinalize(UsageSummary(input_tokens=5, output_tokens=7))

    assert handle.toolbar.get("router_usage") == "5/7"
    assert handle.toolbar_updates[-1] == ("router_usage", "5/7")
    assert handle.invalidated == 2


@pytest.mark.asyncio
async def test_turn_begin_clears_stale_router_decision_for_no_decision_turn() -> None:
    handle = _ToolbarRecordingHandle()
    stale_decision = {
        "router_hud": "route c0 -> stale-model 60% save 90%",
        "router_hud_style": "normal",
        "router_baseline_model": "baseline-model",
        "router_source": "router",
        "router_routing_applied": True,
        "router_rollout_phase": "full",
        "router_context_window": 200_000,
    }
    handle.toolbar.update(stale_decision)
    renderer = OpenTuiStreamRenderer(output_handle=handle)
    renderer.__enter__()

    await renderer.aturn_started()

    assert stale_decision.keys().isdisjoint(handle.toolbar)
    assert all((key, None) in handle.toolbar_updates for key in stale_decision)
    # Decision, context, and usage reset together in one host repaint.
    assert handle.invalidated == 1

    await renderer.afinalize(None)

    assert stale_decision.keys().isdisjoint(handle.toolbar)
    assert handle.invalidated == 1


@pytest.mark.asyncio
async def test_no_usage_turn_keeps_router_usage_cleared() -> None:
    handle = _ToolbarRecordingHandle()
    handle.toolbar["router_usage"] = "999/888"
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(None)
    assert "router_usage" not in handle.toolbar
    assert handle.invalidated == 1


@pytest.mark.asyncio
async def test_afinalize_tolerates_handle_without_set_toolbar() -> None:
    # The plain recording handle has no set_toolbar/invalidate — afinalize must
    # not crash when wiring usage into the router toolbar.
    handle = _RecordingHandle()
    r = OpenTuiStreamRenderer(output_handle=handle)
    r.__enter__()
    await r.aappend_text("done")
    await r.afinalize(UsageSummary(input_tokens=5, output_tokens=7))
    assert [t for t, _ in handle.sent if t == "turn.end"]


def test_tool_result_summary_keeps_meaningful_lines_without_banners() -> None:
    summary = summarize_result(
        "exit_code=0\n"
        ".\n"
        "·\n"
        "...\n"
        "═══ 一级模块 ═══\n"
        "agents\n"
        "────────\n"
        "exit_code=1\n"
        "================\n"
        "src/opensquilla/main.py\n"
    )

    assert summary == "agents\nexit_code=1\nsrc/opensquilla/main.py"
    assert "exit_code=0" not in summary
    assert " / " not in summary
    assert "═══" not in summary


def test_tool_result_summary_stringifies_single_structured_msg_payload() -> None:
    summary = summarize_result(
        {
            "type": "msg",
            "msg": [
                {"kind": "text", "text": "first"},
                {"kind": "data", "value": {"rows": [1, 2]}},
            ],
        }
    )

    assert summary.startswith("[")
    assert '"type": "msg"' not in summary
    assert '"rows": [1, 2]' in summary


def test_tool_result_summary_stringifies_structured_msg_payloads() -> None:
    summary = summarize_result(
        [
            {"type": "msg", "msg": {"files": ["main.py"], "count": 1}},
            {"type": "msg", "msg": ["ok", {"status": "done"}]},
        ]
    )

    assert summary == (
        '{"count": 1, "files": ["main.py"]}\n'
        '["ok", {"status": "done"}]'
    )


async def test_aturn_started_announces_thinking_before_any_provider_event() -> None:
    """The stream loop announces the turn as soon as it starts, so transcript
    activity pulses through the silent model-thinking window instead of the UI
    sitting on "ready" until the first token."""
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aturn_started()

    types = [message_type for message_type, _payload in handle.sent]
    assert "turn.begin" in types
    status = next(p for t, p in handle.sent if t == "turn.status")
    assert status["phase"] == "thinking"
    assert status["active"] is True
    reasoning = [
        p for t, p in handle.sent if t == "block.begin" and p.get("kind") == "reasoning"
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["meta"] == {"waiting": True}
    # Idempotent: the first real event must not begin a second turn.
    await renderer.aturn_started()
    assert sum(t == "turn.begin" for t, _p in handle.sent) == 1
    assert sum(
        t == "block.begin" and p.get("kind") == "reasoning" for t, p in handle.sent
    ) == 1


async def test_answer_stream_strips_routing_directive_tags() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    # Tag split across deltas, then the real answer.
    await renderer.aappend_text("[[reply_to", presentation="answer")
    await renderer.aappend_text("_current]]\n", presentation="answer")
    await renderer.aappend_text("My name is OpenSquilla.", presentation="answer")
    await renderer.afinalize()

    appends = [p["delta"] for t, p in handle.sent if t == "block.append"]
    joined = "".join(appends)
    assert "reply_to_current" not in joined
    assert joined == "My name is OpenSquilla."
    # The raw logical buffer (TurnResult text) keeps the model's exact output.
    assert "[[reply_to_current]]" in renderer.buffer


async def test_tag_only_delta_opens_no_answer_block() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aappend_text("[[reply_to_current]]", presentation="answer")
    await renderer.afinalize()

    kinds = [p["kind"] for t, p in handle.sent if t == "block.begin"]
    assert "answer" not in kinds


async def test_held_bracket_prefix_flushes_into_the_block_on_close() -> None:
    handle = _RecordingHandle()
    renderer = OpenTuiStreamRenderer(output_handle=handle)

    await renderer.aappend_text("see ", presentation="answer")
    # A bracket run that never completes into a directive is ordinary text.
    await renderer.aappend_text("[[re", presentation="answer")
    await renderer.afinalize()

    appends = [p["delta"] for t, p in handle.sent if t == "block.append"]
    assert "".join(appends) == "see [[re"
