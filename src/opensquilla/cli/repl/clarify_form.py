"""CLI clarify-form helpers (PR6).

When the gateway emits a meta-skill user_input pause signal, the CLI
REPL drops into an interactive prompt-toolkit form that collects the
fields one by one and submits them via ``chat.clarify_submit``.

This module is split in two:

* **Pure helpers** (this file's top section) — field-label rendering,
  input coercion, validation. No I/O, easy to unit-test.
* **Async prompter** (``prompt_clarify_form``) — wraps the helpers with
  ``prompt_toolkit.in_terminal`` so the outer ChatApplication
  temporarily releases the terminal while we collect input. The actual
  prompt function is parameterised via ``prompt_fn`` so tests can stub it.

Schema payload shape comes from
``opensquilla.skills.meta.clarify_schema.schema_to_protocol`` —
the same JSON dict the Web UI receives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Bool-coercion tables (mirror clarify_text.py so the CLI form
# accepts the same literals as a hand-typed reply). When extending
# either side, mirror the change here so multi-surface skills do not
# diverge — a user who answers "好" in chat must succeed the same way
# as a user who types "好" into the CLI form.
_TRUE_VALUES = frozenset({
    "true", "yes", "1", "on", "y",
    "是", "好", "对", "嗯", "可以", "确认", "没问题", "ok",
})
_FALSE_VALUES = frozenset({
    "false", "no", "0", "off", "n",
    "否", "不", "不要", "不行", "不用", "算了",
})


# ── Pure helpers ──


def render_field_label(field: dict[str, Any]) -> str:
    """Build a single-line prompt label for a clarify field.

    Examples:
    * ``destination (string, required): 目的地``
    * ``days (int 1-14, required): 天数``
    * ``budget (enum [budget|mid|premium], default=mid): 预算档次``
    """
    name = field.get("name", "?")
    type_ = field.get("type", "?")
    required = field.get("required", False)

    # Build the type qualifier (range / choices / max_chars / default).
    qualifier_bits: list[str] = []
    if type_ == "int":
        lo = field.get("min")
        hi = field.get("max")
        if lo is not None and hi is not None:
            qualifier_bits.append(f"{lo}-{hi}")
        elif lo is not None:
            qualifier_bits.append(f">={lo}")
        elif hi is not None:
            qualifier_bits.append(f"<={hi}")
    elif type_ == "enum":
        choices = field.get("choices") or []
        if choices:
            qualifier_bits.append("[" + "|".join(str(c) for c in choices) + "]")
    elif type_ == "string":
        max_chars = field.get("max_chars")
        if max_chars:
            qualifier_bits.append(f"≤{max_chars} chars")

    type_block = type_
    if qualifier_bits:
        type_block = f"{type_} {qualifier_bits[0]}"

    flags: list[str] = []
    if required:
        flags.append("required")
    elif "default" in field:
        flags.append(f"default={field['default']}")
    else:
        flags.append("optional")

    qualifier_str = f"{type_block}, {', '.join(flags)}"

    prompt = field.get("prompt") or ""
    suffix = f": {prompt}" if prompt else ""
    return f"{name} ({qualifier_str}){suffix}"


def coerce_field_input(
    field: dict[str, Any], raw: str,
) -> tuple[Any, str | None]:
    """Coerce a one-line user input to the field's declared type.

    Returns ``(value, None)`` on success, ``(None, error_message)`` on
    failure. Empty input on a non-required field returns ``(None, None)``
    so the caller can skip emitting the field.
    """
    value = (raw or "").strip()
    if not value:
        if field.get("required"):
            return None, f"{field.get('name', '?')} is required"
        return None, None

    type_ = field.get("type", "string")
    name = field.get("name", "?")

    if type_ == "string":
        max_chars = field.get("max_chars")
        if max_chars is not None and len(value) > max_chars:
            return (
                None,
                f"{name}: length {len(value)} exceeds max_chars={max_chars}",
            )
        return value, None

    if type_ == "int":
        try:
            n = int(value)
        except ValueError:
            return None, f"{name}: {value!r} is not an integer"
        lo = field.get("min")
        hi = field.get("max")
        if lo is not None and n < lo:
            return None, f"{name}: {n} is below min={lo}"
        if hi is not None and n > hi:
            return None, f"{name}: {n} is above max={hi}"
        return n, None

    if type_ == "bool":
        low = value.lower()
        if low in _TRUE_VALUES:
            return True, None
        if low in _FALSE_VALUES:
            return False, None
        return (
            None,
            f"{name}: {value!r} is not a bool (use true/false, yes/no, 1/0)",
        )

    if type_ == "enum":
        choices = field.get("choices") or []
        if value in choices:
            return value, None
        return (
            None,
            f"{name}: {value!r} not in choices {list(choices)}",
        )

    # Unknown type — surface as soft error (parser should have rejected
    # this earlier, but be defensive).
    return None, f"{name}: unknown field type {type_!r}"


def is_cancel_token(raw: str, cancel_keywords: list[str] | tuple[str, ...]) -> bool:
    """True if the user typed a configured cancel keyword."""
    if not cancel_keywords:
        return False
    low = (raw or "").strip().lower()
    for kw in cancel_keywords:
        if kw and kw.lower() == low:
            return True
    return False


# ── Async prompter ──


@dataclass
class ClarifyFormResult:
    """Outcome of an interactive clarify-form session.

    * ``fields``    — collected values; empty dict on cancel.
    * ``cancelled`` — True if user typed a cancel keyword or hit Ctrl-D.
    """

    fields: dict[str, Any]
    cancelled: bool


PromptFn = Callable[[str], Awaitable[str | None]]


async def prompt_clarify_form(
    schema: dict[str, Any],
    *,
    prompt_fn: PromptFn | None = None,
    writer: Callable[[str], None] | None = None,
) -> ClarifyFormResult:
    """Walk the schema's fields and collect values one by one.

    ``prompt_fn`` reads a single line (returns ``None`` on EOF / Ctrl-D).
    Defaults to a prompt_toolkit-backed session that runs inside
    ``in_terminal`` so it composes with the outer ChatApplication.

    ``writer`` prints status / error lines; defaults to ``print``.
    Tests pass a list-appending stub.

    When the schema carries the (d)-protocol auto-prefill payload —
    ``confirmed_fields`` / ``ambiguous_fields`` / ``unknown_mentions`` —
    the form prints a transparency header at the top, then offers each
    confirmed field for one-keystroke acceptance instead of
    re-collection. The user can still override any pre-filled value
    by typing a new one; pressing Enter on an empty line accepts the
    pre-filled value as-is. Ambiguous fields and unknown mentions are
    surfaced verbatim so the operator and the user both see what the
    system inferred and where it was uncertain.
    """
    print_fn = writer or print

    intro = schema.get("intro") or ""
    if intro:
        print_fn(intro)

    confirmed_entries = _entries(schema.get("confirmed_fields"))
    ambiguous_entries = _entries(schema.get("ambiguous_fields"))
    unknown_entries = _entries(schema.get("unknown_mentions"))
    confirmed_by_name: dict[str, Any] = {
        str(e.get("name")): e
        for e in confirmed_entries
        if isinstance(e, dict) and "name" in e
    }
    ambiguous_by_name: dict[str, str] = {
        str(e.get("name")): str(e.get("reason") or "")
        for e in ambiguous_entries
        if isinstance(e, dict) and "name" in e
    }

    if confirmed_by_name or ambiguous_by_name or unknown_entries:
        print_fn(
            "(we noticed details from your earlier messages — please "
            "confirm or override below)",
        )
    for name, entry in confirmed_by_name.items():
        print_fn(
            f"  ✓ {name} = {entry.get('value')!r} "
            f"(source: {entry.get('source') or 'auto_prefill'})",
        )
    for name, reason in ambiguous_by_name.items():
        suffix = f" — {reason}" if reason else ""
        print_fn(f"  ⚠ {name}: needs your input{suffix}")
    for entry in unknown_entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        guess = str(entry.get("guess") or "").strip()
        suffix = f" (looked like: {guess})" if guess else ""
        print_fn(f"  · we also noticed: {text}{suffix}")

    print_fn("(filling form — type a cancel keyword or hit Ctrl-D to abort)")

    cancel_keywords = schema.get("cancel_keywords") or []
    fields = schema.get("fields") or []

    if prompt_fn is None:
        prompt_fn = _default_prompt_fn()

    collected: dict[str, Any] = {}
    for field in fields:
        field_name = field.get("name")
        confirmed_entry = confirmed_by_name.get(str(field_name))
        ambiguous_reason = ambiguous_by_name.get(str(field_name))
        while True:
            label = render_field_label(field)
            extras: list[str] = []
            if confirmed_entry is not None:
                extras.append(
                    f"already filled: {confirmed_entry.get('value')!r} "
                    f"— press Enter to confirm, or type a new value to override",
                )
            elif ambiguous_reason:
                extras.append(f"unclear: {ambiguous_reason}")
            extra_block = ("\n  " + "\n  ".join(extras)) if extras else ""
            line = await prompt_fn(f"{label}{extra_block}\n> ")
            if line is None:
                return ClarifyFormResult(fields={}, cancelled=True)
            if is_cancel_token(line, cancel_keywords):
                return ClarifyFormResult(fields={}, cancelled=True)
            stripped = line.strip()
            if not stripped and confirmed_entry is not None:
                # User pressed Enter on an empty line for a pre-filled
                # field → accept the inferred value as-is. The audit
                # payload already records the source ("auto_prefill")
                # so downstream steps can see this was confirmed
                # rather than typed.
                collected[str(field_name)] = confirmed_entry.get("value")
                break
            value, error = coerce_field_input(field, line)
            if error:
                print_fn(f"  ✗ {error}; please try again.")
                continue
            if value is not None:
                collected[str(field_name)] = value
            break

    return ClarifyFormResult(fields=collected, cancelled=False)


def _entries(raw: Any) -> list:
    """Coerce a protocol field that may be missing / non-list to a list."""
    if isinstance(raw, list):
        return raw
    return []


def _default_prompt_fn() -> PromptFn:
    """Build the default prompt-toolkit-backed prompt fn.

    Runs each line inside ``in_terminal`` so the outer ChatApplication
    temporarily releases the terminal. The import is lazy so importing
    this module doesn't require prompt-toolkit (e.g. for unit tests).
    """

    async def _read(prefix: str) -> str | None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application.run_in_terminal import in_terminal

        async def _do_prompt() -> str | None:
            session: PromptSession[str] = PromptSession()
            try:
                # Print the (potentially multi-line) prefix above the
                # actual single-line prompt so prompt-toolkit only
                # renders the cursor line.
                lines = prefix.rstrip("\n").split("\n")
                head, tail = lines[:-1], lines[-1]
                for h in head:
                    print(h)
                return await session.prompt_async(tail)
            except (EOFError, KeyboardInterrupt):
                return None

        # If we're inside a running event loop with an active
        # ChatApplication, suspend it via in_terminal; otherwise just
        # run directly.
        try:
            async with in_terminal():
                return await _do_prompt()
        except Exception:
            # Fallback (test envs without a running Application instance).
            return await _do_prompt()

    return _read


def fields_to_chat_send_message(fields: dict[str, Any]) -> str:
    """Serialise collected fields into the ``key: value\\n`` reply form
    expected by ``opensquilla.skills.meta.clarify_text.parse_clarify_reply``.

    Bool values render as ``true``/``false``; everything else uses Python's
    natural string repr. Empty / None values are skipped (matches the
    gateway-side helper in ``rpc_chat._clarify_fields_to_text``).
    """
    lines: list[str] = []
    for name, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{name}: {rendered}")
    return "\n".join(lines)


__all__ = [
    "ClarifyFormResult",
    "PromptFn",
    "coerce_field_input",
    "fields_to_chat_send_message",
    "is_cancel_token",
    "prompt_clarify_form",
    "render_field_label",
]
