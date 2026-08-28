"""Typed multi-step wizard state machine.

This module supplies the typed schemas and in-memory registry backing the
``wizard.start`` / ``wizard.next`` / ``wizard.cancel`` / ``wizard.status`` RPC
handlers. Gateway adapters should call this application boundary rather than
owning wizard state directly.

Design notes
------------
* Each :class:`WizardField` is a frozen, typed description of a single input
  (``text``/``int``/``bool``/``select``/``password``). Fields carry their
  own ``required`` flag, optional ``choices`` list, and an optional default.
* A :class:`WizardStep` is an ordered bundle of fields plus the id of the
  step that follows. Terminal steps encode that via ``next_step_id=None``.
* A :class:`WizardSession` tracks the live state of a running wizard: the
  wizard type, the step cursor, accumulated answers, and an ``answers``
  dict keyed by field name.
* :class:`WizardRegistry` is a deliberately small, in-memory state machine
  — persistence across restarts is out of scope for now.
* :data:`WIZARD_DEFINITIONS` registers every concrete wizard type. The
  initial set ships one: ``onboard_agent``, a three-step onboarding flow.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Literal

# Valid field types accepted by the wire schema. The operator UI/clients use
# this to pick the correct input widget (e.g. a password input vs. a select
# dropdown). Keeping this as a Literal pins the surface type statically.
WizardFieldType = Literal["text", "int", "bool", "select", "password"]


@dataclass(slots=True)
class WizardField:
    """A single input descriptor inside a :class:`WizardStep`."""

    name: str
    label: str
    field_type: WizardFieldType
    required: bool = False
    choices: list[str] | None = None
    default: str | int | bool | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire-format dict used by RPC responses."""
        return {
            "name": self.name,
            "label": self.label,
            "fieldType": self.field_type,
            "required": self.required,
            "choices": list(self.choices) if self.choices is not None else None,
            "default": self.default,
            "description": self.description,
        }


@dataclass(slots=True)
class WizardStep:
    """An ordered bundle of fields plus a pointer to the next step id.

    Terminal steps set ``next_step_id=None``.
    """

    step_id: str
    title: str
    description: str
    fields: list[WizardField]
    next_step_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire-format dict used by RPC responses."""
        return {
            "stepId": self.step_id,
            "title": self.title,
            "description": self.description,
            "fields": [f.to_dict() for f in self.fields],
            "nextStepId": self.next_step_id,
        }


@dataclass(slots=True)
class WizardSession:
    """Live state of a running wizard instance."""

    wizard_id: str
    wizard_type: str
    current_step_id: str
    answers: dict[str, str | int | bool]
    started_at: int  # epoch milliseconds
    completed: bool = False

    def to_dict(self, total_steps: int) -> dict[str, object]:
        """Serialize to the wire-format dict used by wizard.status."""
        return {
            "wizardId": self.wizard_id,
            "wizardType": self.wizard_type,
            "currentStepId": self.current_step_id,
            "totalSteps": total_steps,
            "startedAt": self.started_at,
            "completed": self.completed,
        }


# ---------------------------------------------------------------------------
# Concrete wizard definitions
# ---------------------------------------------------------------------------

# The onboard_agent wizard onboards a new agent persona across three steps:
# identity → system prompt → model defaults. Each step is terminal-or-next
# via ``next_step_id``; the final step points at ``None``.
_ONBOARD_AGENT_STEPS: list[WizardStep] = [
    WizardStep(
        step_id="agent_identity",
        title="Agent Identity",
        description="Name the agent and give it an optional display label.",
        fields=[
            WizardField(
                name="agent_name",
                label="Agent ID",
                field_type="text",
                required=True,
                description="Lowercase slug used as the internal agent id (e.g. 'cora').",
            ),
            WizardField(
                name="display_name",
                label="Display Name",
                field_type="text",
                required=False,
                description="Human-facing name shown in chat UI. Defaults to agent_name.",
            ),
        ],
        next_step_id="system_prompt",
    ),
    WizardStep(
        step_id="system_prompt",
        title="System Prompt & Persona",
        description="Define how the agent behaves.",
        fields=[
            WizardField(
                name="system_prompt",
                label="System Prompt",
                field_type="text",
                required=True,
                description="Long-form instructions the LLM sees at the top of every turn.",
            ),
            WizardField(
                name="persona_tone",
                label="Persona Tone",
                field_type="select",
                required=False,
                choices=["professional", "casual", "friendly"],
                default="professional",
                description="Conversational register used when no tone override applies.",
            ),
        ],
        next_step_id="defaults",
    ),
    WizardStep(
        step_id="defaults",
        title="Model Defaults",
        description="Pick the default provider/model and sampling temperature.",
        fields=[
            WizardField(
                name="default_model",
                label="Default Model",
                field_type="select",
                required=True,
                choices=[
                    "anthropic/claude-3-5-sonnet",
                    "anthropic/claude-3-5-haiku",
                    "openai/gpt-4o",
                    "openai/gpt-4o-mini",
                ],
                description="Provider/model pair used when a turn does not override it.",
            ),
            WizardField(
                name="temperature",
                label="Temperature",
                field_type="int",
                required=False,
                default=7,
                description="Sampling temperature on a 0-10 scale (7 ~ 0.7 in provider units).",
            ),
        ],
        next_step_id=None,
    ),
]


WIZARD_DEFINITIONS: dict[str, list[WizardStep]] = {
    "onboard_agent": _ONBOARD_AGENT_STEPS,
}


# ---------------------------------------------------------------------------
# Registry + state machine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AdvanceOutcome:
    """Return-shape for :meth:`WizardRegistry.advance`."""

    next_step: WizardStep | None
    completed: bool
    result: dict[str, object] | None


class WizardRegistry:
    """In-memory session store backing the wizard.* RPC handlers.

    Sessions are keyed by a short random ``wizard_id`` and live only for the
    duration of the process — persistence is out of scope for now.

    Methods
    -------
    start(wizard_type)
        Create a new session. Raises ``ValueError`` if ``wizard_type`` is
        unknown. Returns ``(wizard_id, first_step)``.
    advance(wizard_id, answers)
        Validate ``answers`` against the current step's required fields,
        record them, and advance the cursor. Raises ``KeyError`` if
        ``wizard_id`` is unknown, ``ValueError`` naming the missing fields
        if any required field is blank. Returns
        ``(next_step_or_None, completed, result_or_None)``.
    cancel(wizard_id)
        Drop the session. Raises ``KeyError`` if unknown.
    status(wizard_id)
        Return the live :class:`WizardSession`. Raises ``KeyError`` if
        unknown.
    total_steps(wizard_type)
        Helper exposing the static step count for a wizard type.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, WizardSession] = {}

    # -- lifecycle -------------------------------------------------------

    def start(self, wizard_type: str) -> tuple[str, WizardStep]:
        steps = WIZARD_DEFINITIONS.get(wizard_type)
        if steps is None:
            raise ValueError(f"unknown wizard_type: {wizard_type}")
        wizard_id = uuid.uuid4().hex[:8]
        first_step = steps[0]
        session = WizardSession(
            wizard_id=wizard_id,
            wizard_type=wizard_type,
            current_step_id=first_step.step_id,
            answers={},
            started_at=int(time.time() * 1000),
            completed=False,
        )
        self._sessions[wizard_id] = session
        return wizard_id, first_step

    def advance(
        self,
        wizard_id: str,
        answers: dict[str, str | int | bool],
    ) -> _AdvanceOutcome:
        session = self._sessions.get(wizard_id)
        if session is None:
            raise KeyError(f"unknown wizard_id: {wizard_id}")
        if session.completed:
            raise ValueError(f"wizard already completed: {wizard_id}")

        step = self._step_by_id(session.wizard_type, session.current_step_id)
        missing = [
            f.name for f in step.fields if f.required and self._is_blank(answers.get(f.name))
        ]
        if missing:
            raise ValueError(
                f"missing required field(s) for step {step.step_id}: {', '.join(missing)}"
            )

        # Record this step's answers (validated) against the session. Fields
        # omitted from ``answers`` fall back to their declared ``default`` if
        # one is set and the session has not already captured a value — the
        # final ``result`` dict therefore reflects both caller-supplied input
        # and schema-declared defaults.
        for f in step.fields:
            if f.name in answers:
                session.answers[f.name] = answers[f.name]
            elif f.default is not None and f.name not in session.answers:
                session.answers[f.name] = f.default

        if step.next_step_id is None:
            session.completed = True
            session.current_step_id = step.step_id  # keep cursor on terminal
            return _AdvanceOutcome(
                next_step=None,
                completed=True,
                result={
                    "wizardType": session.wizard_type,
                    "answers": dict(session.answers),
                },
            )

        next_step = self._step_by_id(session.wizard_type, step.next_step_id)
        session.current_step_id = next_step.step_id
        return _AdvanceOutcome(next_step=next_step, completed=False, result=None)

    def cancel(self, wizard_id: str) -> None:
        if wizard_id not in self._sessions:
            raise KeyError(f"unknown wizard_id: {wizard_id}")
        del self._sessions[wizard_id]

    def status(self, wizard_id: str) -> WizardSession:
        session = self._sessions.get(wizard_id)
        if session is None:
            raise KeyError(f"unknown wizard_id: {wizard_id}")
        return session

    def total_steps(self, wizard_type: str) -> int:
        steps = WIZARD_DEFINITIONS.get(wizard_type)
        if steps is None:
            raise ValueError(f"unknown wizard_type: {wizard_type}")
        return len(steps)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _step_by_id(wizard_type: str, step_id: str) -> WizardStep:
        steps = WIZARD_DEFINITIONS[wizard_type]
        for step in steps:
            if step.step_id == step_id:
                return step
        raise KeyError(f"unknown step_id {step_id!r} for wizard_type {wizard_type!r}")

    @staticmethod
    def _is_blank(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_registry: WizardRegistry = WizardRegistry()


def get_wizard_registry() -> WizardRegistry:
    """Return the process-wide :class:`WizardRegistry` singleton."""

    return _registry


def reset_wizard_registry() -> None:
    """Clear the singleton's in-memory sessions. Intended for tests."""

    global _registry
    _registry = WizardRegistry()


__all__ = [
    "WIZARD_DEFINITIONS",
    "WizardField",
    "WizardFieldType",
    "WizardRegistry",
    "WizardSession",
    "WizardStep",
    "get_wizard_registry",
    "reset_wizard_registry",
]
