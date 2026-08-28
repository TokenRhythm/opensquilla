"""Internal event types used between the meta-orchestrator scheduler and its
step executors.

:class:`_StepDone` terminates a step's streaming sub-iterator and carries
the step's final string output back through the same channel as
forwarded ``AgentEvent``\\s, so executors do not need a side-channel
(mutable holder / instance variable) to return text. The scheduler
strips ``_StepDone`` before forwarding the outer stream to callers;
consumers never see it.

:func:`yield_skill_view_preface` is the pre-step ``skill_view`` tool
invocation emitted before ``skill_exec`` / ``agent`` steps so the UI
can show the loaded skill body inline as a tool-call card. Lives here
(not in ``executors/``) because it's a UI affordance, not a step body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass as _dataclass
from typing import Any

from opensquilla.engine.types import AgentEvent, ToolResultEvent, ToolUseStartEvent


@_dataclass(frozen=True)
class _StepDone:
    """Internal sentinel — terminates a step's streaming sub-iterator.

    Not a public event type; the orchestrator strips these before forwarding
    the outer stream to callers. Carrying the text inline avoids needing a
    side-channel (mutable holder / instance variable) to communicate the
    step's final string output back to ``iter_events``.
    """

    text: str
    status: str = "ok"


@_dataclass(frozen=True)
class _FailoverTriggered:
    """Internal sentinel — a step failed but declared an ``on_failure``
    substitute.

    Pushed by ``_run_one`` after emitting the failing step's
    ``ToolResultEvent`` so the scheduler's main loop can dispatch the
    substitute step rather than cascade to plan-level failure. The
    substitute's output is later mirrored into the failed step's output
    slot so downstream ``depends_on`` consumers unblock as if the
    original had succeeded.

    Not a public event type; the orchestrator does not forward these to
    callers.
    """

    failed_step_id: str
    substitute_step_id: str
    error: str


async def yield_skill_view_preface(
    step_id: str,
    effective_skill: str,
    *,
    tool_invoker: Callable[[str, dict[str, Any]], Awaitable[str]] | None,
) -> AsyncIterator[AgentEvent]:
    """Invoke the **real** ``skill_view`` tool before each skill-loading step.

    This is not a synthetic UI hint: it routes through the parent turn's
    registered ``skill_view`` tool via ``tool_invoker`` so the call goes
    through the normal tool boundary (audit log, sandbox checks, usage
    tracking) and the ``result`` is whatever ``skill_view`` actually
    returned — not a pre-computed preview of ``SKILL.md``.

    When the tool invoker is not wired (degraded mode used by some tests),
    the preface is skipped silently — the step executor still runs and
    will surface its own loader error if the skill is missing.
    """

    if tool_invoker is None:
        return

    sv_use_id = f"meta_skill_view_{step_id}"
    sv_tool_name = "skill_view"
    yield ToolUseStartEvent(
        tool_use_id=sv_use_id,
        tool_name=sv_tool_name,
    )
    try:
        result_text = await tool_invoker(
            sv_tool_name,
            {"name": effective_skill},
        )
    except Exception as exc:  # noqa: BLE001 — surface as an error card.
        yield ToolResultEvent(
            tool_use_id=sv_use_id,
            tool_name=sv_tool_name,
            result=str(exc),
            is_error=True,
            arguments={"name": effective_skill},
        )
        return

    yield ToolResultEvent(
        tool_use_id=sv_use_id,
        tool_name=sv_tool_name,
        result=result_text,
        is_error=False,
        arguments={"name": effective_skill},
    )


__all__ = ["_FailoverTriggered", "_StepDone", "yield_skill_view_preface"]
