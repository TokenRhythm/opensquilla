from __future__ import annotations

from typing import Any

from .matcher import command_argv, select_rule
from .reducer import reduce_with_rule
from .rules import load_rules
from .types import Reduction


def _string_arg(args: dict[str, Any] | None, *names: str) -> str | None:
    if not args:
        return None
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _format_inline(summary: str, facts: dict[str, int], *, exit_code: int) -> str:
    parts: list[str] = []
    if exit_code != 0:
        parts.append(f"exit {exit_code}")
    non_zero_facts = [f"{name}: {count}" for name, count in facts.items() if count]
    if non_zero_facts:
        parts.append("; ".join(non_zero_facts))
    parts.append(summary)
    return "\n".join(part for part in parts if part).strip()


def reduce_tool_result(
    *,
    tool_name: str,
    content: str,
    is_error: bool,
    tool_use_id: str,
    arguments: dict[str, Any] | None = None,
    command: str | None = None,
    cwd: str | None = None,
    max_inline_chars: int | None = None,
) -> Reduction | None:
    del cwd, tool_use_id
    command = command or _string_arg(arguments, "command")
    argv = arguments.get("argv") if isinstance(arguments, dict) else None
    argv = argv if isinstance(argv, list) and all(isinstance(item, str) for item in argv) else None
    exit_code = 1 if is_error else 0
    rule = select_rule(
        load_rules(),
        tool_name=tool_name,
        command=command,
        argv=command_argv(command, argv),
        content=content,
        exit_code=exit_code,
    )
    if rule is None:
        return None

    summary, facts = reduce_with_rule(rule, content, exit_code=exit_code)
    if not summary:
        return None
    inline_text = _format_inline(summary, facts, exit_code=exit_code)
    if (
        max_inline_chars is not None
        and max_inline_chars > 0
        and len(inline_text) > max_inline_chars
    ):
        half = max(1, (max_inline_chars - 32) // 2)
        inline_text = f"{inline_text[:half]}\n... omitted chars ...\n{inline_text[-half:]}"
    if len(inline_text) >= len(content):
        return None
    return Reduction(
        inline_text=inline_text,
        raw_chars=len(content),
        reduced_chars=len(inline_text),
        ratio=len(inline_text) / max(1, len(content)),
        reducer=rule.id,
    )
