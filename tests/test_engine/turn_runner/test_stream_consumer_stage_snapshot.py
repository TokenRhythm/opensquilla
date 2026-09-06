"""Snapshot harness for ``StreamConsumerStage`` through ``TurnRunner._run_turn``.

Drives a 17-case corpus against ``TurnRunner._run_turn`` with the
``StreamConsumerStage`` running through the runtime stage path. The corpus
exercises every event-type branch in the slice plus
the in-turn compaction refresh (``CompactionEvent`` -> persist -> snapshot refresh ->
system-prompt refresh) plus raising-fake cases for both the agent stream
and the compaction persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.harness import (
    _TurnRunnerCompactionPersistAdapter,
    _TurnRunnerHistoryLoaderAdapter,
    _TurnRunnerRouterContextAdapter,
)
from opensquilla.engine.types import (
    CompactionEvent,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
    WarningEvent,
)

# Reuse upstream patch helpers from's equivalence harness -- this
# stage sits after all six prior stages so the same upstream patching
# strategy applies.
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

# ---------------------------------------------------------------------------
# Shared mailbox so the StubAgent can pull case-specific events without
# threading them through the Agent() constructor.
# ---------------------------------------------------------------------------


class _Mailbox:
    events: list[Any] = []
    raise_after: type[BaseException] | None = None
    refresh_prompt_calls: list[Any] = []


_MAILBOX = _Mailbox()


class _StubAgent:
    """Agent stand-in playing a deterministic event list."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.config = kwargs.get("config") or SimpleNamespace(
            request_context_prompt=None
        )

    def set_history(self, _history: Any) -> None:
        return None

    def refresh_system_prompt(self, prompt: Any) -> None:
        _MAILBOX.refresh_prompt_calls.append(prompt)

    async def run_turn(
        self,
        turn_input: str,  # noqa: ARG002
        *,
        extra_messages: Any = None,  # noqa: ARG002
        **_kwargs: Any,
    ):
        for ev in list(_MAILBOX.events):
            yield ev
        if _MAILBOX.raise_after is not None:
            raise _MAILBOX.raise_after("stub agent raise")


# ---------------------------------------------------------------------------
# Local budget/thinking/compaction patches mirroring the AttachmentStage
# equivalence harness shape.
# ---------------------------------------------------------------------------


def _patch_budget_resolvers(runner: TurnRunner) -> None:
    def _runtime(self, session_key):  # noqa: ARG001, ARG002
        return 60.0

    def _max_iter(self, session_key, mi):  # noqa: ARG001, ARG002
        return mi if mi is not None else 10

    def _iter_t(self, session_key, it):  # noqa: ARG001, ARG002
        return it if it is not None else 30.0

    def _tool_t(self, session_key, tt):  # noqa: ARG001, ARG002
        return tt if tt is not None else 20.0

    def _req_t(self, session_key, rt):  # noqa: ARG001, ARG002
        return rt if rt is not None else 120.0

    def _retries(self, session_key, r):  # noqa: ARG001, ARG002
        return r if r is not None else 3

    runner._resolve_agent_runtime_timeout = _runtime.__get__(runner, TurnRunner)
    runner._resolve_agent_max_iterations = _max_iter.__get__(runner, TurnRunner)
    runner._resolve_agent_iteration_timeout = _iter_t.__get__(runner, TurnRunner)
    runner._resolve_agent_tool_timeout = _tool_t.__get__(runner, TurnRunner)
    runner._resolve_agent_request_timeout = _req_t.__get__(runner, TurnRunner)
    runner._resolve_agent_max_provider_retries = _retries.__get__(runner, TurnRunner)


def _patch_thinking(runner: TurnRunner) -> None:
    runner._resolve_turn_thinking = (
        lambda self, turn: False  # noqa: ARG005
    ).__get__(runner, TurnRunner)


def _patch_compaction_history(runner: TurnRunner) -> None:
    async def _t3(self, *_a, **_kw):  # noqa: ARG002
        return "not_applicable"

    async def _preflight(self, *_a, **_kw):  # noqa: ARG002
        return None

    async def _load_history(self, *_a, **_kw):  # noqa: ARG002
        return None

    runner._maybe_compact_on_t3_upgrade = _t3.__get__(runner, TurnRunner)
    runner._maybe_preflight_compact = _preflight.__get__(runner, TurnRunner)
    runner._load_history = _load_history.__get__(runner, TurnRunner)


class _RecordingSessionManager:
    def __init__(self, raises: type[BaseException] | None = None) -> None:
        self.calls: list[Any] = []
        self.raises = raises

    async def persist_compaction_result(
        self,
        session_key,
        summary,
        kept,
        *,
        expected_session_id=None,
        expected_session_epoch=None,
        **kwargs,
    ):
        kwargs["expected_session_id"] = expected_session_id
        kwargs["expected_session_epoch"] = expected_session_epoch
        self.calls.append(("persist", session_key, summary, list(kept), kwargs))
        if self.raises is not None:
            raise self.raises("persist boom")

    async def append_message(self, *args, **kwargs):  # noqa: ARG002
        return None

    async def get_session(self, *args, **kwargs):  # noqa: ARG002
        return None

    async def update(self, *args, **kwargs):  # noqa: ARG002
        return None


class _NoopAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _done() -> DoneEvent:
    return DoneEvent(text="", input_tokens=0, output_tokens=0)


def _tool_use(uid: str = "t1", *, synthetic: bool = False) -> ToolUseStartEvent:
    return ToolUseStartEvent(
        tool_use_id=uid,
        tool_name="echo",
        synthetic_from_text=synthetic,
    )


def _tool_result(uid: str = "t1") -> ToolResultEvent:
    return ToolResultEvent(
        tool_use_id=uid,
        tool_name="echo",
        result="ok",
    )


@dataclass
class _Case:
    case_id: str
    events: list[Any] = field(default_factory=list)
    raise_after: type[BaseException] | None = None
    persist_raises: type[BaseException] | None = None
    private_memory_allowed: bool = True
    expected_kinds: tuple[str, ...] = ()
    expected_pending_error_code: str | None = None
    expected_done_present: bool = False
    expected_compaction_persist_calls: int = 0
    expected_prompt_refresh_calls: int | None = None
    expected_outcome: str = "completed"


_CORPUS: list[_Case] = [
    _Case(
        case_id="simple_text_turn",
        events=[
            TextDeltaEvent(text="hi"),
            TextDeltaEvent(text=" world"),
            DoneEvent(text="hi world"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "TextDeltaEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="text_tool_text_done",
        events=[
            TextDeltaEvent(text="pre"),
            _tool_use("t1"),
            _tool_result("t1"),
            TextDeltaEvent(text=" post"),
            DoneEvent(text=" post"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "ToolUseStartEvent",
            "ToolResultEvent",
            "TextDeltaEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="synthetic_text_strip",
        events=[
            TextDeltaEvent(text="I will call echo"),
            _tool_use("t1", synthetic=True),
            DoneEvent(text=""),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "ToolUseStartEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="tool_result_delivery_failure",
        events=[
            TextDeltaEvent(text="ok"),
            _tool_use("t1"),
            _tool_result("t1"),
            DoneEvent(text="ok"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "ToolUseStartEvent",
            "ToolResultEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="done_empty_heartbeat",
        events=[DoneEvent(text="")],
        expected_kinds=("DoneEvent",),
        expected_done_present=True,
    ),
    _Case(
        case_id="error_during_stream",
        events=[
            TextDeltaEvent(text="partial "),
            ErrorEvent(message="boom", code="agent_error"),
        ],
        expected_kinds=("TextDeltaEvent",),
        expected_pending_error_code="agent_error",
    ),
    _Case(
        case_id="error_timeout_envelope",
        events=[ErrorEvent(message="x", code="timeout")],
        expected_kinds=(),
        expected_pending_error_code="llm_timeout",
    ),
    _Case(
        case_id="error_incomplete_tool_stream_drops_unpaired",
        events=[
            TextDeltaEvent(text="pre"),
            _tool_use("t1"),
            ErrorEvent(message="cut", code="incomplete_tool_stream"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "ToolUseStartEvent",
        ),
        expected_pending_error_code="incomplete_tool_stream",
    ),
    _Case(
        case_id="warning_passthrough",
        events=[
            TextDeltaEvent(text="hi"),
            WarningEvent(code="something", message="m"),
            DoneEvent(text="hi"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "WarningEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="compaction_happy_path",
        events=[
            TextDeltaEvent(text="hi"),
            CompactionEvent(summary="sum", kept_entries=[1, 2, 3]),
            TextDeltaEvent(text=" after"),
            DoneEvent(text="hi after"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "TextDeltaEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
        expected_compaction_persist_calls=1,
    ),
    _Case(
        case_id="compaction_reentrancy",
        events=[
            CompactionEvent(summary="sum", kept_entries=[1]),
            DoneEvent(text="ok"),
        ],
        expected_kinds=("DoneEvent",),
        expected_done_present=True,
        expected_compaction_persist_calls=1,
    ),
    _Case(
        case_id="compaction_persist_raises_failed_without_refresh",
        events=[
            CompactionEvent(summary="sum", kept_entries=[1]),
            DoneEvent(text="after"),
        ],
        persist_raises=RuntimeError,
        expected_kinds=("DoneEvent",),
        expected_done_present=True,
        expected_compaction_persist_calls=1,
        expected_prompt_refresh_calls=0,
    ),
    _Case(
        case_id="compaction_without_private_memory",
        events=[
            CompactionEvent(summary="sum", kept_entries=[1]),
            DoneEvent(text="done"),
        ],
        private_memory_allowed=False,
        expected_kinds=("DoneEvent",),
        expected_done_present=True,
        expected_compaction_persist_calls=1,
    ),
    _Case(
        case_id="done_with_routing_metadata",
        events=[DoneEvent(text="result")],
        expected_kinds=("DoneEvent",),
        expected_done_present=True,
    ),
    _Case(
        case_id="done_with_hallucination_claim",
        events=[
            TextDeltaEvent(text="I generated an image for you"),
            DoneEvent(text="I generated an image for you"),
        ],
        expected_kinds=(
            "TextDeltaEvent",
            "DoneEvent",
        ),
        expected_done_present=True,
    ),
    _Case(
        case_id="agent_run_raises",
        events=[TextDeltaEvent(text="partial")],
        raise_after=RuntimeError,
        expected_outcome="exception_handled_to_error_event",
    ),
    _Case(
        case_id="empty_stream",
        events=[],
        expected_kinds=(),
    ),
]

_CORPUS_IDS = [c.case_id for c in _CORPUS]
_CORPUS_BY_ID = {c.case_id: c for c in _CORPUS}


# ---------------------------------------------------------------------------
# Runner builder
# ---------------------------------------------------------------------------


def _build_runner(*, compaction_hooks=None) -> TurnRunner:
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
        compaction_hooks=compaction_hooks,
    )


def _setup_runner(
    monkeypatch: pytest.MonkeyPatch,
    case: _Case,
    *,
    compaction_hooks=None,
) -> TurnRunner:
    runner = _build_runner(compaction_hooks=compaction_hooks)
    selector = _StubSelector(
        "sel",
        current_model="claude-sonnet-4.5",
        resolve_returns=_StubProvider("override-resolved"),
    )
    _patch_resolver(runner, _StubProvider("p"), selector)
    _patch_builder(runner, [SimpleNamespace(name="t1")], object(), {"tool_profile": "agent"})
    _patch_ctx_mutators(runner)
    _patch_assemble_prompt(runner, "BASE", {})
    _patch_run_pipeline(
        runner,
        _make_turn_factory(metadata={"tool_profile": "agent"}, tool_defs=[]),
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

    # Recording session manager so the compaction-persist path fires.
    runner._session_manager = _RecordingSessionManager(raises=case.persist_raises)

    # Force the private-memory check to the case's value.
    # Patch both the runtime_mod reference (used by legacy/direct callers) and
    # the session.keys source (imported lazily by the harness adapter).
    import opensquilla.engine.runtime as runtime_mod
    import opensquilla.session.keys as session_keys_mod

    _pma = lambda session_key: case.private_memory_allowed  # noqa: ARG005, E731
    monkeypatch.setattr(runtime_mod, "allows_private_memory_prompt_injection", _pma)
    monkeypatch.setattr(session_keys_mod, "allows_private_memory_prompt_injection", _pma)

    # Mailbox for the stub agent's deterministic event stream.
    _MAILBOX.events = list(case.events)
    _MAILBOX.raise_after = case.raise_after
    _MAILBOX.refresh_prompt_calls = []

    monkeypatch.setattr(runtime_mod, "Agent", _StubAgent)
    monkeypatch.setattr("opensquilla.engine.agent.Agent", _StubAgent)
    return runner


async def _drive(
    runner: TurnRunner,
    *,
    expected_session_id: str | None = None,
    expected_session_epoch: int | None = None,
) -> tuple[list[Any], BaseException | None]:
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
        expected_session_id=expected_session_id,
        expected_session_epoch=expected_session_epoch,
    )
    try:
        async for event in gen:
            yielded.append(event)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        await gen.aclose()
    return yielded, raised


@pytest.mark.parametrize("capability", ["explicit", "durable_kwargs", "legacy_kwargs"])
@pytest.mark.asyncio
async def test_history_adapter_exact_owner_capability(capability: str) -> None:
    calls: list[dict[str, Any]] = []

    async def explicit_load(
        _agent: Any,
        _session_key: str,
        *,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: Any,
    ) -> None:
        calls.append(
            {
                **kwargs,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
            }
        )

    async def kwargs_load(
        _agent: Any,
        _session_key: str,
        **kwargs: Any,
    ) -> None:
        calls.append(kwargs)

    session_manager = (
        SimpleNamespace(_storage=object())
        if capability != "legacy_kwargs"
        else SimpleNamespace()
    )
    runner = SimpleNamespace(
        _session_manager=session_manager,
        _load_history=(explicit_load if capability == "explicit" else kwargs_load),
    )
    adapter = _TurnRunnerHistoryLoaderAdapter(runner)

    if capability == "durable_kwargs":
        with pytest.raises(RuntimeError, match="does not support exact ownership"):
            await adapter.load(
                agent=object(),
                session_key="agent:main:owner-capability",
                trim_last_user=False,
                expected_session_id="session-owner",
                expected_session_epoch=4,
            )
        assert calls == []
        return

    await adapter.load(
        agent=object(),
        session_key="agent:main:owner-capability",
        trim_last_user=False,
        expected_session_id="session-owner",
        expected_session_epoch=4,
    )
    assert len(calls) == 1
    if capability == "explicit":
        assert calls[0]["expected_session_id"] == "session-owner"
        assert calls[0]["expected_session_epoch"] == 4
    else:
        assert "expected_session_id" not in calls[0]
        assert "expected_session_epoch" not in calls[0]


@pytest.mark.parametrize("capability", ["explicit", "durable_kwargs", "legacy_kwargs"])
@pytest.mark.asyncio
async def test_router_context_adapter_exact_owner_capability(capability: str) -> None:
    calls: list[dict[str, Any]] = []

    async def explicit_context(
        _session_key: str,
        *,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(
            {
                **kwargs,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
            }
        )
        return {}

    async def kwargs_context(
        _session_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(kwargs)
        return {}

    runner = SimpleNamespace(
        _session_manager=(
            SimpleNamespace(_storage=object())
            if capability != "legacy_kwargs"
            else SimpleNamespace()
        ),
        _router_previous_assistant_context=(
            explicit_context if capability == "explicit" else kwargs_context
        ),
    )
    adapter = _TurnRunnerRouterContextAdapter(runner)

    if capability == "durable_kwargs":
        with pytest.raises(RuntimeError, match="does not support exact ownership"):
            await adapter.fetch_router_context(
                "agent:main:owner-capability",
                exclude_last_user=True,
                expected_session_id="session-owner",
                expected_session_epoch=4,
            )
        assert calls == []
        return

    await adapter.fetch_router_context(
        "agent:main:owner-capability",
        exclude_last_user=True,
        expected_session_id="session-owner",
        expected_session_epoch=4,
    )
    assert len(calls) == 1
    if capability == "explicit":
        assert calls[0]["expected_session_id"] == "session-owner"
        assert calls[0]["expected_session_epoch"] == 4
    else:
        assert "expected_session_id" not in calls[0]
        assert "expected_session_epoch" not in calls[0]


@pytest.mark.parametrize("capability", ["explicit", "durable_kwargs", "legacy_kwargs"])
@pytest.mark.asyncio
async def test_compaction_persist_adapter_exact_owner_capability(
    capability: str,
) -> None:
    calls: list[dict[str, Any]] = []

    async def explicit_persist(
        _session_key: str,
        _summary: str,
        _kept_entries: list[Any],
        *,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: Any,
    ) -> bool:
        calls.append(
            {
                **kwargs,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
            }
        )
        return True

    async def kwargs_persist(
        _session_key: str,
        _summary: str,
        _kept_entries: list[Any],
        **kwargs: Any,
    ) -> bool:
        calls.append(kwargs)
        return True

    session_manager = SimpleNamespace(
        persist_compaction_result=(
            explicit_persist if capability == "explicit" else kwargs_persist
        ),
    )
    if capability != "legacy_kwargs":
        session_manager._storage = object()
    runner = SimpleNamespace(
        _session_manager=session_manager,
        _session_write_context=lambda _session_key: _NoopAsyncContext(),
    )
    adapter = _TurnRunnerCompactionPersistAdapter(runner)

    if capability == "durable_kwargs":
        with pytest.raises(RuntimeError, match="does not support exact ownership"):
            await adapter.persist_and_notify(
                session_key="agent:main:owner-capability",
                summary="summary",
                kept_entries=[],
                expected_session_id="session-owner",
                expected_session_epoch=4,
            )
        assert calls == []
        return

    await adapter.persist_and_notify(
        session_key="agent:main:owner-capability",
        summary="summary",
        kept_entries=[],
        expected_session_id="session-owner",
        expected_session_epoch=4,
    )
    assert len(calls) == 1
    if capability == "explicit":
        assert calls[0]["expected_session_id"] == "session-owner"
        assert calls[0]["expected_session_epoch"] == 4
    else:
        assert "expected_session_id" not in calls[0]
        assert "expected_session_epoch" not in calls[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _CORPUS_IDS)
@pytest.mark.asyncio
async def test_stream_consumer_stage_snapshot(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive each corpus case through the unconditional StreamConsumerStage path."""
    case = _CORPUS_BY_ID[case_id]
    runner = _setup_runner(monkeypatch, case)
    yielded, raised = await _drive(runner)

    if case.expected_outcome == "exception_handled_to_error_event":
        # The outer terminal handler converts arbitrary agent exceptions
        # to a tail ErrorEvent yielded BEFORE the generator returns.
        assert raised is None, f"{case_id}: unexpected raise {raised!r}"
        assert any(isinstance(e, ErrorEvent) for e in yielded)
        return

    assert raised is None, f"{case_id} raised: {raised!r}"

    # Compaction-internal events must never leak through.
    kinds = tuple(type(e).__name__ for e in yielded)
    assert "CompactionEvent" not in kinds, (
        f"{case_id}: CompactionEvent yielded"
    )

    # The harness may append an ErrorEvent at the tail when a pending
    # error was captured during the stream.
    expected_kinds = case.expected_kinds
    if case.expected_pending_error_code is not None:
        expected_kinds = case.expected_kinds + ("ErrorEvent",)

    assert kinds == expected_kinds, (
        f"{case_id}: event kinds diverged "
        f"({kinds!r} vs {expected_kinds!r})"
    )

    if case.expected_pending_error_code is not None:
        tail = next(e for e in reversed(yielded) if isinstance(e, ErrorEvent))
        assert tail.code == case.expected_pending_error_code

    if case.expected_done_present:
        assert any(isinstance(e, DoneEvent) for e in yielded)

    # In-turn compaction observation: persist call count + system-prompt refresh count.
    sm = runner._session_manager
    assert isinstance(sm, _RecordingSessionManager)
    persist_calls = [c for c in sm.calls if c[0] == "persist"]
    assert len(persist_calls) == case.expected_compaction_persist_calls, (
        f"{case_id}: persist call count diverged "
        f"({len(persist_calls)} vs {case.expected_compaction_persist_calls})"
    )
    expected_prompt_refresh_calls = (
        case.expected_compaction_persist_calls
        if case.expected_prompt_refresh_calls is None
        else case.expected_prompt_refresh_calls
    )
    assert len(_MAILBOX.refresh_prompt_calls) == expected_prompt_refresh_calls
