"""Structured-message renderer for the OpenTUI footer backend.

Implements the async TUI renderer protocol by emitting one structured timeline
message per call so the JS host can render each block by type. The renderer's
lifetime equals one turn, so turn.begin/status/end are driven by
enter/method-calls/afinalize, with aclose as the teardown safety net for turns
that end on an error path without reaching afinalize.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import asdict
from itertools import count
from typing import Any, Literal

from opensquilla.cli.tui.backend.directives import StreamDirectiveFilter
from opensquilla.cli.tui.backend.render_summary import (
    sanitize_terminal_text,
    summarize_args,
    tool_args_detail,
    tool_result_detail,
)
from opensquilla.cli.tui.opentui.messages import (
    BlockAppend,
    BlockBegin,
    BlockEnd,
    BlockUpdate,
    PromptState,
    TurnBegin,
    TurnEnd,
    TurnStatusState,
)

_turn_ids = count(1)

_ROUTER_DECISION_TOOLBAR_KEYS = (
    "router_hud",
    "router_hud_style",
    "router_baseline_model",
    "router_source",
    "router_routing_applied",
    "router_rollout_phase",
    "router_context_window",
)


class OpenTuiStreamRenderer:
    """Async renderer that emits structured OpenTUI timeline messages."""

    def __init__(self, *, title: str = "squilla", output_handle: Any | None = None) -> None:
        self.title = title
        self.output_handle = output_handle
        self.buffer = ""
        self._turn_id = ""
        from opensquilla.cli.tui.backend.input_identity import (
            current_tui_client_message_id,
        )

        self._client_message_id = current_tui_client_message_id()
        self._began = False
        self._finalized = False
        # Active transcript phase; None after a transient status label so the
        # next stream delta restores the canonical thinking/output/tool phase.
        self._activity_phase: str | None = None
        self._block_seq = 0
        self._open_text_id: str | None = None
        self._open_text_presentation: str = "answer"
        # Every assistant-text block remains addressable after block.end so a
        # terminal snapshot can clear stale preview blocks without touching tool
        # rows in the same turn card.
        self._text_block_ids: list[str] = []
        self._open_reasoning_id: str | None = None
        self._ensemble_block_id: str | None = None
        self._ensemble_members: dict[str, dict[str, Any]] = {}
        self._ensemble_total = 0
        self._tool_block_ids: dict[str, str] = {}
        self._last_tool_block_id: str | None = None
        self._open_tool_ids: set[str] = set()
        # Per-tool start times so atool_finished can surface a " · 0.2s" duration
        # like opencode/codex even when the caller does not pass `elapsed`.
        self._tool_start_times: dict[str, float] = {}
        # Strips [[reply_to_current]]-style routing directives from streamed
        # text; per open text block, reset on block close.
        self._directive_filter = StreamDirectiveFilter()

    async def aturn_started(self) -> None:
        """Announce the turn before the first provider event.

        The stream loop calls this right after the renderer is created, so the
        transcript opens a pulsing "Thinking" row the moment the user submits
        instead of sitting visibly dead until the first token arrives.
        """
        await self._ensure_begin()

    async def _emit(self, message_type: str, payload: Any) -> None:
        await self._emit_raw(message_type, asdict(payload))

    async def _emit_raw(self, message_type: str, payload: dict[str, Any]) -> None:
        handle = self.output_handle
        if handle is None:
            return
        send = getattr(handle, "send_message", None)
        if send is None:
            return
        await send(message_type, payload)

    async def _ensure_begin(self) -> None:
        if self._began:
            return
        self._began = True
        self._turn_id = f"t{next(_turn_ids)}"
        self._clear_router_decision()
        self._set_router_session_input(None)
        self._set_router_usage(None)
        await self._emit(
            "turn.begin",
            TurnBegin(
                id=self._turn_id,
                client_message_id=self._client_message_id,
            ),
        )
        await self._emit(
            "turn.status", TurnStatusState(phase="thinking", label="thinking", active=True)
        )
        self._activity_phase = "thinking"
        # Keep the composer interactive during a turn. The shared TUI runtime
        # already classifies local commands for immediate execution and routes
        # ordinary submissions into its bounded queue / tool-boundary steering
        # provider. Disabling the host composer here made that working backend
        # contract unreachable and turned Enter into a silent no-op.
        # Open the transcript's live activity row immediately, before the
        # provider emits its first event.  Providers that expose reasoning will
        # stream their exact deltas into this same block; providers that do not
        # expose it still show an honest "waiting for model output" state instead
        # of leaving the transcript frozen while only the footer timer moves.
        self._open_reasoning_id = self._next_block_id()
        await self._emit(
            "block.begin",
            BlockBegin(
                id=self._open_reasoning_id,
                kind="reasoning",
                meta={"waiting": True},
            ),
        )

    async def aset_turn_identity(
        self,
        turn_id: str,
        client_message_id: str,
        *,
        disposition: str = "accepted",
    ) -> None:
        """Bind the optimistic prompt card to the Gateway's durable turn id."""
        if not turn_id or not client_message_id:
            return
        await self._emit(
            "prompt.state",
            PromptState(
                turn_id=turn_id,
                client_message_id=client_message_id,
                disposition=disposition,
            ),
        )

    def __enter__(self) -> OpenTuiStreamRenderer:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False

    def pulse(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def start(self) -> None:
        return None

    def _next_block_id(self) -> str:
        self._block_seq += 1
        return f"{self._turn_id}-b{self._block_seq}"

    async def aappend_text(self, delta: str, *, presentation: str = "answer") -> None:
        if not delta:
            return
        await self._ensure_begin()
        if self._activity_phase != "output":
            # Re-emitted whenever text resumes (e.g. the final answer streaming
            # after a tool call) so the activity pulse follows the live block.
            self._activity_phase = "output"
            await self._emit(
                "turn.status", TurnStatusState(phase="output", label="output", active=True)
            )
        # The agent tells us, per text segment, whether it is the turn's answer
        # (-> cyan card) or intermediate narration between tool calls (-> purple ✻
        # thinking line), and we open the matching block kind from the first delta.
        # A single block never changes kind, but a call CAN switch presentation:
        # text streams live as the answer until a tool appears, after which later
        # text in the same call is intermediate (the agent streams rather than
        # buffering — see agent.py / issue #358). When that happens the open block
        # is closed and a fresh block of the new kind is opened (below), so each
        # segment stays its own correctly-typed block.
        kind = "answer" if presentation == "answer" else "thinking"
        # A reasoning stream that was open must close before assistant text.
        await self._close_reasoning()
        # If the presentation flips mid-stream, close the old block so each
        # segment is its own block of the correct kind.
        if self._open_text_id is not None and self._open_text_presentation != kind:
            await self._close_text()
        self.buffer += delta
        # Routing directives are for channel delivery, not the transcript; a
        # tag-only delta opens no block at all (blocking an empty card).
        visible = self._directive_filter.feed(delta)
        if not visible:
            return
        if self._open_text_id is None:
            self._open_text_id = self._next_block_id()
            self._open_text_presentation = kind
            self._text_block_ids.append(self._open_text_id)
            await self._emit(
                "block.begin", BlockBegin(id=self._open_text_id, kind=kind, meta={})
            )
        await self._emit("block.append", BlockAppend(id=self._open_text_id, delta=visible))

    async def areconcile_final_text(self, text: str) -> None:
        """Replace stale text blocks while preserving the turn's tool timeline."""

        previous = self.buffer
        if text == previous:
            return
        if text.startswith(previous):
            await self.aappend_text(text[len(previous) :], presentation="answer")
            return

        # End the current text block without releasing a held directive prefix
        # from the superseded preview.  Closed blocks remain updateable by id.
        self._directive_filter = StreamDirectiveFilter()
        await self._close_text()
        await self._close_reasoning()
        for block_id in self._text_block_ids:
            await self._emit("block.update", BlockUpdate(id=block_id, patch={"text": ""}))
        self._text_block_ids.clear()
        self.buffer = ""

        notice = (
            "Final answer corrected; an earlier streamed preview was superseded."
            if text
            else "Streamed preview withdrawn; the final answer is empty."
        )
        await self.astatus(notice, style="warning")
        if text:
            # A fresh answer block is appended after any preserved tool rows.
            # aappend_text also applies the routing-directive visibility policy.
            await self.aappend_text(text, presentation="answer")

    async def aappend_reasoning(self, delta: str) -> None:
        # Reasoning is the model's extended-thinking PROCESS. aturn_started has
        # already opened the live block, so the first and every subsequent
        # provider delta append to one stable transcript row without a flash or
        # retype. When the stream ends the host collapses real reasoning to a
        # one-line "Thought for Ns" record; a provider that emitted no reasoning
        # keeps only an honest observable wait (sub-second waits disappear,
        # longer waits may settle as "Worked for Ns").
        if not delta:
            return
        await self._ensure_begin()
        if self._open_reasoning_id is None:
            self._open_reasoning_id = self._next_block_id()
            await self._emit(
                "block.begin",
                BlockBegin(id=self._open_reasoning_id, kind="reasoning", meta={}),
            )
        await self._emit("block.append", BlockAppend(id=self._open_reasoning_id, delta=delta))

    async def aensemble_progress(self, payload: dict[str, Any]) -> None:
        """Project a real provider ensemble lifecycle event into one live block.

        The Gateway/provider event intentionally carries member metadata and
        usage only. Candidate bodies, raw reasoning, and execution payloads are
        never copied into the host protocol, even if an unexpected producer
        includes those fields in ``payload``.
        """

        event_type = _ensemble_text(payload.get("event_type"))
        if not event_type:
            return
        await self._ensure_begin()
        if self._ensemble_block_id is None:
            # Replace the synthetic pre-provider wait row with the richer,
            # truthful ensemble lifecycle. Any real reasoning already emitted
            # remains retained in its completed disclosure block.
            await self._close_reasoning()

        member = _ensemble_progress_member(payload, event_type=event_type)
        member_id = member["id"]
        previous = self._ensemble_members.get(member_id, {})
        self._ensemble_members[member_id] = {**previous, **member}
        self._ensemble_total = max(self._ensemble_total, len(self._proposer_members()))

        snapshot = self._ensemble_snapshot(status="running")
        if self._ensemble_block_id is None:
            self._ensemble_block_id = self._next_block_id()
            self._activity_phase = "ensemble"
            await self._emit(
                "turn.status",
                TurnStatusState(phase="ensemble", label="ensemble", active=True),
            )
            await self._emit(
                "block.begin",
                BlockBegin(id=self._ensemble_block_id, kind="ensemble", meta=snapshot),
            )
        else:
            await self._emit(
                "block.update",
                BlockUpdate(id=self._ensemble_block_id, patch=snapshot),
            )

    def _proposer_members(self) -> list[dict[str, Any]]:
        return [
            member
            for member in self._ensemble_members.values()
            if member.get("role", "proposer") == "proposer"
        ]

    def _ensemble_snapshot(
        self,
        *,
        status: str,
        trace: dict[str, Any] | None = None,
        request_count: int = 0,
    ) -> dict[str, Any]:
        trace = trace or {}
        proposers = self._proposer_members()
        trace_total = _ensemble_int(
            trace.get("total_candidates") or trace.get("totalCandidates")
        )
        total = max(self._ensemble_total, trace_total, len(proposers))
        completed = sum(
            member.get("status") in {"done", "error", "cancelled"}
            for member in proposers
        )
        if status != "running" and total:
            completed = total
        return {
            "completed": completed,
            "total": total,
            "members": [
                _ensemble_public_member(member)
                for member in self._ensemble_members.values()
            ],
            "status": status,
            "request_count": request_count,
            "fallback_used": bool(
                trace.get("fallback_used") or trace.get("fallbackUsed")
            ),
            "fallback_reason": _ensemble_text(
                trace.get("fallback_reason") or trace.get("fallbackReason")
            ),
        }

    async def _finalize_ensemble(self, usage: object | None, *, cancelled: bool) -> None:
        trace = _ensemble_trace(usage)
        # A model-usage breakdown is also produced by non-ensemble turns. A
        # trace or a live progress event is the execution proof; never infer
        # ensemble activation from configuration or a generic usage row.
        if self._ensemble_block_id is None and not trace:
            return

        breakdown = _ensemble_breakdown(usage)
        self._merge_ensemble_trace_members(trace)
        self._merge_ensemble_usage_members(breakdown)
        self._ensemble_total = max(
            self._ensemble_total,
            _ensemble_int(trace.get("total_candidates") or trace.get("totalCandidates")),
        )
        request_count = sum(
            max(1, _ensemble_int(row.get("request_count") or row.get("requestCount")))
            for row in breakdown
        )
        fallback_used = bool(trace.get("fallback_used") or trace.get("fallbackUsed"))
        status = "cancelled" if cancelled else "fallback" if fallback_used else "done"
        snapshot = self._ensemble_snapshot(
            status=status,
            trace=trace,
            request_count=request_count,
        )
        if self._ensemble_block_id is None:
            self._ensemble_block_id = self._next_block_id()
            await self._emit(
                "block.begin",
                BlockBegin(id=self._ensemble_block_id, kind="ensemble", meta=snapshot),
            )
        else:
            await self._emit(
                "block.update",
                BlockUpdate(id=self._ensemble_block_id, patch=snapshot),
            )
        block_id = self._ensemble_block_id
        self._ensemble_block_id = None
        await self._emit("block.end", BlockEnd(id=block_id))

    def _merge_ensemble_trace_members(self, trace: dict[str, Any]) -> None:
        raw_candidates = trace.get("candidates")
        if not isinstance(raw_candidates, list | tuple):
            return
        for index, raw in enumerate(raw_candidates):
            if not isinstance(raw, dict):
                continue
            row = {
                "role": "proposer",
                "label": raw.get("label"),
                "provider": raw.get("provider"),
                "model": raw.get("model"),
                "sample_index": raw.get("sample_index") or raw.get("sampleIndex"),
                "proposer_index": raw.get("index", index),
                "elapsed_ms": raw.get("elapsed_ms") or raw.get("elapsedMs"),
                "input_tokens": raw.get("input_tokens") or raw.get("inputTokens"),
                "output_tokens": raw.get("output_tokens") or raw.get("outputTokens"),
                "cost_usd": raw.get("billed_cost") or raw.get("cost_usd"),
                "error": raw.get("error"),
                "status": "done" if raw.get("ok") else "error",
            }
            self._merge_ensemble_member(_ensemble_usage_member(row, fallback_index=index))

    def _merge_ensemble_usage_members(self, breakdown: list[dict[str, Any]]) -> None:
        for index, row in enumerate(breakdown):
            self._merge_ensemble_member(_ensemble_usage_member(row, fallback_index=index))

    def _merge_ensemble_member(self, member: dict[str, Any]) -> None:
        identity = _ensemble_member_identity(member)
        for member_id, existing in self._ensemble_members.items():
            if _ensemble_member_identity(existing) == identity:
                self._ensemble_members[member_id] = {
                    **existing,
                    **{key: value for key, value in member.items() if value not in {"", None}},
                }
                return
        self._ensemble_members[member["id"]] = member

    async def _close_text(self) -> None:
        # A held tail that never completed into a directive tag is ordinary
        # text and belongs to this segment — even when the segment consisted
        # of nothing else (no block opened yet).
        tail = self._directive_filter.flush()
        self._directive_filter = StreamDirectiveFilter()
        if self._open_text_id is None and tail:
            self._open_text_id = self._next_block_id()
            self._text_block_ids.append(self._open_text_id)
            await self._emit(
                "block.begin",
                BlockBegin(id=self._open_text_id, kind=self._open_text_presentation, meta={}),
            )
        if self._open_text_id is None:
            return
        block_id = self._open_text_id
        if tail:
            await self._emit("block.append", BlockAppend(id=block_id, delta=tail))
        self._open_text_id = None
        await self._emit("block.end", BlockEnd(id=block_id))

    async def _close_reasoning(self) -> None:
        if self._open_reasoning_id is None:
            return
        block_id = self._open_reasoning_id
        self._open_reasoning_id = None
        await self._emit("block.end", BlockEnd(id=block_id))

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        # Status lines carry real user-facing information (artifact saved, task
        # group progress, warnings): mirror the native backend by rendering a
        # dim in-card line, and surface the message through the activity state.
        # The phase is cleared so the next delta restores its canonical label.
        text = message.strip()
        if not text:
            return
        await self._ensure_begin()
        await self._emit(
            "turn.status",
            TurnStatusState(
                phase=self._activity_phase or "thinking", label=text, active=True
            ),
        )
        self._activity_phase = None
        block_id = self._next_block_id()
        await self._emit(
            "block.begin",
            BlockBegin(id=block_id, kind="status", meta={"text": text, "style": style}),
        )
        await self._emit("block.end", BlockEnd(id=block_id))

    async def atool_start(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        await self._ensure_begin()
        await self._close_reasoning()
        await self._close_text()
        summary = summarize_args(name, args)
        block_id = tool_use_id or self._next_block_id()
        if tool_use_id:
            self._tool_block_ids[tool_use_id] = block_id
        self._last_tool_block_id = block_id
        self._tool_start_times[block_id] = time.monotonic()
        await self._emit("turn.status", TurnStatusState(phase="tool", label=name, active=True))
        self._activity_phase = "tool"
        await self._emit(
            "block.begin",
            BlockBegin(
                id=block_id,
                kind="tool",
                meta={
                    "name": name,
                    "args_summary": summary,
                    "args_full": tool_args_detail(args),
                },
            ),
        )
        self._open_tool_ids.add(block_id)

    async def atool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
        result: object | None = None,
    ) -> None:
        if tool_use_id:
            block_id = self._tool_block_ids.get(tool_use_id)
        else:
            block_id = self._last_tool_block_id
        if block_id is None:
            block_id = self._next_block_id()
            await self._emit(
                "block.begin",
                BlockBegin(
                    id=block_id,
                    kind="tool",
                    meta={"name": "", "args_summary": "", "args_full": ""},
                ),
            )
        # Preserve the complete safe payload.  Compactness is exclusively a
        # host rendering concern: the JS block keeps a short preview and makes
        # every retained argument/result/error line available via Ctrl+O.
        result_detail = tool_result_detail(result)
        error_detail = tool_result_detail(error)
        if result_detail:
            await self._emit("block.append", BlockAppend(id=block_id, delta=result_detail))
        patch: dict[str, Any] = {"status": "ok" if success else "error"}
        if error_detail:
            patch["error"] = error_detail
        start = self._tool_start_times.pop(block_id, None)
        if elapsed is None and start is not None:
            elapsed = time.monotonic() - start
        if elapsed is not None:
            patch["duration"] = f"{elapsed:.1f}s"
        await self._emit("block.update", BlockUpdate(id=block_id, patch=patch))
        await self._emit("block.end", BlockEnd(id=block_id))
        self._open_tool_ids.discard(block_id)

    async def aerror(self, message: str) -> None:
        await self._ensure_begin()
        await self._close_reasoning()
        await self._close_text()
        block_id = self._next_block_id()
        await self._emit(
            "block.begin", BlockBegin(id=block_id, kind="error", meta={"text": message})
        )
        await self._emit("block.end", BlockEnd(id=block_id))

    async def afinalize(self, usage: Any | None = None, *, cancelled: bool = False) -> None:
        self._finalized = True
        await self._ensure_begin()
        await self._close_reasoning()
        await self._close_text()
        # Force-close any tool blocks still open (e.g. a turn cancelled mid-tool
        # never reaches atool_finished). They resolve to ✗: a cancelled in-flight
        # tool did not succeed, so error is the honest status.
        for block_id in list(self._open_tool_ids):
            await self._emit("block.update", BlockUpdate(id=block_id, patch={"status": "error"}))
            await self._emit("block.end", BlockEnd(id=block_id))
        self._open_tool_ids.clear()
        await self._finalize_ensemble(usage, cancelled=cancelled)
        # Emit usage BEFORE turn.end so it attaches to the still-active turn view
        # (turn.end marks the turn ended; a later block would spawn an orphan turn).
        usage_id = self._next_block_id()
        await self._emit(
            "block.begin",
            BlockBegin(id=usage_id, kind="usage", meta={"text": _format_usage(usage)}),
        )
        await self._emit("block.end", BlockEnd(id=usage_id))
        await self._emit("turn.end", TurnEnd(id=self._turn_id, cancelled=cancelled))
        await self._emit("turn.status", TurnStatusState(phase="idle", label="ready", active=False))
        self._activity_phase = "idle"
        await self._emit_raw("composer.set", {"disabled": False})
        self._publish_usage_to_router_toolbar(usage)

    def _publish_usage_to_router_toolbar(self, usage: Any | None) -> None:
        # Surface this turn's token in/out in the router panel's ctx row. The
        # router panel reads its data from the output handle's toolbar and
        # repaints on invalidate(); defensively guard both methods so test
        # recording handles (which expose neither) never crash the turn.
        if usage is None:
            return
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        if in_tok is None and out_tok is None:
            return
        self._set_router_session_input(_session_input_tokens(usage))
        self._set_router_usage(f"{_format_tokens(in_tok)}/{_format_tokens(out_tok)}")

    def _set_router_session_input(self, value: object | None) -> None:
        set_toolbar = getattr(self.output_handle, "set_toolbar", None)
        if callable(set_toolbar):
            set_toolbar("router_session_input", value)

    def _clear_router_decision(self) -> None:
        set_toolbar = getattr(self.output_handle, "set_toolbar", None)
        if not callable(set_toolbar):
            return
        for key in _ROUTER_DECISION_TOOLBAR_KEYS:
            set_toolbar(key, None)

    def _set_router_usage(self, value: object | None) -> None:
        set_toolbar = getattr(self.output_handle, "set_toolbar", None)
        if not callable(set_toolbar):
            return
        set_toolbar("router_usage", value)
        invalidate = getattr(self.output_handle, "invalidate", None)
        if callable(invalidate):
            invalidate()

    async def aclose(self) -> None:
        # The stream callers guarantee aclose via finally even on error paths
        # that never reach afinalize (provider errors, timeouts, error frames).
        # Without teardown the transcript would pulse forever, the composer
        # would stay disabled-colored, and the next turn would merge into the
        # unfinished card — so emit the minimal turn-teardown sequence here.
        if not self._began or self._finalized:
            return
        self._finalized = True
        # Best-effort: the bridge may already be gone, and teardown must never
        # mask the error that ended the turn.
        with contextlib.suppress(Exception):
            await self._close_reasoning()
            await self._close_text()
            for block_id in list(self._open_tool_ids):
                await self._emit(
                    "block.update", BlockUpdate(id=block_id, patch={"status": "error"})
                )
                await self._emit("block.end", BlockEnd(id=block_id))
            self._open_tool_ids.clear()
            await self._finalize_ensemble(None, cancelled=True)
            await self._emit("turn.end", TurnEnd(id=self._turn_id))
            await self._emit(
                "turn.status", TurnStatusState(phase="idle", label="ready", active=False)
            )
            self._activity_phase = "idle"
            await self._emit_raw("composer.set", {"disabled": False})


_ENSEMBLE_MEMBER_FIELDS = (
    "id",
    "role",
    "label",
    "model",
    "provider",
    "status",
    "elapsed_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "error",
)


def _ensemble_text(value: object) -> str:
    return sanitize_terminal_text(str(value or "")).strip()


def _ensemble_int(value: object) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _ensemble_float(value: object) -> float:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _ensemble_optional_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in row and row[key] is not None:
            return _ensemble_int(row[key])
    return None


def _ensemble_optional_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row and row[key] is not None:
            return _ensemble_float(row[key])
    return None


def _ensemble_progress_member(
    event: dict[str, Any], *, event_type: str
) -> dict[str, Any]:
    role = "aggregator" if event_type.startswith("aggregator_") else "proposer"
    index = _ensemble_int(event.get("proposer_index"))
    sample_index = _ensemble_int(event.get("sample_index"))
    error = _ensemble_text(event.get("error"))
    finished = event_type.endswith("finish") or event_type.endswith("finished")
    status = "error" if error else "done" if finished else "running"
    return {
        # Aggregator progress uses proposer_index=-1 on the provider contract.
        # Give it a separate stable identity instead of clamping -1 to zero and
        # overwriting the first proposer while the final model is running.
        "id": f"{role}:{0 if role == 'aggregator' else index}:{sample_index}",
        "role": role,
        "_sample_index": sample_index,
        "label": _ensemble_text(event.get("proposer_label"))
        or ("aggregator" if role == "aggregator" else f"proposer {index + 1}"),
        "model": _ensemble_text(event.get("proposer_model")),
        "provider": _ensemble_text(event.get("proposer_provider")),
        "status": status,
        "elapsed_ms": (
            _ensemble_int(event.get("elapsed_ms"))
            if finished
            else None
        ),
        "input_tokens": (
            _ensemble_int(event.get("input_tokens"))
            if finished
            else None
        ),
        "output_tokens": (
            _ensemble_int(event.get("output_tokens"))
            if finished
            else None
        ),
        "cost_usd": (
            _ensemble_float(event.get("cost_usd"))
            if finished
            else None
        ),
        "error": error,
    }


def _ensemble_usage_member(
    row: dict[str, Any], *, fallback_index: int
) -> dict[str, Any]:
    role = _ensemble_text(row.get("role")) or "member"
    sample_index = _ensemble_int(row.get("sample_index") or row.get("sampleIndex"))
    index = _ensemble_int(
        row.get("proposer_index") or row.get("proposerIndex") or row.get("index")
    )
    label = _ensemble_text(row.get("label")) or role
    error = _ensemble_text(row.get("error"))
    explicit_status = _ensemble_text(row.get("status"))
    status = explicit_status or ("error" if error else "done")
    id_index = index if role == "proposer" else fallback_index
    return {
        "id": f"{role}:{id_index}:{sample_index}",
        "role": role,
        "_sample_index": sample_index,
        "label": label,
        "model": _ensemble_text(row.get("model")),
        "provider": _ensemble_text(row.get("provider")),
        "status": status,
        "elapsed_ms": _ensemble_optional_int(row, "elapsed_ms", "elapsedMs"),
        "input_tokens": _ensemble_optional_int(row, "input_tokens", "inputTokens"),
        "output_tokens": _ensemble_optional_int(row, "output_tokens", "outputTokens"),
        "cost_usd": _ensemble_optional_float(
            row,
            "billed_cost",
            "billedCost",
            "cost_usd",
            "costUsd",
        ),
        "error": error,
    }


def _ensemble_member_identity(member: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        _ensemble_text(member.get("role")),
        _ensemble_text(member.get("label")),
        _ensemble_text(member.get("provider")),
        _ensemble_text(member.get("model")),
        _ensemble_int(member.get("_sample_index")),
    )


def _ensemble_public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {field: member.get(field) for field in _ENSEMBLE_MEMBER_FIELDS}


def _ensemble_trace(usage: object | None) -> dict[str, Any]:
    raw = getattr(usage, "ensemble_trace", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _ensemble_breakdown(usage: object | None) -> list[dict[str, Any]]:
    raw = getattr(usage, "model_usage_breakdown", None)
    if not isinstance(raw, list | tuple):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def _format_tokens(value: Any) -> str:
    count = int(value or 0)
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _session_input_tokens(usage: Any) -> Any | None:
    session_totals = getattr(usage, "session_totals", None)
    if session_totals is None:
        return None
    return getattr(session_totals, "input_tokens", None)


def _format_usage(usage: Any) -> str:
    model = getattr(usage, "model", None)
    in_tok = getattr(usage, "input_tokens", None)
    out_tok = getattr(usage, "output_tokens", None)
    reasoning_tok = getattr(usage, "reasoning_tokens", None)
    parts: list[str] = []
    if in_tok is not None or out_tok is not None:
        token_text = (
            f"in {int(in_tok or 0):,} / out {int(out_tok or 0):,}"
        )
        # Reasoning tokens are a reported subset of output tokens, not another
        # amount to add to the total. Surface the breakdown only when the
        # provider actually reports a positive value; legacy/unsupported
        # providers remain visually unchanged instead of showing "think 0".
        if reasoning_tok is not None and int(reasoning_tok or 0) > 0:
            token_text += f" / think {int(reasoning_tok):,}"
        parts.append(token_text)
    if model:
        parts.append(str(model))
    return " · ".join(parts) if parts else "done"
