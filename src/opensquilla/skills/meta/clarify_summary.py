"""Pure renderer for a user_input step's filled values.

The output of this function is what ``MetaOrchestrator.resume`` writes
into ``outputs[<step_id>]`` so downstream LLM steps see a compact,
human-readable context block. Structured values still live in
``inputs.collected.<step_id>.<field>``; this renderer is *only* for the
markdown view.

Design references:
* §5.3 (Field-Value Addressing): outputs.<step_id> is a markdown summary.
* §8.3 (resume): keeps the output under 1 KB for downstream context budget.
"""

from __future__ import annotations

from typing import Any

from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig

_MAX_BYTES = 1024


def render_clarify_summary(
    *,
    schema: ClarifyStepConfig,
    filled: dict[str, Any],
) -> str:
    """Render a markdown bullet list of filled values + default annotations."""

    lines: list[str] = []
    if schema.intro:
        lines.append(schema.intro)
        lines.append("")

    for field in schema.fields:
        line = _render_field_line(field, filled)
        lines.append(line)

    out = "\n".join(lines)

    # Belt-and-suspenders truncation. Trim on UTF-8 byte boundary.
    encoded = out.encode("utf-8")
    if len(encoded) <= _MAX_BYTES:
        return out
    while encoded and len(encoded) > _MAX_BYTES - 5:
        encoded = encoded[:-1]
    try:
        return encoded.decode("utf-8", errors="ignore") + "\n…"
    except UnicodeDecodeError:
        return out[:_MAX_BYTES] + "…"


def _render_field_line(field: ClarifyField, filled: dict[str, Any]) -> str:
    if field.name in filled:
        value = filled[field.name]
        origin = "from user"
    elif field.default is not None:
        value = field.default
        origin = "default"
    else:
        return f"- {field.name}: (pending)"
    return f"- {field.name}: {value} ({origin})"
