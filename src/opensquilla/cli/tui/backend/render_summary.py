"""Backend-neutral summaries for compact tool rows."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

# String sequences (OSC/DCS/SOS/PM/APC) tolerate a missing terminator so a
# result truncated mid-sequence upstream cannot leak its payload as visible
# text. Payloads stop at newlines: a stray introducer in binary-ish output
# must not swallow the meaningful lines that follow it.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|[PX^_][^\x1b\x9c\n]*(?:\x1b\\|\x9c)?"
    r"|[@-Z\\-_]"
    r")"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x9d[^\x07\x9c\x1b\n]*(?:\x07|\x9c|\x1b\\)?"
    r"|[\x90\x98\x9e\x9f][^\x9c\x1b\n]*(?:\x9c|\x1b\\)?"
)
# C0 controls plus the 8-bit C1 range: C1 codepoints execute on some terminals
# and break cell alignment on the rest, so both are stripped.
_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")
_RESULT_DECORATION_ONLY_RE = re.compile(r"^[\s═─=\-]{3,}$")
_RESULT_DECORATION_BANNER_RE = re.compile(r"^[\s═─=\-]{2,}.+[\s═─=\-]{2,}$")
_PATCH_FILE_LINE_RE = re.compile(r"^\*{3} (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)

_ZWJ = "\u200d"

# Registry-facing contract: each specially summarized tool name and the
# declared argument key its branch reads. Conformance tests pin this mapping
# against the builtin tool registry so renames cannot silently blank rows.
TOOL_SUMMARY_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "exec_command": ("command",),
    "background_process": ("command",),
    "execute_code": ("code",),
    "read_file": ("path",),
    "write_file": ("path",),
    "list_dir": ("path",),
    "apply_patch": ("patch",),
    "web_search": ("query",),
    "web_discover": ("query",),
    "web_fetch": ("url",),
}

# Tools without a dedicated branch still summarize their most useful argument.
_FALLBACK_ARG_KEYS = ("command", "cmd", "code", "path", "file_path", "url", "query", "pattern")


def summarize_args(name: str, args: dict | None) -> str:
    """Return a short human-readable summary of a tool call's key argument."""
    if not args:
        return ""
    if name in {"exec_command", "background_process"}:
        cmd = args.get("command") or args.get("cmd") or ""
        return clip_arg(str(cmd)) if cmd else ""
    if name == "execute_code":
        code = args.get("code") or args.get("source") or ""
        first_line = str(code).split("\n", 1)[0]
        return clip_arg(first_line) if first_line else ""
    if name == "apply_patch":
        # apply_patch has no path argument: the file targets live inside the
        # patch text as '*** Add/Update/Delete File: path' lines.
        target = _first_patch_file(str(args.get("patch") or ""))
        return clip_arg(target, keep_end=True) if target else ""
    if name in {"read_file", "write_file", "list_dir"}:
        path = args.get("path") or args.get("file_path") or args.get("target") or ""
        return clip_arg(str(path), keep_end=True) if path else ""
    if name in {"web_search", "web_discover"}:
        query = args.get("query") or ""
        return clip_arg(str(query)) if query else ""
    if name == "web_fetch":
        url = args.get("url") or args.get("uri") or ""
        return clip_arg(str(url)) if url else ""
    for key in _FALLBACK_ARG_KEYS:
        value = args.get(key)
        if not value:
            continue
        # Multiline values (code bodies, compound commands) must stay on the
        # single-line tool row: collapse whitespace runs before clipping.
        flattened = " ".join(str(value).split())
        if not flattened:
            continue
        keep_end = key in {"path", "file_path"}
        return clip_arg(flattened, keep_end=keep_end)
    return ""


def _first_patch_file(patch_text: str) -> str:
    match = _PATCH_FILE_LINE_RE.search(patch_text)
    if match is None:
        return ""
    return match.group(1).strip()


def clip_arg(value: str, *, limit: int = 90, keep_end: bool = False) -> str:
    """Clip a tool-call argument to a display-cell budget for the tool row."""
    if _text_cells(value) <= limit:
        return value
    budget = max(0, limit - 3)
    if keep_end:
        return f"...{_clip_cells(value, budget, keep_end=True)}"
    return f"{_clip_cells(value, budget)}..."


def summarize_result(result: Any | None, *, max_chars: int = 220) -> str:
    """Return a compact, terminal-safe tool result preview."""
    if result is None:
        return ""
    text = _stringify_result_for_summary(result)
    text = sanitize_terminal_text(text).strip()
    if not text:
        return ""
    stripped_lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [line for line in stripped_lines if not _is_result_decoration_line(line)]
    lines = [line for line in candidates if not _is_useless_result_line(line)]
    if not lines:
        # A one-character or punctuation-only line is noise next to richer
        # output, but when it is all the tool produced it IS the result
        # (e.g. a computed "7") — keep it rather than render nothing. The
        # meaningful line usually trails the noise (spinners, separators),
        # so prefer the last non-punctuation candidate.
        remaining = [line for line in candidates if line != "exit_code=0"]
        substantive = [line for line in remaining if not _is_punctuation_only_line(line)]
        lines = substantive[-1:] if substantive else remaining[:1]
    if not lines:
        return ""
    preview = "\n".join(lines[:3])
    if _text_cells(preview) > max_chars:
        preview = f"{_clip_cells(preview, max_chars - 3).rstrip()}..."
    return preview


def tool_args_detail(args: dict[str, Any] | None) -> str:
    """Return complete terminal-safe tool arguments for explicit disclosure.

    Compact rows should continue to use :func:`summarize_args`; this payload is
    the retained source shown only when the user expands process details.
    """

    if not args:
        return ""
    try:
        rendered = json.dumps(
            _terminal_safe_payload(args),
            ensure_ascii=False,
            indent=2,
            default=lambda value: sanitize_terminal_text(str(value)),
        )
    except (TypeError, ValueError):
        rendered = str(args)
    return sanitize_terminal_text(rendered)


def tool_result_detail(result: Any | None) -> str:
    """Return the complete terminal-safe result payload without preview clipping."""

    if result is None:
        return ""
    if isinstance(result, str):
        rendered = result
    else:
        # Pure ``[{type: msg, msg: ...}, ...]`` streams are transport wrappers;
        # show every payload in order.  A mixed list is serialized whole so a
        # non-msg event can never disappear behind the convenience unwrap.
        payloads: list[Any] | None = None
        if isinstance(result, dict):
            payload = _msg_event_payload(result)
            payloads = [payload] if payload is not None else None
        elif isinstance(result, list) and result:
            candidates = [_msg_event_payload(item) for item in result]
            if all(payload is not None for payload in candidates):
                payloads = candidates
        if payloads is not None:
            rendered = "\n".join(_stringify_result_value(payload) for payload in payloads)
        else:
            try:
                rendered = json.dumps(
                    _terminal_safe_payload(result),
                    ensure_ascii=False,
                    indent=2,
                    default=lambda value: sanitize_terminal_text(str(value)),
                )
            except (TypeError, ValueError):
                rendered = str(result)
    return sanitize_terminal_text(rendered)


def _terminal_safe_payload(value: Any) -> Any:
    """Recursively remove executable terminal controls before JSON escaping."""

    if isinstance(value, str):
        return sanitize_terminal_text(value)
    if isinstance(value, dict):
        return {
            sanitize_terminal_text(str(key)): _terminal_safe_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_terminal_safe_payload(item) for item in value]
    return value


def sanitize_terminal_text(text: str) -> str:
    """Strip ANSI escape sequences and C0/C1 controls from preview text."""
    return _C0_RE.sub("", _ANSI_RE.sub("", text))


def _char_cells(char: str) -> int:
    if char == _ZWJ or "\ufe00" <= char <= "\ufe0f":
        return 0
    if unicodedata.category(char) in {"Mn", "Me"}:
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    if "\U0001f300" <= char <= "\U0001faff":
        return 2
    return 1


def _text_cells(text: str) -> int:
    return sum(_char_cells(char) for char in text)


def _iter_clusters(text: str) -> Iterator[str]:
    """Group zero-width joiners/marks with their base so clips never split them."""
    cluster = ""
    for char in text:
        if cluster and (_char_cells(char) == 0 or cluster.endswith(_ZWJ)):
            cluster += char
            continue
        if cluster:
            yield cluster
        cluster = char
    if cluster:
        yield cluster


def _clip_cells(value: str, budget: int, *, keep_end: bool = False) -> str:
    clusters = list(_iter_clusters(value))
    if keep_end:
        clusters.reverse()
    kept: list[str] = []
    used = 0
    for cluster in clusters:
        width = _text_cells(cluster)
        if used + width > budget:
            break
        kept.append(cluster)
        used += width
    if keep_end:
        kept.reverse()
    return "".join(kept)


def _stringify_result_for_summary(result: Any) -> str:
    msg_text = _stringify_msg_event_payloads(result)
    if msg_text is not None:
        return msg_text
    return _stringify_result_value(result)


def _stringify_msg_event_payloads(result: Any) -> str | None:
    if isinstance(result, dict):
        payload = _msg_event_payload(result)
        if payload is None:
            return None
        return _stringify_result_value(payload)
    if not isinstance(result, list):
        return None

    parts: list[str] = []
    for item in result:
        payload = _msg_event_payload(item)
        if payload is None:
            continue
        rendered = _stringify_result_value(payload).strip()
        if rendered:
            parts.append(rendered)
    return "\n".join(parts) if parts else None


def _msg_event_payload(event: Any) -> Any | None:
    if not isinstance(event, dict) or event.get("type") != "msg" or "msg" not in event:
        return None
    return event["msg"]


def _stringify_result_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _is_result_decoration_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _RESULT_DECORATION_ONLY_RE.fullmatch(stripped)
        or _RESULT_DECORATION_BANNER_RE.fullmatch(stripped)
    )


def _is_useless_result_line(line: str) -> bool:
    stripped = line.strip()
    if stripped == "exit_code=0":
        return True
    if len(stripped) <= 1:
        return True
    return _is_punctuation_only_line(stripped)


def _is_punctuation_only_line(line: str) -> bool:
    return bool(line) and all(unicodedata.category(char).startswith("P") for char in line)
