"""Fail-closed RPC and session ownership policy for anonymous Web guests."""

from __future__ import annotations

import re
import secrets
from typing import Any

from opensquilla.session.keys import canonicalize_session_key, parse_agent_id

_OWNER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")

GUEST_RPC_ALLOWLIST = frozenset(
    {
        "chat.send",
        "chat.history",
        "chat.abort",
        "chat.clarify_submit",
        "artifacts.list",
        "artifacts.get",
        "sessions.list",
        "sessions.rename",
        "sessions.delete",
        "sessions.bootstrap",
        "sessions.messages.subscribe",
        "sessions.messages.hydrate",
        "sessions.messages.snapshot",
        "sessions.messages.unsubscribe",
        "sessions.pending_inputs.enqueue",
        "sessions.pending_inputs.list",
        "sessions.pending_inputs.update",
        "sessions.pending_inputs.reorder",
        "sessions.pending_inputs.cancel",
        "sessions.pending_inputs.dispatch",
    }
)

_SESSION_KEY_FIELDS = {
    "artifacts.list": ("sessionKey",),
    "artifacts.get": ("sessionKey",),
    "chat.history": ("sessionKey", "key"),
    "chat.abort": ("sessionKey", "key"),
    "chat.clarify_submit": ("sessionKey", "key"),
    "sessions.bootstrap": ("key", "sessionKey"),
    "sessions.rename": ("key", "sessionKey"),
    "sessions.messages.subscribe": ("key", "sessionKey"),
    "sessions.messages.hydrate": ("key", "sessionKey"),
    "sessions.messages.snapshot": ("key", "sessionKey"),
    "sessions.messages.unsubscribe": ("key", "sessionKey"),
    "sessions.pending_inputs.enqueue": ("key", "sessionKey"),
    "sessions.pending_inputs.list": ("key", "sessionKey"),
    "sessions.pending_inputs.update": ("key", "sessionKey"),
    "sessions.pending_inputs.reorder": ("key", "sessionKey"),
    "sessions.pending_inputs.cancel": ("key", "sessionKey"),
    "sessions.pending_inputs.dispatch": ("key", "sessionKey"),
}


class GuestRpcPolicyError(PermissionError):
    """Raised when an anonymous guest crosses the RPC ownership boundary."""


def _guest_namespace_parts(session_key: object) -> tuple[str, str] | None:
    key = canonicalize_session_key(str(session_key or ""))
    parts = key.split(":")
    if (
        len(parts) == 6
        and parts[0] == "agent"
        and parts[2] == "webchat"
        and parts[3] == "guest"
        and _OWNER_ID_RE.fullmatch(parts[4])
        and parts[5]
    ):
        return parts[4], parts[5]
    return None


def guest_owns_session_key(guest_owner_id: str | None, session_key: object) -> bool:
    """Return whether ``session_key`` is in the server-derived guest namespace."""

    if not guest_owner_id or not _OWNER_ID_RE.fullmatch(guest_owner_id):
        return False
    parts = _guest_namespace_parts(session_key)
    return parts is not None and secrets.compare_digest(parts[0], guest_owner_id)


def guest_owned_session_key(
    guest_owner_id: str,
    requested_session_key: object = None,
    *,
    agent_id: str | None = None,
) -> str:
    """Bind a client-selected WebChat slug to a server-derived guest owner id."""

    if not _OWNER_ID_RE.fullmatch(str(guest_owner_id or "")):
        raise ValueError("guest_owner_id must be a lowercase SHA-256 hex digest")

    requested = canonicalize_session_key(str(requested_session_key or ""))
    if guest_owns_session_key(guest_owner_id, requested):
        return requested

    resolved_agent_id = agent_id or parse_agent_id(requested) or "main"
    raw_slug = requested.rsplit(":", 1)[-1] if requested else "default"
    if raw_slug in {"", "unknown", "webchat"}:
        raw_slug = "default"
    slug = _SESSION_SLUG_RE.sub("-", raw_slug).strip("-_")[:96]
    if not slug:
        slug = secrets.token_urlsafe(18)
    return f"agent:{resolved_agent_id}:webchat:guest:{guest_owner_id}:{slug}"


class GuestRpcPolicy:
    """Central allowlist and session-key guard for unauthenticated principals."""

    @staticmethod
    def is_guest(ctx: Any) -> bool:
        principal = getattr(ctx, "principal", None)
        capabilities = getattr(principal, "capabilities", ())
        guest_marked = (
            getattr(principal, "auth_state", None) in {"guest", "invalid"}
            or "guest.safe" in capabilities
        )
        return bool(
            principal is not None
            and not getattr(principal, "authenticated", False)
            and not getattr(principal, "is_owner", False)
            and guest_marked
        )

    @classmethod
    def authorize(cls, method: str, params: Any, ctx: Any) -> Any:
        """Return safe params or raise when a guest is outside the allowlist."""

        if not cls.is_guest(ctx):
            return params
        if method not in GUEST_RPC_ALLOWLIST:
            raise GuestRpcPolicyError("Anonymous guest RPC method is not allowed")

        owner_id = getattr(ctx.principal, "guest_owner_id", None)
        if not owner_id or not _OWNER_ID_RE.fullmatch(str(owner_id)):
            raise GuestRpcPolicyError("Anonymous guest identity is unavailable")

        # Guests may send their first message but cannot choose a cost or
        # capability profile that their principal is not allowed to mutate.
        # Keep the global routing default for both immediate and staged sends.
        if method in {"chat.send", "sessions.pending_inputs.enqueue"} and isinstance(
            params, dict
        ):
            params = dict(params)
            params.pop("initialRoutingMode", None)
            params.pop("initial_routing_mode", None)

        if method == "sessions.list":
            return params

        if method == "chat.send":
            if not isinstance(params, dict):
                return params
            normalized = dict(params)
            normalized["sessionKey"] = guest_owned_session_key(
                owner_id,
                params.get("sessionKey") or params.get("key"),
            )
            normalized.pop("key", None)
            normalized["noMemoryCapture"] = True
            normalized["no_memory_capture"] = True
            source = normalized.get("_source")
            normalized_source = dict(source) if isinstance(source, dict) else {}
            normalized_source["noMemoryCapture"] = True
            normalized_source["no_memory_capture"] = True
            normalized["_source"] = normalized_source
            return normalized

        if method == "sessions.delete":
            if not isinstance(params, dict):
                raise GuestRpcPolicyError("Guest session key is required")
            raw_keys = params.get("keys")
            if raw_keys is None:
                raw_keys = [params.get("key")]
            if not isinstance(raw_keys, list) or not raw_keys:
                raise GuestRpcPolicyError("Guest session key is required")
            if not all(guest_owns_session_key(owner_id, key) for key in raw_keys):
                raise GuestRpcPolicyError("Guest session is not owned by this browser")
            normalized = dict(params)
            normalized["keys"] = raw_keys
            normalized.pop("key", None)
            return normalized

        if not isinstance(params, dict):
            raise GuestRpcPolicyError("Guest session key is required")
        fields = _SESSION_KEY_FIELDS[method]
        key = next((params.get(field) for field in fields if params.get(field)), None)
        if not guest_owns_session_key(owner_id, key):
            raise GuestRpcPolicyError("Guest session is not owned by this browser")
        normalized = dict(params)
        normalized[fields[0]] = key
        for alias in fields[1:]:
            normalized.pop(alias, None)
        # Keep a guest WebUI's task id only after proving ownership of the
        # session key. chat.abort binds that id back to this same session in
        # TaskRuntime; stripping it here would widen a precise Stop into the
        # legacy whole-session cancellation path.
        return normalized


__all__ = [
    "GUEST_RPC_ALLOWLIST",
    "GuestRpcPolicy",
    "GuestRpcPolicyError",
    "guest_owned_session_key",
    "guest_owns_session_key",
]
