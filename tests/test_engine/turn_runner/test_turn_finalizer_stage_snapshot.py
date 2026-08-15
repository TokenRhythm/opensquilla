"""Snapshot harness for ``TurnFinalizerStage`` through ``TurnRunner._run_turn``.

Drives a 13-case corpus against ``TurnRunner._run_turn`` with the
``TurnFinalizerStage`` running through the runtime stage path. The corpus
exercises every branch in the slice plus the
heartbeat-empty edge, DeepSeek/non-DeepSeek reasoning, hallucination
passthrough, no-session-manager edge, and raising-fake cases for memory
capture and session totals (the two log-and-continue arms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolUseStartEvent,
)

# Reuse upstream patch helpers + stub agent from's equivalence
# harness -- this stage sits one step after the stream consumer, so the
# same patching strategy applies for everything before the slice.
from .test_agent_bootstrap_stage_snapshot import (
    _make_turn_factory,
    _patch_assemble_prompt,
    _patch_builder,
    _patch_ctx_mutators,
    _patch_memory_helpers,
    _patch_observability,
    _patch_resolve_prompt_config,
    _patch_resolver,
    _patch_router_context,
    _patch_run_pipeline,
    _patch_session_id,
    _StubModelCatalog,
    _StubProvider,
    _StubSelector,
)
from .test_stream_consumer_stage_snapshot import (
    _MAILBOX,
    _patch_budget_resolvers,
    _patch_compaction_history,
    _patch_thinking,
    _StubAgent,
)

# ---------------------------------------------------------------------------
# Recording session manager: exposes the four post-stream calls so the
# harness can pin bit-identical behavior across modes.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingSessionRow:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    billed_cost_usd: float = 0.0
    estimated_cost_component_usd: float = 0.0
    cost_source: str = "unknown"
    missing_cost_entries: int = 0
    cache_read: int = 0
    cache_write: int = 0
    model_override: str | None = None


class _RecordingSessionManager:
    """Records the four post-stream invocations for the harness to pin."""

    def __init__(
        self,
        *,
        memory_capture_raises: type[BaseException] | None = None,
        session_update_raises: type[BaseException] | None = None,
        session_row: _RecordingSessionRow | None = None,
    ) -> None:
        self.memory_capture_raises = memory_capture_raises
        self.session_update_raises = session_update_raises
        self._session_row = session_row if session_row is not None else _RecordingSessionRow()
        self.append_message_calls: list[dict[str, Any]] = []
        self.persist_error_calls: list[dict[str, Any]] = []
        self.get_session_calls: list[str] = []
        self.update_calls: list[dict[str, Any]] = []

    async def append_message(self, session_key: str, **kwargs: Any) -> None:
        # System-role appends originate from _persist_turn_error; route
        # them into a separate bucket so the harness can pin each
        # invocation independently.
        if kwargs.get("role") == "system" and kwargs.get(
            "content", ""
        ).startswith("Error: "):
            self.persist_error_calls.append(
                {"session_key": session_key, **kwargs}
            )
            return
        self.append_message_calls.append({"session_key": session_key, **kwargs})

    async def get_session(self, session_key: str) -> _RecordingSessionRow | None:
        self.get_session_calls.append(session_key)
        return self._session_row

    async def update(self, session_key: str, **kwargs: Any) -> None:
        if self.session_update_raises is not None:
            raise self.session_update_raises("recording rollup boom")
        self.update_calls.append({"session_key": session_key, **kwargs})

    # The CompactionPersistPort calls this; not relevant to but
    # the stub-agent corpus is event-empty for compaction so it never
    # fires.
    async def persist_compaction_result(
        self,
        session_key: str,
        summary: str,
        kept_entries: list[Any],
    ) -> None:
        return None


@dataclass
class _Case:
    case_id: str
    events: list[Any] = field(default_factory=list)
    error_message: str | None = None  # asserted post-run, not seeded
    expected_transcript_append_count: int = 0
    expected_memory_capture_calls: int = 0
    expected_persist_error_calls: int = 0
    expected_update_calls: int = 0
    expected_done_present: bool | None = None
    expected_pending_error_code: str | None = None
    private_memory_allowed: bool = True
    memory_capture_raises: type[BaseException] | None = None
    session_update_raises: type[BaseException] | None = None
    no_session_manager: bool = False
    resolved_model: str = "claude-sonnet-4.5"


def _done(**kw: Any) -> DoneEvent:
    return DoneEvent(
        text=kw.pop("text", ""),
        input_tokens=kw.pop("input_tokens", 0),
        output_tokens=kw.pop("output_tokens", 0),
        **kw,
    )


_CORPUS: list[_Case] = [
    # 1. Simple text turn, no done event
    _Case(
        case_id="simple_text_no_done",
        events=[TextDeltaEvent(text="hi")],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
    ),
    # 2. Simple text turn with DoneEvent -> rollup fires
    _Case(
        case_id="simple_text_with_done_rollup",
        events=[TextDeltaEvent(text="hi"), _done(text="hi", input_tokens=5, output_tokens=3)],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 3. Turn with tool segments (no text) -> transcript with tool_calls
    _Case(
        case_id="tool_use_only_segments",
        events=[
            TextDeltaEvent(text="pre"),
            ToolUseStartEvent(
                tool_use_id="t1", tool_name="echo", synthetic_from_text=False
            ),
            _done(text="", input_tokens=2, output_tokens=2),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 4. Turn with pending error -> error persist fires, no rollup
    _Case(
        case_id="pending_error_no_done",
        events=[
            TextDeltaEvent(text="partial "),
            ErrorEvent(message="boom", code="agent_error"),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_persist_error_calls=1,
        expected_pending_error_code="agent_error",
    ),
    # 5. Human sentinel-only reply -> explicit error, but Done usage still rolls up.
    _Case(
        case_id="heartbeat_sentinel_empty",
        events=[
            TextDeltaEvent(text="HEARTBEAT_OK"),
            _done(text="HEARTBEAT_OK", input_tokens=1, output_tokens=1),
        ],
        expected_transcript_append_count=0,
        expected_memory_capture_calls=0,
        expected_persist_error_calls=1,
        expected_update_calls=1,
        expected_done_present=False,
        expected_pending_error_code="silent_reply_not_allowed",
    ),
    # 6. DeepSeek reasoning_content included
    _Case(
        case_id="deepseek_reasoning_included",
        events=[
            TextDeltaEvent(text="hi"),
            _done(
                text="hi",
                input_tokens=1,
                output_tokens=1,
                model="deepseek-r1",
                reasoning_content="thinking...",
            ),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
        resolved_model="deepseek-r1",
    ),
    # 7. Non-DeepSeek: reasoning_content omitted
    _Case(
        case_id="non_deepseek_reasoning_omitted",
        events=[
            TextDeltaEvent(text="hi"),
            _done(
                text="hi",
                input_tokens=1,
                output_tokens=1,
                model="claude-opus-4",
                reasoning_content="thinking...",
            ),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 8. DoneEvent with opensquilla_estimate cost source
    _Case(
        case_id="savings_estimate_cost_source",
        events=[
            TextDeltaEvent(text="hi"),
            _done(
                text="hi",
                input_tokens=1,
                output_tokens=1,
                cost_source="opensquilla_estimate",
                cost_usd=0.0012,
            ),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 9. Hallucination passthrough (no impact on finalizer; identical to #1)
    _Case(
        case_id="hallucination_passthrough_no_impact",
        events=[
            TextDeltaEvent(text="I generated an image for you"),
            _done(text="I generated an image for you", input_tokens=1, output_tokens=1),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 10. Multiple text deltas, accumulated
    _Case(
        case_id="multiple_text_deltas",
        events=[
            TextDeltaEvent(text="hi "),
            TextDeltaEvent(text="there"),
            _done(text="hi there", input_tokens=2, output_tokens=2),
        ],
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 11. No-session-manager edge: all writes skipped
    _Case(
        case_id="no_session_manager",
        events=[
            TextDeltaEvent(text="hi"),
            _done(text="hi", input_tokens=1, output_tokens=1),
        ],
        no_session_manager=True,
        expected_transcript_append_count=0,
        expected_memory_capture_calls=0,
        expected_update_calls=0,
        expected_done_present=True,
    ),
    # 12. Memory capture raises -> log-and-continue (call attempted,
    #     exception swallowed, rollup still fires)
    _Case(
        case_id="memory_capture_raises_log_and_continue",
        events=[
            TextDeltaEvent(text="hi"),
            _done(text="hi", input_tokens=1, output_tokens=1),
        ],
        memory_capture_raises=RuntimeError,
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        expected_update_calls=1,
        expected_done_present=True,
    ),
    # 13. Session totals raises -> log-and-continue
    _Case(
        case_id="session_totals_raises_log_and_continue",
        events=[
            TextDeltaEvent(text="hi"),
            _done(text="hi", input_tokens=1, output_tokens=1),
        ],
        session_update_raises=RuntimeError,
        expected_transcript_append_count=1,
        expected_memory_capture_calls=1,
        # update() call is attempted but raises; record_count==0 (raise before append)
        expected_update_calls=0,
        expected_done_present=True,
    ),
]

_CORPUS_IDS = [c.case_id for c in _CORPUS]
_CORPUS_BY_ID = {c.case_id: c for c in _CORPUS}


# ---------------------------------------------------------------------------
# Runner builder
# ---------------------------------------------------------------------------


def _build_runner() -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=_StubModelCatalog(),
        memory_retrievers=None,
        turn_capture_services=None,
        session_flush_service=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _capture_turn_memory_counter() -> list[dict[str, Any]]:
    return []


def _patch_capture_turn_memory(
    runner: TurnRunner,
    *,
    capture_calls: list[dict[str, Any]],
    raises: type[BaseException] | None,
) -> None:
    """Replace ``_capture_turn_memory`` with a recording stub.

    The runtime path calls ``self._capture_turn_memory`` directly; the
    new arm goes through the ``TurnMemoryCapturePort`` adapter which
    forwards to the same method. Patching once is bit-identical across
    both modes.
    """

    async def _stub(
        self,  # noqa: ARG001
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: Any,
        input_provenance: dict[str, Any] | None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
    ) -> None:
        capture_calls.append(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "runtime_message": runtime_message,
                "final_text": final_text,
                "input_mode": input_mode,
                "tool_context": tool_context,
                "input_provenance": input_provenance,
                "run_kind": run_kind,
                "no_memory_capture": no_memory_capture,
            }
        )
        if raises is not None:
            raise raises("recording capture boom")

    runner._capture_turn_memory = _stub.__get__(runner, TurnRunner)


def _setup_runner(monkeypatch: pytest.MonkeyPatch, case: _Case) -> tuple[
    TurnRunner,
    _RecordingSessionManager | None,
    list[dict[str, Any]],
]:
    runner = _build_runner()
    selector = _StubSelector(
        "sel",
        current_model=case.resolved_model,
        resolve_returns=_StubProvider("override-resolved"),
    )
    _patch_resolver(runner, _StubProvider("p"), selector)
    _patch_builder(
        runner,
        [SimpleNamespace(name="t1")],
        object(),
        {"tool_profile": "agent"},
    )
    _patch_ctx_mutators(runner)
    _patch_assemble_prompt(runner, "BASE", {})
    _patch_run_pipeline(
        runner,
        _make_turn_factory(
            metadata={"tool_profile": "agent"},
            tool_defs=[],
        ),
        provider=_StubProvider("post-pipeline"),
    )
    _patch_router_context(runner)
    _patch_resolve_prompt_config(runner, "FINAL", None, None)
    _patch_session_id(runner, "sess-1")
    _patch_budget_resolvers(runner)
    _patch_thinking(runner)
    _patch_memory_helpers(runner)
    _patch_observability(runner)
    _patch_compaction_history(runner)

    # Reinstate ``_persist_turn_error`` after observability patched it
    # to a no-op. needs the real helper to route through the
    # recording session manager so the harness can pin the system-role
    # append count for the error-persist case.
    runner._persist_turn_error = TurnRunner._persist_turn_error.__get__(
        runner, TurnRunner
    )

    # Wire the recording session manager unless the case opts out.
    session_manager: _RecordingSessionManager | None
    if case.no_session_manager:
        session_manager = None
    else:
        session_manager = _RecordingSessionManager(
            memory_capture_raises=case.memory_capture_raises,
            session_update_raises=case.session_update_raises,
        )
    runner._session_manager = session_manager

    # Record memory-capture invocations so the harness can pin call
    # counts + arg shape across modes.
    capture_calls: list[dict[str, Any]] = []
    _patch_capture_turn_memory(
        runner,
        capture_calls=capture_calls,
        raises=case.memory_capture_raises,
    )

    # Private-memory toggle — patch both the runtime_mod reference and the
    # session.keys source (imported lazily by the harness adapter).
    import opensquilla.engine.runtime as runtime_mod
    import opensquilla.session.keys as session_keys_mod

    _pma = lambda session_key: case.private_memory_allowed  # noqa: ARG005, E731
    monkeypatch.setattr(runtime_mod, "allows_private_memory_prompt_injection", _pma)
    monkeypatch.setattr(session_keys_mod, "allows_private_memory_prompt_injection", _pma)

    # Mailbox for the stub agent's deterministic event stream.
    _MAILBOX.events = list(case.events)
    _MAILBOX.raise_after = None
    _MAILBOX.refresh_prompt_calls = []
    monkeypatch.setattr(runtime_mod, "Agent", _StubAgent)
    monkeypatch.setattr("opensquilla.engine.agent.Agent", _StubAgent)

    return runner, session_manager, capture_calls


async def _drive(runner: TurnRunner) -> tuple[list[Any], BaseException | None]:
    yielded: list[Any] = []
    raised: BaseException | None = None
    gen = runner._run_turn(
        message="hello",
        session_key="agent:main:s1",
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=None,
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        history_has_persisted_user=True,
        semantic_message=None,
    )
    try:
        async for event in gen:
            yielded.append(event)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        await gen.aclose()
    return yielded, raised


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _CORPUS_IDS)
@pytest.mark.asyncio
async def test_turn_finalizer_stage_snapshot(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive each corpus case through the unconditional TurnFinalizerStage path."""
    case = _CORPUS_BY_ID[case_id]
    runner, session_manager, capture_calls = _setup_runner(monkeypatch, case)
    yielded, raised = await _drive(runner)

    assert raised is None, f"{case_id} raised: {raised!r}"

    if case.expected_pending_error_code is not None:
        tail = next(e for e in reversed(yielded) if isinstance(e, ErrorEvent))
        assert tail.code == case.expected_pending_error_code

    if case.expected_done_present is not None:
        assert any(isinstance(e, DoneEvent) for e in yielded) is case.expected_done_present, (
            f"{case_id}: DoneEvent presence diverged"
        )

    if session_manager is None:
        # The runner's session_manager is None; no calls observed via the
        # recording wrapper because we never created one. Nothing else to
        # pin.
        return

    assert len(session_manager.append_message_calls) == case.expected_transcript_append_count, (
        f"{case_id}: transcript append count diverged "
        f"({len(session_manager.append_message_calls)} vs "
        f"{case.expected_transcript_append_count})"
    )
    assert len(capture_calls) == case.expected_memory_capture_calls, (
        f"{case_id}: memory capture call count diverged "
        f"({len(capture_calls)} vs {case.expected_memory_capture_calls})"
    )
    assert len(session_manager.persist_error_calls) == case.expected_persist_error_calls, (
        f"{case_id}: persist error call count diverged "
        f"({len(session_manager.persist_error_calls)} vs "
        f"{case.expected_persist_error_calls})"
    )
    assert len(session_manager.update_calls) == case.expected_update_calls, (
        f"{case_id}: session update call count diverged "
        f"({len(session_manager.update_calls)} vs "
        f"{case.expected_update_calls})"
    )

    # Pin the cost rollup arguments per-case when an update was made.
    if case.expected_update_calls == 1:
        upd = session_manager.update_calls[0]
        done = next(
            (event for event in yielded if isinstance(event, DoneEvent)),
            next(event for event in case.events if isinstance(event, DoneEvent)),
        )
        # input_tokens / output_tokens / cache totals come straight from
        # the DoneEvent additive over the zero-baseline session row.
        assert upd["input_tokens"] == done.input_tokens
        assert upd["output_tokens"] == done.output_tokens
        assert upd["total_tokens"] == done.input_tokens + done.output_tokens
        assert upd["total_tokens_fresh"] is True
        assert upd["cache_read"] == done.cached_tokens
        assert upd["cache_write"] == done.cache_write_tokens
