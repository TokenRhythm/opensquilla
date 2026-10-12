"""SOP→DAG compiler — translates ``kind: meta_sop`` SKILL.md into the
standard ``composition.steps`` YAML before the parser sees it.

Four stages:

1. :func:`_lex` — line-based scanner producing tokens with ``SourceSpan``.
2. :func:`_parse` — token stream → ``SOPDocument`` AST.
3. :func:`_resolve` — skill lookup + kind inference per invocation.
4. :func:`_emit` — AST → ``composition_raw`` dict.

Public surface:

* :func:`compile` — driver that runs all four stages and returns a fresh
  ``SkillSpec(kind="meta", composition_raw=..., sop_source=...)``.
* :class:`SOPCompileError` — parse-time error, subclass of
  :class:`MetaPlanError` so the loader's existing error path catches it.
* :class:`SourceSpan` — line/column/excerpt for error reporting; carried
  on every AST node.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import yaml

from opensquilla.skills.meta.parser import MetaPlanError

if TYPE_CHECKING:
    from opensquilla.skills.types import SkillSpec


@dataclass(frozen=True)
class SourceSpan:
    """Source location for an AST node or error pointer.

    Lines and columns are 1-indexed (matches editor display conventions).
    ``excerpt`` is the literal source text the span covers; truncated to
    ~80 chars when stored. Carried on every node so errors render as
    ``Phase N (line K): <reason>\\n> <excerpt>``.
    """

    start_line: int
    start_col: int
    end_line: int
    end_col: int
    excerpt: str


class SOPCompileError(MetaPlanError):
    """Compile-time SOP parse/resolve/emit failure.

    Subclasses :class:`MetaPlanError` so the loader's existing
    ``except MetaPlanError`` path catches us. Carries structured fields
    (skill_name, phase_index, span, reason) so callers can render rich
    diagnostics; ``str(exc)`` produces a human-readable one-block format.
    """

    def __init__(
        self,
        *,
        skill_name: str,
        phase_index: int | None,
        span: SourceSpan | None,
        reason: str,
    ) -> None:
        self.skill_name = skill_name
        self.phase_index = phase_index
        self.span = span
        self.reason = reason
        super().__init__(self._render())

    def _render(self) -> str:
        parts: list[str] = [self.skill_name]
        if self.phase_index is not None:
            parts.append(f"Phase {self.phase_index}")
        if self.span is not None:
            parts.append(f"line {self.span.start_line}")
        head = ":".join(parts) + ": " + self.reason
        if self.span is not None and self.span.excerpt:
            return head + "\n> " + self.span.excerpt
        return head


class TokenType(enum.Enum):
    """Tokens emitted by :func:`_lex`."""

    FRONTMATTER_END = "frontmatter_end"
    PHASE_HEADING = "phase_heading"
    FENCED_YAML_FOR_EACH = "fenced_yaml_for_each"
    INVOCATION_LINE = "invocation_line"  # Run/Invoke/Call tool/Classify
    WITH_BULLET = "with_bullet"
    SAVE_AS_LINE = "save_as_line"
    BLANK = "blank"
    TEXT = "text"  # any other body line


@dataclass(frozen=True)
class Token:
    type: TokenType
    span: SourceSpan
    payload: dict[str, str] = field(default_factory=dict)


_PHASE_HEADING_RE = re.compile(
    r"^##\s+Phase\s+(?P<num>\d+)\s*:\s*(?P<title>[^\[]+?)\s*"
    r"(?:\[(?P<annotations>(?:[^\[\]]|\[[^\[\]]*\])*)\])?\s*$",
)
_INVOCATION_RUN_RE = re.compile(r"^(?P<verb>Run|Invoke|Call tool|Classify)\s+")
_WITH_BULLET_RE = re.compile(r"^-\s+(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>.+)$")
_SAVE_AS_RE = re.compile(r"^Save\s+as\s+`(?P<id>[^`]+)`\s*\.?\s*$")
_FENCE_START_RE = re.compile(r"^```(?P<lang>[A-Za-z_][A-Za-z0-9_ ]*)?\s*$")
_FOR_EACH_FENCE_HINT = "yaml for_each"


def _lex(body: str) -> Iterator[Token]:
    """Tokenize a SOP body line by line.

    Frontmatter is NOT handled here — the loader strips it before calling
    :func:`compile`. The body input is the markdown after the closing
    ``---``.

    Lexer skips the contents of generic fenced code blocks (\\`\\`\\` ...) so
    that markdown documentation code blocks don't confuse the parser.
    The single exception is fenced blocks tagged ``yaml for_each``: these
    are captured wholesale as a single ``FENCED_YAML_FOR_EACH`` token so
    the parser can ``yaml.safe_load`` the contents.
    """

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        stripped = raw.strip()

        # Fenced code block detection
        fence_match = _FENCE_START_RE.match(raw)
        if fence_match:
            lang = (fence_match.group("lang") or "").strip()
            # Find the closing fence (a bare ```)
            j = i + 1
            while j < len(lines) and lines[j].strip() != "```":
                j += 1
            fence_lines = lines[i + 1 : j]
            if lang == _FOR_EACH_FENCE_HINT:
                yield Token(
                    type=TokenType.FENCED_YAML_FOR_EACH,
                    span=SourceSpan(
                        start_line=line_no + 1,  # first line inside fence
                        start_col=0,
                        end_line=j,
                        end_col=0,
                        excerpt="\n".join(fence_lines),
                    ),
                )
            # else: silently skip docs code blocks
            i = j + 1
            continue

        if not stripped:
            yield Token(
                type=TokenType.BLANK,
                span=SourceSpan(
                    start_line=line_no,
                    start_col=0,
                    end_line=line_no,
                    end_col=0,
                    excerpt="",
                ),
            )
            i += 1
            continue

        m = _PHASE_HEADING_RE.match(raw)
        if m:
            payload = {
                "num": m.group("num"),
                "title": m.group("title").strip(),
                "annotations": (m.group("annotations") or "").strip(),
            }
            yield Token(
                type=TokenType.PHASE_HEADING,
                span=SourceSpan(
                    start_line=line_no,
                    start_col=0,
                    end_line=line_no,
                    end_col=len(raw),
                    excerpt=raw[:120],
                ),
                payload=payload,
            )
            i += 1
            continue

        if _INVOCATION_RUN_RE.match(stripped):
            yield Token(
                type=TokenType.INVOCATION_LINE,
                span=SourceSpan(
                    start_line=line_no,
                    start_col=0,
                    end_line=line_no,
                    end_col=len(raw),
                    excerpt=raw[:120],
                ),
            )
            i += 1
            continue

        if _SAVE_AS_RE.match(stripped):
            m_save = _SAVE_AS_RE.match(stripped)
            assert m_save is not None
            yield Token(
                type=TokenType.SAVE_AS_LINE,
                span=SourceSpan(
                    start_line=line_no,
                    start_col=0,
                    end_line=line_no,
                    end_col=len(raw),
                    excerpt=raw[:120],
                ),
                payload={"id": m_save.group("id")},
            )
            i += 1
            continue

        wm = _WITH_BULLET_RE.match(stripped)
        if wm:
            yield Token(
                type=TokenType.WITH_BULLET,
                span=SourceSpan(
                    start_line=line_no,
                    start_col=0,
                    end_line=line_no,
                    end_col=len(raw),
                    excerpt=raw[:120],
                ),
                payload={"key": wm.group("key"), "value": wm.group("value").strip()},
            )
            i += 1
            continue

        yield Token(
            type=TokenType.TEXT,
            span=SourceSpan(
                start_line=line_no,
                start_col=0,
                end_line=line_no,
                end_col=len(raw),
                excerpt=raw[:120],
            ),
        )
        i += 1


@dataclass(frozen=True)
class SOPInvocation:
    """One ``Run`` / ``Invoke`` block inside a phase."""

    skill_name: str
    kind_hint: str | None  # 'agent' | 'skill_exec' | None
    with_args: dict[str, str]
    step_id_template: str
    span: SourceSpan


@dataclass(frozen=True)
class SOPPhase:
    """A `## Phase N: title [annotations]` block plus its body."""

    index: int
    title: str
    annotations: dict[str, str]
    invocations: tuple[SOPInvocation, ...]
    for_each_var: str | None = None
    for_each_items: tuple[dict[str, str], ...] = ()
    span: SourceSpan | None = None


@dataclass(frozen=True)
class SOPDocument:
    phases: tuple[SOPPhase, ...]


_INVOCATION_DETAILED_RE = re.compile(
    r"^(?P<verb>Run|Invoke)\s+`(?P<skill>[A-Za-z0-9_\-]+)`"
    r"(?:\s+as\s+(?P<kind>[A-Za-z_]+))?"
    r"(?:\s+with\s*:)?"
    r"\.?\s*$",
)
# Combined single-line form: ``Run `skill`. Save as `id`.``
_INVOCATION_COMBINED_RE = re.compile(
    r"^(?P<verb>Run|Invoke)\s+`(?P<skill>[A-Za-z0-9_\-]+)`"
    r"(?:\s+as\s+(?P<kind>[A-Za-z_]+))?"
    r"\s*\.\s+Save\s+as\s+`(?P<id>[^`]+)`\s*\.?\s*$",
)
_STDIN_PROSE_RE = re.compile(r"\bPipe\b.*\bto\s+stdin\b", re.IGNORECASE)
_ASSEMBLE_PROSE_RE = re.compile(r"\bAssemble\b.*\bfrom\s+template\b", re.IGNORECASE)
_SUPPORTED_KIND_HINTS = frozenset({"agent", "skill_exec"})
_ALLOWED_ANNOTATIONS = frozenset({"parallel", "parallel for_each", "depends_on"})
_REJECTED_ANNOTATIONS = frozenset({"when", "force_skip", "route"})


def _parse_annotations(
    raw: str, *, skill_name: str, phase_index: int, span: SourceSpan,
) -> dict[str, str]:
    """Parse ``[a; b: c; d]`` annotation block into a dict.

    Reject unsupported annotations explicitly so authors get a clear error.
    """

    result: dict[str, str] = {}
    if not raw:
        return result
    for raw_item in raw.split(";"):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            key, _, value = item.partition(":")
            key = key.strip()
            value = value.strip()
        else:
            key, value = item, ""
        if key in _REJECTED_ANNOTATIONS:
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=span,
                reason=f"annotation {key!r} not in MVP scope (deferred to a future phase)",
            )
        if key not in _ALLOWED_ANNOTATIONS:
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=span,
                reason=f"unknown annotation {key!r}; allowed: {sorted(_ALLOWED_ANNOTATIONS)}",
            )
        result[key] = value
    return result


def _parse_for_each_block(
    text: str,
    *,
    skill_name: str,
    phase_index: int,
    span: SourceSpan,
    expected_var: str,
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Load ``yaml for_each`` block and validate item shape."""

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=span,
            reason=f"for_each YAML invalid: {exc}",
        ) from exc
    if not isinstance(data, dict) or len(data) != 1:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=span,
            reason="for_each block must have exactly one top-level key (the loop variable)",
        )
    var_name, items = next(iter(data.items()))
    if var_name != expected_var:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=span,
            reason=(
                f"for_each block key {var_name!r} does not match "
                f"annotation variable {expected_var!r}"
            ),
        )
    if not isinstance(items, list) or not items:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=span,
            reason=f"for_each {var_name!r} must be a non-empty list",
        )
    seen_ids: set[str] = set()
    normalised: list[dict[str, str]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=span,
                reason=f"for_each item[{idx}] must be a mapping, got {type(item).__name__}",
            )
        if "id" not in item or not isinstance(item["id"], str):
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=span,
                reason=f"for_each item[{idx}] missing required 'id' field",
            )
        if item["id"] in seen_ids:
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=span,
                reason=f"for_each duplicate id {item['id']!r} at item[{idx}]",
            )
        seen_ids.add(item["id"])
        normalised.append({k: str(v) for k, v in item.items()})
    return var_name, tuple(normalised)


def _parse(tokens: list[Token], *, skill_name: str) -> SOPDocument:
    """Group lexer tokens into ``SOPPhase`` objects.

    Each ``Run``/``Invoke`` line starts an invocation that consumes
    subsequent ``WITH_BULLET`` lines until ``Save as``.
    """

    phases: list[SOPPhase] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type != TokenType.PHASE_HEADING:
            i += 1
            continue

        phase_index = int(tok.payload["num"])
        title = tok.payload["title"]
        annotations = _parse_annotations(
            tok.payload["annotations"],
            skill_name=skill_name,
            phase_index=phase_index,
            span=tok.span,
        )
        for_each_var: str | None = None
        for_each_items: tuple[dict[str, str], ...] = ()
        if "parallel for_each" in annotations:
            for_each_var = annotations["parallel for_each"]
            if not for_each_var:
                raise SOPCompileError(
                    skill_name=skill_name,
                    phase_index=phase_index,
                    span=tok.span,
                    reason="parallel for_each missing variable name",
                )

        i += 1
        # collect invocations + (optional) for_each yaml block
        invocations: list[SOPInvocation] = []
        current_skill: str | None = None
        current_kind: str | None = None
        current_with: dict[str, str] = {}
        current_inv_span: SourceSpan | None = None
        for_each_seen = False

        while i < len(tokens) and tokens[i].type != TokenType.PHASE_HEADING:
            t = tokens[i]
            if t.type == TokenType.FENCED_YAML_FOR_EACH:
                if for_each_var is None:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason=(
                            "fenced 'yaml for_each' block without "
                            "[parallel for_each: VAR] annotation"
                        ),
                    )
                if for_each_seen:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason="multiple 'yaml for_each' blocks in one phase",
                    )
                _, items = _parse_for_each_block(
                    t.span.excerpt,
                    skill_name=skill_name,
                    phase_index=phase_index,
                    span=t.span,
                    expected_var=for_each_var,
                )
                for_each_items = items
                for_each_seen = True
                i += 1
                continue
            if t.type == TokenType.INVOCATION_LINE:
                if current_skill is not None:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason="new invocation started before previous one's `Save as` line",
                    )
                line = t.span.excerpt
                # Reject stdin/assemble prose patterns explicitly.
                if _STDIN_PROSE_RE.search(line):
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason=(
                            "step-level stdin not supported in MVP; "
                            "declare it on the callee skill's entrypoint instead"
                        ),
                    )
                if _ASSEMBLE_PROSE_RE.search(line):
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason=(
                            "step-level assemble not supported in MVP; "
                            "declare it on the callee skill's entrypoint instead"
                        ),
                    )
                combined = _INVOCATION_COMBINED_RE.match(line)
                if combined is not None:
                    kind = combined.group("kind")
                    if kind is not None and kind not in _SUPPORTED_KIND_HINTS:
                        raise SOPCompileError(
                            skill_name=skill_name,
                            phase_index=phase_index,
                            span=t.span,
                            reason=(
                                f"unknown kind {kind!r}; must be one of "
                                f"{sorted(_SUPPORTED_KIND_HINTS)}"
                            ),
                        )
                    invocations.append(
                        SOPInvocation(
                            skill_name=combined.group("skill"),
                            kind_hint=kind,
                            with_args={},
                            step_id_template=combined.group("id"),
                            span=t.span,
                        ),
                    )
                    i += 1
                    continue
                m = _INVOCATION_DETAILED_RE.match(line)
                if not m:
                    if line.lstrip().startswith(("Call tool", "Classify")):
                        raise SOPCompileError(
                            skill_name=skill_name,
                            phase_index=phase_index,
                            span=t.span,
                            reason=(
                                "Call tool/Classify patterns recognised but not "
                                "implemented in v1; use Run/Invoke instead"
                            ),
                        )
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason=f"invocation line does not match Run/Invoke grammar: {line!r}",
                    )
                current_skill = m.group("skill")
                current_kind = m.group("kind")
                if current_kind is not None and current_kind not in _SUPPORTED_KIND_HINTS:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason=(
                            f"unknown kind {current_kind!r}; must be one of "
                            f"{sorted(_SUPPORTED_KIND_HINTS)}"
                        ),
                    )
                current_with = {}
                current_inv_span = t.span
                i += 1
                continue
            if t.type == TokenType.WITH_BULLET:
                if current_skill is None:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason="with-bullet outside an invocation block",
                    )
                current_with[t.payload["key"]] = t.payload["value"]
                i += 1
                continue
            if t.type == TokenType.SAVE_AS_LINE:
                if current_skill is None:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase_index,
                        span=t.span,
                        reason="`Save as` line outside an invocation block",
                    )
                assert current_inv_span is not None
                invocations.append(
                    SOPInvocation(
                        skill_name=current_skill,
                        kind_hint=current_kind,
                        with_args=current_with,
                        step_id_template=t.payload["id"],
                        span=current_inv_span,
                    ),
                )
                current_skill = None
                current_kind = None
                current_with = {}
                current_inv_span = None
                i += 1
                continue
            # BLANK / TEXT — skip
            i += 1

        if current_skill is not None:
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=current_inv_span,
                reason="invocation missing `Save as` line",
            )
        if not invocations:
            raise SOPCompileError(
                skill_name=skill_name,
                phase_index=phase_index,
                span=tok.span,
                reason="phase contains no invocations",
            )

        phases.append(
            SOPPhase(
                index=phase_index,
                title=title,
                annotations=annotations,
                invocations=tuple(invocations),
                for_each_var=for_each_var,
                for_each_items=for_each_items,
                span=tok.span,
            ),
        )

    if not phases:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=None,
            span=None,
            reason="no '## Phase N:' headings found in SOP body",
        )
    return SOPDocument(phases=tuple(phases))


class _LoaderProtocol(Protocol):
    """Minimal interface the SOP compiler needs from a SkillLoader."""

    def get_by_name(self, name: str) -> SkillSpec | None: ...


def _resolve_kind(
    invocation: SOPInvocation,
    *,
    skill_loader: _LoaderProtocol,
    skill_name: str,
    phase_index: int,
) -> str:
    """Return the effective step ``kind`` for an invocation.

    Precedence:
        1. Explicit ``as <kind>`` on the invocation line.
        2. Skill has ``entrypoint:`` → ``skill_exec``.
        3. Default → ``agent``.

    Raises :class:`SOPCompileError` if the referenced skill is not
    registered with the loader (so authors get a clear error pointing
    to the offending phase + line + excerpt).
    """

    spec = skill_loader.get_by_name(invocation.skill_name)
    if spec is None:
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=invocation.span,
            reason=f"skill {invocation.skill_name!r} not registered in the loader",
        )

    if invocation.kind_hint is not None:
        return invocation.kind_hint

    entrypoint = getattr(spec, "entrypoint", None)
    if isinstance(entrypoint, dict) and entrypoint:
        return "skill_exec"
    return "agent"


def _strip_inline_backticks(value: str) -> str:
    """Strip surrounding backticks from a ``with:`` bullet value if present.

    Authors often write `` - section: `{{ inputs.x }}` `` for readability;
    the emitted YAML should hold the raw template, not the backticks.
    """

    if len(value) >= 2 and value[0] == "`" and value[-1] == "`":
        return value[1:-1]
    return value


def _emit_step_from_invocation(
    inv: SOPInvocation,
    *,
    kind: str,
    depends_on: list[str],
    step_id: str,
    with_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a single ``composition.steps`` entry from an SOPInvocation."""

    final_with: dict[str, str] = {
        k: _strip_inline_backticks(v) for k, v in inv.with_args.items()
    }
    if with_overrides:
        final_with.update(with_overrides)
    step: dict[str, object] = {
        "id": step_id,
        "kind": kind,
        "skill": inv.skill_name,
        "depends_on": depends_on,
    }
    if final_with:
        step["with"] = final_with
    return step


_LOOP_REF_RE = re.compile(
    r"\{\{\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
)


def _substitute_loop_vars(
    template: str,
    *,
    loop_var: str,
    item: dict[str, str],
    skill_name: str,
    phase_index: int,
    span: SourceSpan,
) -> str | None:
    """Resolve ``{{ <loop_var>.<field> }}`` references at compile time.

    Returns the substituted string, or ``None`` if the template is *just*
    a single loop reference and that field is missing from ``item``
    (signals: drop the bullet entirely).

    If the template mixes a missing loop reference with other content
    (e.g. ``"Figure for {{ section.figure_path }}"``), raises an error.

    Non-loop templates (``{{ outputs.foo }}``, ``{{ inputs.bar }}``) pass
    through unchanged.
    """

    matches = list(_LOOP_REF_RE.finditer(template))
    relevant = [m for m in matches if m.group("var") == loop_var]
    if not relevant:
        # No loop-var references: pass through as a runtime template.
        return template

    # Check for missing fields.
    missing: list[str] = []
    for m in relevant:
        if m.group("field") not in item:
            missing.append(m.group("field"))

    if missing:
        # If the ENTIRE template is a single missing loop reference,
        # signal "drop this bullet" by returning None.
        stripped_inner = template.strip()
        if (
            len(relevant) == 1
            and stripped_inner.startswith("{{")
            and stripped_inner.endswith("}}")
            and stripped_inner.count("{{") == 1
        ):
            return None
        # Otherwise mixed content: raise.
        raise SOPCompileError(
            skill_name=skill_name,
            phase_index=phase_index,
            span=span,
            reason=(
                f"for_each item {item.get('id', '?')!r} missing field(s) "
                f"{missing!r} referenced in template; either define all "
                f"items with this field or move the reference to its "
                f"own bullet so the omission rule can drop it"
            ),
        )

    # All fields present — substitute (only the loop-var refs; runtime
    # templates pass through).
    def _replace(m: re.Match[str]) -> str:
        if m.group("var") != loop_var:
            return m.group(0)  # leave runtime templates untouched
        return item[m.group("field")]

    return _LOOP_REF_RE.sub(_replace, template)


def _parse_depends_on_value(raw: str) -> list[str]:
    """Parse a ``depends_on:`` annotation value as either a single id or ``[a, b, c]``.

    Whitespace around items is stripped.
    """

    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [s.strip() for s in inner.split(",") if s.strip()]
    return [raw]


def _emit(
    doc: SOPDocument,
    *,
    skill_loader: _LoaderProtocol,
    skill_name: str,
) -> dict[str, list[dict[str, object]]]:
    all_steps: list[dict[str, object]] = []
    previous_phase_step_ids: list[str] = []
    seen_ids: set[str] = set()

    for phase in doc.phases:
        # Resolve depends_on: explicit annotation overrides sequential default.
        if "depends_on" in phase.annotations:
            depends_on = _parse_depends_on_value(phase.annotations["depends_on"])
            for dep in depends_on:
                if dep not in seen_ids:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase.index,
                        span=phase.span,
                        reason=(
                            f"depends_on references unknown step id {dep!r}; "
                            f"defined ids so far: {sorted(seen_ids)}"
                        ),
                    )
        else:
            depends_on = list(previous_phase_step_ids)
        phase_step_ids: list[str] = []

        if phase.for_each_var is not None:
            # Fan-out: each item produces one step.
            if len(phase.invocations) != 1:
                raise SOPCompileError(
                    skill_name=skill_name,
                    phase_index=phase.index,
                    span=phase.span,
                    reason="for_each phase must have exactly one invocation block",
                )
            inv = phase.invocations[0]
            kind = _resolve_kind(
                inv,
                skill_loader=skill_loader,
                skill_name=skill_name,
                phase_index=phase.index,
            )
            for item in phase.for_each_items:
                # Substitute step id template
                step_id_resolved = _substitute_loop_vars(
                    inv.step_id_template,
                    loop_var=phase.for_each_var,
                    item=item,
                    skill_name=skill_name,
                    phase_index=phase.index,
                    span=inv.span,
                )
                if step_id_resolved is None:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase.index,
                        span=inv.span,
                        reason=(
                            f"for_each item {item.get('id', '?')!r} produces an "
                            f"empty step id"
                        ),
                    )
                if step_id_resolved in seen_ids:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase.index,
                        span=inv.span,
                        reason=(
                            f"duplicate step id {step_id_resolved!r} (already "
                            f"defined in an earlier phase)"
                        ),
                    )
                seen_ids.add(step_id_resolved)

                # Build per-item with_args; drop bullets that reduce to None
                final_with: dict[str, str] = {}
                for key, value in inv.with_args.items():
                    unwrapped = _strip_inline_backticks(value)
                    substituted = _substitute_loop_vars(
                        unwrapped,
                        loop_var=phase.for_each_var,
                        item=item,
                        skill_name=skill_name,
                        phase_index=phase.index,
                        span=inv.span,
                    )
                    if substituted is None:
                        continue  # field omission rule
                    final_with[key] = substituted

                step: dict[str, object] = {
                    "id": step_id_resolved,
                    "kind": kind,
                    "skill": inv.skill_name,
                    "depends_on": depends_on,
                }
                if final_with:
                    step["with"] = final_with
                all_steps.append(step)
                phase_step_ids.append(step_id_resolved)
        else:
            # Plain phase
            is_parallel = "parallel" in phase.annotations
            if not is_parallel and len(phase.invocations) > 1:
                raise SOPCompileError(
                    skill_name=skill_name,
                    phase_index=phase.index,
                    span=phase.span,
                    reason=(
                        f"phase has {len(phase.invocations)} invocations but no "
                        f"[parallel] annotation — add [parallel] or split into "
                        f"multiple phases"
                    ),
                )
            for inv in phase.invocations:
                if inv.step_id_template in seen_ids:
                    raise SOPCompileError(
                        skill_name=skill_name,
                        phase_index=phase.index,
                        span=inv.span,
                        reason=f"duplicate step id {inv.step_id_template!r}",
                    )
                seen_ids.add(inv.step_id_template)
                kind = _resolve_kind(
                    inv,
                    skill_loader=skill_loader,
                    skill_name=skill_name,
                    phase_index=phase.index,
                )
                step = _emit_step_from_invocation(
                    inv,
                    kind=kind,
                    depends_on=depends_on,
                    step_id=inv.step_id_template,
                )
                all_steps.append(step)
                phase_step_ids.append(inv.step_id_template)
        previous_phase_step_ids = phase_step_ids

    return {"steps": all_steps}


def compile(  # noqa: A001 — public API; standard `compile` shadow is acceptable here
    spec: SkillSpec,
    *,
    skill_loader: _LoaderProtocol,
) -> SkillSpec:
    """Compile a ``kind: meta_sop`` SkillSpec into a normalised ``kind: meta`` one.

    The returned spec has:
    - ``kind = "meta"`` so the existing parser/orchestrator accept it
    - ``composition_raw`` populated with the compiled DAG
    - All other frontmatter fields (triggers, priority, etc.) preserved

    The input spec is NOT mutated; a fresh ``SkillSpec`` is returned.
    """

    if spec.kind != "meta_sop":
        raise ValueError(
            f"sop_compiler.compile expects kind='meta_sop', got {spec.kind!r}",
        )

    body = spec.content or ""
    tokens = list(_lex(body))
    doc = _parse(tokens, skill_name=spec.name)
    composition_raw: dict[str, object] = dict(
        _emit(doc, skill_loader=skill_loader, skill_name=spec.name),
    )

    # Build a new SkillSpec; do not mutate the input.
    from dataclasses import replace

    return replace(
        spec,
        kind="meta",
        composition_raw=composition_raw,
    )


__all__ = [
    "SOPCompileError",
    "SourceSpan",
    "compile",
]
