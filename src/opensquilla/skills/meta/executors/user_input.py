"""Executor for the user_input meta-skill step.

Behavior (design §8.1):
  1. If skip_if (Jinja boolean) evaluates truthy against inputs + outputs,
     return immediately with an empty output ("" markdown). The step is
     treated like a successfully-completed pass-through.
  2. Otherwise, try to claim awaiting_user state via the injected DAO.
     On success, raise MetaPaused — the scheduler catches it ahead of
     CancelledError and emits a paused MetaResult.
     On failure (CAS rowcount==0 or partial unique index conflict),
     raise RuntimeError to signal normal step failure; on_failure
     substitute may then fire.

The executor itself is async to fit the scheduler's contract; DAO calls
are sync (MetaRunWriter holds a sync sqlite3 connection) and are routed
through ``asyncio.to_thread`` so the CAS's SQLite commit (which can block
for up to ``busy_timeout=5000`` ms under contention) never runs on the
event loop. The writer owns locking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol

import structlog

from opensquilla.skills.meta.clarify_text import _coerce_and_validate
from opensquilla.skills.meta.templating import evaluate_when, render_with_args
from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaPaused,
    MetaStep,
)

# Reserved key inside ``awaiting_filled_json`` carrying the prefill
# audit payload (which fields were auto-extracted, from which source,
# at what time). Surfaces consume this to render "we noticed X from
# your earlier message — please confirm" rather than treating the
# prefill values as user-entered. The leading double-underscore keeps
# it out of any field-name collision since clarify field names cannot
# start with ``__`` (parser invariant).
_PREFILL_AUDIT_KEY = "__prefill_audit__"
_EMPTY_PREFILL_SENTINELS: frozenset[str] = frozenset({"", "(empty)"})

LLMChatProto = Callable[[str, str], Awaitable[str]]

log = structlog.get_logger(__name__)

_EN_INTRO = (
    "A few required details are still missing. Please provide the fields "
    "below so I can continue."
)


def _contains_cjk(text: str) -> bool:
    return any("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" for ch in text)


def _english_field_prompt(field: ClarifyField) -> str:
    prompt = (field.prompt or "").strip()
    if not prompt:
        return field.name.replace("_", " ").title()
    for delimiter in (" / ", "/", " | ", "|"):
        if delimiter in prompt:
            candidates = [part.strip() for part in prompt.split(delimiter)]
            for candidate in reversed(candidates):
                if candidate and not _contains_cjk(candidate):
                    return candidate
    if _contains_cjk(prompt):
        return field.name.replace("_", " ").title()
    return prompt


def _language_key(inputs: Mapping[str, Any]) -> str:
    raw = str(inputs.get("user_language") or "").strip().lower()
    if raw.startswith("zh") or raw in {"chinese", "中文"}:
        return "zh"
    if raw.startswith("en") or raw in {"english", "英文"}:
        return "en"
    return ""


def _localize_clarify_config(
    cfg: ClarifyStepConfig,
    inputs: dict[str, Any],
) -> ClarifyStepConfig:
    language = _language_key(inputs)
    if not language:
        return cfg
    fields = tuple(
        replace(
            field,
            prompt=(
                field.prompt_by_language.get(language)
                or (_english_field_prompt(field) if language == "en" else field.prompt)
            ),
        )
        for field in cfg.fields
    )
    intro = cfg.intro
    if cfg.intro_by_language.get(language):
        intro = cfg.intro_by_language[language]
    elif language == "en" and (not intro.strip() or _contains_cjk(intro)):
        intro = _EN_INTRO
    cancel_keywords = cfg.cancel_keywords
    if language == "en":
        cancel_keywords = tuple(kw for kw in cfg.cancel_keywords if not _contains_cjk(kw))
    return ClarifyStepConfig(
        mode=cfg.mode,
        fields=fields,
        skip_if=cfg.skip_if,
        cancel_keywords=cancel_keywords,
        timeout_hours=cfg.timeout_hours,
        intro=intro,
        intro_by_language=cfg.intro_by_language,
        nl_extract=cfg.nl_extract,
        nl_extract_tier=cfg.nl_extract_tier,
    )


class _DAOProto(Protocol):
    """Minimal DAO surface this executor depends on (PR2 MetaRunWriter)."""

    def try_claim_awaiting(
        self,
        *,
        run_id: str,
        step_id: str,
        schema_json: str,
        session_id: str,
        inputs_json: str,
        step_outputs_json: str,
        awaiting_since: float,
        awaiting_filled_json: str = "{}",
    ) -> bool: ...


def _claim_failure_message(
    dao: _DAOProto,
    *,
    run_id: str,
    step_id: str,
    session_id: str,
) -> str:
    get_run = getattr(dao, "get_run", None)
    if callable(get_run):
        try:
            run = get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "meta_user_input.claim_failure_get_run_failed",
                run_id=run_id,
                step=step_id,
                error=str(exc),
            )
            run = ...
        if run is None:
            return (
                f"meta run {run_id!r} was not found while step {step_id!r} "
                "was entering awaiting_user; meta-skill persistence did not "
                "create or retain the running row"
            )
        status = getattr(run, "status", None)
        if isinstance(status, str) and status != "running":
            return (
                f"meta run {run_id!r} is {status!r} while step {step_id!r} "
                "was entering awaiting_user; expected status 'running'"
            )

    peek_awaiting = getattr(dao, "peek_awaiting", None)
    if callable(peek_awaiting):
        try:
            awaiting = peek_awaiting(session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "meta_user_input.claim_failure_peek_awaiting_failed",
                run_id=run_id,
                step=step_id,
                session_id=session_id,
                error=str(exc),
            )
            awaiting = None
        if awaiting is not None:
            awaiting_run_id = getattr(awaiting, "run_id", "")
            awaiting_step_id = getattr(awaiting, "step_id", "")
            if isinstance(awaiting_run_id, str) and isinstance(awaiting_step_id, str):
                return (
                    f"session {session_id!r} already has awaiting_user run "
                    f"{awaiting_run_id!r} at step {awaiting_step_id!r}; "
                    f"step {step_id!r} in run {run_id!r} cannot claim awaiting_user"
                )

    return (
        f"awaiting claim rejected for run_id={run_id!r} step={step_id!r} "
        f"(run is no longer 'running' or partial unique index conflict)"
    )


async def run_user_input_step(
    step: MetaStep,
    *,
    inputs: dict[str, Any],
    outputs: dict[str, str],
    run_id: str,
    session_id: str,
    dao: _DAOProto,
    now: Callable[[], float],
    llm_chat: LLMChatProto | None = None,
    prefill_context: Mapping[str, Any] | None = None,
) -> str:
    """Either pass through (skip_if true) or raise MetaPaused.

    Returns empty str on the pass-through path so downstream depends_on
    consumers see a defined output value. Never returns a non-empty
    string: the only "filled" content comes from the resume path,
    which writes via ``MetaOrchestrator.resume``.

    When ``llm_chat`` and ``prefill_context`` are both supplied AND the
    step's clarify config has ``nl_extract: true``, run a single NL
    extraction pass over the context BEFORE claiming the awaiting
    state (step c — "ask the user only what we cannot already
    answer"). High-confidence extracted fields are merged into the
    initial ``awaiting_filled_json`` so the surface can render them
    as pre-confirmed values for the user to verify rather than re-
    enter. Ambiguous fields are NOT pre-filled — the user must still
    answer those explicitly. The audit payload (which fields came
    from prefill, with what reasons) lands under the reserved
    ``__prefill_audit__`` key so step (d)'s ``confirmed_fields``
    protocol can surface it.

    Both arguments default to ``None`` so existing call sites (and
    every test that constructs the executor without LLM wiring)
    behave exactly as before.
    """

    cfg = step.clarify_config
    if cfg is None:
        # parser.py guarantees this won't happen for kind=user_input.
        raise RuntimeError(
            f"user_input step {step.id!r} missing clarify_config "
            f"(parser invariant violated)",
        )

    if cfg.skip_if:
        try:
            should_skip = evaluate_when(
                cfg.skip_if, inputs=inputs, outputs=outputs,
            )
        except ValueError as exc:
            # Per design §10: skip_if raising UndefinedError is treated
            # as "skip-not-applicable" — proceed to pause.
            log.warning(
                "meta_user_input.skip_if_error",
                step=step.id,
                error=str(exc),
            )
            should_skip = False
        if should_skip:
            log.info("meta_user_input.skipped", step=step.id)
            return ""

    localized_cfg = _localize_clarify_config(cfg, inputs)
    rendered_cfg = _render_clarify_config(localized_cfg, inputs=inputs, outputs=outputs)

    # Step c: pre-fill scan — pull field values out of the context the
    # user already supplied (original_user_message,
    # conversation_history, previously_collected, etc.) so we never
    # ask the user to repeat themselves. Best-effort: any failure
    # here downgrades to "no pre-fill" and we proceed to the normal
    # pause path.
    prefilled_values: dict[str, Any] = {}
    prefill_audit: dict[str, Any] = {}
    if (
        rendered_cfg.nl_extract
        and llm_chat is not None
        and prefill_context
    ):
        # Deterministic pre-pass — extract KEY: value lines from any
        # upstream step output where KEY matches an active field
        # name. This catches the common case where an "intel_context"
        # / "preferences" / "doc_context" llm_chat step already
        # emitted ``ACCOUNTS: OpenAI`` / ``TIME_WINDOW: LAST_MONTH``,
        # so the form's accounts / time_window field doesn't need a
        # second LLM call to recover the same value. The LLM scan
        # still runs after, but it now sees the already-resolved
        # fields and focuses on the unresolved ones (or fills
        # ``ambiguous_fields`` for them).
        deterministic_hits = _deterministic_upstream_prefill(
            rendered_cfg, prefill_context,
        )
        log.info(
            "meta_user_input.prefill_scan_started",
            step=step.id,
            context_keys=sorted(prefill_context.keys()),
            field_count=len(rendered_cfg.fields),
            deterministic_prefilled=sorted(deterministic_hits.keys()),
        )
        prefilled_values, prefill_audit = await _run_prefill_scan(
            cfg=rendered_cfg,
            llm_chat=llm_chat,
            context=prefill_context,
            step_id=step.id,
        )
        prefilled_values, prefill_audit = _drop_empty_prefill_sentinels(
            prefilled_values,
            prefill_audit,
        )
        # Deterministic hits win on conflict — they came directly
        # from an upstream emitter that we know followed the
        # ``KEY: value`` contract, while the LLM scan is best-effort
        # interpretation. The audit records both sources so the
        # surface can show provenance.
        if deterministic_hits:
            for name, value in deterministic_hits.items():
                prefilled_values[name] = value
            raw_audit_fields = (
                prefill_audit.get("fields") if isinstance(prefill_audit, dict) else []
            )
            current_audit_fields = (
                list(raw_audit_fields)
                if isinstance(raw_audit_fields, list | tuple | set)
                else []
            )
            merged_fields = sorted({*current_audit_fields, *deterministic_hits.keys()})
            if not isinstance(prefill_audit, dict):
                prefill_audit = {}
            prefill_audit["fields"] = merged_fields
            prefill_audit.setdefault("source", "auto_prefill")
            prefill_audit["deterministic_fields"] = sorted(deterministic_hits.keys())
        log.info(
            "meta_user_input.prefill_scan_finished",
            step=step.id,
            prefilled_count=len(prefilled_values),
            ambiguous_count=len(prefill_audit.get("ambiguous", []) or []),
            errors=list(prefill_audit.get("errors", []) or [])[:2],
        )
    else:
        log.info(
            "meta_user_input.prefill_scan_skipped",
            step=step.id,
            nl_extract=bool(rendered_cfg.nl_extract),
            has_llm_chat=llm_chat is not None,
            has_prefill_context=bool(prefill_context),
        )

    initial_filled: dict[str, Any] = dict(prefilled_values)
    if prefill_audit:
        initial_filled[_PREFILL_AUDIT_KEY] = prefill_audit

    schema_json = _serialize_schema(rendered_cfg)
    inputs_json = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
    step_outputs_json = json.dumps(outputs, ensure_ascii=False, sort_keys=True)
    awaiting_filled_json = json.dumps(
        initial_filled, ensure_ascii=False, sort_keys=True,
    )

    awaiting_since = now()

    # CancelledError MUST propagate so the scheduler can tear down
    # sibling tasks consistently — see design §8.1.
    # ``awaiting_filled_json`` is now a required slot in both the
    # ``_DAOProto`` declaration and the real ``MetaRunWriter`` SQL —
    # no broad ``TypeError`` swallow here, otherwise a legitimate
    # call-site signature mismatch silently dumps the prefill.
    try:
        claimed = await asyncio.to_thread(
            dao.try_claim_awaiting,
            run_id=run_id,
            step_id=step.id,
            schema_json=schema_json,
            session_id=session_id,
            inputs_json=inputs_json,
            step_outputs_json=step_outputs_json,
            awaiting_since=awaiting_since,
            awaiting_filled_json=awaiting_filled_json,
        )
    except asyncio.CancelledError:
        raise

    if not claimed:
        raise RuntimeError(
            await asyncio.to_thread(
                _claim_failure_message,
                dao,
                run_id=run_id,
                step_id=step.id,
                session_id=session_id,
            ),
        )

    raise MetaPaused(
        run_id=run_id,
        step_id=step.id,
        schema=rendered_cfg,
        intro=rendered_cfg.intro,
        language=str(inputs.get("user_language") or ""),
        confirmed_fields=dict(prefilled_values) if prefilled_values else None,
        prefill_audit=dict(prefill_audit) if prefill_audit else None,
    )


async def _run_prefill_scan(
    *,
    cfg: ClarifyStepConfig,
    llm_chat: LLMChatProto,
    context: Mapping[str, Any],
    step_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single LLM pass that extracts pre-stated field values.

    Returns ``(prefilled, audit)`` where:

    * ``prefilled`` is the validated ``{field_name: value}`` dict for
      fields the model is confident about. Ambiguous fields and
      genuinely missing fields are NOT pre-filled.
    * ``audit`` is a metadata payload describing which fields the
      pre-fill scan touched, mapped to ``{"source": "auto_prefill",
      "ambiguous": [...], "unknown_mentions": [...]}``. Surfaces
      consume this through the ``confirmed_fields`` protocol (step
      d) to show the user which answers were inferred and from
      where.

    Failure modes (LLM error, malformed JSON, every field validator
    rejecting) downgrade silently to ``({}, {"error": "..."})`` —
    pre-fill is best-effort; falling through to the normal ask-the-
    user path is always safe.
    """
    # Local import to avoid a circular import: clarify_nl_extract
    # depends on clarify_text.py which lives next to executors/.
    from opensquilla.skills.meta.clarify_nl_extract import extract  # noqa: PLC0415

    try:
        result = await extract(
            reply_text="",  # nothing from the user yet — purely scan context
            schema=cfg,
            active_fields=cfg.fields,
            llm_chat=llm_chat,
            tier=cfg.nl_extract_tier,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 — pre-fill is best-effort
        log.warning(
            "meta_user_input.prefill_scan_failed",
            step=step_id,
            error=str(exc),
        )
        return {}, {"error": str(exc)[:200]}

    audit: dict[str, Any] = {
        "source": "auto_prefill",
        "fields": list(result.fields.keys()),
        "ambiguous": [
            {"name": a.name, "reason": a.reason}
            for a in result.ambiguous_fields
        ],
        "unknown_mentions": [
            {"text": m.text, "guess": m.guess}
            for m in result.unknown_mentions
        ],
    }
    if result.errors:
        audit["errors"] = list(result.errors)
    return dict(result.fields), audit


def _drop_empty_prefill_sentinels(
    prefilled_values: dict[str, Any],
    prefill_audit: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove extractor placeholders that only mean "no user reply yet".

    Prefill scanning intentionally calls NL extraction with ``reply_text=""``.
    Catch-all fields may instruct the extractor to emit ``(empty)`` for a blank
    user reply; that sentinel is useful during real resume parsing, but it must
    not become an auto-confirmed value before the user has responded.
    """
    dropped = sorted(
        name
        for name, value in prefilled_values.items()
        if isinstance(value, str)
        and value.strip().lower() in _EMPTY_PREFILL_SENTINELS
    )
    if not dropped:
        return prefilled_values, prefill_audit

    cleaned = {
        name: value
        for name, value in prefilled_values.items()
        if name not in dropped
    }
    audit = dict(prefill_audit) if isinstance(prefill_audit, dict) else {}
    raw_fields = audit.get("fields")
    if isinstance(raw_fields, list | tuple | set):
        audit["fields"] = [name for name in raw_fields if name not in dropped]
    else:
        audit["fields"] = []
    audit["dropped_empty_sentinels"] = dropped
    return cleaned, audit


def _render_clarify_config(
    cfg: ClarifyStepConfig,
    *,
    inputs: dict[str, Any],
    outputs: dict[str, str],
) -> ClarifyStepConfig:
    """Render user-facing clarify copy against the live meta context.

    The parser keeps clarify schemas static, but language-sensitive forms need
    access to earlier extraction steps (for example ``LANGUAGE: en`` vs
    ``LANGUAGE: zh``). Only copy is rendered; field names, types, choices,
    defaults, and validation limits remain the parsed contract.
    """

    rendered = render_with_args(
        {
            "intro": cfg.intro,
            "fields": [
                {"prompt": field.prompt}
                for field in cfg.fields
            ],
        },
        inputs=inputs,
        outputs=outputs,
    )
    rendered_fields: list[ClarifyField] = []
    rendered_prompts = rendered.get("fields", [])
    for index, field in enumerate(cfg.fields):
        prompt = field.prompt
        if isinstance(rendered_prompts, list) and index < len(rendered_prompts):
            rendered_prompt = rendered_prompts[index].get("prompt")
            if isinstance(rendered_prompt, str):
                prompt = rendered_prompt
        rendered_fields.append(replace(field, prompt=prompt))

    intro = rendered.get("intro", cfg.intro)
    return replace(
        cfg,
        intro=intro if isinstance(intro, str) else cfg.intro,
        fields=tuple(rendered_fields),
    )


def _serialize_schema(cfg: ClarifyStepConfig) -> str:
    """JSON-serialize ClarifyStepConfig for persistence (DAO + surface renderers).

    Format mirrors clarify_config sub-tree in plan_serde.to_jsonable
    (PR2). The full meta-skill envelope is not needed here — only the
    awaiting_user row's schema column."""
    from opensquilla.skills.meta.plan_serde import clarify_config_to_jsonable

    payload = clarify_config_to_jsonable(cfg)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# Sentinel values that upstream "context-extraction" steps emit to
# mean "I considered this field but couldn't fill it" — these MUST
# NOT be treated as valid values for deterministic prefill, otherwise
# we'd silently lock the form to ``UNKNOWN``.
_DETERMINISTIC_PREFILL_SENTINELS: frozenset[str] = frozenset({
    "", "unknown", "n/a", "na", "none", "null", "tbd", "todo",
    "unspecified", "[]", "<missing>", "<unknown>", "<none>", "未指定",
    "未提供", "无",
})


def _deterministic_upstream_prefill(
    cfg: ClarifyStepConfig,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract ``KEY: value`` lines from upstream step outputs whose
    KEY matches an active schema field name (case-insensitive).

    Returns ``{field_name: validated_value}``. Fields that fail
    type/range/choice validation are silently skipped (the LLM scan
    or the user will fill them). Sentinel values (UNKNOWN / N/A /
    none / 未指定 / etc.) are skipped — they mean "upstream couldn't
    fill this", not "the answer is literally 'UNKNOWN'".

    This is the deterministic pre-pass that runs BEFORE the LLM
    prefill scan. It catches the common case where a
    context-extraction llm_chat step emitted structured output that
    happens to match the form's field names — no second LLM call
    needed.
    """
    if not context:
        return {}
    prior = context.get("prior_step_outputs")
    if not isinstance(prior, Mapping) or not prior:
        return {}

    field_by_name_ci: dict[str, ClarifyField] = {
        f.name.lower(): f for f in cfg.fields
    }
    if not field_by_name_ci:
        return {}

    hits: dict[str, Any] = {}
    for upstream_output in prior.values():
        if not isinstance(upstream_output, str) or not upstream_output.strip():
            continue
        lines = upstream_output.splitlines()
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            stripped = raw_line.strip()
            if not stripped or ":" not in stripped:
                i += 1
                continue
            key, _, inline_value = stripped.partition(":")
            key_norm = key.strip().lower()
            inline_value = inline_value.strip()
            field = field_by_name_ci.get(key_norm)
            if field is None:
                i += 1
                continue

            # Resolve the field's value. Three cases:
            #   1. ``KEY: value`` — inline, the original common case.
            #   2. ``KEY:`` followed by indented ``- item`` lines —
            #      YAML block list; join items as comma-separated.
            #   3. ``KEY: []`` / ``KEY:\n  - <missing>`` etc. — falls
            #      through to the sentinel filter below.
            value: str = inline_value
            if not value:
                items: list[str] = []
                j = i + 1
                while j < len(lines):
                    follow_raw = lines[j]
                    if not follow_raw.strip():
                        j += 1
                        continue
                    indent = len(follow_raw) - len(follow_raw.lstrip())
                    if indent == 0:
                        break
                    follow_stripped = follow_raw.strip()
                    if not follow_stripped.startswith("-"):
                        break
                    item = follow_stripped.lstrip("-").strip()
                    if item and item.lower() not in _DETERMINISTIC_PREFILL_SENTINELS:
                        items.append(item)
                    j += 1
                i = j
                if items:
                    value = ", ".join(items)
            else:
                i += 1

            if field.name in hits:
                continue
            if not value or value.lower() in _DETERMINISTIC_PREFILL_SENTINELS:
                continue
            coerced, errs = _coerce_and_validate(field, value)
            if errs or coerced is None:
                continue
            hits[field.name] = coerced
    return hits


__all__ = ["run_user_input_step"]
