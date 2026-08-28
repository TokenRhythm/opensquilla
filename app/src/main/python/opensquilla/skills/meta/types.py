"""Dataclasses for the Meta-Skill MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteCase:
    """One conditional branch on a MetaStep.

    The orchestrator evaluates ``when`` as a Jinja boolean expression against
    ``inputs`` + ``outputs``; the first truthy case wins and ``to`` overrides
    the step's default skill name. Empty route list ⇒ static behavior.
    """

    when: str
    to: str


#: Supported step execution kinds.
#:
#: * ``agent``         — spawn a one-shot sub-Agent with the named skill's
#:                       SKILL.md body as system prompt. Full tool loop.
#:                       Right for genuinely open-ended steps. (MVP default.)
#: * ``llm_classify``  — single constrained LLM call, no tool loop. The model
#:                       must reply with exactly one of ``output_choices``.
#:                       Cheap & deterministic. Use for routing classifiers,
#:                       label extraction, etc.
#: * ``llm_chat``      — single unconstrained LLM call, no tool loop. Use for
#:                       bounded synthesis steps that should not spawn a full
#:                       sub-Agent or call tools.
#: * ``tool_call``     — direct tool handler invocation, no LLM. The named
#:                       ``tool`` is invoked with ``tool_args`` (Jinja-rendered).
#:                       Use for deterministic side-effects (memory_save,
#:                       file writes, etc.).
StepKind = str  # Literal["agent", "llm_classify", "tool_call"] in annotation


@dataclass(frozen=True)
class MetaStep:
    """One step in a Meta-Skill composition DAG.

    ``kind`` selects the execution mode. ``agent`` is the default and
    preserves MVP behavior (full sub-Agent). ``llm_classify`` and
    ``tool_call`` are lighter-weight executors with their own required
    fields validated at parse time.
    """

    id: str
    skill: str
    with_args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    # Optional Jinja boolean expression evaluated against ``inputs`` and
    # ``outputs`` after dependencies complete. False skips this step while
    # still satisfying downstream ``depends_on`` links with an empty output.
    when: str = ""
    route: tuple[RouteCase, ...] = ()
    # New in B: execution-mode dispatch.
    kind: StepKind = "agent"
    # Required when kind == "llm_classify": the closed set of valid labels.
    output_choices: tuple[str, ...] = ()
    # Required when kind == "tool_call": the tool to invoke and its args
    # (args are Jinja-rendered against ``inputs`` + ``outputs``).
    tool: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    # Optional per-step tool gate (kind == "tool_call" only). Empty
    # tuple = no allowlist (pre-existing behaviour, backwards
    # compatible). When non-empty, the parser cross-validates that
    # ``tool`` is one of these names; the runtime executor also
    # double-checks defensively.
    tool_allowlist: tuple[str, ...] = ()
    # Optional. Names another step in the same plan that should be spawned
    # if this step fails. The substitute's output is mirrored to outputs under
    # THIS step's id, so downstream depends_on links remain satisfied.
    # Empty string = no substitute (DAG fails normally on error).
    on_failure: str = ""
    # Optional side-effect contract. ``external_paid_submit`` marks a
    # non-idempotent provider submission: replay is fail-closed unless the
    # trusted child proves that it failed before any submit was possible.
    side_effect: str = ""
    # New in PR1 (design §6): populated only when kind == "user_input".
    # All other step kinds keep this as None and the executor layer
    # ignores it. Frozen at parse time; the orchestrator never mutates.
    clarify_config: ClarifyStepConfig | None = None
    # New in P0-1: human-readable label for the step ribbon chip.
    # Empty string ⇒ frontend humanizes ``id``.
    label: str = ""
    # Optional localized labels keyed by language bucket (for example
    # ``{"zh": "风险审查", "en": "Risk review"}``).
    label_by_language: dict[str, str] = field(default_factory=dict)
    # New in P0-1: whether the executor may emit per-step ``status_text``
    # updates via the run-progress event channel. ``tool_call`` defaults
    # to False (single deterministic call); ``agent`` / ``skill_exec``
    # default to True; ``llm_chat`` / ``llm_classify`` ignore this flag.
    progress_emits: bool = True


@dataclass(frozen=True)
class ClarifyField:
    """One field in a user_input step's collection schema.

    The user-input schema parser owns the concrete validation contract.
    The validator semantics (min/max for int, max_chars for string, choices
    for enum) are enforced by parser.py and at field-value-collection time;
    this dataclass is the static declaration only.
    """

    name: str
    type: str  # "string" | "enum" | "int" | "bool"
    required: bool = False
    prompt: str = ""
    prompt_by_language: dict[str, str] = field(default_factory=dict)
    choices: tuple[str, ...] = ()
    default: Any = None
    min: int | None = None
    max: int | None = None
    max_chars: int | None = None


@dataclass(frozen=True)
class ClarifyStepConfig:
    """Static schema describing what a user_input step collects.

    All side-effect semantics (skip_if evaluation, cancel detection,
    timeout, nl_extract LLM call) live in the executor and meta_resolution
    layer; this dataclass is the parsed declaration only.
    """

    mode: str  # "form" | "chat"
    fields: tuple[ClarifyField, ...]
    skip_if: str = ""
    cancel_keywords: tuple[str, ...] = ()
    timeout_hours: int = 24
    intro: str = ""
    intro_by_language: dict[str, str] = field(default_factory=dict)
    nl_extract: bool = False
    nl_extract_tier: str = ""  # "" ⇒ lowest configured router tier


class MetaPaused(Exception):  # noqa: N818
    """Control-flow signal raised by the user_input executor.

    Carries enough information to render a clarify form on any surface
    (Web / CLI / IM) without re-loading the SkillSpec from disk.
    Subclasses Exception so it can propagate through asyncio's task
    machinery; the scheduler intercepts it ahead of CancelledError /
    generic Exception per design §8.1.

    NOTE: design §6 declares this as ``@dataclass(frozen=True)``, but
    frozen dataclasses cannot subclass Exception cleanly (the
    ``__init__`` rewrites collide with BaseException.args bookkeeping).
    PR1 implements the same surface as a hand-written class with a
    keyword-only constructor; treat instances as immutable by
    convention.
    """

    __slots__ = (
        "run_id", "step_id", "schema", "intro", "language",
        "confirmed_fields", "prefill_audit",
    )

    def __init__(
        self,
        *,
        run_id: str,
        step_id: str,
        schema: ClarifyStepConfig,
        intro: str = "",
        language: str = "",
        confirmed_fields: dict[str, Any] | None = None,
        prefill_audit: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"meta-skill paused at step {step_id!r}")
        self.run_id = run_id
        self.step_id = step_id
        self.schema = schema
        self.intro = intro
        self.language = language
        # Step (c)/(d): values the prefill scan inferred from earlier
        # context plus the audit payload describing where they came
        # from. Both default to ``None`` so call sites that don't run
        # a prefill scan keep the historical signal unchanged.
        self.confirmed_fields = confirmed_fields
        self.prefill_audit = prefill_audit


@dataclass(frozen=True)
class MetaPreflightRequired:
    """Control payload for a blocking request-template preflight gate."""

    run_id: str
    meta_skill_name: str
    request_template: dict[str, Any]
    interpreted_request: str = ""
    missing_fields: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    can_skip: bool = True
    requires_confirmation: bool = True


@dataclass(frozen=True)
class MetaPlan:
    """Parsed composition plan for a Meta-Skill."""

    name: str
    triggers: tuple[str, ...]
    priority: int
    steps: tuple[MetaStep, ...]
    fallback_body: str = ""
    # How MetaOrchestrator should derive the user-facing
    # ``MetaResult.final_text``:
    #   "auto" (default): post-process step_outputs via a single LLM call
    #     into a short Markdown summary (status + key deliverables + next
    #     step). Adds ~1-2s and ~¥0.001 per DAG run on v4-flash.
    #   "raw": legacy behaviour — return the last non-substitute step's
    #     output verbatim. Use when the last step already produces a
    #     Markdown report (e.g. summarize / deep-research).
    #   "step:<step_id>": return outputs[step_id] verbatim. Use to point
    #     at a specific deliverable step that is not the last.
    final_text_mode: str = "auto"
    # Optional request scaffold used by the P0-2 pre-flight preview
    # surface. Shape is intentionally manifest-owned so individual
    # meta-skills can evolve their fields without a schema migration.
    request_template: dict[str, Any] = field(default_factory=dict)
    # Optional final-answer contract. Deterministic UX layers read this
    # today; future audit/self-repair steps can consume the same manifest
    # field without changing plan persistence.
    output_contract: dict[str, Any] = field(default_factory=dict)
    # Optional P1-4 regression baseline prompts and judge rubrics.
    eval_prompts: list[dict[str, Any]] = field(default_factory=list)
    # Optional P2 preference/policy declarations. These are static authoring
    # metadata only; runtime memory/policy integration remains separate.
    preference_keys: tuple[str, ...] = ()
    policy_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetaMatch:
    """Resolver hit — a plan plus the inputs supplied for this turn."""

    plan: MetaPlan
    inputs: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""


@dataclass
class MetaResult:
    """Outcome of MetaOrchestrator.run().

    ``ok=True`` ⇒ ``final_text`` is the user-facing reply (last step output).
    ``ok=False`` ⇒ caller should fall back to a normal turn with
    ``failed_step_id`` and ``step_outputs`` injected as context.
    """

    ok: bool
    final_text: str = ""
    step_outputs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    failed_step_id: str | None = None
    # New in PR3 (design §8.1, §8.3): pause signal vs failure distinction.
    paused: bool = False
    paused_payload: MetaPaused | MetaPreflightRequired | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
