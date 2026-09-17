"""Jinja-templated argument rendering for MetaOrchestrator steps.

Houses the restricted Jinja environment used to render ``with_args`` /
``tool_args`` / ``entrypoint`` templates, the route resolver that picks one
``RouteCase`` per step, and the small text formatters that turn rendered
arguments into the user-message payload of a sub-Agent (or the classifier
prompt body, or the choice coercion at reply time).

These functions are deliberately stateless and free of orchestrator
internals so they can be re-used by future executors (sop_block, policy
attachments) without dragging the scheduler in.
"""

from __future__ import annotations

import html
import re
from decimal import Decimal
from typing import Any

import jinja2
import jinja2.sandbox

from opensquilla.skills.meta.types import MetaStep, RouteCase

# ---------------------------------------------------------------------------
# Restricted Jinja environment for ``with_args`` rendering
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:/[\w./@%+\-=]+|[\w./@%+\-=]+?\."
    r"(?:md|txt|csv|tsv|json|yaml|yml|xlsx))"
    r"(?![\w.-])",
    re.IGNORECASE,
)
_PATH_TRAILING_PUNCT = "`\"'，。；;,)）]】"
_SHORT_DRAMA_SECTION_HEADER_RE = re.compile(
    r"(?m)^=== (OVERVIEW|SHOT_(?:10|[1-9])) ===[ \t]*\r?$",
)
_SHORT_DRAMA_ANY_SECTION_HEADER_RE = re.compile(
    r"(?m)^=== ([A-Z][A-Z0-9_]*) ===[ \t]*\r?$",
)
_SHORT_DRAMA_SECTION_NAME_RE = re.compile(r"(?:OVERVIEW|SHOT_(?:10|[1-9]))")
_SHORT_DRAMA_REFERENCE_OVERVIEW_FIELDS = frozenset(
    {"IDENTITY_ANCHOR", "RENDER_STYLE"},
)
_SHORT_DRAMA_REFERENCE_SHOT_FIELDS = frozenset({"IMAGE_PROMPT", "VIDEO_PROMPT"})
_SHORT_DRAMA_DURATION_FIELD_RE = re.compile(
    r"(?m)^[ \t]*DURATION_S:[ \t]*(.*?)[ \t]*\r?$",
)
_SHORT_DRAMA_DECLARED_SHOTS_FIELD_RE = re.compile(
    r"(?m)^[ \t]*N_SHOTS:[ \t]*(.*?)[ \t]*\r?$",
)


def _filter_xml_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _filter_truncate(value: object, length: int = 1024) -> str:
    text = str(value)
    if length <= 0 or len(text) <= length:
        return text
    return text[:length]


def _filter_short_drama_section(value: object, section: object) -> str:
    """Return one complete strict-format drama section without clipping it.

    Media-preparation prompts need the exact consented shot bytes, but sending
    the whole ten-shot script to every extractor wastes context.  Sectioning
    preserves the requested block verbatim and bounds each per-shot prompt by
    the script format's own field budgets instead of an arbitrary global
    character cutoff.
    """

    text = str(value or "")
    requested = str(section or "").strip().upper()
    if _SHORT_DRAMA_SECTION_NAME_RE.fullmatch(requested) is None:
        return ""
    headers = list(_SHORT_DRAMA_SECTION_HEADER_RE.finditer(text))
    for index, header in enumerate(headers):
        if header.group(1) != requested:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        return text[header.start() : end]
    return ""


def _filter_short_drama_reference_context(value: object) -> str:
    """Keep the exact identity-bearing fields from every consented shot.

    The reference-card model must inspect all ten shots, while unrelated
    voiceover and card copy needlessly consume its context window.  This is a
    structural projection, not a character cutoff: every complete relevant
    field from the immutable script snapshot is retained verbatim.
    """

    text = str(value or "")
    headers = list(_SHORT_DRAMA_SECTION_HEADER_RE.finditer(text))
    context_lines: list[str] = []
    for index, header in enumerate(headers):
        section = header.group(1)
        allowed = (
            _SHORT_DRAMA_REFERENCE_OVERVIEW_FIELDS
            if section == "OVERVIEW"
            else _SHORT_DRAMA_REFERENCE_SHOT_FIELDS
        )
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        selected = [header.group(0).rstrip()]
        for line in text[header.end() : end].splitlines():
            field, separator, _value = line.partition(":")
            if separator and field.strip() in allowed:
                selected.append(line)
        context_lines.extend(selected)
    return "\n".join(context_lines)


def _filter_slugify(value: object) -> str:
    return _SLUG_RE.sub("-", str(value)).strip("-").lower()[:128]


def _filter_extract_path(value: object, suffix: str = "") -> str:
    wanted = str(suffix or "").strip().lower().lstrip(".")
    for match in _PATH_TOKEN_RE.findall(str(value)):
        token = match.strip().strip(_PATH_TRAILING_PUNCT)
        if not token:
            continue
        if not wanted or token.lower().endswith(f".{wanted}"):
            return str(token)
    return ""


def _filter_contains_cjk(value: object) -> bool:
    return bool(re.search(r"[\u3400-\u9fff\uf900-\ufaff]", str(value or "")))


def _filter_int(value: object, default: int = 0) -> int:
    """Parse ``value`` as an integer, returning ``default`` on failure."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        match = re.search(r"-?\d+", text)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return default
        return default


def _short_drama_single_integer_field(
    section: str,
    pattern: re.Pattern[str],
    *,
    field_name: str,
) -> int:
    values = pattern.findall(section)
    if len(values) != 1:
        raise ValueError(f"short-drama {field_name} must appear exactly once")
    value = values[0]
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError(f"short-drama {field_name} must be an integer")
    return int(value)


def _short_drama_duration_contract(value: object) -> dict[int, int]:
    """Parse the one duration contract used by consent and paid execution.

    The script is an immutable, user-approved execution snapshot.  Every paid
    video duration therefore comes directly from a unique canonical shot field,
    never from an LLM extraction or a permissive fallback.  Any ambiguity fails
    closed before a provider submission can be scheduled.
    """

    text = str(value or "")
    headers = list(_SHORT_DRAMA_SECTION_HEADER_RE.finditer(text))
    all_headers = list(_SHORT_DRAMA_ANY_SECTION_HEADER_RE.finditer(text))
    if not headers or len(headers) != len(all_headers):
        raise ValueError("short-drama script contains an invalid section header")
    if text[: headers[0].start()].strip():
        raise ValueError("short-drama script must begin with the overview section")

    section_names = [header.group(1) for header in headers]
    if section_names[0] != "OVERVIEW":
        raise ValueError("short-drama script must begin with the overview section")

    overview_end = headers[1].start() if len(headers) > 1 else len(text)
    overview = text[headers[0].end() : overview_end]
    shot_count = _short_drama_single_integer_field(
        overview,
        _SHORT_DRAMA_DECLARED_SHOTS_FIELD_RE,
        field_name="N_SHOTS",
    )
    if not 1 <= shot_count <= 10:
        raise ValueError("short-drama N_SHOTS must be between 1 and 10")

    expected_sections = ["OVERVIEW", *(f"SHOT_{number}" for number in range(1, shot_count + 1))]
    if section_names != expected_sections:
        raise ValueError("short-drama shot sections must be unique and contiguous")

    durations: dict[int, int] = {}
    for index, header in enumerate(headers[1:], start=1):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        section = text[header.end() : end]
        duration = _short_drama_single_integer_field(
            section,
            _SHORT_DRAMA_DURATION_FIELD_RE,
            field_name=f"SHOT_{index}.DURATION_S",
        )
        if not 3 <= duration <= 15:
            raise ValueError("short-drama shot DURATION_S must be between 3 and 15")
        durations[index] = duration

    overview_duration = _short_drama_single_integer_field(
        overview,
        _SHORT_DRAMA_DURATION_FIELD_RE,
        field_name="OVERVIEW.DURATION_S",
    )
    if overview_duration != sum(durations.values()):
        raise ValueError("short-drama overview duration must equal the shot-duration sum")
    return durations


def _filter_short_drama_duration_contract_valid(value: object) -> bool:
    try:
        _short_drama_duration_contract(value)
    except ValueError:
        return False
    return True


def _filter_short_drama_shot_duration(value: object, section: object) -> int:
    requested = str(section or "").strip().upper()
    match = re.fullmatch(r"SHOT_(10|[1-9])", requested)
    if match is None:
        raise ValueError("short-drama duration requires a SHOT_1..SHOT_10 selector")
    shot_number = int(match.group(1))
    durations = _short_drama_duration_contract(value)
    try:
        return durations[shot_number]
    except KeyError as exc:
        raise ValueError(f"short-drama script does not contain SHOT_{shot_number}") from exc


def _short_drama_cost_basis(value: object) -> tuple[int, Decimal, Decimal] | None:
    """Return active shots and exact provider-billed duration.

    OpenRouter bills the workflow's 3-second execution option as a 4-second
    generation before the local runtime trims the delivered clip.  All other
    valid durations are billed as requested.
    """

    try:
        durations = _short_drama_duration_contract(value)
    except ValueError:
        return None
    billed_duration = sum(4 if duration == 3 else duration for duration in durations.values())
    billed = Decimal(billed_duration)
    return len(durations), billed, billed


def _format_duration_range(low: Decimal, high: Decimal, *, language: str) -> str:
    def _format(value: Decimal) -> str:
        return format(value.normalize(), "f")

    if low == high:
        return f"{_format(low)} 秒" if language == "zh" else f"{_format(low)}s"
    separator = "-"
    if language == "zh":
        return f"{_format(low)}{separator}{_format(high)} 秒"
    return f"{_format(low)}-{_format(high)}s"


def _filter_short_drama_media_cost(value: object, language: str = "en") -> str:
    """Render a deterministic consent-time USD estimate for a drama script."""

    localized = "zh" if str(language).strip().lower().startswith("zh") else "en"
    basis = _short_drama_cost_basis(value)
    if basis is None:
        if localized == "zh":
            return (
                "本次脚本无法解析出镜头数和总时长，暂时无法给出可授权的美元估算区间；"
                "请先修订脚本，不要批准媒体生成。"
            )
        return (
            "The script does not expose a parseable shot count and total duration, "
            "so no authorizable USD estimate can be shown yet. Revise the script "
            "before approving media generation."
        )

    shot_count, duration_low, duration_high = basis
    image_low = Decimal(shot_count) * Decimal("0.05") + Decimal("0.05")
    image_high = Decimal(shot_count) * Decimal("0.05") + Decimal("0.10")
    total_low = image_low + duration_low * Decimal("0.15")
    total_high = image_high + duration_high * Decimal("0.15")
    cost_range = f"USD ${total_low:.2f}-${total_high:.2f}"
    duration = _format_duration_range(duration_low, duration_high, language=localized)

    if localized == "zh":
        return (
            f"本次脚本媒体成本估算：{shot_count} 个镜头，计费剧情时长 {duration}；"
            f"预计总计 {cost_range}。依据：镜头图约 $0.05/张、全角色参考图 "
            "$0.05-$0.10、视频约 $0.15/秒；实际账单由所选提供商决定。"
        )
    shot_label = "shot" if shot_count == 1 else "shots"
    return (
        f"Estimated media cost for this script: {shot_count} {shot_label}, "
        f"{duration} of billable story footage; estimated total {cost_range}. "
        "Basis: about $0.05 per shot image, $0.05-$0.10 for the full-cast "
        "reference image, and about $0.15 per video second. The selected "
        "provider determines the actual bill."
    )


def _build_jinja_env() -> jinja2.sandbox.ImmutableSandboxedEnvironment:
    # ``ImmutableSandboxedEnvironment`` blocks Python attribute introspection
    # (``__class__`` / ``__mro__`` / ``__subclasses__``) and mutation
    # operations on inputs. The previous ``jinja2.Environment`` cleared
    # globals and installed a filter allowlist, but left attribute access
    # open — a SKILL.md author could escape via
    # ``{{ inputs.__class__.__mro__[1].__subclasses__() }}``. Sandboxing
    # at the env level keeps ``inputs.get(...)`` / ``inputs['x']`` /
    # ``outputs.prev`` working for legitimate templates.
    env = jinja2.sandbox.ImmutableSandboxedEnvironment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        extensions=[],
        keep_trailing_newline=False,
    )
    # Strip unsafe globals/filters; install our allowlist.
    env.globals.clear()
    env.filters = {
        "xml_escape": _filter_xml_escape,
        "truncate": _filter_truncate,
        "short_drama_section": _filter_short_drama_section,
        "short_drama_reference_context": _filter_short_drama_reference_context,
        "slugify": _filter_slugify,
        "tojson": jinja2.filters.do_tojson,
        "default": jinja2.filters.do_default,
        "length": len,
        "join": jinja2.filters.do_join,
        "lower": lambda value: str(value).lower(),
        "extract_path": _filter_extract_path,
        "contains_cjk": _filter_contains_cjk,
        "int": _filter_int,
        "short_drama_duration_contract_valid": (
            _filter_short_drama_duration_contract_valid
        ),
        "short_drama_shot_duration": _filter_short_drama_shot_duration,
        "short_drama_media_cost": _filter_short_drama_media_cost,
    }
    return env


_JINJA_ENV = _build_jinja_env()


def render_with_args(
    template_map: dict[str, Any],
    *,
    inputs: dict[str, Any],
    outputs: dict[str, str],
) -> dict[str, Any]:
    """Render every leaf string in ``template_map`` against ``inputs/outputs``.

    Non-string leaves pass through unchanged. Nested dicts / lists are walked
    recursively. A ``jinja2.UndefinedError`` becomes a regular ValueError so
    the orchestrator's StepFailure handling treats it as a normal failure.
    """

    context = {
        "inputs": inputs,
        "outputs": outputs,
    }

    def _render(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return _JINJA_ENV.from_string(value).render(**context)
            except jinja2.UndefinedError as exc:
                raise ValueError(f"undefined template variable: {exc}") from exc
            except jinja2.TemplateSyntaxError as exc:
                raise ValueError(f"template syntax error: {exc}") from exc
            except jinja2.sandbox.SecurityError as exc:
                raise ValueError(
                    f"template security violation: {exc}",
                ) from exc
        if isinstance(value, dict):
            return {k: _render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_render(item) for item in value]
        return value

    rendered = _render(template_map)
    assert isinstance(rendered, dict)
    return rendered


def resolve_route(
    cases: tuple[RouteCase, ...],
    *,
    inputs: dict[str, Any],
    outputs: dict[str, str],
) -> str | None:
    """Return the ``to`` skill of the first case whose ``when`` evaluates truthy.

    Returns ``None`` when ``cases`` is empty or no case matches — caller falls
    back to the step's default ``skill`` name.  Jinja errors are surfaced as
    :class:`ValueError` so the orchestrator's step-failure path catches them.
    """

    if not cases:
        return None
    context = {"inputs": inputs, "outputs": outputs}
    for index, case in enumerate(cases):
        # Wrap the expression as ``{{ (<expr>) | tojson }}`` is overkill; use
        # Jinja's ``compile_expression`` so the user writes a real expression
        # (``outputs.classify == 'URL'``) rather than a template.
        try:
            expr = _JINJA_ENV.compile_expression(case.when)
        except jinja2.TemplateSyntaxError as exc:
            raise ValueError(
                f"route[{index}] when expression syntax error: {exc}",
            ) from exc
        try:
            value = expr(**context)
        except jinja2.UndefinedError as exc:
            raise ValueError(
                f"route[{index}] when references undefined variable: {exc}",
            ) from exc
        except jinja2.sandbox.SecurityError as exc:
            raise ValueError(
                f"route[{index}] when security violation: {exc}",
            ) from exc
        if value:
            return case.to
    return None


def evaluate_when(
    expression: str,
    *,
    inputs: dict[str, Any],
    outputs: dict[str, str],
) -> bool:
    """Evaluate a step-level ``when`` expression with route-like semantics."""

    if not expression:
        return True
    context = {"inputs": inputs, "outputs": outputs}
    try:
        expr = _JINJA_ENV.compile_expression(expression)
    except jinja2.TemplateSyntaxError as exc:
        raise ValueError(f"when expression syntax error: {exc}") from exc
    try:
        return bool(expr(**context))
    except jinja2.UndefinedError as exc:
        raise ValueError(f"when references undefined variable: {exc}") from exc
    except jinja2.sandbox.SecurityError as exc:
        raise ValueError(f"when security violation: {exc}") from exc


def format_step_prompt(
    skill_name: str,
    args: dict[str, Any],
    *,
    language_instruction: str = "",
) -> str:
    """Render the user-message payload that drives one sub-Agent turn."""

    if not args:
        prompt = (
            f"Run the {skill_name} skill with no arguments. "
            "Produce the deliverable described in its SKILL.md."
        )
        if language_instruction.strip():
            prompt = f"{prompt}\n\n{language_instruction.strip()}"
        return prompt

    lines = [f"Invoke the {skill_name} skill with the following arguments:"]
    for key, value in args.items():
        if isinstance(value, str):
            lines.append(f"- {key}: {value}")
        else:
            lines.append(f"- {key}: {value!r}")
    lines.append(
        "\nWhen the work is complete, reply with the final deliverable as plain text. "
        "If the skill produced a file, include the absolute path on the last line.",
    )
    if language_instruction.strip():
        lines.append(f"\n{language_instruction.strip()}")
    return "\n".join(lines)


def _format_classify_prompt(step: MetaStep, args: dict[str, Any]) -> str:
    """Render the user-message body for an ``llm_classify`` step.

    Concatenates the rendered ``with_args`` values into a flat prompt — the
    classifier system prompt already constrains the output, so we don't
    re-state the choices here.
    """

    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        text = value if isinstance(value, str) else repr(value)
        # Skip purely-decorative keys; otherwise prefix with the key for clarity.
        if key in ("text", "prompt", "task", "input"):
            parts.append(text)
        else:
            parts.append(f"{key}: {text}")
    return "\n".join(parts).strip()


def _expand_skill_placeholders(skill_spec: Any) -> str:
    """Substitute ``{baseDir}`` (and aliases) in a skill body with its real path.

    Bundled SKILL.md files reference helper scripts via ``{baseDir}/scripts/foo.py``.
    Regular skill invocation routes the body through tooling that resolves
    these placeholders; meta-skill composition injects the body directly into
    a sub-Agent system prompt, so we must do the same substitution here —
    otherwise the sub-Agent sees a literal ``{baseDir}`` and tries to glob
    the workspace for it.
    """

    body = (getattr(skill_spec, "content", "") or "").strip()
    base_dir = str(getattr(skill_spec, "base_dir", "") or "").rstrip("/")
    if not base_dir:
        return body
    # Cover both the canonical ``{baseDir}`` and the snake-case alias some
    # internal tools emit; keep substitution simple (no regex) so the body
    # remains byte-stable for callers that hash it.
    return body.replace("{baseDir}", base_dir).replace("{base_dir}", base_dir)


def _coerce_to_choice(raw: str, choices: list[str]) -> str:
    """Normalise a model reply to one of the allowed labels.

    Match precedence: exact → quote/punctuation-stripped → case-insensitive →
    uppercase-substring containment. When nothing matches the original trimmed
    text is returned — downstream route ``when`` clauses use Python's ``in``
    against it and can still succeed.
    """

    if not choices:
        return raw.strip()
    text = raw.strip()
    if text in choices:
        return text
    stripped = text.strip("'\"`.,!? \t\r\n")
    if stripped in choices:
        return stripped
    upper = stripped.upper()
    for choice in choices:
        if upper == choice.upper():
            return choice
    for choice in choices:
        if choice.upper() in upper:
            return choice
    return stripped or text


__all__ = [
    "_JINJA_ENV",
    "_coerce_to_choice",
    "_expand_skill_placeholders",
    "_format_classify_prompt",
    "format_step_prompt",
    "evaluate_when",
    "render_with_args",
    "resolve_route",
]
