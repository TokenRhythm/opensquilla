"""Tool failure envelope.

Single helper that converts any exception raised by a tool handler into a
stable, user-facing JSON envelope. Keys are fixed and exhaustive:

    {"status": "error",
     "tool": <str>,
     "error_class": <str>,
     "user_message": <str>,
     "retry_allowed": <bool>}

No raw ``repr(exc)`` and no traceback text ever leaks into the envelope —
the ``user_message`` field is sanitised so it is safe to show to an end
user or write to a channel transcript.

Callers expect a ``dict[str, Any]`` with exactly these five keys. Extra
keys are a breaking change; additions go through a new slice.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Final

from opensquilla.tools.types import SafeToolUserMessage

# Class names whose instances represent transient infrastructure problems.
# The list is intentionally conservative — subclass matching is handled by
# walking the exception's MRO below.
_RETRIABLE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "TimeoutError",
        "TimeoutException",
        "ConnectError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "BrokenPipeError",
        "TransientError",
        "TypeError",
        "RetryableToolInputError",
    }
)

# Exception classes that are explicitly NOT retriable even if their name
# coincidentally shares a token with one of the retriable classes above.
_NEVER_RETRIABLE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "PermissionError",
        "PermissionDenied",
        "ValueError",
        "KeyError",
        "FileNotFoundError",
        "IsADirectoryError",
        "NotADirectoryError",
        "NotImplementedError",
    }
)

# Human-readable messages keyed by error_class. Anything not in the table
# falls back to a generic "The tool '{tool}' failed." line.
_USER_MESSAGES: Final[dict[str, str]] = {
    "TimeoutError": "The tool took too long to respond. Please try again.",
    "ConnectionError": "A network error occurred while running the tool. Please try again.",
    "ConnectionResetError": ("The connection was reset while running the tool. Please try again."),
    "ConnectionAbortedError": (
        "The connection was aborted while running the tool. Please try again."
    ),
    "ConnectionRefusedError": (
        "The network connection was refused while running the tool. Please try again."
    ),
    "BrokenPipeError": "A pipe error interrupted the tool. Please try again.",
    "TransientError": "A transient error occurred while running the tool. Please try again.",
    "PermissionError": "The tool was not permitted to perform this action.",
    "PermissionDenied": "The tool was not permitted to perform this action.",
    "ValueError": "The tool received an invalid argument.",
    "TypeError": (
        "The tool received an argument of the wrong type. Retry with arguments "
        "that match the tool schema."
    ),
    "KeyError": "The tool was asked for a value it could not find.",
    "FileNotFoundError": "The tool could not find the requested file.",
    "IsADirectoryError": "The tool expected a file but received a directory.",
    "NotADirectoryError": "The tool expected a directory but received a file.",
    "NotImplementedError": "This tool does not support the requested operation.",
    "TimeoutException": "The tool timed out while connecting or waiting for a response.",
    "ConnectError": "The tool could not connect to the remote service.",
    "JSONDecodeError": "The tool received an invalid response payload.",
    "ToolRunBudgetExceededError": "The tool run budget for this turn is exhausted.",
    "policy_denial": "The action was blocked by policy. See user-facing reason for details.",
}

_TRACEBACK_FRAME_RE = re.compile(r'^\s+File ".+?", line \d+, in .+$', re.MULTILINE)
_USER_MESSAGE_MAX_CHARS: Final[int] = 500
# Engine-authored messages — explicit overrides and SafeToolUserMessage payloads
# (denial guidance, retry hints) — carry actionable, sometimes multi-line detail
# that a 500-char cap would truncate mid-instruction. Give them a wider bound; the
# generic canned lines above are all far shorter than either cap, so this only
# affects deliberately-authored text.
_CURATED_USER_MESSAGE_MAX_CHARS: Final[int] = 2000
# Opt-in cap override for policy-gate denial envelopes (off by default).
# Policy gates author remediation guidance whose tail carries the binding
# instruction; the default cap can sever it while the sibling block-payload
# path delivers the same text in full. Gates mark their SafeToolError with a
# ``policy_gate_denial`` attribute before raising; non-marked errors keep the
# curated/default cap even when the lever is set.
_POLICY_DENY_MAX_CHARS_ENV = "OPENSQUILLA_TOOL_ENVELOPE_POLICY_DENY_MAX_CHARS"


def _policy_deny_max_chars() -> int:
    raw = os.environ.get(_POLICY_DENY_MAX_CHARS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _exception_class_name(exc: BaseException) -> str:
    """Return the primary class name for ``exc`` without module qualifiers."""

    return type(exc).__name__


def _exception_mro_names(exc: BaseException) -> list[str]:
    """Return the exception's MRO class names for retriable-class matching."""

    return [cls.__name__ for cls in type(exc).__mro__ if cls is not object]


def _is_retriable(exc: BaseException) -> bool:
    """Decide whether ``exc`` represents a transient / retriable failure."""

    mro = _exception_mro_names(exc)
    # Explicit never-retriable takes precedence even for oddly-named
    # subclasses of TimeoutError etc.
    if any(name in _NEVER_RETRIABLE_CLASSES for name in mro):
        return False
    return any(name in _RETRIABLE_CLASSES for name in mro)


def _sanitise_user_message(tool_name: str, exc: BaseException) -> str:
    """Derive a human-readable message for the envelope.

    The message is never ``repr(exc)`` and never includes traceback frames.
    Any embedded newlines are collapsed to single spaces so the envelope is
    safe to display in chat transcripts.
    """

    class_name = _exception_class_name(exc)
    if isinstance(exc, SafeToolUserMessage):
        safe_message = getattr(exc, "user_message", "")
        if isinstance(safe_message, str) and safe_message.strip():
            return safe_message
        return SafeToolUserMessage.user_message
    if class_name in _USER_MESSAGES:
        return _USER_MESSAGES[class_name]

    # Unknown classes: render a generic line that names the tool. Do NOT
    # interpolate str(exc) / repr(exc) — operators may have put secrets in
    # the exception message.
    return f"The tool {tool_name!r} failed with an internal error."


def _resolve_error_class(
    exc: BaseException,
    *,
    error_class_override: str | None = None,
    policy_denial: bool = False,
) -> str:
    if policy_denial:
        return error_class_override or "PolicyDenied"
    if error_class_override is not None:
        return error_class_override
    return _exception_class_name(exc)


def build_tool_failure_envelope(
    exc: BaseException,
    tool_name: str,
    *,
    policy_denial: bool = False,
    error_class_override: str | None = None,
    user_message_override: str | None = None,
) -> dict[str, Any]:
    """Build the canonical tool-failure envelope for ``exc`` raised by ``tool_name``.

    The returned dict has exactly these keys:

        status="error", tool=<str>, error_class=<str>,
        user_message=<str>, retry_allowed=<bool>

    ``error_class`` is the Python class name only (no module prefix).
    ``user_message`` is guaranteed to contain neither the traceback nor
    ``repr(exc)``.
    """

    if not isinstance(tool_name, str) or not tool_name:
        tool_name = "<unknown>"

    curated = user_message_override is not None or isinstance(exc, SafeToolUserMessage)
    user_message = user_message_override or _sanitise_user_message(tool_name, exc)
    # Defense in depth: belt-and-braces guard against a future caller
    # pre-rendering a traceback into the sanitiser output.
    user_message = _TRACEBACK_FRAME_RE.sub("", user_message).strip()
    user_message = user_message.replace("\n", " ").strip() or (f"The tool {tool_name!r} failed.")
    max_chars = _CURATED_USER_MESSAGE_MAX_CHARS if curated else _USER_MESSAGE_MAX_CHARS
    if getattr(exc, "policy_gate_denial", False):
        override_max_chars = _policy_deny_max_chars()
        # Caps that cannot fit the truncation marker would replace content
        # with marker text; treat them as off like the other invalid values.
        if override_max_chars > len("...[truncated]"):
            max_chars = override_max_chars
    if len(user_message) > max_chars:
        user_message = user_message[: max_chars - len("...[truncated]")]
        user_message = f"{user_message}...[truncated]"

    return {
        "status": "error",
        "tool": tool_name,
        "error_class": _resolve_error_class(
            exc,
            error_class_override=error_class_override,
            policy_denial=policy_denial,
        ),
        "user_message": user_message,
        "retry_allowed": False if policy_denial else _is_retriable(exc),
    }


def build_denial_envelope(denial: Any, tool_name: str) -> dict[str, Any]:
    """Wrap a :class:`opensquilla.sandbox.DenialResult` as a tool-visible envelope.

    The sandbox governance layer produces a :class:`DenialResult` with the
    §8.2 fields (``reason``, ``suggested_next_step``, ``level``,
    ``action_fingerprint``, ``message``, ``retryable``). This helper forwards
    those fields verbatim and decorates them with the tool name so callers
    can distinguish a sandbox denial from a handler failure on the
    ``status`` field alone (``"denied"`` vs ``"error"``).
    """
    if hasattr(denial, "to_dict"):
        payload = dict(denial.to_dict())
    elif isinstance(denial, dict):
        payload = dict(denial)
    else:  # pragma: no cover - defensive
        payload = {"status": "denied", "message": str(denial)}
    payload.setdefault("status", "denied")
    payload["tool"] = tool_name if isinstance(tool_name, str) and tool_name else "<unknown>"
    return payload


_TERMINAL_DENIAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"denied", "blocked", "approval_denied"}
)


def is_denial_payload(content: Any) -> bool:
    """Return True when ``content`` is a terminal denial payload.

    Covers three shapes that all signal "do not execute":

    * ``status=denied`` — :class:`opensquilla.sandbox.DenialResult.to_dict`
    * ``status=blocked`` — sensitive-path hard block from
      :func:`opensquilla.sandbox.sensitive_paths.build_block_envelope`
    * ``status=approval_denied`` — a queued approval was rejected by the user

    ``approval_required`` / ``approval_pending`` are explicitly *not* denials
    — those are retry signals for the model.
    """
    if isinstance(content, dict):
        return content.get("status") in _TERMINAL_DENIAL_STATUSES
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") in _TERMINAL_DENIAL_STATUSES
    )
