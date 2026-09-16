"""Snapshot regression net for ``InputStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. Drives the corpus through
``TurnRunner._run_turn`` and short-circuits ``TurnRunner._resolve_provider``
to raise a sentinel ``BaseException`` immediately after the input slice
runs; that exception's payload carries the post-slice state for the
snapshot assertion. ``BaseException`` bypasses the ``except Exception``
terminal handler in ``_run_turn``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner

from .test_input_stage_unit import CORPUS, CORPUS_BY_ID, _RecordingSessionAppend, _Snapshot

# ---------------------------------------------------------------------------
# Probe — fires after the input slice, captures the locals, halts the
# generator without touching any downstream stage. Subclasses
# ``BaseException`` so the legacy ``except Exception`` terminal handler in
# ``_run_turn`` does not swallow it.
# ---------------------------------------------------------------------------


class _InputSliceCapture(BaseException):
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot


@dataclass
class _ProbeState:
    fake: _RecordingSessionAppend | None = None


def _make_resolve_provider_probe(state: _ProbeState):
    """Wrap ``_resolve_provider`` so that, when invoked, it climbs one
    frame to read the ``_run_turn`` locals, builds a snapshot, and raises
    ``_InputSliceCapture`` to halt the generator.

    ``_run_turn`` exposes ``runtime_message`` / ``semantic_input`` /
    ``extra_prompt_context`` as locals after the input stage has run.
    The fake's ``calls`` list is the source of truth for ``persisted_call_shape``;
    the fake's recorded return content is the source of truth for
    ``persisted_returned_content``. This way the snapshot shape is
    identical across arms even though ``persisted_entry`` is local to the
    InputStage in the new arm.
    """

    import sys

    def _probe(self):  # noqa: ARG001 - frame inspection drives this
        # Walk up the call stack until we find ``_run_turn``'s frame —
        # identified by the presence of ``runtime_message`` AND
        # ``semantic_input`` AND ``extra_prompt_context`` in ``f_locals``.
        # ``_resolve_provider`` is invoked below ``_run_turn`` through the
        # provider/tools stage adapter, so the probe climbs until it finds
        # the runtime locals it needs.
        frame = sys._getframe(1)
        while frame is not None:
            locs = frame.f_locals
            if (
                "runtime_message" in locs
                and "semantic_input" in locs
                and "extra_prompt_context" in locs
            ):
                break
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("probe could not locate _run_turn frame")
        locs = frame.f_locals
        runtime_message = locs.get("runtime_message", "")
        semantic_input = locs.get("semantic_input", "")
        extra_prompt_context = locs.get("extra_prompt_context", None)

        persisted_call: tuple[str, str, str, dict[str, Any] | None] | None = None
        persisted_returned: str | None = None
        if state.fake is not None and state.fake.calls:
            persisted_call = state.fake.calls[-1]
            # Derive what the fake would have returned (mirrors
            # ``_RecordingSessionAppend.append_message``'s return logic).
            persisted_returned = (
                state.fake.stamped_content
                if state.fake.stamped_content is not None
                else persisted_call[2]
            )

        snapshot = _Snapshot(
            runtime_message=runtime_message,
            semantic_input=semantic_input,
            extra_prompt_context=extra_prompt_context,
            persisted_call_shape=persisted_call,
            persisted_returned_content=persisted_returned,
            raises=None,
        )
        raise _InputSliceCapture(snapshot)

    return _probe


# ---------------------------------------------------------------------------
# TurnRunner construction (minimal — only the deps the input slice needs)
# ---------------------------------------------------------------------------


def _build_turn_runner(session_manager: Any) -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=session_manager,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=None,
        memory_retrievers=None,
        turn_capture_services=None,

        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


@dataclass
class _DriveResult:
    snapshot: _Snapshot | None
    yielded_codes: list[str]
    raised: type[BaseException] | None


async def _drive_input_slice(
    runner: TurnRunner,
    case_kwargs: dict[str, Any],
) -> _DriveResult:
    raised: type[BaseException] | None = None
    captured: _Snapshot | None = None
    yielded_codes: list[str] = []

    gen = runner._run_turn(
        message=case_kwargs["message"],
        session_key=case_kwargs["session_key"],
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=case_kwargs["tool_context"],
        input_mode=case_kwargs["input_mode"],
        persist_input=case_kwargs["persist_input"],
        input_provenance=case_kwargs["input_provenance"],
        semantic_message=case_kwargs["semantic_message"],
    )
    try:
        async for event in gen:
            code = getattr(event, "code", None)
            if code is not None:
                yielded_codes.append(str(code))
    except _InputSliceCapture as capture:
        captured = capture.snapshot
    except BaseException as exc:  # noqa: BLE001 - want to record propagation type
        raised = type(exc)
    finally:
        await gen.aclose()

    return _DriveResult(snapshot=captured, yielded_codes=yielded_codes, raised=raised)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [c.case_id for c in CORPUS])
@pytest.mark.asyncio
async def test_input_stage_snapshot(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CORPUS_BY_ID[case_id]
    no_port = case.session_behavior.get("no_port", False)
    fake: _RecordingSessionAppend | None = None
    if not no_port:
        fake = _RecordingSessionAppend(
            stamped_content=case.session_behavior.get("stamped_content"),
            raises=case.session_behavior.get("raises"),
        )

    runner = _build_turn_runner(session_manager=fake)

    state = _ProbeState(fake=fake)
    monkeypatch.setattr(
        TurnRunner,
        "_resolve_provider",
        _make_resolve_provider_probe(state),
    )

    result = await _drive_input_slice(runner, case.inp_kwargs)

    if case.expected.raises is not None:
        # Raising-port case: ConnectionError fires inside the input slice.
        # The terminal ``except Exception`` catches it and re-invokes
        # ``session_manager.append_message`` to persist a ``"Error: ..."``
        # system message — which our fake also raises on, producing a
        # SECOND recorded call.
        assert fake is not None
        # First call: the input-slice persist attempt (matches expected).
        assert fake.calls[0] == case.expected.persisted_call_shape, (
            f"case={case_id}: first call diverged.\n"
            f"  expected={case.expected.persisted_call_shape}\n"
            f"  actual  ={fake.calls[0]}"
        )
        # The terminal handler fires a second append for ``role="system"``
        # carrying the ``Error: ...`` payload.
        assert len(fake.calls) >= 2, (
            f"case={case_id}: terminal handler did not "
            f"re-invoke session_manager.append_message; calls={fake.calls}"
        )
        assert fake.calls[1][1] == "system"
        assert fake.calls[1][2].startswith("Error: ")
        # The whole turn surfaces as a yielded error event.
        assert "ConnectionError" in result.yielded_codes or any(
            c.lower().endswith("error") for c in result.yielded_codes
        ) or result.raised is not None, (
            f"case={case_id}: expected error surface; "
            f"yielded_codes={result.yielded_codes} raised={result.raised}"
        )
        return

    # Happy path: probe must have fired, snapshot must match expected.
    assert result.snapshot is not None, (
        f"case={case_id}: probe never fired; "
        f"raised={result.raised} yielded_codes={result.yielded_codes}"
    )
    assert result.snapshot == case.expected, (
        f"case={case_id}: snapshot diverged.\n"
        f"  expected={case.expected}\n  actual  ={result.snapshot}"
    )
