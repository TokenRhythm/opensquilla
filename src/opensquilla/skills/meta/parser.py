"""Parse SkillSpec.composition_raw into MetaPlan; provide topological iteration."""

from __future__ import annotations

import re
from collections.abc import Iterator
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Any

from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaPlan,
    MetaStep,
    RouteCase,
)

if TYPE_CHECKING:
    from opensquilla.skills.types import SkillSpec


_SUPPORTED_KINDS = frozenset(
    {"agent", "llm_classify", "llm_chat", "tool_call", "skill_exec", "user_input"},
)
_BILINGUAL_SEPARATOR_RE = re.compile(r"\s+/\s+")


class MetaPlanError(ValueError):
    """Raised when a meta-skill's composition is malformed."""


def _fallback_label_from_step_id(step_id: str) -> str:
    """Return a readable fallback label for older meta-skill steps."""

    return step_id.replace("_", " ").replace("-", " ").strip().title() or step_id


def _split_bilingual_text(text: str) -> dict[str, str]:
    """Best-effort split for legacy ``中文 / English`` prompt strings."""

    if not text.strip():
        return {}
    parts = _BILINGUAL_SEPARATOR_RE.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return {}
    left, right = (part.strip() for part in parts)
    if not left or not right:
        return {}
    if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", left) and re.search(
        r"[A-Za-z]",
        right,
    ):
        return {"zh": left, "en": right}
    if re.search(r"[A-Za-z]", left) and re.search(
        r"[\u3400-\u9fff\uf900-\ufaff]",
        right,
    ):
        return {"en": left, "zh": right}
    return {}


def parse_meta_plan(spec: SkillSpec) -> MetaPlan | None:
    """Return a MetaPlan if ``spec`` is a meta-skill with a valid composition.

    Returns ``None`` for non-meta skills.
    Raises :class:`MetaPlanError` for meta-skills whose composition is malformed
    (missing keys, cycles, duplicate ids).
    """

    if getattr(spec, "kind", "skill") != "meta":
        return None

    composition = getattr(spec, "composition_raw", None)
    if not isinstance(composition, dict):
        raise MetaPlanError(f"meta-skill {spec.name!r}: missing or non-dict composition")

    raw_steps = composition.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise MetaPlanError(f"meta-skill {spec.name!r}: composition.steps must be a non-empty list")

    seen_ids: set[str] = set()
    steps: list[MetaStep] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step[{index}] must be a mapping",
            )
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise MetaPlanError(f"meta-skill {spec.name!r}: step[{index}] missing id")
        if step_id in seen_ids:
            raise MetaPlanError(f"meta-skill {spec.name!r}: duplicate step id {step_id!r}")
        seen_ids.add(step_id)

        kind = raw.get("kind", "agent")
        if not isinstance(kind, str) or kind not in _SUPPORTED_KINDS:
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} kind={kind!r} not in "
                f"{sorted(_SUPPORTED_KINDS)}",
            )

        skill_name = raw.get("skill", "")
        if kind in ("agent", "skill_exec"):
            if not isinstance(skill_name, str) or not skill_name:
                raise MetaPlanError(
                    f"meta-skill {spec.name!r}: step {step_id!r} (kind={kind}) "
                    f"missing skill",
                )
        else:
            # Informational only for llm_classify / llm_chat / tool_call /
            # user_input; default to step_id.
            if not isinstance(skill_name, str):
                raise MetaPlanError(
                    f"meta-skill {spec.name!r}: step {step_id!r} skill must be a string",
                )
            if not skill_name:
                skill_name = step_id

        depends_on_raw = raw.get("depends_on") or []
        if not isinstance(depends_on_raw, list):
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} depends_on must be a list",
            )
        with_args = raw.get("with") or {}
        if not isinstance(with_args, dict):
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} with must be a mapping",
            )
        route = _parse_route(spec.name, step_id, raw.get("route") or [])
        when = _parse_when(spec.name, step_id, raw.get("when"))

        output_choices = _parse_output_choices(spec.name, step_id, kind, raw.get("output_choices"))
        tool, tool_args = _parse_tool_call(
            spec.name,
            step_id,
            kind,
            raw.get("tool"),
            raw.get("tool_args"),
        )
        tool_allowlist = _parse_tool_allowlist(
            spec.name,
            step_id,
            kind,
            raw.get("tool_allowlist"),
            tool,
        )

        on_failure_raw = raw.get("on_failure")
        if on_failure_raw is None or on_failure_raw == "":
            on_failure = ""
        elif isinstance(on_failure_raw, str) and on_failure_raw.strip():
            on_failure = on_failure_raw.strip()
        else:
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} on_failure must be "
                f"a non-empty string (target step id) or omitted",
            )

        side_effect_raw = raw.get("side_effect", "")
        if side_effect_raw in (None, ""):
            side_effect = ""
        elif side_effect_raw == "external_paid_submit":
            if kind != "skill_exec":
                raise MetaPlanError(
                    f"meta-skill {spec.name!r}: step {step_id!r} "
                    "external_paid_submit is only valid for kind=skill_exec",
                )
            side_effect = side_effect_raw
        else:
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} side_effect must be "
                "'external_paid_submit' or omitted",
            )

        clarify_config = None
        if kind == "user_input":
            clarify_config = _parse_clarify_config(
                spec.name, step_id, raw.get("clarify"),
            )
        elif raw.get("clarify") is not None:
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} 'clarify' only valid "
                f"for kind=user_input",
            )

        label_raw = raw.get("label", "")
        if not isinstance(label_raw, str):
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} label must be "
                f"a string (or omitted)",
            )
        label = label_raw.strip() or _fallback_label_from_step_id(step_id)
        label_by_language: dict[str, str] = {}
        for lang in ("zh", "en"):
            localized_label = raw.get(f"label_{lang}")
            if isinstance(localized_label, str) and localized_label.strip():
                label_by_language[lang] = localized_label.strip()
        if "en" not in label_by_language:
            label_by_language["en"] = label

        progress_emits_raw = raw.get("progress_emits")
        if progress_emits_raw is None:
            # Defaults by kind: tool_call → False; everything else → True.
            progress_emits = kind != "tool_call"
        elif isinstance(progress_emits_raw, bool):
            progress_emits = progress_emits_raw
        else:
            raise MetaPlanError(
                f"meta-skill {spec.name!r}: step {step_id!r} progress_emits "
                f"must be a boolean (or omitted)",
            )

        steps.append(
            MetaStep(
                id=step_id,
                skill=skill_name,
                with_args=dict(with_args),
                depends_on=tuple(str(d) for d in depends_on_raw),
                when=when,
                route=route,
                kind=kind,
                output_choices=output_choices,
                tool=tool,
                tool_args=tool_args,
                tool_allowlist=tool_allowlist,
                on_failure=on_failure,
                side_effect=side_effect,
                clarify_config=clarify_config,
                label=label,
                label_by_language=label_by_language,
                progress_emits=progress_emits,
            ),
        )

    _ensure_acyclic(spec.name, steps)
    _ensure_on_failure_valid(spec.name, steps)

    triggers_raw: Any = getattr(spec, "triggers", None) or []
    if not isinstance(triggers_raw, list):
        triggers_raw = [str(triggers_raw)]
    priority = int(getattr(spec, "meta_priority", 0) or 0)
    fallback_body = getattr(spec, "content", "") or ""

    final_text_mode = str(
        getattr(spec, "final_text_mode", "auto") or "auto",
    ).strip() or "auto"
    request_template_raw = getattr(spec, "request_template", {}) or {}
    request_template = (
        dict(request_template_raw)
        if isinstance(request_template_raw, dict)
        else {}
    )
    output_contract_raw = getattr(spec, "output_contract", {}) or {}
    output_contract = (
        dict(output_contract_raw)
        if isinstance(output_contract_raw, dict)
        else {}
    )
    eval_prompts_raw = getattr(spec, "eval_prompts", []) or []
    eval_prompts = (
        [dict(item) for item in eval_prompts_raw if isinstance(item, dict)]
        if isinstance(eval_prompts_raw, list)
        else []
    )
    preference_keys_raw = getattr(spec, "preference_keys", []) or []
    preference_keys = (
        tuple(str(item) for item in preference_keys_raw if str(item).strip())
        if isinstance(preference_keys_raw, list)
        else ()
    )
    policy_tags_raw = getattr(spec, "policy_tags", []) or []
    policy_tags = (
        tuple(str(item) for item in policy_tags_raw if str(item).strip())
        if isinstance(policy_tags_raw, list)
        else ()
    )

    return MetaPlan(
        name=spec.name,
        triggers=tuple(str(t) for t in triggers_raw),
        priority=priority,
        steps=tuple(steps),
        fallback_body=fallback_body,
        final_text_mode=final_text_mode,
        request_template=request_template,
        output_contract=output_contract,
        eval_prompts=eval_prompts,
        preference_keys=preference_keys,
        policy_tags=policy_tags,
    )


def _parse_when(skill_name: str, step_id: str, raw: object) -> str:
    """Validate an optional step-level ``when`` expression."""

    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} when must be "
            f"a non-empty string or omitted",
        )
    return raw.strip()


def topological_order(steps: tuple[MetaStep, ...]) -> Iterator[MetaStep]:
    """Yield steps in a valid topological order (depends_on satisfied first).

    Cycles or undefined deps raise :class:`MetaPlanError` (also caught at parse time).
    """

    by_id = {s.id: s for s in steps}
    graph: dict[str, list[str]] = {s.id: list(s.depends_on) for s in steps}
    try:
        sorter = TopologicalSorter(graph)
        order = list(sorter.static_order())
    except CycleError as exc:
        raise MetaPlanError(f"composition has dependency cycle: {exc.args[1]}") from exc
    for sid in order:
        if sid not in by_id:
            raise MetaPlanError(f"composition references undefined step id {sid!r}")
        yield by_id[sid]


def _parse_route(
    skill_name: str,
    step_id: str,
    raw: object,
) -> tuple[RouteCase, ...]:
    """Validate and convert a step's raw ``route`` list into RouteCase tuple.

    Each entry must be a mapping with non-empty string ``when`` + ``to``.
    Empty/missing route returns an empty tuple (no branching).
    """

    if not isinstance(raw, list):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} route must be a list",
        )
    cases: list[RouteCase] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} route[{index}] "
                f"must be a mapping",
            )
        when = item.get("when")
        to = item.get("to")
        if not isinstance(when, str) or not when.strip():
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} route[{index}] "
                f"missing non-empty 'when' string",
            )
        if not isinstance(to, str) or not to.strip():
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} route[{index}] "
                f"missing non-empty 'to' string",
            )
        cases.append(RouteCase(when=when, to=to))
    return tuple(cases)


def _parse_output_choices(
    skill_name: str,
    step_id: str,
    kind: str,
    raw: object,
) -> tuple[str, ...]:
    """Validate ``output_choices`` for llm_classify steps.

    Required (non-empty list of non-empty strings) when kind == "llm_classify";
    must be empty/absent otherwise.
    """

    if kind == "llm_classify":
        if not isinstance(raw, list) or not raw:
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} (kind=llm_classify) "
                f"requires non-empty output_choices list",
            )
        choices: list[str] = []
        for index, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} output_choices[{index}] "
                    f"must be a non-empty string",
                )
            choices.append(item)
        if len(set(choices)) != len(choices):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} output_choices must be unique",
            )
        return tuple(choices)
    if raw not in (None, [], ()):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} output_choices only valid "
            f"for kind=llm_classify",
        )
    return ()


def _parse_tool_call(
    skill_name: str,
    step_id: str,
    kind: str,
    tool_raw: object,
    tool_args_raw: object,
) -> tuple[str, dict[str, Any]]:
    """Validate ``tool`` + ``tool_args`` for tool_call steps."""

    if kind == "tool_call":
        if not isinstance(tool_raw, str) or not tool_raw.strip():
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} (kind=tool_call) "
                f"requires non-empty 'tool' string",
            )
        args = tool_args_raw or {}
        if not isinstance(args, dict):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} tool_args must be a mapping",
            )
        return tool_raw, dict(args)
    if tool_raw not in (None, ""):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} 'tool' only valid for kind=tool_call",
        )
    if tool_args_raw not in (None, {}, ()):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} 'tool_args' only valid "
            f"for kind=tool_call",
        )
    return "", {}


def _parse_tool_allowlist(
    skill_name: str,
    step_id: str,
    kind: str,
    raw: object,
    tool: str,
) -> tuple[str, ...]:
    """Validate optional ``tool_allowlist`` for tool_call steps.

    Empty/absent ⇒ no allowlist (pre-existing behaviour). When non-empty:
    items must be non-empty strings; the step's ``tool`` must appear in
    the list; and the step's ``kind`` must be ``tool_call`` (the field
    has no meaning for other kinds).
    """

    if raw in (None, [], ()):
        return ()
    if not isinstance(raw, list):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} tool_allowlist must "
            f"be a list of strings",
        )
    items: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} "
                f"tool_allowlist[{index}] must be a non-empty string",
            )
        items.append(item)
    if not items:
        return ()
    if kind != "tool_call":
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} tool_allowlist is "
            f"only valid for kind=tool_call (got kind={kind!r})",
        )
    if tool not in items:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} tool {tool!r} "
            f"not in tool_allowlist {items!r}",
        )
    return tuple(items)


def _ensure_on_failure_valid(name: str, steps: list[MetaStep]) -> None:
    """Cross-validate ``on_failure`` references after all steps are parsed.

    Five rules (minimum subset for Step A.3):

    1. The target step id must exist in the same plan.
    2. A step cannot name itself as its own substitute.
    3. A substitute step cannot itself have ``on_failure`` (no chains).
    4. Each substitute step may be designated by at most ONE primary
       (no shared substitutes) — otherwise concurrent failovers would
       overwrite the alias and silently strand one parent's output slot.
    5. A substitute step cannot declare ``depends_on`` — the scheduler
       force-clears its pending deps on failover, so honouring them would
       require a more elaborate semantic than the minimum subset offers.
    """

    by_id = {s.id: s for s in steps}
    designated_by: dict[str, str] = {}
    for s in steps:
        if not s.on_failure:
            continue
        if s.on_failure == s.id:
            raise MetaPlanError(
                f"meta-skill {name!r}: step {s.id!r} on_failure cannot "
                f"target itself",
            )
        if s.on_failure not in by_id:
            raise MetaPlanError(
                f"meta-skill {name!r}: step {s.id!r} on_failure target "
                f"{s.on_failure!r} is not a step in this plan",
            )
        substitute = by_id[s.on_failure]
        if substitute.on_failure:
            raise MetaPlanError(
                f"meta-skill {name!r}: step {s.id!r} on_failure target "
                f"{s.on_failure!r} may not have its own on_failure "
                f"(nested substitution is not supported)",
            )
        if substitute.depends_on:
            raise MetaPlanError(
                f"meta-skill {name!r}: step {s.id!r} on_failure target "
                f"{s.on_failure!r} must not declare depends_on "
                f"(substitute steps are dispatched on failover, not by "
                f"dependency resolution)",
            )
        prior = designated_by.get(s.on_failure)
        if prior is not None:
            raise MetaPlanError(
                f"meta-skill {name!r}: step {s.on_failure!r} is already "
                f"designated as on_failure substitute by step {prior!r}; "
                f"a substitute may only be referenced by one primary",
            )
        designated_by[s.on_failure] = s.id


def _ensure_acyclic(name: str, steps: list[MetaStep]) -> None:
    ids = {s.id for s in steps}
    graph: dict[str, list[str]] = {}
    for s in steps:
        for dep in s.depends_on:
            if dep not in ids:
                raise MetaPlanError(
                    f"meta-skill {name!r}: step {s.id!r} depends on undefined step {dep!r}",
                )
        graph[s.id] = list(s.depends_on)
    try:
        list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise MetaPlanError(
            f"meta-skill {name!r}: dependency cycle: {exc.args[1]}",
        ) from exc


_CLARIFY_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CLARIFY_VALID_TYPES = frozenset({"string", "enum", "int", "bool"})
_GENERIC_ADDITIONAL_NOTES_FIELD = ClarifyField(
    name="additional_notes",
    type="string",
    required=False,
    prompt=(
        "其他可能有用的备注、限制或背景 / "
        "Additional notes, constraints, or context"
    ),
    prompt_by_language={
        "zh": "其他可能有用的备注、限制或背景",
        "en": "Additional notes, constraints, or context",
    },
    max_chars=2000,
)


def _parse_clarify_field(
    skill_name: str, step_id: str, raw: dict, index: int,
) -> ClarifyField:
    if not isinstance(raw, dict):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"must be a mapping",
        )

    name = raw.get("name")
    if not isinstance(name, str) or not _CLARIFY_FIELD_NAME_RE.match(name):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"name {name!r} must match ^[a-z][a-z0-9_]{{0,31}}$",
        )

    type_ = raw.get("type")
    if type_ not in _CLARIFY_VALID_TYPES:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"type {type_!r} must be one of {sorted(_CLARIFY_VALID_TYPES)}",
        )

    raw_required = raw.get("required", False)
    if not isinstance(raw_required, bool):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"{name!r}: required must be a boolean, got {type(raw_required).__name__}",
        )
    required = raw_required
    default = raw.get("default", None)
    if required and default is not None:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"{name!r}: required=true and default are mutually exclusive",
        )

    choices_raw = raw.get("choices", ())
    choices: tuple[str, ...] = ()
    if type_ == "enum":
        if not isinstance(choices_raw, list) or not choices_raw:
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: enum type requires non-empty choices list",
            )
        for ci, c in enumerate(choices_raw):
            if not isinstance(c, str) or not c.strip():
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}].choices[{ci}] must be a non-empty string",
                )
        if len(set(choices_raw)) != len(choices_raw):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: choices must be unique",
            )
        choices = tuple(str(c) for c in choices_raw)
    elif choices_raw not in ((), [], None):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"{name!r}: choices only valid when type=enum",
        )

    min_v = raw.get("min")
    max_v = raw.get("max")
    if type_ == "int":
        if min_v is not None and not isinstance(min_v, int):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: min must be an integer",
            )
        if max_v is not None and not isinstance(max_v, int):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: max must be an integer",
            )
        if min_v is not None and max_v is not None and min_v > max_v:
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: min={min_v} must be <= max={max_v}",
            )
    else:
        if min_v is not None or max_v is not None:
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: min/max only valid when type=int",
            )

    max_chars = raw.get("max_chars")
    if type_ == "string":
        if max_chars is not None and (
            not isinstance(max_chars, int) or not (1 <= max_chars <= 4000)
        ):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
                f"{name!r}: max_chars must be an int in [1, 4000]",
            )
    elif max_chars is not None:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"{name!r}: max_chars only valid when type=string",
        )

    if default is not None:
        if type_ == "enum":
            if default not in choices:
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default {default!r} "
                    f"not in choices {list(choices)}",
                )
        elif type_ == "int":
            if not isinstance(default, int) or isinstance(default, bool):
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default for int field "
                    f"must be an int",
                )
            if min_v is not None and default < min_v:
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default {default} "
                    f"is below min={min_v}",
                )
            if max_v is not None and default > max_v:
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default {default} "
                    f"is above max={max_v}",
                )
        elif type_ == "bool":
            if not isinstance(default, bool):
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default for bool field "
                    f"must be a bool",
                )
        elif type_ == "string":
            if not isinstance(default, str):
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default for string "
                    f"field must be a string",
                )
            if max_chars is not None and len(default) > max_chars:
                raise MetaPlanError(
                    f"meta-skill {skill_name!r}: step {step_id!r} "
                    f"clarify.fields[{index}] {name!r}: default length "
                    f"{len(default)} exceeds max_chars={max_chars}",
                )

    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields[{index}] "
            f"{name!r}: prompt must be a string",
        )
    prompt_by_language: dict[str, str] = {}
    for lang in ("zh", "en"):
        localized_prompt = raw.get(f"prompt_{lang}", "")
        if localized_prompt and not isinstance(localized_prompt, str):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} "
                f"clarify.fields[{index}] {name!r}: prompt_{lang} must be a string",
            )
        if isinstance(localized_prompt, str) and localized_prompt.strip():
            prompt_by_language[lang] = localized_prompt
    if not prompt_by_language:
        prompt_by_language.update(_split_bilingual_text(prompt))

    return ClarifyField(
        name=name,
        type=type_,
        required=required,
        prompt=prompt,
        prompt_by_language=prompt_by_language,
        choices=choices,
        default=default,
        min=min_v,
        max=max_v,
        max_chars=max_chars,
    )


def _parse_clarify_config(
    skill_name: str, step_id: str, raw: object,
) -> ClarifyStepConfig:
    if not isinstance(raw, dict):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} (kind=user_input) "
            f"requires a 'clarify' mapping",
        )

    mode = raw.get("mode", "form")
    if mode not in ("form", "chat"):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.mode {mode!r} "
            f"must be 'form' or 'chat'",
        )

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields must be "
            f"a non-empty list",
        )

    fields: list[ClarifyField] = []
    seen_names: set[str] = set()
    for index, raw_field in enumerate(fields_raw):
        cf = _parse_clarify_field(skill_name, step_id, raw_field, index)
        if cf.name in seen_names:
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields "
                f"contains duplicate name {cf.name!r}",
            )
        seen_names.add(cf.name)
        fields.append(cf)

    if _GENERIC_ADDITIONAL_NOTES_FIELD.name not in seen_names:
        fields.append(_GENERIC_ADDITIONAL_NOTES_FIELD)

    max_fields = 5 if mode == "chat" else 13
    if len(fields) > max_fields:
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.fields "
            f"has {len(fields)} entries; max for mode={mode!r} is {max_fields}",
        )

    skip_if = raw.get("skip_if", "")
    if skip_if and not isinstance(skip_if, str):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.skip_if "
            f"must be a string Jinja expression",
        )
    if skip_if:
        from opensquilla.skills.meta.templating import _JINJA_ENV
        try:
            _JINJA_ENV.compile_expression(skip_if)
        except Exception as exc:  # noqa: BLE001
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} clarify.skip_if "
                f"failed to compile: {exc}",
            ) from exc

    cancel_keywords_raw = raw.get("cancel_keywords", [])
    if cancel_keywords_raw and (
        not isinstance(cancel_keywords_raw, list)
        or not all(isinstance(k, str) and k.strip() for k in cancel_keywords_raw)
    ):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.cancel_keywords "
            f"must be a list of non-empty strings (or omitted)",
        )
    cancel_keywords = tuple(str(k).strip().lower() for k in cancel_keywords_raw)

    timeout_hours = raw.get("timeout_hours", 24)
    if not isinstance(timeout_hours, int) or not (1 <= timeout_hours <= 168):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.timeout_hours "
            f"must be an int in [1, 168]",
        )

    intro = raw.get("intro", "")
    if not isinstance(intro, str):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.intro "
            f"must be a string",
        )
    intro_by_language: dict[str, str] = {}
    for lang in ("zh", "en"):
        localized_intro = raw.get(f"intro_{lang}", "")
        if localized_intro and not isinstance(localized_intro, str):
            raise MetaPlanError(
                f"meta-skill {skill_name!r}: step {step_id!r} "
                f"clarify.intro_{lang} must be a string",
            )
        if isinstance(localized_intro, str) and localized_intro.strip():
            intro_by_language[lang] = localized_intro
    if not intro_by_language:
        intro_by_language.update(_split_bilingual_text(intro))

    nl_extract = raw.get("nl_extract", False)
    if not isinstance(nl_extract, bool):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.nl_extract "
            f"must be a boolean",
        )

    nl_extract_tier_raw = raw.get("nl_extract_tier", "")
    if nl_extract_tier_raw and not isinstance(nl_extract_tier_raw, str):
        raise MetaPlanError(
            f"meta-skill {skill_name!r}: step {step_id!r} clarify.nl_extract_tier "
            f"must be a string (router tier name) or omitted",
        )
    nl_extract_tier = nl_extract_tier_raw if nl_extract else ""

    return ClarifyStepConfig(
        mode=mode,
        fields=tuple(fields),
        skip_if=skip_if,
        cancel_keywords=cancel_keywords,
        timeout_hours=timeout_hours,
        intro=intro,
        intro_by_language=intro_by_language,
        nl_extract=nl_extract,
        nl_extract_tier=nl_extract_tier,
    )
