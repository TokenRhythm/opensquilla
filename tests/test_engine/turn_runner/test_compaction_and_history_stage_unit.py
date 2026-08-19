"""Unit tests for ``CompactionAndHistoryStage`` driven directly (no full
TurnRunner stack).

Drives a 13-case corpus through ``CompactionAndHistoryStage.run`` with
four recording fakes (one per port) plus a recording ``CompactionHook``.
Raising fakes exercise both the hook-isolation contract and the
exception-propagation contract without the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.hooks.types import CompactionState
from opensquilla.engine.turn_runner.compaction_and_history_stage import (
    CompactionAndHistoryStage,
    CompactionAndHistoryStageInput,
)
from opensquilla.engine.turn_runner.outcome import StageOutcome

# ---------------------------------------------------------------------------
# Recording fakes (one per port + one CompactionHook)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingT3:
    return_value: str = "not_applicable"
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def maybe_compact(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording t3 boom")
        return self.return_value


@dataclass
class _RecordingPreflight:
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def maybe_compact(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording preflight boom")
        return None


@dataclass
class _RecordingHistoryLoader:
    return_value: str | None = None
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def load(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording history loader boom")
        return self.return_value


@dataclass
class _RecordingPrepender:
    return_value: str | None | object = object()
    calls: list[dict[str, Any]] = field(default_factory=list)

    def prepend(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        if isinstance(self.return_value, object) and not isinstance(
            self.return_value, str | type(None)
        ):
            # Default: replicate the production helper output for assert parity
            existing = kwargs.get("existing")
            prepended = kwargs.get("prepended")
            if not prepended or not prepended.strip():
                return existing
            if not existing or not existing.strip():
                return prepended.strip()
            return f"{prepended.strip()}\n\n{existing.strip()}"
        return self.return_value  # type: ignore[return-value]


@dataclass
class _RecordingCompactionHook:
    name: str = "rec-hook"
    before_raises: type[BaseException] | None = None
    after_raises: type[BaseException] | None = None
    events: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def before_compact(self, state: CompactionState) -> None:
        self.events.append(("before", state.extra.get("phase", ""), None))
        if self.before_raises is not None:
            raise self.before_raises("hook before boom")

    async def after_compact(self, state: CompactionState, outcome: Any) -> None:
        outcome_payload = dict(outcome) if isinstance(outcome, dict) else None
        self.events.append(("after", state.extra.get("phase", ""), outcome_payload))
        if self.after_raises is not None:
            raise self.after_raises("hook after boom")


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_agent_stub(
    *,
    request_context_prompt: str | None = None,
) -> Any:
    """Build a minimal Agent-shape with the attributes the stage reads."""
    return SimpleNamespace(
        config=SimpleNamespace(request_context_prompt=request_context_prompt),
    )


def _make_input(
    *,
    agent: Any | None = None,
    request_context_prompt: str | None = None,
    session_key: str = "agent:main:s1",
    agent_id: str = "agent:main",
    history_has_persisted_user: bool = True,
    bound_user_message_id: str | None = None,
    context_window_tokens: int = 200_000,
    skip_compaction: bool = False,
    transcript_snapshot: Any | None = None,
) -> CompactionAndHistoryStageInput:
    if agent is None:
        agent = _make_agent_stub(request_context_prompt=request_context_prompt)
    return CompactionAndHistoryStageInput(
        agent=agent,
        context_window_tokens=context_window_tokens,
        provider=SimpleNamespace(name="prov"),
        resolved_model="claude-sonnet-4.5",
        turn=SimpleNamespace(metadata={}, model=""),
        session_key=session_key,
        agent_id=agent_id,
        history_has_persisted_user=history_has_persisted_user,
        bound_user_message_id=bound_user_message_id,
        skip_compaction=skip_compaction,
        transcript_snapshot=transcript_snapshot,
    )


def _make_stage(
    *,
    t3: _RecordingT3 | None = None,
    preflight: _RecordingPreflight | None = None,
    history: _RecordingHistoryLoader | None = None,
    prepender: _RecordingPrepender | None = None,
    hooks: tuple[Any, ...] = (),
) -> tuple[
    CompactionAndHistoryStage,
    _RecordingT3,
    _RecordingPreflight,
    _RecordingHistoryLoader,
    _RecordingPrepender,
]:
    t3 = t3 or _RecordingT3()
    preflight = preflight or _RecordingPreflight()
    history = history or _RecordingHistoryLoader()
    prepender = prepender or _RecordingPrepender()
    stage = CompactionAndHistoryStage(
        t3_upgrade=t3,
        preflight=preflight,
        history_loader=history,
        request_context_prepender=prepender,
        compaction_hooks=hooks,
    )
    return stage, t3, preflight, history, prepender


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_not_applicable_falls_through_to_preflight() -> None:
    stage, t3, preflight, history, prepender = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        history=_RecordingHistoryLoader(return_value=None),
    )
    inp = _make_input()
    outcome = await stage.run(inp)

    assert isinstance(outcome, StageOutcome)
    assert outcome.terminate is False
    assert outcome.output is not None
    assert outcome.output.t3_upgrade_status == "not_applicable"
    assert outcome.output.preflight_invoked is True
    assert outcome.output.compaction_summary_context is None
    assert outcome.output.final_request_context_prompt is None
    assert len(t3.calls) == 1
    assert len(preflight.calls) == 1
    assert len(history.calls) == 1
    assert len(prepender.calls) == 1


@pytest.mark.asyncio
async def test_t3_handled_skips_preflight() -> None:
    stage, t3, preflight, history, _ = _make_stage(
        t3=_RecordingT3(return_value="handled"),
    )
    outcome = await stage.run(_make_input())

    assert outcome.output.t3_upgrade_status == "handled"
    assert outcome.output.preflight_invoked is False
    assert len(t3.calls) == 1
    assert len(preflight.calls) == 0
    assert len(history.calls) == 1


@pytest.mark.asyncio
async def test_explicit_skip_bypasses_compaction_but_still_loads_history() -> None:
    hook = _RecordingCompactionHook()
    stage, t3, preflight, history, prepender = _make_stage(hooks=(hook,))

    outcome = await stage.run(_make_input(skip_compaction=True))

    assert outcome.output.t3_upgrade_status == "skipped"
    assert outcome.output.preflight_invoked is False
    assert t3.calls == []
    assert preflight.calls == []
    assert len(history.calls) == 1
    assert len(prepender.calls) == 1
    assert hook.events == []


@pytest.mark.asyncio
async def test_t3_compact_failed_skips_preflight() -> None:
    stage, t3, preflight, _, _ = _make_stage(
        t3=_RecordingT3(return_value="compact_failed"),
    )
    outcome = await stage.run(_make_input())

    assert outcome.output.t3_upgrade_status == "compact_failed"
    assert outcome.output.preflight_invoked is False
    assert len(preflight.calls) == 0


@pytest.mark.asyncio
async def test_t3_flush_failed_falls_through_to_preflight() -> None:
    stage, t3, preflight, _, _ = _make_stage(
        t3=_RecordingT3(return_value="flush_failed"),
    )
    outcome = await stage.run(_make_input())

    assert outcome.output.t3_upgrade_status == "flush_failed"
    assert outcome.output.preflight_invoked is True
    assert len(preflight.calls) == 1


@pytest.mark.asyncio
async def test_history_loader_returns_summary_context() -> None:
    stage, _, _, history, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUMMARY1"),
    )
    outcome = await stage.run(_make_input(request_context_prompt="EXISTING"))

    assert outcome.output.compaction_summary_context == "SUMMARY1"
    # Default prepender replicates production: "<prepended>\n\n<existing>"
    assert outcome.output.final_request_context_prompt == "SUMMARY1\n\nEXISTING"
    assert prepender.calls[0]["existing"] == "EXISTING"
    assert prepender.calls[0]["prepended"] == "SUMMARY1"


@pytest.mark.asyncio
async def test_history_loader_called_with_trim_last_user_true() -> None:
    stage, _, _, history, _ = _make_stage()
    inp = _make_input(history_has_persisted_user=True)
    await stage.run(inp)
    assert history.calls[0]["trim_last_user"] is True


@pytest.mark.asyncio
async def test_history_loader_called_with_trim_last_user_false() -> None:
    stage, _, _, history, _ = _make_stage()
    inp = _make_input(history_has_persisted_user=False)
    await stage.run(inp)
    assert history.calls[0]["trim_last_user"] is False


@pytest.mark.asyncio
async def test_active_prompt_binding_is_forwarded_to_both_compaction_ports() -> None:
    stage, t3, preflight, _, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
    )

    await stage.run(
        _make_input(
            history_has_persisted_user=True,
            bound_user_message_id="active-user-message",
        )
    )

    for call in (t3.calls[0], preflight.calls[0]):
        assert call["history_has_persisted_user"] is True
        assert call["bound_user_message_id"] == "active-user-message"


@pytest.mark.asyncio
async def test_turn_transcript_snapshot_is_shared_across_all_reading_ports() -> None:
    stage, t3, preflight, history, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
    )
    transcript_snapshot = SimpleNamespace()

    await stage.run(_make_input(transcript_snapshot=transcript_snapshot))

    assert t3.calls[0]["transcript_snapshot"] is transcript_snapshot
    assert preflight.calls[0]["transcript_snapshot"] is transcript_snapshot
    assert history.calls[0]["transcript_snapshot"] is transcript_snapshot


@pytest.mark.asyncio
async def test_compaction_hook_fires_around_both_calls() -> None:
    hook = _RecordingCompactionHook()
    stage, _, _, _, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        hooks=(hook,),
    )
    await stage.run(_make_input())

    kinds = [(kind, phase) for kind, phase, _ in hook.events]
    assert kinds == [
        ("before", "t3_upgrade"),
        ("after", "t3_upgrade"),
        ("before", "preflight"),
        ("after", "preflight"),
    ]
    # After-compact outcome dict carries status
    after_t3 = next(p for k, ph, p in hook.events if k == "after" and ph == "t3_upgrade")
    assert after_t3 == {"status": "not_applicable"}
    after_preflight = next(
        p for k, ph, p in hook.events if k == "after" and ph == "preflight"
    )
    assert after_preflight == {"status": "ran"}


@pytest.mark.asyncio
async def test_compaction_hook_only_fires_t3_when_handled() -> None:
    hook = _RecordingCompactionHook()
    stage, _, _, _, _ = _make_stage(
        t3=_RecordingT3(return_value="handled"),
        hooks=(hook,),
    )
    await stage.run(_make_input())

    kinds = [(kind, phase) for kind, phase, _ in hook.events]
    assert kinds == [("before", "t3_upgrade"), ("after", "t3_upgrade")]


@pytest.mark.asyncio
async def test_raising_before_compact_hook_is_isolated() -> None:
    """Hook isolation contract: a hook that raises MUST NOT break the turn."""
    hook = _RecordingCompactionHook(before_raises=RuntimeError)
    stage, t3, preflight, _, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        hooks=(hook,),
    )
    # Should NOT raise.
    outcome = await stage.run(_make_input())
    assert outcome.output.t3_upgrade_status == "not_applicable"
    assert outcome.output.preflight_invoked is True
    assert len(t3.calls) == 1
    assert len(preflight.calls) == 1


@pytest.mark.asyncio
async def test_raising_after_compact_hook_is_isolated() -> None:
    hook = _RecordingCompactionHook(after_raises=RuntimeError)
    stage, t3, preflight, _, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        hooks=(hook,),
    )
    outcome = await stage.run(_make_input())
    assert outcome.output.t3_upgrade_status == "not_applicable"
    assert len(t3.calls) == 1
    assert len(preflight.calls) == 1


@pytest.mark.asyncio
async def test_history_loader_exception_propagates() -> None:
    """HistoryLoader exceptions propagate; the stage does not catch them."""
    stage, _, _, _, _ = _make_stage(
        history=_RecordingHistoryLoader(raises=RuntimeError),
    )
    with pytest.raises(RuntimeError):
        await stage.run(_make_input())


@pytest.mark.asyncio
async def test_t3_exception_propagates() -> None:
    """T3 port exceptions propagate (the helper handles its own swallow)."""
    stage, _, preflight, _, _ = _make_stage(t3=_RecordingT3(raises=RuntimeError))
    with pytest.raises(RuntimeError):
        await stage.run(_make_input())
    # Preflight never runs because t3 raised first
    assert len(preflight.calls) == 0


@pytest.mark.asyncio
async def test_preflight_exception_propagates() -> None:
    stage, _, _, history, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        preflight=_RecordingPreflight(raises=RuntimeError),
    )
    with pytest.raises(RuntimeError):
        await stage.run(_make_input())
    # History loader never runs because preflight raised first
    assert len(history.calls) == 0


@pytest.mark.asyncio
async def test_prepender_receives_existing_and_prepended() -> None:
    stage, _, _, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUM"),
    )
    inp = _make_input(request_context_prompt="EXIST")
    await stage.run(inp)
    assert prepender.calls == [{"existing": "EXIST", "prepended": "SUM"}]


@pytest.mark.asyncio
async def test_prepender_when_summary_is_none() -> None:
    stage, _, _, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value=None),
    )
    inp = _make_input(request_context_prompt="EXIST")
    out = await stage.run(inp)
    assert prepender.calls == [{"existing": "EXIST", "prepended": None}]
    # The default prepender mirrors production: when prepended is None, the
    # existing string is returned unchanged.
    assert out.output.final_request_context_prompt == "EXIST"


@pytest.mark.asyncio
async def test_stage_does_not_mutate_agent_config() -> None:
    """Harness owns the agent.config.request_context_prompt mutation."""
    agent = _make_agent_stub(request_context_prompt="EXISTING")
    stage, _, _, _, _ = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUMMARY"),
    )
    inp = _make_input(agent=agent)
    outcome = await stage.run(inp)
    # Agent.config.request_context_prompt unchanged by the stage; harness
    # applies output.final_request_context_prompt afterwards.
    assert agent.config.request_context_prompt == "EXISTING"
    assert outcome.output.final_request_context_prompt == "SUMMARY\n\nEXISTING"


@pytest.mark.asyncio
async def test_compaction_state_threshold_uses_context_window_tokens() -> None:
    seen_states: list[CompactionState] = []

    class _CapturingHook:
        name = "cap"

        async def before_compact(self, state: CompactionState) -> None:
            seen_states.append(state)

        async def after_compact(
            self, state: CompactionState, outcome: Any
        ) -> None:  # noqa: ARG002
            return None

    stage, _, _, _, _ = _make_stage(
        t3=_RecordingT3(return_value="not_applicable"),
        hooks=(_CapturingHook(),),
    )
    await stage.run(_make_input(context_window_tokens=123_456))

    assert len(seen_states) == 2
    for s in seen_states:
        assert s.threshold_tokens == 123_456
        assert s.session_key == "agent:main:s1"
        assert s.agent_id == "agent:main"
    assert seen_states[0].extra == {"phase": "t3_upgrade"}
    assert seen_states[1].extra == {"phase": "preflight"}
