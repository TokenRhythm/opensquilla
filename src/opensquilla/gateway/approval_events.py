"""Bridge approval queue lifecycle into operator-facing WS push events.

When a run blocks on an exec/plugin approval, the queue records the request;
this module turns those transitions into ``<namespace>.approval.requested`` /
``<namespace>.approval.resolved`` events pushed to every connection holding
the approvals scope, so UIs can react without polling. Additive only: no
existing event is renamed or reshaped, and clients that ignore these events
keep working unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, cast

from opensquilla.gateway.scopes import APPROVALS_SCOPE
from opensquilla.safety.secret_redaction import redact_secret_value

_EVENT_SUFFIXES = frozenset({"requested", "resolved"})
_REDACTED = "[REDACTED]"
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DISPLAY_KEY_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_COOKIE_HEADER_RE = re.compile(
    r"(?P<double_quote>\")"
    r"(?P<double_name>\b(?:set-)?cookie\s*:\s*)"
    r"(?:\\.|[^\"\\])*\""
    r"|(?P<single_quote>')"
    r"(?P<single_name>\b(?:set-)?cookie\s*:\s*)"
    r"(?:\\.|[^'\\])*'"
    r"|(?P<plain_name>\b(?:set-)?cookie\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK_MARKER_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_COOKIE_VALUE_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "set_cookie",
        "cookie_header",
        "cookie_value",
        "cookie_jar",
    }
)
_TERMINAL_SECRET_TOKENS = frozenset(
    {"auth", "authorization", "credential", "password", "secret", "token"}
)


def _approval_sensitive_display_key(key: str) -> bool:
    """Return whether one approval display value is credential-bearing.

    This is intentionally local to the browser-facing approval projection.
    Expanding the shared secret redactor would also change provider traces,
    tool results, and model-visible runtime payloads.  Tokenizing camelCase and
    separators catches header/argument variants without treating benign fields
    such as ``cookies_enabled`` or ``cookie_policy`` as credentials.
    """

    snake_key = _CAMEL_CASE_BOUNDARY_RE.sub("_", key).casefold()
    normalized = _DISPLAY_KEY_SEPARATOR_RE.sub("_", snake_key).strip("_")
    if normalized in _COOKIE_VALUE_KEYS:
        return True
    tokens = tuple(part for part in normalized.split("_") if part)
    if tokens and tokens[-1] in {"cookie", "cookies"}:
        return True
    if tokens and tokens[-1] in _TERMINAL_SECRET_TOKENS:
        return True
    if any(token in {"cookie", "cookies"} for token in tokens) and any(
        token in {"data", "header", "jar", "payload", "value"} for token in tokens
    ):
        return True
    return any(
        left == "private" and right == "key"
        for left, right in zip(tokens, tokens[1:], strict=False)
    )


def _redact_approval_display_text(value: str) -> str:
    """Mask browser-specific credential forms missed by the shared scrubber."""

    if _PRIVATE_KEY_BLOCK_MARKER_RE.search(value):
        return _REDACTED

    def _replace_cookie_header(match: re.Match[str]) -> str:
        if match.group("double_quote") is not None:
            quote = '"'
            name = match.group("double_name") or ""
        elif match.group("single_quote") is not None:
            quote = "'"
            name = match.group("single_name") or ""
        else:
            quote = ""
            name = match.group("plain_name") or ""
        return f"{quote}{name}{_REDACTED}{quote}"

    return _COOKIE_HEADER_RE.sub(_replace_cookie_header, value)


def _json_safe_display_value(value: Any) -> Any:
    """Return a redacted, JSON-safe value for an operator display surface.

    Approval queue params are an internal policy record and can contain review
    fingerprints/actions or tool credentials.  They must never be copied to a
    browser response wholesale.  Callers first select the fields that are part
    of the public display contract; this helper then redacts secret-shaped
    values recursively and normalizes tuples/mappings for JSON encoding.
    """

    redacted = redact_secret_value(value)
    if isinstance(redacted, Mapping):
        projected: dict[str, Any] = {}
        for key, item in redacted.items():
            text_key = str(key)
            lowered_key = text_key.lower()
            if "fingerprint" in lowered_key or lowered_key.startswith("review"):
                continue
            projected[text_key] = (
                _REDACTED
                if _approval_sensitive_display_key(text_key)
                else _json_safe_display_value(item)
            )
        return projected
    if isinstance(redacted, (list, tuple)):
        return [_json_safe_display_value(item) for item in redacted]
    if isinstance(redacted, str):
        return _redact_approval_display_text(redacted)
    if redacted is None or isinstance(redacted, (int, float, bool)):
        return redacted
    return str(redacted)


def _non_empty_text(value: Any) -> str:
    text = str(value or "").strip()
    return str(_json_safe_display_value(text)) if text else ""


def _selected_scalar_fields(params: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for name in names:
        value = params.get(name)
        if value is None or not isinstance(value, (str, int, float, bool)):
            continue
        selected[name] = _json_safe_display_value(value)
    return selected


def approval_display_fields(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the sole browser-safe display projection of approval params.

    Both WS pushes and the HTTP pending snapshot consume this projection.  In
    particular, sandbox policy internals (fingerprints, review actions,
    choices) stay server-side; only the exact context needed to identify the
    requested access is exposed.
    """

    source: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
    approval_kind = _non_empty_text(source.get("approvalKind") or source.get("approval_kind"))

    args: dict[str, Any] | None
    if approval_kind == "sandbox_path":
        args = _selected_scalar_fields(source, ("path", "access", "workspace")) or None
    elif approval_kind == "sandbox_network":
        args = _selected_scalar_fields(source, ("host", "bundle_id", "workspace")) or None
    elif approval_kind.startswith("sandbox_"):
        # Future/legacy sandbox approvals may carry canonical policy actions.
        # Until a kind has an explicit display allowlist, expose no arguments.
        args = None
    elif isinstance(source.get("args"), Mapping):
        safe_args = _json_safe_display_value(source["args"])
        args = safe_args if isinstance(safe_args, dict) and safe_args else None
    elif "permissions" in source:
        # Plugin approvals expose declared permission names as one named field,
        # not the plugin queue's complete params record.
        args = {"permissions": _json_safe_display_value(source.get("permissions"))}
    else:
        args = None

    argv_value = source.get("argv")
    argv = _json_safe_display_value(argv_value) if isinstance(argv_value, (list, tuple)) else []
    if not isinstance(argv, list):  # defensive; _json_safe_display_value normalizes tuples
        argv = []
    command = _non_empty_text(source.get("command"))
    if not command and argv:
        # Redact once more after joining so split argv such as
        # ``Authorization:`` / ``Bearer`` / ``credential`` is recognized as a
        # complete header rather than three individually harmless strings.
        command = _non_empty_text(" ".join(str(part) for part in argv))

    tool_name = _non_empty_text(
        source.get("toolName")
        or source.get("pluginId")
        or source.get("action_kind")
        or approval_kind
    )
    return {
        "tool_name": tool_name,
        "session_key": _non_empty_text(
            source.get("sessionKey") or source.get("session_key") or source.get("session_id")
        ),
        "agent": _non_empty_text(source.get("agent")),
        "args": args,
        "command": command,
        "warning": _non_empty_text(source.get("warning") or source.get("reason")),
        "approval_kind": approval_kind,
        "action_kind": _non_empty_text(source.get("action_kind")),
        "mode": _non_empty_text(source.get("mode")),
    }


def build_approval_snapshot_item(
    info: Mapping[str, Any],
    *,
    default_mode: str,
) -> dict[str, Any]:
    """Build one safe ``/api/approvals`` pending item."""

    params = info.get("params")
    display = approval_display_fields(params if isinstance(params, Mapping) else None)
    return {
        "id": str(info.get("id") or ""),
        "namespace": str(info.get("namespace") or "exec"),
        "created_at": info.get("created_at"),
        "deadline": info.get("deadline"),
        "toolName": display["tool_name"] or "Unknown",
        "sessionKey": display["session_key"],
        "agent": display["agent"],
        "args": display["args"],
        "command": display["command"],
        "warning": display["warning"],
        "approvalKind": display["approval_kind"],
        "actionKind": display["action_kind"],
        "mode": display["mode"] or default_mode,
    }


def approval_event_name(event: str, info: dict[str, Any]) -> str | None:
    """Wire event name for a queue transition, or None for unknown events."""
    if event not in _EVENT_SUFFIXES:
        return None
    params = info.get("params")
    if isinstance(params, dict) and params.get("humanActionable") is False:
        return None
    namespace = str(info.get("namespace") or "exec")
    return f"{namespace}.approval.{event}"


def build_approval_event_payload(info: dict[str, Any]) -> dict[str, Any]:
    """Build the WS payload for an approval lifecycle event.

    ``info`` mirrors ``ApprovalQueue.status()``. The summary fields follow
    the pending-item shape served by the approvals snapshot so push and
    poll consumers see consistent vocabulary.
    """
    raw_params = info.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    display = approval_display_fields(params)
    payload: dict[str, Any] = {
        "approval_id": str(info.get("id") or ""),
        "namespace": str(info.get("namespace") or "exec"),
        "session_key": display["session_key"],
        "tool_name": display["tool_name"],
        "command": display["command"],
        "approval_kind": display["approval_kind"],
        "agent": display["agent"],
        # Explicit even when empty so new clients can distinguish a complete
        # push from the lean pre-contract shape that requires HTTP hydration.
        "args": display["args"],
        "warning": display["warning"],
        "created_at": info.get("created_at"),
        "deadline": info.get("deadline"),
    }
    if info.get("resolved"):
        payload["approved"] = bool(info.get("approved"))
        resolution = info.get("resolution")
        if resolution:
            payload["resolution"] = str(resolution)
    return payload


def register_approval_event_bridge(
    queue: Any,
    event_bridge: Any,
    *,
    schedule: Callable[[Any], Any],
) -> Callable[[], None]:
    """Subscribe ``event_bridge`` to approval queue lifecycle transitions.

    ``schedule`` receives the broadcast coroutine (gateway boot passes
    ``create_background_task``). Returns the listener remove callable.
    """

    def _listener(event: str, info: dict[str, Any]) -> None:
        event_name = approval_event_name(event, info)
        if event_name is None:
            return
        emit_coro = event_bridge.broadcast_scoped(
            event_name,
            build_approval_event_payload(info),
            required_scope=APPROVALS_SCOPE,
        )
        try:
            schedule(emit_coro)
        except RuntimeError:
            emit_coro.close()

    return cast("Callable[[], None]", queue.add_event_listener(_listener))
