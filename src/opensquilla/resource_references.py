"""Transport-neutral resource identities shared by trusted runtime boundaries.

This module is a pure value contract. It must not import application, Gateway,
SessionStorage, or tool implementations, so loading a builtin tool never pulls
in the application composition layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from opensquilla.session_key import canonicalize_session_key


@dataclass(frozen=True, slots=True)
class SessionReferenceV1:
    """Stable model-facing reference for one session.

    References deliberately carry identity and capabilities instead of a
    transport URL.  Clients can use ``scope.sessionKey`` with the operation
    they already use to open or copy a session, while model-facing callers
    can retain the opaque ``id`` without guessing a route.
    """

    id: str
    label: str
    run_status: str | None = None
    available: bool = True
    can_open: bool = True
    can_copy: bool = True

    def as_dict(self) -> dict[str, object]:
        """Return the wire-shaped ReferenceV1 value."""

        return {
            "version": 1,
            "kind": "session",
            "id": self.id,
            "label": self.label,
            "scope": {"sessionKey": self.id},
            "state": {
                "available": self.available,
                "runStatus": self.run_status,
            },
            "capabilities": {
                "open": self.can_open,
                "copy": self.can_copy,
            },
        }


def session_reference_v1(
    session_key: str,
    *,
    title: str | None = None,
    run_status: str | None = None,
    available: bool = True,
    can_open: bool = True,
    can_copy: bool = True,
) -> dict[str, object]:
    """Build a transport-neutral ReferenceV1 for a session key.

    Session keys are canonicalized at this shared seam so legacy aliases do
    not create distinct references.  The label is intentionally bounded and
    falls back to the key when no human title is available.
    """

    key = canonicalize_session_key(session_key)
    label = " ".join(str(title or "").split())[:512] or key
    if run_status in {"timeout", "abandoned", "interrupted", "cancelled"}:
        run_status = "failed"
    normalized_status = run_status if run_status in {
        "queued", "running", "idle", "failed", "missing",
    } else None
    return SessionReferenceV1(
        id=key,
        label=label,
        run_status=normalized_status,
        available=available,
        can_open=can_open,
        can_copy=can_copy,
    ).as_dict()


__all__ = ["SessionReferenceV1", "session_reference_v1"]
