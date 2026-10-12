"""Round-trippable JSON serialization for MetaPlan.

PR2 extracts the previously-inline `_serialize_plan` helper from
`opensquilla.persistence.meta_run_writer` and adds the inverse
`from_jsonable`. The serialization envelope is versioned so plan
layouts can evolve across releases without breaking durable awaiting
runs from older OpenSquilla versions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from opensquilla.skills.meta.types import MetaPlan, MetaStep, RouteCase

PLAN_SERDE_VERSION: int = 2


def to_jsonable(plan: MetaPlan) -> dict[str, Any]:
    """Serialize a MetaPlan to a JSON-dumpable dict with a version envelope.

    The envelope is ``{"v": <PLAN_SERDE_VERSION>, "plan": <plan_dict>}``.
    The plan-dict shape matches the format previously emitted by the
    inline ``_serialize_plan`` helper for backward compatibility with
    rows written before PR2.
    """

    plan_dict = {
        "name": plan.name,
        "triggers": list(plan.triggers),
        "priority": plan.priority,
        "fallback_body": plan.fallback_body,
        "final_text_mode": plan.final_text_mode,
        "request_template": dict(plan.request_template),
        "output_contract": dict(plan.output_contract),
        "eval_prompts": [dict(item) for item in plan.eval_prompts],
        "preference_keys": list(plan.preference_keys),
        "policy_tags": list(plan.policy_tags),
        "steps": [
            {
                "id": s.id,
                "skill": s.skill,
                "kind": s.kind,
                "with_args": dict(s.with_args),
                "depends_on": list(s.depends_on),
                "when": s.when,
                "route": [{"when": r.when, "to": r.to} for r in s.route],
                "output_choices": list(s.output_choices),
                "tool": s.tool,
                "tool_args": dict(s.tool_args),
                "tool_allowlist": list(s.tool_allowlist),
                "on_failure": s.on_failure,
                "side_effect": s.side_effect,
                "clarify_config": clarify_config_to_jsonable(s.clarify_config),
                "label": s.label,
                "label_by_language": dict(s.label_by_language),
                "progress_emits": s.progress_emits,
            }
            for s in plan.steps
        ],
    }
    return {"v": PLAN_SERDE_VERSION, "plan": plan_dict}


def from_jsonable(payload: dict[str, Any]) -> MetaPlan:
    """Reverse of `to_jsonable`. Tolerates unknown keys (logged at DEBUG).

    Raises ``ValueError`` if the envelope version is in the future
    (forward-compat: refuse to deserialize, do not silently strip).
    """

    if not isinstance(payload, dict):
        raise ValueError(f"plan payload must be a dict, got {type(payload).__name__}")

    version = payload.get("v")
    if version is None:
        # Tolerate legacy snapshot dicts (no envelope) from rows written
        # before PR2 — those came from the old `_serialize_plan` helper
        # which emitted the plan dict directly without an envelope.
        plan_dict = payload
    else:
        if not isinstance(version, int) or version > PLAN_SERDE_VERSION:
            raise ValueError(
                f"plan_serde envelope version {version!r} not supported; "
                f"this build understands up to v{PLAN_SERDE_VERSION}",
            )
        plan_dict = payload.get("plan", {})
        if not isinstance(plan_dict, dict):
            raise ValueError("plan envelope missing 'plan' key")

    steps_raw = plan_dict.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ValueError("plan.steps must be a list")

    steps: list[MetaStep] = []
    for index, raw in enumerate(steps_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"plan.steps[{index}] must be a mapping")
        steps.append(_step_from_jsonable(raw, index))

    return MetaPlan(
        name=str(plan_dict.get("name", "")),
        triggers=tuple(str(t) for t in plan_dict.get("triggers", []) or []),
        priority=int(plan_dict.get("priority", 0) or 0),
        steps=tuple(steps),
        fallback_body=str(plan_dict.get("fallback_body", "") or ""),
        final_text_mode=str(plan_dict.get("final_text_mode", "auto") or "auto"),
        request_template=(
            dict(plan_dict.get("request_template") or {})
            if isinstance(plan_dict.get("request_template"), dict)
            else {}
        ),
        output_contract=(
            dict(plan_dict.get("output_contract") or {})
            if isinstance(plan_dict.get("output_contract"), dict)
            else {}
        ),
        eval_prompts=(
            [dict(item) for item in plan_dict.get("eval_prompts", []) if isinstance(item, dict)]
            if isinstance(plan_dict.get("eval_prompts", []), list)
            else []
        ),
        preference_keys=(
            tuple(str(item) for item in plan_dict.get("preference_keys", []) or [])
            if isinstance(plan_dict.get("preference_keys", []), list)
            else ()
        ),
        policy_tags=(
            tuple(str(item) for item in plan_dict.get("policy_tags", []) or [])
            if isinstance(plan_dict.get("policy_tags", []), list)
            else ()
        ),
    )


def _step_from_jsonable(raw: dict[str, Any], index: int) -> MetaStep:
    route_raw = raw.get("route", []) or []
    if not isinstance(route_raw, list):
        raise ValueError(f"step[{index}].route must be a list")
    route = tuple(
        RouteCase(when=str(r["when"]), to=str(r["to"]))
        for r in route_raw
        if isinstance(r, dict) and "when" in r and "to" in r
    )

    kind = str(raw.get("kind", "agent"))
    progress_emits_raw = raw.get("progress_emits")
    progress_emits = (
        bool(progress_emits_raw)
        if isinstance(progress_emits_raw, bool)
        else kind != "tool_call"
    )

    return MetaStep(
        id=str(raw.get("id", "")),
        skill=str(raw.get("skill", "")),
        with_args=dict(raw.get("with_args", {}) or {}),
        depends_on=tuple(str(d) for d in raw.get("depends_on", []) or []),
        when=str(raw.get("when", "") or ""),
        route=route,
        kind=kind,
        output_choices=tuple(str(c) for c in raw.get("output_choices", []) or []),
        tool=str(raw.get("tool", "") or ""),
        tool_args=dict(raw.get("tool_args", {}) or {}),
        tool_allowlist=tuple(
            str(t) for t in raw.get("tool_allowlist", []) or []
        ),
        on_failure=str(raw.get("on_failure", "") or ""),
        side_effect=str(raw.get("side_effect", "") or ""),
        clarify_config=clarify_config_from_jsonable(raw.get("clarify_config")),
        label=str(raw.get("label", "") or ""),
        label_by_language={
            str(key): str(value)
            for key, value in dict(raw.get("label_by_language", {}) or {}).items()
            if str(key).strip() and str(value).strip()
        },
        progress_emits=progress_emits,
    )


def clarify_config_to_jsonable(cfg: Any) -> dict[str, Any] | None:
    """ClarifyStepConfig → plain dict; None passes through.

    Public (no underscore) so cross-module callers in PR3 can use it
    directly without importing a private helper.
    """

    if cfg is None:
        return None
    return {
        "mode": cfg.mode,
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "required": f.required,
                "prompt": f.prompt,
                "prompt_by_language": dict(f.prompt_by_language),
                "choices": list(f.choices),
                "default": f.default,
                "min": f.min,
                "max": f.max,
                "max_chars": f.max_chars,
            }
            for f in cfg.fields
        ],
        "skip_if": cfg.skip_if,
        "cancel_keywords": list(cfg.cancel_keywords),
        "timeout_hours": cfg.timeout_hours,
        "intro": cfg.intro,
        "intro_by_language": dict(cfg.intro_by_language),
        "nl_extract": cfg.nl_extract,
        "nl_extract_tier": cfg.nl_extract_tier,
    }


def clarify_config_from_jsonable(raw: Any) -> Any:
    """Inverse of clarify_config_to_jsonable. Returns ClarifyStepConfig or None."""

    if raw is None:
        return None
    from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig

    fields_raw = raw.get("fields", []) or []
    fields = tuple(
        ClarifyField(
            name=str(f.get("name", "")),
            type=str(f.get("type", "string")),
            required=bool(f.get("required", False)),
            prompt=str(f.get("prompt", "") or ""),
            prompt_by_language={
                str(key): str(value)
                for key, value in dict(f.get("prompt_by_language", {}) or {}).items()
                if str(key).strip() and str(value).strip()
            },
            choices=tuple(str(c) for c in f.get("choices", []) or []),
            default=f.get("default"),
            min=f.get("min"),
            max=f.get("max"),
            max_chars=f.get("max_chars"),
        )
        for f in fields_raw
    )
    return ClarifyStepConfig(
        mode=str(raw.get("mode", "form") or "form"),
        fields=fields,
        skip_if=str(raw.get("skip_if", "") or ""),
        cancel_keywords=tuple(str(k) for k in raw.get("cancel_keywords", []) or []),
        timeout_hours=int(raw.get("timeout_hours", 24) or 24),
        intro=str(raw.get("intro", "") or ""),
        intro_by_language={
            str(key): str(value)
            for key, value in dict(raw.get("intro_by_language", {}) or {}).items()
            if str(key).strip() and str(value).strip()
        },
        nl_extract=bool(raw.get("nl_extract", False)),
        nl_extract_tier=str(raw.get("nl_extract_tier", "") or ""),
    )


def plan_digest(plan: MetaPlan) -> str:
    """SHA-256 of the canonical JSON serialization. Used by
    MetaRunWriter to detect plan drift across resume turns."""
    payload = json.dumps(to_jsonable(plan), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
