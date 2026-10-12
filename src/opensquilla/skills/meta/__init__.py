"""Meta-Skill subsystem — orchestrates fixed compositions of regular Skills.

MVP scope: parse SKILL.md ``composition`` block, resolve triggers against
incoming user messages, run steps in topological order via one-shot sub-Agents,
fall back to a normal turn (with the SKILL.md body injected) on any failure.

The orchestrator factories live here (``parser``, ``scheduler``,
``executors``, ``orchestrator``) but the two entry points that wire
this subsystem into the runtime live elsewhere:

* Hard path — ``opensquilla.engine.steps.meta_resolution`` (pipeline
  step) matches triggers and stashes a ``MetaMatch`` on
  ``turn.metadata``. ``TurnRunner._build_event_source`` in
  ``opensquilla.engine.runtime`` then branches into
  ``MetaOrchestrator.iter_events`` and applies the SKILL.md fallback
  if the plan fails.
* Soft path — ``opensquilla.tools.builtin.meta_tools.meta_invoke``
  exposes the orchestrator to the LLM as a callable tool, intercepted
  by ``Agent._run_one_streaming``.

Intentionally out of MVP scope (see docs/proposals/meta-skills/MECHANISM.md
§20 for the full prerequisites list): input-side taint provenance, frozen
ToolContext, sub-turn event routing, DSL semantic lint, Skill-side
outputs_schema contract, persistence, UI.
"""

from __future__ import annotations

from opensquilla.skills.meta.parser import parse_meta_plan, topological_order
from opensquilla.skills.meta.types import (
    MetaMatch,
    MetaPlan,
    MetaResult,
    MetaStep,
    RouteCase,
)

__all__ = [
    "MetaMatch",
    "MetaPlan",
    "MetaResult",
    "MetaStep",
    "RouteCase",
    "parse_meta_plan",
    "topological_order",
]
