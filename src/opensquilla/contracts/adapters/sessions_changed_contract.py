"""Compatibility adapter for the v4 ``sessions.changed`` event payload.

The wire event predates the Contract registry in a few scheduler paths.  The
adapter therefore accepts both the current canonical payload (which carries
``schema_version=1``) and the older unversioned payload, while keeping the
original mapping untouched for exact v4 delivery.  Generated Pydantic models
are deliberately confined to this adapter; Gateway producers and future
clients consume these small functions instead of importing generated wire
types directly.
"""

from __future__ import annotations

import logging
import math
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_changed import (
    SessionsChangedCanonicalPayload,
    SessionsChangedLegacyPayload,
)
from opensquilla.contracts.generated.v4.sessions_changed_metadata import (
    SESSIONS_CHANGED_EVENT,
    SESSIONS_CHANGED_SCHEMA_VERSION,
)

log = logging.getLogger(__name__)


class SessionsChangedContractError(ValueError):
    """Raised when a sessions.changed payload cannot be decoded."""


def _validate_payload_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SessionsChangedContractError(
            f"{SESSIONS_CHANGED_EVENT} payload must be a JSON object"
        )
    return payload


def validate_sessions_changed_payload(
    payload: Any,
    *,
    allow_legacy: bool = True,
) -> dict[str, Any]:
    """Validate one payload without normalising or dropping unknown fields.

    The generated Pydantic model cannot express JSON Schema's ``not``
    assertion for the legacy branch, so the discriminator is checked here
    before selecting the corresponding generated model.  In particular,
    ``schema_version=2`` must not be silently accepted as an unversioned
    legacy event.
    """

    value = _validate_payload_shape(payload)
    if "schema_version" in value:
        schema_version = value["schema_version"]
        # JSON Schema/AJV model JSON numbers as one JavaScript number: an
        # integral ``1.0`` is therefore valid for ``type: integer``.  Keep
        # that cross-language rule while rejecting booleans and non-finite
        # values (``bool`` is an ``int`` subclass in Python).
        is_version_one = (
            (type(schema_version) is int and schema_version == SESSIONS_CHANGED_SCHEMA_VERSION)
            or (
                type(schema_version) is float
                and math.isfinite(schema_version)
                and schema_version == float(SESSIONS_CHANGED_SCHEMA_VERSION)
            )
        )
        if not is_version_one:
            raise SessionsChangedContractError(
                f"{SESSIONS_CHANGED_EVENT} schema_version must be integer "
                f"{SESSIONS_CHANGED_SCHEMA_VERSION}"
            )
        model_type: type[Any] = SessionsChangedCanonicalPayload
    else:
        if not allow_legacy:
            raise SessionsChangedContractError(
                f"{SESSIONS_CHANGED_EVENT} payload is missing schema_version"
            )
        model_type = SessionsChangedLegacyPayload

    try:
        model_type.model_validate(value)
    except ValidationError as exc:
        raise SessionsChangedContractError(
            f"{SESSIONS_CHANGED_EVENT} payload violated its v4 Contract"
        ) from exc
    return value


def canonicalize_sessions_changed_payload(payload: Any) -> dict[str, Any]:
    """Return a domain-safe canonical copy while preserving extension fields.

    This is intended for the later ``SessionDirectoryChanges`` decoder.  It
    is not used by the current producer path: v4 delivery must retain the
    legacy scheduler tree byte-for-byte until that client migration lands.
    """

    value = validate_sessions_changed_payload(payload)
    if "schema_version" in value:
        return dict(value)
    return {"schema_version": SESSIONS_CHANGED_SCHEMA_VERSION, **value}


def observe_sessions_changed_payload(
    payload: Any,
    *,
    source: str,
    allow_legacy: bool = True,
) -> Any:
    """Observe producer drift without changing best-effort event delivery.

    Existing event paths intentionally swallow subscriber failures.  A
    contract diagnostic must follow the same rule: log a bounded, structured
    warning and return the original payload instead of turning a notification
    into a new request failure.
    """

    try:
        value = validate_sessions_changed_payload(payload, allow_legacy=allow_legacy)
    except SessionsChangedContractError as exc:
        try:
            log.warning(
                "sessions_changed.contract_violation event=%s source=%s error_type=%s",
                SESSIONS_CHANGED_EVENT,
                source,
                type(exc).__name__,
            )
        except Exception:
            # Logging is diagnostic infrastructure and must not affect event
            # delivery when a processor/sink is unavailable.
            pass
        # Notifications are best-effort.  Preserve the original malformed
        # value so the existing sender/serializer remains the component that
        # decides how to handle it; this observer must not introduce a new
        # producer failure at a boundary.
        return payload
    return cast(dict[str, Any], value)


__all__ = [
    "SESSIONS_CHANGED_EVENT",
    "SESSIONS_CHANGED_SCHEMA_VERSION",
    "SessionsChangedContractError",
    "canonicalize_sessions_changed_payload",
    "observe_sessions_changed_payload",
    "validate_sessions_changed_payload",
]
