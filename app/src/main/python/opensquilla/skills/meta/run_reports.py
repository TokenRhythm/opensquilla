"""Read-model helpers for persisted meta-skill runs.

Gateway RPC and CLI both consume these pure report builders. Keeping them
outside gateway transport code avoids coupling command-line inspection to RPC
registration and authorization glue.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from opensquilla.persistence.meta_run_writer import (
    RunRecord,
    StepRecord,
    summarize_run_record,
)
from opensquilla.skills.meta.plan_serde import from_jsonable
from opensquilla.skills.meta.replay_safety import (
    is_external_paid_step,
    paid_replay_is_safe,
)
from opensquilla.skills.meta.templating import evaluate_when
from opensquilla.skills.meta.types import MetaPlan

REPLAY_CONTEXT_MAX_CHARS = 4000

_WHEN_OUTPUT_DOT_RE = re.compile(r"\boutputs\.([A-Za-z_][A-Za-z0-9_-]*)")
_WHEN_OUTPUT_GET_RE = re.compile(
    r"\boutputs\.get\(\s*['\"]([^'\"]+)['\"]",
)
_WHEN_OUTPUT_ITEM_RE = re.compile(
    r"\boutputs\[\s*['\"]([^'\"]+)['\"]\s*\]",
)


def json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def deserialize_plan(record: RunRecord) -> MetaPlan:
    return from_jsonable(json.loads(record.plan_snapshot_json))


def template_fields(request_template: dict[str, Any]) -> list[dict[str, Any]]:
    fields = request_template.get("fields", [])
    if not isinstance(fields, list):
        return []
    return [dict(item) for item in fields if isinstance(item, dict)]


def field_name(field: dict[str, Any]) -> str:
    name = field.get("name")
    return str(name).strip() if name is not None else ""


def template_field_names(request_template: dict[str, Any]) -> list[str]:
    return [
        name
        for field in template_fields(request_template)
        if (name := field_name(field))
    ]


def required_template_field_names(request_template: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for field in template_fields(request_template):
        name = field_name(field)
        if name and field.get("required") is True:
            names.append(name)
    return names


def filter_template_fields(
    request_template: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    allowed = set(template_field_names(request_template))
    return {
        key: value
        for key, value in fields.items()
        if key in allowed and value is not None and str(value).strip()
    }


def missing_required_fields(
    request_template: dict[str, Any],
    fields: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    for name in required_template_field_names(request_template):
        value = fields.get(name)
        if value is None or not str(value).strip():
            missing.append(name)
    return missing


def encode_preflight_fields(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def confirmation_message(
    *,
    record: RunRecord,
    interpreted_request: str,
    fields: dict[str, Any],
) -> str:
    original_inputs = json_object(record.inputs_json)
    base = interpreted_request.strip() or str(original_inputs.get("user_message") or "").strip()
    lines = [base] if base else []
    if fields:
        lines.extend(["", "Confirmed request fields:"])
        for key in sorted(fields):
            value = fields[key]
            if value is not None and str(value).strip():
                lines.append(f"- {key}: {value}")
    lines.extend([
        "",
        "<!-- opensquilla:meta_preflight_confirmed=1 -->",
        f"<!-- opensquilla:meta_preflight_run_id={record.run_id} -->",
    ])
    if fields:
        lines.append(
            f"<!-- opensquilla:meta_preflight_fields={encode_preflight_fields(fields)} -->"
        )
    return "\n".join(lines).strip()


def _serialize_record_summary(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "triggered_by": record.triggered_by,
        "session_key": record.session_key,
        "turn_id": record.turn_id,
        "status": record.status,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "failed_step_id": record.failed_step_id,
        "error_present": bool(record.error),
        "truncated_fields": list(record.truncated_fields),
        "summary": summarize_run_record(record),
    }


def _step_by_id(record: RunRecord) -> dict[str, StepRecord]:
    return {step.step_id: step for step in record.steps}


def _diff_step(left: StepRecord | None, right: StepRecord | None, step_id: str) -> dict[str, Any]:
    left_output_chars = len(left.output_text or "") if left else 0
    right_output_chars = len(right.output_text or "") if right else 0
    return {
        "step_id": step_id,
        "left_status": left.status if left else None,
        "right_status": right.status if right else None,
        "status_changed": (left.status if left else None) != (right.status if right else None),
        "output_chars_delta": right_output_chars - left_output_chars,
        "error_changed": bool(left.error if left else None) != bool(right.error if right else None),
        "declared_skill_changed": (left.declared_skill if left else None)
        != (right.declared_skill if right else None),
        "effective_skill_changed": (left.effective_skill if left else None)
        != (right.effective_skill if right else None),
    }


def build_run_diff(left: RunRecord, right: RunRecord) -> dict[str, Any]:
    left_steps = _step_by_id(left)
    right_steps = _step_by_id(right)
    step_ids = sorted(set(left_steps) | set(right_steps))
    return {
        "left": _serialize_record_summary(left),
        "right": _serialize_record_summary(right),
        "status_changed": left.status != right.status,
        "failed_step_changed": left.failed_step_id != right.failed_step_id,
        "final_text_chars_delta": len(right.final_text or "") - len(left.final_text or ""),
        "step_count_delta": len(right.steps) - len(left.steps),
        "metadata": {
            "meta_skill_digest_changed": left.meta_skill_digest != right.meta_skill_digest,
            "trigger_changed": left.triggered_by != right.triggered_by,
        },
        "steps": [
            _diff_step(left_steps.get(step_id), right_steps.get(step_id), step_id)
            for step_id in step_ids
        ],
    }


def _bounded_text(text: str, limit: int = REPLAY_CONTEXT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 25].rstrip() + "\n...[truncated for replay]"


def build_replay_request(record: RunRecord, *, mode: str = "run") -> dict[str, Any]:
    inputs = json_object(record.inputs_json)
    failed_step_id = record.failed_step_id or ""
    successful = [
        step for step in record.steps
        if step.status in {"ok", "substituted"} and step.output_text
    ]
    failed = next((step for step in record.steps if step.step_id == failed_step_id), None)
    context_lines = [
        f"Replay meta-skill run {record.run_id} ({record.meta_skill_name}).",
        f"Replay mode: {mode}.",
    ]
    user_message = str(inputs.get("user_message") or "").strip()
    if user_message:
        context_lines.append(f"Original request: {user_message}")
    if failed_step_id:
        context_lines.append(f"The prior failed step was {failed_step_id}.")
    if failed and failed.error:
        context_lines.append(f"Prior failure: {failed.error}")
    if successful:
        context_lines.append("Prior successful outputs:")
        for step in successful:
            context_lines.append(
                f"- {step.step_id}: {_bounded_text(step.output_text or '', 800)}"
            )
    # Older surfaces send ``replay.message`` through their normal composer.
    # Keep that path deterministic and machine-routable: a canonical /meta
    # command starts a fresh orchestrated run instead of asking the outer model
    # to interpret replay prose.  New surfaces use the live two-phase replay
    # ticket and preserve successful step outputs; ``context_message`` remains
    # available for inspection/history without ever being sent as a command.
    fallback_message = f"/meta {record.meta_skill_name}"
    if user_message:
        fallback_message += f" -- {user_message}"
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "mode": mode,
        "failed_step_id": failed_step_id or None,
        "request": inputs,
        "message": _bounded_text(fallback_message),
        "context_message": _bounded_text("\n".join(context_lines)),
        "replay_kind": "draft",
        "live_replay": {
            "available": False,
            "reason": (
                "live replay requires a session-bound prepare/commit request; "
                "the message fallback starts a fresh orchestrated run"
            ),
        },
    }


def _recovery_step_label(step: Any, language: str) -> str:
    """Resolve the same zh/en label bucket used by the live scheduler."""

    if language:
        suffix = "zh" if language.lower().startswith("zh") else "en"
        localized = getattr(step, "label_by_language", {}).get(suffix)
        if localized:
            return str(localized)
    return str(getattr(step, "label", "") or "")


def _recovery_actions(
    step: Any,
    error: object,
    *,
    plan_has_external_paid_steps: bool,
) -> list[dict[str, str]]:
    """Return replay controls that remain valid after the live stream is gone."""

    if is_external_paid_step(step) and not paid_replay_is_safe(error):
        return [
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
                "description": "Use a different workflow without repeating this paid submit.",
            },
        ]

    actions = [
        {
            "id": "retry-run",
            "label": "Retry this run",
            "description": "Run the same meta-skill again with the original request.",
        },
        {
            "id": "retry-step",
            "label": "Retry failed step",
            "description": "Retry only the failed step and reuse successful prior outputs.",
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
    return actions


def _when_output_references(expression: str) -> set[str] | None:
    """Return statically named output keys, or ``None`` for dynamic access.

    Recovery may only re-evaluate a historical condition from dependency
    outputs that are both durable and complete. Dynamic mapping access cannot
    prove that boundary, so it deliberately stays pending.
    """

    references = {
        *(match for match in _WHEN_OUTPUT_DOT_RE.findall(expression) if match != "get"),
        *_WHEN_OUTPUT_GET_RE.findall(expression),
        *_WHEN_OUTPUT_ITEM_RE.findall(expression),
    }
    if re.search(r"\boutputs\b", expression) and not references:
        return None
    return references


def build_recovery_events(record: RunRecord) -> dict[str, Any] | None:
    """Rebuild terminal ribbon events from one persisted failed run.

    Meta progress events are deliberately transient and do not belong in the
    assistant transcript.  This read model gives reconnecting clients the same
    UI state without replaying model-visible content or inventing a new run.
    """

    if record.status != "failed" or not record.failed_step_id:
        return None
    try:
        plan = deserialize_plan(record)
    except Exception:  # noqa: BLE001 - corrupt historical rows stay inspectable elsewhere
        return None

    inputs = json_object(record.inputs_json)
    language = str(inputs.get("user_language") or "").strip()
    records_by_id = {step.step_id: step for step in record.steps}
    plan_steps_by_id = {step.id: step for step in plan.steps}
    # A step with no persistence row may still have emitted a live `skipped`
    # event: scheduler conditions are evaluated before on_step_begin writes a
    # row. First walk backwards from durable execution evidence to find missing
    # dependencies that cannot still be pending. Do not label every such gap as
    # skipped: persistence is fail-open, so a row can also be absent after real
    # execution. We prove conditional skips below by re-evaluating `when` using
    # persisted inputs/outputs locally; none of that content enters the payload.
    resolved_dependency_ids: set[str] = set()
    dependency_stack = [
        step_id
        for step_id in set(records_by_id) | {record.failed_step_id}
        if step_id in plan_steps_by_id
    ]
    while dependency_stack:
        step_id = dependency_stack.pop()
        for dependency_id in plan_steps_by_id[step_id].depends_on:
            if (
                dependency_id not in plan_steps_by_id
                or dependency_id in resolved_dependency_ids
            ):
                continue
            resolved_dependency_ids.add(dependency_id)
            dependency_stack.append(dependency_id)
    dependency_closures: dict[str, set[str]] = {}

    def dependency_closure(step_id: str) -> set[str]:
        cached = dependency_closures.get(step_id)
        if cached is not None:
            return cached
        closure: set[str] = set()
        stack = list(plan_steps_by_id[step_id].depends_on)
        while stack:
            dependency_id = stack.pop()
            if dependency_id in closure or dependency_id not in plan_steps_by_id:
                continue
            closure.add(dependency_id)
            stack.extend(plan_steps_by_id[dependency_id].depends_on)
        dependency_closures[step_id] = closure
        return closure

    inferred_skipped_ids: set[str] = set()

    def trusted_output(step_id: str) -> str | None:
        if step_id in inferred_skipped_ids:
            return ""
        persisted = records_by_id.get(step_id)
        if persisted is None:
            return None
        source: StepRecord | None = persisted
        if persisted.status == "substituted":
            if not persisted.substitute_step_id:
                return None
            source = records_by_id.get(persisted.substitute_step_id)
            if source is None or source.status != "ok":
                return None
        elif persisted.status != "ok":
            return None
        if source is None:
            return None
        if "output_text" in source.truncated_fields:
            return None
        return source.output_text or ""

    changed = True
    while changed:
        changed = False
        for step in plan.steps:
            if (
                step.id in records_by_id
                or step.id in inferred_skipped_ids
                or step.id not in resolved_dependency_ids
                or not step.when.strip()
            ):
                continue
            # Persisted inputs are intentionally redacted/bounded, and a
            # recovery read must never guess how that altered an old branch.
            if re.search(r"\binputs\b", step.when):
                continue
            references = _when_output_references(step.when)
            closure = dependency_closure(step.id)
            if references is None or not references.issubset(closure):
                continue
            candidate_outputs: dict[str, str] = {}
            for dependency_id in closure:
                value = trusted_output(dependency_id)
                if value is not None:
                    candidate_outputs[dependency_id] = value
            # The original scheduler could only have evaluated the condition
            # after every direct dependency resolved. Missing or truncated
            # evidence therefore leaves the historical state pending.
            if any(
                dependency_id not in candidate_outputs
                for dependency_id in step.depends_on
            ) or any(reference not in candidate_outputs for reference in references):
                continue
            try:
                should_run = evaluate_when(
                    step.when,
                    inputs={},
                    outputs=candidate_outputs,
                )
            except Exception:  # noqa: BLE001 - ambiguous history remains pending
                continue
            if should_run:
                continue
            inferred_skipped_ids.add(step.id)
            changed = True
    completed_steps: list[str] = []
    recovered_steps: list[str] = []
    skipped_steps: list[str] = []
    step_states: list[dict[str, Any]] = []
    plan_has_external_paid_steps = any(
        is_external_paid_step(candidate) for candidate in plan.steps
    )

    for step in plan.steps:
        persisted = records_by_id.get(step.id)
        if step.id == record.failed_step_id:
            state = "failed"
        elif persisted is not None and persisted.status == "ok":
            state = "succeeded"
            completed_steps.append(step.id)
        elif persisted is not None and persisted.status == "substituted":
            state = "substituted"
            recovered_steps.append(step.id)
        elif persisted is None and step.id in inferred_skipped_ids:
            state = "skipped"
            skipped_steps.append(step.id)
        else:
            # Unstarted and cancellation-swept siblings remained pending in
            # the original live ribbon. Preserve that distinction here.
            state = "pending"

        step_states.append({
            "run_id": record.run_id,
            "step_id": step.id,
            "state": state,
            "status_text": "Restored from run history" if state == "succeeded" else None,
            # Unlike explicit admin inspection, this projection is fetched
            # automatically when a chat opens. Persisted executor errors are
            # not redacted by MetaRunWriter and may contain credentials, so
            # never copy them into the reconnect UI payload.
            "error": (
                "This step failed in a previous run. Review its tool result before retrying."
                if state == "failed"
                else None
            ),
            "substitute_for": None,
            "rescue": {
                "actions": _recovery_actions(
                    step,
                    getattr(persisted, "error", None),
                    plan_has_external_paid_steps=plan_has_external_paid_steps,
                )
            } if state == "failed" else {},
        })

    announced = {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "language": language,
        "steps": [
            {
                "id": step.id,
                "label": _recovery_step_label(step, language),
                "kind": step.kind,
                "depends_on": list(step.depends_on),
            }
            for step in plan.steps
        ],
        "total": len(plan.steps),
        "parent_run_id": None,
    }
    completed = {
        "run_id": record.run_id,
        "outcome": "failed",
        "completed_steps": completed_steps,
        "failed_steps": [record.failed_step_id],
        "recovered_steps": recovered_steps,
        "skipped_steps": skipped_steps,
    }
    return {
        "run_id": record.run_id,
        "session_key": record.session_key,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "announced": announced,
        "step_states": step_states,
        "completed": completed,
    }


def _unavailable_usage() -> dict[str, Any]:
    return {
        "available": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "unavailable",
        "reason": "meta run persistence does not store historical usage yet",
    }


def _aggregate_summaries_usage(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    usages: list[dict[str, Any]] = []
    for summary in summaries:
        raw_usage = summary.get("usage")
        if isinstance(raw_usage, dict) and raw_usage.get("available") is True:
            usages.append(raw_usage)
    if not usages:
        return _unavailable_usage()
    cost_sources = {
        str(usage.get("cost_source") or "").strip()
        for usage in usages
        if str(usage.get("cost_source") or "").strip()
    }

    def int_total(key: str) -> int:
        return sum(int(usage.get(key) or 0) for usage in usages)

    def float_total(key: str) -> float:
        return round(sum(float(usage.get(key) or 0.0) for usage in usages), 6)

    return {
        "available": True,
        "input_tokens": int_total("input_tokens"),
        "output_tokens": int_total("output_tokens"),
        "total_tokens": int_total("total_tokens"),
        "cache_read_tokens": int_total("cache_read_tokens"),
        "cache_write_tokens": int_total("cache_write_tokens"),
        "cost_usd": float_total("cost_usd"),
        "billed_cost_usd": float_total("billed_cost_usd"),
        "estimated_cost_usd": float_total("estimated_cost_usd"),
        "cost_source": next(iter(cost_sources)) if len(cost_sources) == 1 else "mixed",
        "run_count": len(usages),
    }


def build_cost_summary(records: list[RunRecord]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_meta_skill: dict[str, int] = {}
    summaries = [summarize_run_record(record) for record in records]
    for record in records:
        by_status[record.status] = by_status.get(record.status, 0) + 1
        by_meta_skill[record.meta_skill_name] = by_meta_skill.get(record.meta_skill_name, 0) + 1
    return {
        "aggregate": {
            "run_count": len(records),
            "by_status": by_status,
            "by_meta_skill": by_meta_skill,
            "usage": _aggregate_summaries_usage(summaries),
        },
        "runs": [
            {
                "run_id": record.run_id,
                "meta_skill_name": record.meta_skill_name,
                "status": record.status,
                "started_at_ms": record.started_at_ms,
                "usage": summary["usage"],
                "steps": [
                    {
                        "step_id": step.step_id,
                        "status": step.status,
                        "effective_skill": step.effective_skill,
                        "usage": summary["steps"][index]["usage"],
                    }
                    for index, step in enumerate(record.steps)
                ],
            }
            for record, summary in zip(records, summaries, strict=True)
        ],
    }


def build_validation_summary(record: RunRecord) -> dict[str, Any]:
    plan = deserialize_plan(record)
    request_template = dict(plan.request_template)
    fields = template_fields(request_template)
    output_contract = dict(plan.output_contract)
    multimodal = output_contract.get("modalities") or output_contract.get("media")
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "request_template": {
            "present": bool(request_template),
            "outcome": request_template.get("outcome"),
            "field_names": [field_name(field) for field in fields if field_name(field)],
            "required_fields": required_template_field_names(request_template),
            "fields": fields,
        },
        "output_contract": output_contract,
        "eval_prompts": [dict(item) for item in plan.eval_prompts],
        "preference_keys": list(plan.preference_keys),
        "policy_tags": list(plan.policy_tags),
        "multimodal": {
            "declared": bool(multimodal),
            "modalities": multimodal if isinstance(multimodal, list) else [],
        },
    }


def build_validation_availability(record: RunRecord) -> dict[str, Any]:
    try:
        plan = deserialize_plan(record)
    except Exception:  # noqa: BLE001 - list views should fail open
        return {
            "available": False,
            "request_template": False,
            "output_contract": False,
            "eval_baseline": False,
            "field_count": 0,
            "required_field_count": 0,
            "eval_prompt_count": 0,
            "reason": "plan snapshot could not be parsed",
        }
    request_template = dict(plan.request_template)
    fields = template_fields(request_template)
    output_contract = dict(plan.output_contract)
    eval_prompt_count = len(plan.eval_prompts)
    return {
        "available": bool(request_template or output_contract or eval_prompt_count),
        "request_template": bool(request_template),
        "output_contract": bool(output_contract),
        "eval_baseline": eval_prompt_count > 0,
        "field_count": len(fields),
        "required_field_count": len(required_template_field_names(request_template)),
        "eval_prompt_count": eval_prompt_count,
    }


def build_eval_baseline(record: RunRecord) -> dict[str, Any]:
    plan = deserialize_plan(record)
    items = []
    for item in plan.eval_prompts:
        prompt = str(item.get("prompt") or "")
        rubric = item.get("rubric", [])
        if isinstance(rubric, str):
            rubric_items = [rubric]
        elif isinstance(rubric, list):
            rubric_items = [str(entry) for entry in rubric]
        else:
            rubric_items = []
        items.append({
            "name": str(item.get("name") or "eval"),
            "prompt_chars": len(prompt),
            "rubric": rubric_items,
            "judge": {
                "mode": "deterministic_metadata",
                "status": "not_run",
                "reason": "live LLM judge execution is not available in gateway history RPC",
            },
        })
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "available": bool(items),
        "items": items,
        "drift": {
            "status": "not_run",
            "reason": (
                "baseline metadata is exposed; scheduled judge execution is "
                "outside this local RPC"
            ),
        },
    }
