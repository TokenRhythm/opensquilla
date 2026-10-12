"""DAG-parallel scheduler for MetaOrchestrator.

Topologically orders the plan, dispatches each ready step as its own
``asyncio.Task``, drains a shared event queue, preserves per-step
ordering (``ToolUseStartEvent → [skill_view + nested events] →
ToolResultEvent``), short-circuits on failure (cancel siblings + emit
synthetic close-brackets for already-opened steps), and yields one
terminal :class:`MetaResult`.

The two executor-shaped callables (``dispatch_step_stream`` for the
per-step body, ``yield_skill_view_preface`` for the optional pre-step
``skill_view`` tool invocation) are injected by the orchestrator
facade so the scheduler stays decoupled from the concrete executors.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

import structlog

from opensquilla.engine.types import (
    AgentEvent,
    MetaPreflightEvent,
    MetaRunAnnouncedEvent,
    MetaRunCompletedEvent,
    MetaStepStateEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from opensquilla.engine.usage import usage_scope
from opensquilla.meta_preflight_protocol import strip_preflight_confirmation_protocol_text
from opensquilla.skills.meta.events import _FailoverTriggered, _StepDone
from opensquilla.skills.meta.parser import topological_order
from opensquilla.skills.meta.replay_safety import (
    PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
    PAID_SUBMISSION_MAYBE_ACCEPTED,
    PAID_SUBMISSION_RECEIPT,
    PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
    PAID_SUBMISSION_SAFE_NO_SUBMIT,
    encode_paid_submission_dispositions,
    encode_paid_submission_receipt_proofs,
    is_external_paid_step,
    paid_receipt_proof,
    paid_replay_is_safe,
    public_step_error,
)
from opensquilla.skills.meta.templating import (
    evaluate_when,
    render_with_args,
    resolve_route,
)
from opensquilla.skills.meta.types import (
    MetaMatch,
    MetaPaused,
    MetaPreflightRequired,
    MetaResult,
    MetaStep,
)

log = structlog.get_logger(__name__)

# Surface-side hint copy that ships in every clarify_skip_summary so a
# renderer always has a sensible default label. Surfaces are free to
# replace it with localised copy via the ``label`` key.
_CLARIFY_SKIP_DEFAULT_LABEL = (
    "We inferred the answers below from earlier context. "
    "Confirm to continue, or reply 'change <field>' to redo."
)
_CLARIFY_SKIP_LABEL_BY_LANGUAGE = {
    "zh": "已从前文推断出下面信息。确认即可继续；如需修改，请回复“修改 <字段>”。",
    "en": _CLARIFY_SKIP_DEFAULT_LABEL,
}
_CLARIFY_SKIP_HINT_BY_LANGUAGE = {
    "zh": "回复“修改 <字段>”重新填写",
    "en": "reply 'change <field>' to redo",
}

# How many characters of each upstream context excerpt to ship to the
# surface. Bounded so a 4 KB step output doesn't bloat every tool
# result. Surfaces can still request more via a follow-up if needed.
_CLARIFY_SKIP_EXCERPT_CHARS = 600
_RESCUE_OUTPUT_EXCERPT_CHARS = 600
_PRIVATE_MACHINE_OUTPUT_KEYS = frozenset(
    {
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
    }
)


def _field_has_value(inputs: dict[str, Any], name: str) -> bool:
    value = inputs.get(name)
    if value is not None and str(value).strip():
        return True
    collected = inputs.get("collected")
    if not isinstance(collected, dict):
        return False
    for item in collected.values():
        if isinstance(item, dict):
            nested = item.get(name)
            if nested is not None and str(nested).strip():
                return True
    return False


def _preflight_missing_fields(
    request_template: dict[str, Any],
    inputs: dict[str, Any],
) -> list[str]:
    fields = request_template.get("fields", [])
    if not isinstance(fields, list):
        return []
    missing: list[str] = []
    for field in fields:
        if not isinstance(field, dict) or field.get("required") is not True:
            continue
        name = field.get("name")
        if isinstance(name, str) and name and not _field_has_value(inputs, name):
            missing.append(name)
    return missing


def _language_suffix(language: object) -> str:
    return "zh" if str(language).lower().startswith("zh") else "en"


def _localized_scalar(mapping: dict[str, Any], key: str, language: str) -> Any:
    suffix = _language_suffix(language)
    for candidate in (f"{key}_{suffix}", f"{key}_{suffix.upper()}"):
        value = mapping.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return value
    value = mapping.get(key)
    if isinstance(value, dict):
        localized = value.get(suffix) or value.get(suffix.upper()) or value.get("default")
        if isinstance(localized, str) and localized.strip():
            return localized.strip()
        if isinstance(localized, list):
            return localized
    return value


def _localized_request_template(
    request_template: dict[str, Any],
    language: str,
) -> dict[str, Any]:
    localized = dict(request_template)
    for key in ("outcome", "deliverable", "title", "intro"):
        value = _localized_scalar(request_template, key, language)
        if isinstance(value, str) and value.strip():
            localized[key] = value

    assumptions = _preflight_assumptions(request_template, language)
    if assumptions:
        localized["assumptions"] = assumptions

    fields = request_template.get("fields", [])
    if isinstance(fields, list):
        localized_fields: list[Any] = []
        for field in fields:
            if not isinstance(field, dict):
                localized_fields.append(field)
                continue
            item = dict(field)
            for key in ("label", "title", "prompt", "description", "help", "hint", "default"):
                value = _localized_scalar(field, key, language)
                if isinstance(value, str) and value.strip():
                    item[key] = value
            localized_fields.append(item)
        localized["fields"] = localized_fields
    return localized


def _localized_step_label(step: MetaStep, language: str) -> str:
    if not str(language).strip():
        return step.label
    suffix = _language_suffix(language)
    localized = step.label_by_language.get(suffix)
    if localized:
        return localized
    return step.label


def _preflight_assumptions(
    request_template: dict[str, Any],
    language: str = "",
) -> list[str]:
    raw = _localized_scalar(request_template, "assumptions", language)
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _preflight_is_confirmed(inputs: dict[str, Any], expected_run_id: str) -> bool:
    marker_present = bool(
        inputs.get("meta_preflight_confirmed")
        or inputs.get("preflight_confirmed")
        or inputs.get("_meta_preflight_confirmed")
    )
    if not marker_present:
        return False
    confirmed_run_id = str(
        inputs.get("meta_preflight_run_id")
        or inputs.get("preflight_run_id")
        or inputs.get("_meta_preflight_run_id")
        or ""
    ).strip()
    return bool(confirmed_run_id and confirmed_run_id == expected_run_id)


def _preflight_requires_confirmation(request_template: dict[str, Any]) -> bool:
    mode = str(request_template.get("mode") or "").strip().lower()
    if (
        request_template.get("preflight_required") is False
        or request_template.get("requires_confirmation") is False
        or mode in {"preview", "soft", "advisory", "informational"}
    ):
        return False
    return bool(
        request_template.get("preflight_required") is True
        or request_template.get("requires_confirmation") is True
        or mode in {"confirm", "confirmation", "required"}
    )


def _failure_hint(error: str) -> dict[str, str]:
    text = error.lower()
    missing_dependency_markers = (
        "not found",
        "no such file",
        "missing dependency",
        "command not found",
        "executable",
        "wkhtmltopdf",
        "ffmpeg",
        "playwright",
    )
    auth_markers = (
        "unauthorized",
        "forbidden",
        "permission denied",
        "401",
        "403",
        "api key",
        "token",
        "credential",
    )
    if any(marker in text for marker in missing_dependency_markers):
        return {
            "category": "missing_dependency",
            "message": "A required local tool or runtime dependency appears to be missing.",
        }
    if any(marker in text for marker in auth_markers):
        return {
            "category": "auth_or_permission",
            "message": "The step appears blocked by authentication or permissions.",
        }
    if "timeout" in text or "timed out" in text:
        return {
            "category": "timeout",
            "message": "The step timed out; retrying with narrower scope may help.",
        }
    return {
        "category": "runtime_error",
        "message": "The step failed at runtime; partial outputs may still be useful.",
    }


def _failure_rescue_payload(
    *,
    plan_name: str,
    step: MetaStep,
    error: str,
    outputs: dict[str, str],
    has_substitute: bool,
    plan_has_external_paid_steps: bool = False,
) -> dict[str, Any]:
    hint = _failure_hint(error)
    unsafe_paid_replay = is_external_paid_step(step) and not paid_replay_is_safe(error)
    if unsafe_paid_replay:
        actions = [
            {
                "id": "review-paid-submit",
                "label": "Review paid submission",
                "description": (
                    "The provider may already have accepted or billed this request. "
                    "Check provider history before starting another generation."
                ),
            },
            {
                "id": "switch-meta-skill",
                "label": "Switch meta-skill",
                "description": "Use a different meta-skill without repeating this paid submit.",
            },
        ]
    else:
        actions = [
            {
                "id": "retry-run",
                "label": "Retry this run",
                "description": "Run the same meta-skill again with the original request.",
            },
            {
                "id": "retry-step",
                "label": "Retry failed step",
                "description": (
                    "Retry only the failed step with successful prior outputs as context."
                ),
            },
            {
                "id": "retry-with-partial-context",
                "label": "Retry with partial context",
                "description": "Reuse successful prior step outputs as context for the retry.",
            },
            {
                "id": "switch-meta-skill",
                "label": "Switch meta-skill",
                "description": "Use a different meta-skill if this was the wrong workflow.",
            },
        ]
    if plan_has_external_paid_steps:
        actions = [action for action in actions if action["id"] != "retry-run"]
    if hint["category"] == "missing_dependency":
        actions.append({
            "id": "install-dependency",
            "label": "Install missing dependency",
            "description": "Install the missing local tool or runtime dependency, then retry.",
        })
    if step.kind == "skill_exec" or "artifact" in step.id.lower():
        actions.append({
            "id": "continue-text-only",
            "label": "Continue without artifact",
            "description": "Keep successful text outputs and skip generated-file delivery.",
        })
    public_outputs = {
        step_id: text
        for step_id, text in outputs.items()
        if step_id not in _PRIVATE_MACHINE_OUTPUT_KEYS
    }
    prior_outputs: list[dict[str, Any]] = []
    for step_id, text in sorted(public_outputs.items()):
        excerpt = str(text or "")
        truncated = len(excerpt) > _RESCUE_OUTPUT_EXCERPT_CHARS
        if truncated:
            excerpt = excerpt[:_RESCUE_OUTPUT_EXCERPT_CHARS] + "...[truncated]"
        prior_outputs.append({
            "step_id": step_id,
            "excerpt": excerpt,
            "truncated": truncated,
        })
    return {
        "meta_skill_name": plan_name,
        "failed_step_id": step.id,
        "failed_step_label": step.label or step.id,
        "failed_step_kind": step.kind,
        "partial_output_step_ids": sorted(public_outputs),
        "prior_outputs": prior_outputs,
        "has_substitute": has_substitute,
        "hint": hint,
        "actions": actions,
    }


def _build_clarify_skip_summary(
    step: MetaStep,
    inputs: dict[str, Any],
    outputs: dict[str, str],
) -> dict[str, Any] | None:
    """Build a transparency payload for a ``when:``-skipped user_input
    step (the "trust gap" close-out from the (c)/(d) protocol).

    Returns ``None`` for non-user_input skipped steps so the surface
    only renders a card when the gap is real. The payload includes:

    * ``label``           — default copy the surface can localise.
    * ``step_id``         — the skipped step's id so the surface can
                            attribute the card.
    * ``fields``          — the field names the user *would* have been
                            asked for, including each field's prompt
                            and required flag. Lets the surface render
                            "destination (required): city" rows even
                            though no values were collected.
    * ``inferred_from``   — bounded excerpts of the upstream step
                            outputs the meta author's ``when:``
                            expression keyed off. The user can read
                            these to verify the system inferred their
                            intent correctly.
    * ``trigger_message`` — the original user message that launched
                            the meta-skill (capped). Lets the surface
                            show "you said: ..." attribution.
    * ``hint_action``     — short ``"reply: change"`` snippet a CLI
                            surface can echo as a one-liner.
    """
    if step.kind != "user_input":
        return None
    cfg = getattr(step, "clarify_config", None)
    if cfg is None:
        return None

    language = _language_suffix(inputs.get("user_language", ""))
    field_payload: list[dict[str, Any]] = []
    for field in cfg.fields:
        entry: dict[str, Any] = {
            "name": field.name,
            "required": bool(field.required),
        }
        prompt = field.prompt_by_language.get(language) or field.prompt
        if prompt:
            entry["prompt"] = prompt
        field_payload.append(entry)

    inferred_from: list[dict[str, str]] = []
    for upstream_id in step.depends_on:
        text = outputs.get(upstream_id, "")
        if not isinstance(text, str) or not text.strip():
            continue
        excerpt = text.strip()
        if len(excerpt) > _CLARIFY_SKIP_EXCERPT_CHARS:
            excerpt = excerpt[:_CLARIFY_SKIP_EXCERPT_CHARS] + "...[truncated]"
        inferred_from.append({"step": upstream_id, "excerpt": excerpt})

    trigger_message = ""
    raw_message = inputs.get("user_message")
    if isinstance(raw_message, str) and raw_message.strip():
        visible_message = strip_preflight_confirmation_protocol_text(raw_message) or raw_message
        trigger_message = visible_message.strip()
        if len(trigger_message) > _CLARIFY_SKIP_EXCERPT_CHARS:
            trigger_message = trigger_message[:_CLARIFY_SKIP_EXCERPT_CHARS] + "...[truncated]"

    return {
        "label": _CLARIFY_SKIP_LABEL_BY_LANGUAGE[language],
        "step_id": step.id,
        "fields": field_payload,
        "inferred_from": inferred_from,
        "trigger_message": trigger_message,
        "hint_action": _CLARIFY_SKIP_HINT_BY_LANGUAGE[language],
    }


def _default_status_text(step: MetaStep, effective_skill: str) -> str:
    """Default ``status_text`` per step kind (design §3.3).

    The WebUI ribbon shows this string under the currently running chip.
    Returned per-kind so the scheduler does not duplicate prompt-shaped
    copy at each emission site.
    """
    if step.kind == "llm_chat":
        return "起草中…"
    if step.kind == "llm_classify":
        return "分类中…"
    if step.kind == "agent":
        return f"调用 {effective_skill} 中…"
    if step.kind == "skill_exec":
        return f"执行 {effective_skill} 中…"
    if step.kind == "tool_call":
        return f"调用 {step.tool}…"
    if step.kind == "user_input":
        return "等待你回复表单"
    return "运行中…"


async def run_dag(
    match: MetaMatch,
    *,
    dispatch_step_stream: Callable[
        [MetaStep, str, dict[str, Any], dict[str, str]],
        AsyncIterator[AgentEvent | _StepDone],
    ],
    yield_skill_view_preface: Callable[
        [str, str], AsyncIterator[AgentEvent],
    ],
    max_parallelism: int | None = None,
    on_step_begin: Callable[[str, str, dict[str, Any]], Awaitable[None]]
    | None = None,
    on_step_finish: Callable[
        [str, str, str | None, str | None], Awaitable[None],
    ]
    | None = None,
    on_step_failover: Callable[[str, str, str], Awaitable[None]] | None = None,
    usage_tracker: Any | None = None,
    session_key: str | None = None,
    usage_scope_prefix: str | None = None,
    seed_outputs: dict[str, str] | None = None,
    replay_failover_aliases: dict[str, str] | None = None,
    trusted_preflight_replay: bool = False,
) -> AsyncIterator[AgentEvent | MetaResult]:
    """Run the plan and stream a flat sequence of events for the UI.

    DAG-parallel scheduler (M7): steps whose ``depends_on`` is satisfied
    run concurrently; events from different steps interleave in arrival
    order. Per-step ordering is preserved:
    ``ToolUseStartEvent → [skill_view + nested events] → ToolResultEvent``.

    Failure of any step cancels all in-flight sibling tasks and yields
    one terminal ``MetaResult(ok=False)``.

    ``max_parallelism``: optional concurrency cap. ``None`` (default) is
    unbounded — every step whose deps are satisfied is spawned
    immediately. An integer ``N`` limits the in-flight task pool to at
    most ``N``; any extra ready steps stay queued in ``unstarted`` and
    are picked up on the next ``_spawn_ready()`` (called after each
    ``_StepDone``). Guardrails fan-out for meta-skills with many
    independent steps so we don't fan token usage past provider rate
    limits.

    The three optional lifecycle callbacks let external observers
    (e.g. ``MetaRunWriter``) record per-step state without patching
    scheduler internals:

    * ``on_step_begin(step_id, effective_skill, rendered_inputs)`` fires
      just before the step's ``dispatch_step_stream`` is invoked.
    * ``on_step_finish(step_id, status, output_text, error)`` fires when
      ``_StepDone`` is consumed; ``status`` is currently always ``"ok"``
      (the failover path uses ``on_step_failover`` instead, and hard
      failures with no substitute surface via the terminal
      ``MetaResult(ok=False)``).
    * ``on_step_failover(failed_step_id, substitute_step_id, error)``
      fires alongside the existing ``_FailoverTriggered`` consumption.

    Callback exceptions are swallowed and logged at warning level —
    observer bugs must never break the scheduler.
    """
    outputs: dict[str, str] = dict(seed_outputs) if seed_outputs else {}
    # These slots belong to the scheduler, never to caller-provided replay
    # seeds. They are refreshed below from parent-observed executor outcomes.
    # In particular, receipt proofs are current-process-only and must never be
    # reconstructed from persisted output or workspace sidecars.
    for private_key in _PRIVATE_MACHINE_OUTPUT_KEYS:
        outputs.pop(private_key, None)
    try:
        ordered = list(topological_order(match.plan.steps))
    except Exception as exc:  # noqa: BLE001
        log.warning("meta_orchestrator.plan_topo_failed", error=str(exc))
        yield MetaResult(ok=False, error=f"plan topology error: {exc}")
        return

    if not ordered:
        yield MetaResult(ok=True, final_text="", step_outputs={})
        return

    if any(step.id in _PRIVATE_MACHINE_OUTPUT_KEYS for step in ordered):
        yield MetaResult(
            ok=False,
            error="meta-skill plan uses a reserved runtime step id",
        )
        return

    paid_submission_dispositions: dict[str, str] = {
        step.id: PAID_SUBMISSION_RECEIPT
        for step in ordered
        if is_external_paid_step(step) and step.id in outputs
    }
    paid_submission_receipt_proofs: dict[str, str] = {}

    def _refresh_paid_submission_runtime_outputs() -> None:
        outputs[PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY] = (
            encode_paid_submission_dispositions(paid_submission_dispositions)
        )
        outputs[PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY] = (
            encode_paid_submission_receipt_proofs(paid_submission_receipt_proofs)
        )

    def _record_paid_submission_disposition(step_id: str, disposition: str) -> None:
        paid_submission_dispositions[step_id] = disposition
        _refresh_paid_submission_runtime_outputs()

    def _record_paid_submission_receipt_proof(step_id: str, proof: str) -> None:
        paid_submission_receipt_proofs[step_id] = proof
        _refresh_paid_submission_runtime_outputs()

    # Keep both channels present even before the first paid step. Trusted
    # templates therefore receive stable JSON objects, never caller values.
    _refresh_paid_submission_runtime_outputs()

    def _public_step_outputs() -> dict[str, str]:
        return {
            key: value
            for key, value in outputs.items()
            if key not in _PRIVATE_MACHINE_OUTPUT_KEYS
        }

    # Announce the static composition so the WebUI can seed its step
    # ribbon before any per-step tool-call event arrives. The orchestrator
    # passes its persisted run_id when available; direct/unit callers may
    # still rely on the local fallback for non-persistent runs.
    _run_id = getattr(match, "run_id", "") or f"{match.plan.name}:{id(match)}"
    template_language = str(match.inputs.get("user_language") or "").strip()
    if match.plan.request_template:
        request_template = _localized_request_template(
            match.plan.request_template,
            template_language,
        )
        missing_fields = _preflight_missing_fields(
            match.plan.request_template,
            match.inputs,
        )
        assumptions = _preflight_assumptions(
            match.plan.request_template,
            template_language,
        )
        requires_confirmation = _preflight_requires_confirmation(
            match.plan.request_template,
        )
        can_skip = trusted_preflight_replay or not (
            requires_confirmation and missing_fields
        )
        yield MetaPreflightEvent(
            run_id=_run_id,
            meta_skill_name=match.plan.name,
            request_template=request_template,
            interpreted_request=str(match.inputs.get("user_message") or "").strip(),
            missing_fields=missing_fields,
            assumptions=assumptions,
            can_skip=can_skip,
            requires_confirmation=requires_confirmation,
        )
        preflight_confirmed = (
            trusted_preflight_replay
            or _preflight_is_confirmed(match.inputs, _run_id)
        )
        if requires_confirmation and (missing_fields or not preflight_confirmed):
            yield MetaResult(
                ok=False,
                paused=True,
                paused_payload=MetaPreflightRequired(
                    run_id=_run_id,
                    meta_skill_name=match.plan.name,
                    request_template=request_template,
                    interpreted_request=str(match.inputs.get("user_message") or "").strip(),
                    missing_fields=missing_fields,
                    assumptions=assumptions,
                    can_skip=can_skip,
                    requires_confirmation=requires_confirmation,
                ),
            )
            return
    yield MetaRunAnnouncedEvent(
        run_id=_run_id,
        meta_skill_name=match.plan.name,
        language=template_language,
        steps=[
            {
                "id": s.id,
                "label": _localized_step_label(s, template_language),
                "kind": s.kind,
                "depends_on": list(s.depends_on),
            }
            for s in match.plan.steps
        ],
        total=len(match.plan.steps),
        parent_run_id=None,
    )

    # Terminal-state tracking for the final MetaRunCompletedEvent.  Seeded
    # outputs are real successful work reused from a prior run; mark them as
    # succeeded immediately so the replay ribbon and completion history never
    # regress those steps back to pending. A replayed failover primary is the
    # one exception: its placeholder exists only to suppress a second paid
    # submit and must remain unresolved until the forced fallback succeeds.
    plan_step_ids = {step.id for step in ordered}
    replay_pending_primary_ids = set((replay_failover_aliases or {}).values())
    _succeeded_step_ids: set[str] = (
        set(outputs).intersection(plan_step_ids) - replay_pending_primary_ids
    )
    _failed_step_ids: set[str] = set()
    _recovered_failed_step_ids: set[str] = set()
    _skipped_step_ids: set[str] = set()

    for step in ordered:
        if step.id not in _succeeded_step_ids:
            continue
        yield MetaStepStateEvent(
            run_id=_run_id,
            step_id=step.id,
            state="succeeded",
            status_text="Reused from the previous run",
        )

    def _yield_completion(
        outcome: Literal["ok", "failed", "cancelled"],
    ) -> MetaRunCompletedEvent:
        unresolved_failed = _failed_step_ids - _recovered_failed_step_ids
        return MetaRunCompletedEvent(
            run_id=_run_id,
            outcome=outcome,
            completed_steps=sorted(_succeeded_step_ids),
            failed_steps=sorted(unresolved_failed),
            recovered_steps=sorted(_recovered_failed_step_ids),
            skipped_steps=sorted(_skipped_step_ids),
        )

    steps_by_id: dict[str, MetaStep] = {s.id: s for s in ordered}
    reusable_output_ids = set(outputs) - replay_pending_primary_ids
    pending_deps: dict[str, set[str]] = {
        s.id: set(s.depends_on) - reusable_output_ids for s in ordered
    }
    # Steps that are *only* reachable as another step's ``on_failure``
    # substitute must not run autonomously — they exist on the DAG so
    # downstream consumers can declare ``depends_on`` against them, but
    # they only fire when the scheduler dispatches them via the failover
    # path. We pull them out of the initial ready set and re-add them on
    # ``_FailoverTriggered``.
    substitute_only: set[str] = {
        s.on_failure for s in ordered if s.on_failure
    }
    trusted_replay_aliases = {
        substitute_id: primary_id
        for substitute_id, primary_id in (replay_failover_aliases or {}).items()
        if (
            substitute_id in steps_by_id
            and primary_id in outputs
            and getattr(steps_by_id.get(primary_id), "on_failure", "") == substitute_id
            and substitute_id not in outputs
        )
    }
    # A failed fallback replay intentionally starts that substitute directly;
    # mirror normal failover semantics by clearing its declared dependencies.
    for substitute_id in trusted_replay_aliases:
        substitute_only.discard(substitute_id)
        pending_deps[substitute_id] = set()
    unstarted: set[str] = (
        set(steps_by_id.keys()) - substitute_only - set(outputs.keys())
    )
    running: dict[str, asyncio.Task[None]] = {}
    # Aliases populated when a step fails over: maps the substitute step
    # id to the original failed step id. On the substitute's ``_StepDone``
    # we mirror its output into the original's slot so downstream
    # ``depends_on`` links see a value as if the original had succeeded.
    failover_aliases: dict[str, str] = dict(trusted_replay_aliases)
    # ``final_text`` is taken from the last non-substitute step in topological
    # order. Substitute-only steps would yield an empty string if they never
    # fire (their primary succeeded), so they cannot serve as the deliverable.
    non_substitute_order = [s.id for s in ordered if s.id not in substitute_only]
    last_step_id = non_substitute_order[-1] if non_substitute_order else ordered[-1].id

    event_queue: asyncio.Queue[
        tuple[
            str,
            AgentEvent | MetaResult | _StepDone | _FailoverTriggered | MetaPaused | Exception,
        ]
    ] = asyncio.Queue()
    scope_prefix = usage_scope_prefix or f"meta:{match.plan.name}:{id(match)}"

    def _step_usage_args(step_id: str) -> dict[str, Any]:
        if usage_tracker is None or not session_key:
            return {}
        get_scope = getattr(usage_tracker, "get_scope", None)
        if not callable(get_scope):
            return {}
        scoped = get_scope(session_key, f"{scope_prefix}:{step_id}")
        if scoped is None:
            return {}
        input_tokens = int(getattr(scoped, "input_tokens", 0) or 0)
        output_tokens = int(getattr(scoped, "output_tokens", 0) or 0)
        cache_read_tokens = int(getattr(scoped, "cache_read_tokens", 0) or 0)
        cache_write_tokens = int(getattr(scoped, "cache_write_tokens", 0) or 0)
        cost_usd = float(getattr(scoped, "total_cost", 0.0) or 0.0)
        estimated_cost = float(getattr(scoped, "cost", 0.0) or 0.0)
        billed_cost = float(getattr(scoped, "billed_cost", 0.0) or 0.0)
        cost_source = str(getattr(scoped, "cost_source", "") or "")
        if not (
            input_tokens
            or output_tokens
            or cache_read_tokens
            or cache_write_tokens
        ):
            return {}
        return {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cost_usd": round(cost_usd, 6),
                "billed_cost": round(billed_cost, 6),
                "billed_cost_usd": round(billed_cost, 6),
                "estimated_cost_usd": round(estimated_cost, 6),
                "cost_source": cost_source,
                "is_provider_billed": cost_source == "provider_billed",
                "model": str(getattr(scoped, "model_id", "") or ""),
            }
        }

    async def _run_one(step: MetaStep) -> None:
        """Drive a single step; push its events into the shared queue."""
        try:
            if not evaluate_when(
                step.when, inputs=match.inputs, outputs=outputs,
            ):
                if is_external_paid_step(step):
                    _record_paid_submission_disposition(
                        step.id,
                        PAID_SUBMISSION_SAFE_NO_SUBMIT,
                    )
                log.info(
                    "meta_orchestrator.step_skipped",
                    step=step.id,
                    kind=step.kind,
                    skill=step.skill,
                    when=step.when,
                )
                step_use_id = f"meta_step_{step.id}"
                step_tool_name = f"meta-step:{step.id}"
                await event_queue.put(
                    (
                        step.id,
                        ToolUseStartEvent(
                            tool_use_id=step_use_id,
                            tool_name=step_tool_name,
                        ),
                    ),
                )
                outputs[step.id] = ""
                # Step-(c)/(d) follow-up — close the "trust gap" the
                # skipped user_input path used to open. When a meta
                # author writes ``when: 'NEEDS_CLARIFICATION: yes' in
                # outputs.x`` to suppress an unnecessary clarify form,
                # the user never sees what the system inferred —
                # confirmed_fields / ambiguous_fields / unknown_mentions
                # only fire on the MetaPaused path. Attach a
                # ``clarify_skip_summary`` payload here so the surface
                # can render a "we inferred this — confirm / change"
                # card without forcing the form back open.
                clarify_skip_summary = _build_clarify_skip_summary(
                    step, match.inputs, outputs,
                )
                arguments: dict[str, Any] = {
                    "kind": step.kind,
                    "skill": step.skill,
                    "default_skill": step.skill,
                    "routed": False,
                    "skipped": True,
                    "when": step.when,
                    "output_chars": 0,
                }
                if clarify_skip_summary is not None:
                    arguments["clarify_skip_summary"] = clarify_skip_summary
                await event_queue.put(
                    (
                        step.id,
                        ToolResultEvent(
                            tool_use_id=step_use_id,
                            tool_name=step_tool_name,
                            result="skipped: condition evaluated false",
                            is_error=False,
                            arguments=arguments,
                        ),
                    ),
                )
                await event_queue.put((
                    step.id,
                    MetaStepStateEvent(
                        run_id=_run_id,
                        step_id=step.id,
                        state="skipped",
                    ),
                ))
                await event_queue.put((step.id, _StepDone(text="", status="skipped")))
                return

            routed_to = resolve_route(
                step.route, inputs=match.inputs, outputs=outputs,
            )
            effective_skill = routed_to or step.skill
            log.info(
                "meta_orchestrator.step_started",
                step=step.id,
                kind=step.kind,
                skill=effective_skill,
                default_skill=step.skill,
                routed=routed_to is not None,
            )
            step_use_id = f"meta_step_{step.id}"
            step_tool_name = f"meta-step:{step.id}"
            await event_queue.put((
                step.id,
                MetaStepStateEvent(
                    run_id=_run_id,
                    step_id=step.id,
                    state="running",
                    status_text=_default_status_text(step, effective_skill),
                ),
            ))
            await event_queue.put(
                (
                    step.id,
                    ToolUseStartEvent(
                        tool_use_id=step_use_id,
                        tool_name=step_tool_name,
                    ),
                ),
            )
            if step.kind in ("skill_exec", "agent"):
                async for sv_ev in yield_skill_view_preface(
                    step.id, effective_skill,
                ):
                    await event_queue.put((step.id, sv_ev))

            if on_step_begin is not None:
                try:
                    observer_outputs = dict(outputs)
                    # Receipt proofs are valid only inside this scheduler
                    # process. Observability hooks may persist their rendered
                    # inputs, so give them an empty machine channel while the
                    # actual executor below retains the live proof map.
                    observer_outputs[PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY] = "{}"
                    rendered_inputs = render_with_args(
                        step.with_args,
                        inputs=dict(match.inputs),
                        outputs=observer_outputs,
                    )
                    await on_step_begin(
                        step.id, effective_skill, rendered_inputs,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "scheduler.on_step_begin_failed",
                        step=step.id,
                        error=str(exc),
                    )

            final_text = ""
            current_receipt_proof: str | None = None
            with usage_scope(f"{scope_prefix}:{step.id}"):
                async for ev in dispatch_step_stream(
                    step, effective_skill, match.inputs, outputs,
                ):
                    if isinstance(ev, _StepDone):
                        current_receipt_proof = paid_receipt_proof(ev.text)
                        # Strip the private str-subclass carrier immediately;
                        # templates, persistence and public events receive an
                        # ordinary string only.
                        final_text = str(ev.text)
                    else:
                        await event_queue.put((step.id, ev))

            outputs[step.id] = final_text
            if is_external_paid_step(step):
                # Exit zero alone does not prove that a matching receipt was
                # produced by this invocation. Only the exact bundled
                # executor can attach the stdout+sidecar digest carrier.
                if current_receipt_proof is not None:
                    _record_paid_submission_receipt_proof(
                        step.id,
                        current_receipt_proof,
                    )
                _record_paid_submission_disposition(
                    step.id,
                    (
                        PAID_SUBMISSION_RECEIPT
                        if current_receipt_proof is not None
                        else PAID_SUBMISSION_MAYBE_ACCEPTED
                    ),
                )
            log.info(
                "meta_orchestrator.step_finished",
                step=step.id,
                kind=step.kind,
                skill=effective_skill,
                output_chars=len(final_text),
                output_preview=final_text[:200],
            )
            # The card preview is what users see expanded in chat. Keep it
            # tight (≤100 chars) so 11 cards in a row don't drown the
            # surface in raw step content — the full output is still
            # available via the gateway log's ``output_preview`` (200 chars)
            # and ``output_chars``. For 1-line outputs (skill_exec status
            # strings like "wrote paper/results.csv") the full text shows
            # through naturally.
            preview = (
                final_text if len(final_text) <= 100
                else f"{final_text[:80]}… ({len(final_text)} chars)"
            )
            await event_queue.put(
                (
                    step.id,
                    ToolResultEvent(
                        tool_use_id=step_use_id,
                        tool_name=step_tool_name,
                        result=preview,
                        is_error=False,
                        arguments={
                            "kind": step.kind,
                            "skill": effective_skill,
                            "default_skill": step.skill,
                            "routed": routed_to is not None,
                            "output_chars": len(final_text),
                            **_step_usage_args(step.id),
                        },
                    ),
                ),
            )
            await event_queue.put((
                step.id,
                MetaStepStateEvent(
                    run_id=_run_id,
                    step_id=step.id,
                    state="succeeded",
                ),
            ))
            await event_queue.put((step.id, _StepDone(text=final_text)))
        except MetaPaused as paused:
            # Pause is not failure. Stash on the queue so the main loop
            # can shut down siblings cleanly and emit a single terminal
            # MetaResult(paused=True). on_failure substitute is intentionally
            # NOT triggered (design §8.1).
            await event_queue.put((step.id, paused))
            return
        except asyncio.CancelledError:
            # Re-raise so gather/wait see the cancellation, but the
            # queue drain in iter_events will not see a _StepDone for
            # this step — that's how the outer loop detects siblings
            # that never completed.
            raise
        except Exception as exc:  # noqa: BLE001
            has_substitute = bool(step.on_failure)
            current_receipt_proof = paid_receipt_proof(exc)
            if is_external_paid_step(step) and current_receipt_proof is not None:
                _record_paid_submission_receipt_proof(
                    step.id,
                    current_receipt_proof,
                )
            persisted_error = str(exc)
            if is_external_paid_step(step):
                _record_paid_submission_disposition(
                    step.id,
                    (
                        PAID_SUBMISSION_SAFE_NO_SUBMIT
                        if paid_replay_is_safe(persisted_error)
                        else PAID_SUBMISSION_MAYBE_ACCEPTED
                    ),
                )
            display_error = public_step_error(persisted_error)
            log.warning(
                "meta_orchestrator.step_failed",
                step=step.id,
                error=display_error,
                failover=has_substitute,
                substitute=step.on_failure or None,
            )
            step_use_id = f"meta_step_{step.id}"
            step_tool_name = f"meta-step:{step.id}"
            rescue = _failure_rescue_payload(
                plan_name=match.plan.name,
                step=step,
                error=persisted_error,
                outputs=outputs,
                has_substitute=has_substitute,
                plan_has_external_paid_steps=any(
                    is_external_paid_step(candidate) for candidate in match.plan.steps
                ),
            )
            await event_queue.put(
                (
                    step.id,
                    ToolResultEvent(
                        tool_use_id=step_use_id,
                        tool_name=step_tool_name,
                        result=display_error,
                        is_error=True,
                        arguments={
                            "step": step.id,
                            "failover": has_substitute,
                            "rescue": rescue,
                        },
                    ),
                ),
            )
            _failed_step_ids.add(step.id)
            await event_queue.put((
                step.id,
                MetaStepStateEvent(
                    run_id=_run_id,
                    step_id=step.id,
                    state="failed",
                    error=display_error,
                    rescue=rescue,
                ),
            ))
            if has_substitute:
                # Soft failure — defer to the substitute. The main loop
                # will dispatch ``step.on_failure`` and alias its output
                # back to this step's slot.
                await event_queue.put(
                    (
                        step.id,
                        _FailoverTriggered(
                            failed_step_id=step.id,
                            substitute_step_id=step.on_failure,
                            error=persisted_error,
                        ),
                    ),
                )
            else:
                await event_queue.put((step.id, exc))

    def _spawn_ready() -> None:
        for sid in list(unstarted):
            if max_parallelism is not None and len(running) >= max_parallelism:
                # Cap reached — leave remaining ready steps in
                # ``unstarted`` for the next _spawn_ready() call.
                break
            if not pending_deps[sid]:
                unstarted.discard(sid)
                task = asyncio.create_task(_run_one(steps_by_id[sid]))
                running[sid] = task

    _spawn_ready()
    if not running:
        yield _yield_completion("failed")
        yield MetaResult(
            ok=False,
            error="no runnable steps (all blocked by dependencies)",
        )
        return

    failure: Exception | None = None
    failed_step_id: str | None = None
    # Step IDs whose ToolUseStartEvent we have already forwarded to the
    # caller but whose matching ToolResultEvent has not yet been yielded.
    # On failure we use this set to emit synthetic close-bracket frames
    # for every still-open step, so the UI never sees a dangling
    # in-progress tool-call card.
    seen_starts: set[str] = set()

    def _track_yielded(ev: AgentEvent, sid: str) -> None:
        if isinstance(ev, ToolUseStartEvent) and ev.tool_name.startswith(
            "meta-step:",
        ):
            seen_starts.add(sid)
        elif isinstance(ev, ToolResultEvent) and ev.tool_name.startswith(
            "meta-step:",
        ):
            seen_starts.discard(sid)

    try:
        while running or not event_queue.empty():
            step_id, item = await event_queue.get()
            if isinstance(item, _StepDone):
                task = running.pop(step_id, None)
                if task is not None and not task.done():
                    await task
                if item.status == "skipped":
                    _skipped_step_ids.add(step_id)
                else:
                    _succeeded_step_ids.add(step_id)
                if on_step_finish is not None:
                    try:
                        await on_step_finish(step_id, item.status, item.text, None)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "scheduler.on_step_finish_failed",
                            step=step_id,
                            error=str(exc),
                        )
                # Failover alias propagation: if this completing step is
                # a substitute spawned for a previously-failed step, mirror
                # its output into the failed step's slot AND treat the
                # failed step's id as resolved for any dependent's deps
                # set. (Downstream steps declared ``depends_on`` against
                # the original id, not the substitute.)
                aliased_failed = failover_aliases.get(step_id)
                if aliased_failed is not None:
                    outputs[aliased_failed] = item.text
                    _recovered_failed_step_ids.add(aliased_failed)
                for deps in pending_deps.values():
                    deps.discard(step_id)
                    if aliased_failed is not None:
                        deps.discard(aliased_failed)
                # A step that SUCCEEDED never fires its ``on_failure``
                # substitute, so that substitute stays parked in
                # ``substitute_only`` forever. Any downstream step that
                # declared ``depends_on`` against the substitute would then
                # block permanently and be silently dropped while the run
                # still reported ok. Resolve the un-fired substitute as
                # skipped so those dependents unblock.
                if step_id not in _recovered_failed_step_ids:
                    completed_step = steps_by_id.get(step_id)
                    unfired_substitute_id = getattr(completed_step, "on_failure", None)
                    if (
                        isinstance(unfired_substitute_id, str)
                        and unfired_substitute_id
                        and unfired_substitute_id in substitute_only
                    ):
                        substitute_only.discard(unfired_substitute_id)
                        _skipped_step_ids.add(unfired_substitute_id)
                        for deps in pending_deps.values():
                            deps.discard(unfired_substitute_id)
                _spawn_ready()
                continue
            if isinstance(item, _FailoverTriggered):
                # The original step's _run_one task has finished (it
                # already published its failing ToolResultEvent ahead of
                # this sentinel). Remove it from ``running``, record the
                # alias, force-clear the substitute's pending deps
                # (substitute fires when its parent fails, not when the
                # substitute's own depends_on resolves — the minimal
                # subset semantic), move the substitute out of
                # ``substitute_only`` into ``unstarted``, and spawn.
                failed_task = running.pop(item.failed_step_id, None)
                if failed_task is not None and not failed_task.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await failed_task
                if on_step_failover is not None:
                    try:
                        await on_step_failover(
                            item.failed_step_id,
                            item.substitute_step_id,
                            item.error,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "scheduler.on_step_failover_failed",
                            failed_step=item.failed_step_id,
                            substitute=item.substitute_step_id,
                            error=str(exc),
                        )
                failover_aliases[item.substitute_step_id] = item.failed_step_id
                if item.substitute_step_id in pending_deps:
                    pending_deps[item.substitute_step_id] = set()
                if item.substitute_step_id in substitute_only:
                    substitute_only.discard(item.substitute_step_id)
                if (
                    item.substitute_step_id in steps_by_id
                    and item.substitute_step_id not in running
                ):
                    unstarted.add(item.substitute_step_id)
                await event_queue.put((
                    item.substitute_step_id,
                    MetaStepStateEvent(
                        run_id=_run_id,
                        step_id=item.substitute_step_id,
                        state="substituted",
                        substitute_for=item.failed_step_id,
                    ),
                ))
                _spawn_ready()
                continue
            if isinstance(item, MetaPaused):
                # The per-step task already emitted a ToolUseStartEvent
                # before invoking dispatch_step_stream. Without a matching
                # ToolResultEvent, Web UI tool cards stay "in flight" forever.
                # Emit a synthetic paused ToolResultEvent first so the card
                # closes cleanly.
                #
                # The ``arguments`` payload carries the surface-agnostic
                # schema protocol (PR5 ``clarify_schema.schema_to_protocol``)
                # so Web/CLI/IM surfaces can render a clickable form. The
                # ``paused`` flag remains the cheap signal for surfaces that
                # don't render forms.
                paused_use_id = f"meta_step_{item.step_id}"
                paused_tool_name = f"meta-step:{item.step_id}"
                from opensquilla.skills.meta.clarify_schema import schema_to_protocol
                clarify_protocol = schema_to_protocol(
                    item.schema,
                    intro_override=item.intro,
                    language=item.language,
                    confirmed_fields=item.confirmed_fields,
                    prefill_audit=item.prefill_audit,
                )
                yield ToolResultEvent(
                    tool_use_id=paused_use_id,
                    tool_name=paused_tool_name,
                    result=f"paused: awaiting user input (step {item.step_id!r})",
                    is_error=False,
                    arguments={
                        "kind": "user_input",
                        "paused": True,
                        "step": item.step_id,
                        "run_id": item.run_id,
                        "clarify_schema": clarify_protocol,
                    },
                )
                # Cancel all in-flight sibling tasks.
                for task in running.values():
                    if not task.done():
                        task.cancel()
                if running:
                    await asyncio.gather(*running.values(), return_exceptions=True)
                yield _yield_completion("cancelled")
                yield MetaResult(
                    ok=False,
                    paused=True,
                    paused_payload=item,
                    step_outputs=_public_step_outputs(),
                )
                return
            if isinstance(item, Exception):
                failure = item
                failed_step_id = step_id
                # Mark the step row as ``failed`` so
                # ``skills meta runs steps <id>`` does not show a stale
                # ``running`` row after the run finalises. The failover
                # path (``_FailoverTriggered``) is unchanged and still
                # records ``substituted`` via ``on_step_failover``; only
                # the hard-failure branch (no ``on_failure`` substitute)
                # reaches this code path.
                if on_step_finish is not None:
                    try:
                        await on_step_finish(step_id, "failed", None, str(item))
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "scheduler.on_step_finish_failed_exception_path",
                            step=step_id,
                            error=str(exc),
                        )
                seen_starts.discard(step_id)  # failed step's result already yielded
                running.pop(step_id, None)
                for tid, t in list(running.items()):
                    if not t.done():
                        t.cancel()
                break
            if isinstance(item, MetaResult):
                # Defensive — _run_one never publishes MetaResult.
                continue
            if isinstance(item, AgentEvent):
                _track_yielded(item, step_id)
            yield item
    except BaseException:
        # Generator was closed early (GeneratorExit / task cancellation)
        # or an unexpected error bubbled out of the loop body. Clean up
        # any in-flight sibling tasks so we don't leak them. We
        # intentionally do NOT emit synthetic close-brackets here — the
        # consumer is no longer listening.
        for t in running.values():
            if not t.done():
                t.cancel()
        for t in running.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        raise

    # On failure: cancelled siblings may have published real
    # ToolResultEvent close-brackets to the queue just before their
    # cancellation took effect. Drain non-blockingly and forward any
    # such results so the UI sees the authentic outcome rather than a
    # synthetic placeholder. Anything still un-closed afterwards gets
    # a synthetic cancellation frame so the UI always sees a balanced
    # ToolUseStart/ToolResult pair per step.
    if failure is not None:
        for t in running.values():
            if not t.done():
                t.cancel()
        for t in running.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        while not event_queue.empty():
            try:
                step_id, item = event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, ToolResultEvent) and item.tool_name.startswith(
                "meta-step:",
            ):
                _track_yielded(item, step_id)
                yield item
        for orphan_id in sorted(seen_starts):
            yield ToolResultEvent(
                tool_use_id=f"meta_step_{orphan_id}",
                tool_name=f"meta-step:{orphan_id}",
                result="cancelled due to sibling step failure",
                is_error=True,
                arguments={
                    "step": orphan_id,
                    "cancelled_by": failed_step_id,
                },
            )

    if failure is not None:
        yield _yield_completion("failed")
        yield MetaResult(
            ok=False,
            step_outputs=_public_step_outputs(),
            error=str(failure),
            failed_step_id=failed_step_id,
        )
        return

    yield _yield_completion("failed" if (_failed_step_ids - _recovered_failed_step_ids) else "ok")
    yield MetaResult(
        ok=True,
        final_text=outputs.get(last_step_id, ""),
        step_outputs=_public_step_outputs(),
    )


__all__ = ["run_dag"]
