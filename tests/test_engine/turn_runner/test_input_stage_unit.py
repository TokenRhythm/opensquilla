"""Unit tests for ``InputStage`` driven directly (no full TurnRunner stack).

Drives the 8-case corpus from the design through ``InputStage.run``
in isolation, plus a 9th case that exercises the
``persist_input=True with session_append=None`` short-circuit. Includes a
raising-port case so the propagation contract is exercised even without
the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.turn_runner.input_stage import (
    ExtraContextResolver,
    InputStage,
    InputStageInput,
    InputStageOutput,
    SessionAppendPort,
)
from opensquilla.tools.types import CallerKind, ToolContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_INTERNAL_EVENT_MODE_VALUE = (
    "The next input is an internal scheduler event, not a human user"
    " message. Treat it as system-originated context."
)
_GOAL_CONTINUATION_MODE_VALUE = (
    "The next input is an internal Goal continuation. It is an actionable work"
    " round, not a heartbeat or status poll. Inspect the durable Goal, make"
    " concrete progress, and use update_goal when the Goal is complete or"
    " blocked. Do not emit NO_REPLY or HEARTBEAT_OK for this input."
)
_SUBAGENT_TASK_PROTOCOL_VALUE = (
    "You are a spawned subagent. Execute only the delegated task and return "
    "a compact result for the parent agent to use. Prefer a direct answer; "
    "call tools only when the task explicitly requires external state, files, "
    "network data, or tool output. If the delegated task asks you to reply with "
    "an exact phrase, only reply, output a sentinel token, or avoid explanation, "
    "Do not call tools and return exactly that requested text. Do not treat "
    "uppercase sentinel-like strings as shell commands, filenames, or config keys."
)


@dataclass
class _StubEntry:
    """Minimal stand-in for ``TranscriptEntry`` — the stage only reads
    ``content`` for stamp pickup."""

    content: str | None


@dataclass
class _RecordingSessionAppend:
    """Records every ``append_message`` call. Optionally raises."""

    stamped_content: str | None = None
    raises: type[BaseException] | None = None
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> _StubEntry:
        self.calls.append((session_key, role, content, provenance))
        if self.raises is not None:
            raise self.raises("recording session append boom")
        # The stamped_content overrides what SessionManager would return.
        return _StubEntry(
            content=self.stamped_content if self.stamped_content is not None else content
        )


class _StaticExtraContextResolver(ExtraContextResolver):
    """Replicates ``TurnRunner._extra_context_for_tool_context`` and
    ``TurnRunner._merge_extra_prompt_context`` exactly."""

    def extra_context_for(self, ctx: ToolContext | None) -> dict[str, str]:
        if ctx is None or ctx.caller_kind is not CallerKind.SUBAGENT:
            return {}
        return {"Subagent Task Protocol": _SUBAGENT_TASK_PROTOCOL_VALUE}

    def merge(
        self,
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None:
        if not extra:
            return base
        if base is None:
            return dict(extra)
        merged = dict(base)
        merged.update(extra)
        return merged


@pytest.fixture
def stage() -> InputStage:
    return InputStage(extra_ctx=_StaticExtraContextResolver())


@dataclass(frozen=True)
class _Snapshot:
    runtime_message: str
    semantic_input: str
    extra_prompt_context: dict[str, str] | None
    persisted_call_shape: tuple[str, str, str, dict[str, Any] | None] | None
    persisted_returned_content: str | None
    raises: type[BaseException] | None


def _snapshot(
    out: InputStageOutput | None,
    fake: _RecordingSessionAppend | None,
    raised: type[BaseException] | None,
) -> _Snapshot:
    persisted_call = fake.calls[-1] if fake is not None and fake.calls else None
    persisted_returned = (
        out.persisted_entry.content
        if out is not None and out.persisted_entry is not None
        else None
    )
    return _Snapshot(
        runtime_message=out.runtime_message if out is not None else "",
        semantic_input=out.semantic_input if out is not None else "",
        extra_prompt_context=out.extra_prompt_context if out is not None else None,
        persisted_call_shape=persisted_call,
        persisted_returned_content=persisted_returned,
        raises=raised,
    )


def _subagent_tool_context() -> ToolContext:
    return ToolContext(caller_kind=CallerKind.SUBAGENT, agent_id="sub")


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


@dataclass
class _Case:
    case_id: str
    inp_kwargs: dict[str, Any]
    session_behavior: dict[str, Any]
    expected: _Snapshot


_PROVENANCE = {"kind": "test"}


def _build_corpus() -> list[_Case]:
    cases: list[_Case] = []

    # Case 1 — base path, no persist
    cases.append(
        _Case(
            case_id="user_no_persist_no_semantic",
            inp_kwargs=dict(
                message="hello",
                semantic_message=None,
                input_mode="user",
                persist_input=False,
                input_provenance=None,
                session_key="agent:main:s1",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message="hello",
                semantic_input="hello",
                extra_prompt_context=None,
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 2 — semantic override
    cases.append(
        _Case(
            case_id="user_no_persist_with_semantic",
            inp_kwargs=dict(
                message="hello",
                semantic_message="semantic-x",
                input_mode="user",
                persist_input=False,
                input_provenance=None,
                session_key="agent:main:s2",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message="hello",
                semantic_input="semantic-x",
                extra_prompt_context=None,
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 3 — system_event wrap + Internal Event Mode dict
    cases.append(
        _Case(
            case_id="system_event_no_persist_no_semantic",
            inp_kwargs=dict(
                message="event-payload",
                semantic_message=None,
                input_mode="system_event",
                persist_input=False,
                input_provenance=None,
                session_key="agent:main:s3",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message="[INTERNAL SYSTEM EVENT]\nevent-payload",
                semantic_input="event-payload",
                extra_prompt_context={"Internal Event Mode": _INTERNAL_EVENT_MODE_VALUE},
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 4 — system_event + semantic_message ignored, semantic_input is raw
    cases.append(
        _Case(
            case_id="system_event_no_persist_with_semantic_ignored",
            inp_kwargs=dict(
                message="event",
                semantic_message="sem",
                input_mode="system_event",
                persist_input=False,
                input_provenance=None,
                session_key="agent:main:s4",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message="[INTERNAL SYSTEM EVENT]\nevent",
                semantic_input="event",
                extra_prompt_context={"Internal Event Mode": _INTERNAL_EVENT_MODE_VALUE},
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 5 — persist + stamp pickup (user role)
    cases.append(
        _Case(
            case_id="user_persist_with_stamp_pickup",
            inp_kwargs=dict(
                message="hello",
                semantic_message=None,
                input_mode="user",
                persist_input=True,
                input_provenance=_PROVENANCE,
                session_key="agent:main:s5",
                tool_context=None,
            ),
            session_behavior={"stamped_content": "[T] hello"},
            expected=_Snapshot(
                runtime_message="[T] hello",
                semantic_input="hello",
                extra_prompt_context=None,
                persisted_call_shape=("agent:main:s5", "user", "hello", _PROVENANCE),
                persisted_returned_content="[T] hello",
                raises=None,
            ),
        )
    )

    # Case 6 — persist_input=True but empty message → skip persist
    cases.append(
        _Case(
            case_id="user_persist_empty_message_skips",
            inp_kwargs=dict(
                message="",
                semantic_message=None,
                input_mode="user",
                persist_input=True,
                input_provenance=None,
                session_key="agent:main:s6",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message="",
                semantic_input="",
                extra_prompt_context=None,
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 7 — persist with role="system" (system_event), NO stamp pickup
    cases.append(
        _Case(
            case_id="system_event_persist_no_stamp_pickup",
            inp_kwargs=dict(
                message="alert",
                semantic_message=None,
                input_mode="system_event",
                persist_input=True,
                input_provenance=None,
                session_key="agent:main:s7",
                tool_context=None,
            ),
            # Even if SessionManager stamps a system-role row, the stage MUST
            # NOT pick it up — system_event role is excluded by the inline
            # body's gating condition.
            session_behavior={"stamped_content": "[T] alert"},
            expected=_Snapshot(
                runtime_message="[INTERNAL SYSTEM EVENT]\nalert",
                semantic_input="alert",
                extra_prompt_context={"Internal Event Mode": _INTERNAL_EVENT_MODE_VALUE},
                persisted_call_shape=("agent:main:s7", "system", "alert", None),
                persisted_returned_content="[T] alert",
                raises=None,
            ),
        )
    )

    # Case 8 — subagent caller_kind + RAISING port → propagation
    cases.append(
        _Case(
            case_id="user_persist_subagent_raising",
            inp_kwargs=dict(
                message="hello",
                semantic_message=None,
                input_mode="user",
                persist_input=True,
                input_provenance=None,
                session_key="agent:main:s8",
                tool_context=_subagent_tool_context(),
            ),
            session_behavior={"raises": ConnectionError},
            expected=_Snapshot(
                runtime_message="",
                semantic_input="",
                extra_prompt_context=None,
                persisted_call_shape=("agent:main:s8", "user", "hello", None),
                persisted_returned_content=None,
                raises=ConnectionError,
            ),
        )
    )

    # Case 9 — persist_input=True with session_append=None (short-circuit)
    cases.append(
        _Case(
            case_id="user_persist_no_session_manager_short_circuit",
            inp_kwargs=dict(
                message="hello",
                semantic_message=None,
                input_mode="user",
                persist_input=True,
                input_provenance=None,
                session_key="agent:main:s9",
                tool_context=None,
            ),
            # Sentinel: no session_append at all.
            session_behavior={"no_port": True},
            expected=_Snapshot(
                runtime_message="hello",
                semantic_input="hello",
                extra_prompt_context=None,
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    # Case 10 — Goal continuation is hidden but user-like/actionable.
    cases.append(
        _Case(
            case_id="goal_continuation_is_actionable",
            inp_kwargs=dict(
                message="Inspect the Goal and continue the remaining work.",
                semantic_message="ignored semantic override",
                input_mode="goal_continuation",
                persist_input=False,
                input_provenance=None,
                session_key="agent:main:s10",
                tool_context=None,
            ),
            session_behavior={},
            expected=_Snapshot(
                runtime_message=(
                    "[GOAL CONTINUATION]\n"
                    "Inspect the Goal and continue the remaining work."
                ),
                semantic_input="Inspect the Goal and continue the remaining work.",
                extra_prompt_context={"Goal Continuation Mode": _GOAL_CONTINUATION_MODE_VALUE},
                persisted_call_shape=None,
                persisted_returned_content=None,
                raises=None,
            ),
        )
    )

    return cases


CORPUS = _build_corpus()
CORPUS_BY_ID = {case.case_id: case for case in CORPUS}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [c.case_id for c in CORPUS])
@pytest.mark.asyncio
async def test_input_stage_unit(case_id: str, stage: InputStage) -> None:
    case = CORPUS_BY_ID[case_id]

    no_port = case.session_behavior.get("no_port", False)
    fake: _RecordingSessionAppend | None = None
    if not no_port:
        fake = _RecordingSessionAppend(
            stamped_content=case.session_behavior.get("stamped_content"),
            raises=case.session_behavior.get("raises"),
        )

    inp = InputStageInput(
        session_append=fake,
        **case.inp_kwargs,
    )

    raised: type[BaseException] | None = None
    out: InputStageOutput | None = None
    try:
        out = await stage.run(inp)
    except BaseException as exc:  # noqa: BLE001 - want to catch propagated types
        raised = type(exc)

    snapshot = _snapshot(out, fake, raised)
    assert snapshot == case.expected


def test_session_append_port_is_runtime_checkable() -> None:
    """Sanity: structural conformance is detectable at runtime."""

    fake = _RecordingSessionAppend()
    assert isinstance(fake, SessionAppendPort)


def test_input_stage_name_constant() -> None:
    """Stage exposes the discoverable name the harness loop logs."""

    assert InputStage.name == "input_stage"
