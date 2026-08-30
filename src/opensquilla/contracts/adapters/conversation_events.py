"""Decode the v4 conversation event family at the Gateway boundary.

The v4 transport deliberately carries an open ``event`` string and an open
payload.  That is useful for additive rollout, but it used to leave every
consumer responsible for its own snake/camel aliases and replay metadata.
This adapter is the single compatibility seam for the next Conversation
Runtime slice.  It validates the envelope, normalises identity/cursor aliases
on a *copy*, and keeps unknown future events observable without handing a raw
wire mapping to application code.

No producer imports this module in S9.  Keeping the decoder dormant lets the
Contract land independently from the high-risk event-consumer migration.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.conversation_events import (
    ConversationEventCanonicalPayload,
    ConversationEventFrame,
    ConversationEventLegacyPayload,
)
from opensquilla.contracts.generated.v4.conversation_events_metadata import (
    CONVERSATION_EVENTS_EVENT,
    CONVERSATION_EVENTS_EVENT_METADATA,
    CONVERSATION_EVENTS_SCHEMA_VERSION,
)

ConversationEventKind = Literal["known", "unknown"]


class ConversationEventContractError(ValueError):
    """Raised when an event cannot be safely decoded at the wire seam."""


@dataclass(frozen=True, slots=True)
class DecodedConversationEvent:
    """Small, transport-independent projection of one conversation event."""

    name: str
    kind: ConversationEventKind
    payload: Mapping[str, Any] | None
    raw_payload: Any
    meta: Mapping[str, Any] | None
    session_key: str | None
    task_id: str | None
    turn_id: str | None
    stream_generation: str | None
    stream_seq: int | None
    connection_seq: int | None
    generation_epoch: int | None
    schema_version: int | None
    legacy: bool

    @property
    def is_known(self) -> bool:
        """Whether the event name is in the versioned Contract manifest."""

        return self.kind == "known"


_WIRE_NAMES = frozenset(
    name
    for name in cast(
        tuple[Any, ...],
        CONVERSATION_EVENTS_EVENT_METADATA.get("wireNames", ()),
    )
    if isinstance(name, str)
)
_EVENT_PREFIX = "session.event."
_BARE_EVENT_ALIASES = frozenset(
    name.removeprefix(_EVENT_PREFIX)
    for name in _WIRE_NAMES
    if name.startswith(_EVENT_PREFIX)
)
_MISSING = object()


def _object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _text_alias(value: Mapping[str, Any], *names: str) -> str | None:
    found: list[tuple[str, str]] = []
    for name in names:
        candidate = value.get(name)
        if candidate is None:
            continue
        if not isinstance(candidate, str) or not candidate.strip():
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} {name} must be a non-empty string"
            )
        found.append((name, candidate.strip()))
    unique = {candidate for _, candidate in found}
    if len(unique) > 1:
        fields = ", ".join(name for name, _ in found)
        raise ConversationEventContractError(
            f"{CONVERSATION_EVENTS_EVENT} has conflicting aliases: {fields}"
        )
    return next(iter(unique), None)


def _integer_alias(value: Mapping[str, Any], *names: str) -> int | None:
    found: list[tuple[str, int]] = []
    for name in names:
        candidate = value.get(name)
        if candidate is None:
            continue
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} {name} must be a JSON integer"
            )
        if isinstance(candidate, float):
            if not math.isfinite(candidate) or not candidate.is_integer():
                raise ConversationEventContractError(
                    f"{CONVERSATION_EVENTS_EVENT} {name} must be a JSON integer"
                )
        normalized = int(candidate)
        if normalized < 0:
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} {name} must be non-negative"
            )
        found.append((name, normalized))
    unique = {candidate for _, candidate in found}
    if len(unique) > 1:
        fields = ", ".join(name for name, _ in found)
        raise ConversationEventContractError(
            f"{CONVERSATION_EVENTS_EVENT} has conflicting numeric aliases: {fields}"
        )
    return next(iter(unique), None)


def canonical_event_name(value: Any) -> str:
    """Return the canonical v4 name for a known legacy event spelling."""

    if not isinstance(value, str) or not value.strip():
        raise ConversationEventContractError(
            f"{CONVERSATION_EVENTS_EVENT} event name must be a non-empty string"
        )
    name = value.strip()
    if name in _BARE_EVENT_ALIASES:
        return f"{_EVENT_PREFIX}{name}"
    if name == "session.answer_generation_reset.v1":
        return "session.event.answer_generation_reset"
    if name == "session.turn_committed.v1":
        return "session.event.turn_committed"
    return name


def _frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConversationEventContractError(
            f"{CONVERSATION_EVENTS_EVENT} frame must be a JSON object"
        )
    copied = dict(value)
    event = canonical_event_name(copied.get("event"))
    copied["event"] = event
    try:
        # The generated model owns the envelope shape/pattern.  Its result is
        # intentionally discarded so aliases and unknown fields stay intact.
        ConversationEventFrame.model_validate(copied)
    except ValidationError as exc:
        raise ConversationEventContractError(
            f"{CONVERSATION_EVENTS_EVENT} frame violated the v4 Contract"
        ) from exc
    return copied


def decode_conversation_event_frame(frame: Any) -> DecodedConversationEvent:
    """Decode a complete ``event`` frame without mutating the caller value."""

    original_name = frame.get("event") if isinstance(frame, Mapping) else None
    value = _frame(frame)
    event_name = cast(str, value["event"])
    legacy_name = (
        isinstance(original_name, str)
        and original_name.strip() != event_name
    )
    raw_payload = value.get("payload")
    payload = _object(raw_payload)
    meta = _object(value.get("meta"))

    connection_seq = _integer_alias(value, "seq")
    if payload is None:
        schema_version = None
        session_key = task_id = turn_id = stream_generation = None
        stream_seq = generation_epoch = None
        legacy = True
    else:
        # Select the generated canonical/legacy model first.  The generated
        # model enforces the version discriminator; alias conflict checks
        # below cover the cross-field invariant JSON Schema cannot express.
        try:
            payload_model = (
                ConversationEventCanonicalPayload
                if "schema_version" in payload
                and payload.get("schema_version") is not None
                else ConversationEventLegacyPayload
            )
            payload_model.model_validate(payload)
        except ValidationError as exc:
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} payload violated common field rules"
            ) from exc
        schema_version = _integer_alias(payload, "schema_version")
        if schema_version is not None and schema_version != CONVERSATION_EVENTS_SCHEMA_VERSION:
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} schema_version must be "
                f"{CONVERSATION_EVENTS_SCHEMA_VERSION}"
            )
        session_key = _text_alias(payload, "key", "session_key", "sessionKey")
        task_id = _text_alias(payload, "task_id", "taskId")
        turn_id = _text_alias(payload, "turn_id", "turnId")
        stream_generation = _text_alias(
            payload,
            "stream_generation",
            "streamGeneration",
        )
        stream_seq = _integer_alias(payload, "stream_seq", "streamSeq")
        generation_epoch = _integer_alias(
            payload,
            "generation_epoch",
            "generationEpoch",
        )
        _integer_alias(payload, "emitted_at", "emittedAt")
        legacy = schema_version is None or legacy_name

    return DecodedConversationEvent(
        name=event_name,
        kind="known" if event_name in _WIRE_NAMES else "unknown",
        payload=payload,
        raw_payload=raw_payload,
        meta=meta,
        session_key=session_key,
        task_id=task_id,
        turn_id=turn_id,
        stream_generation=stream_generation,
        stream_seq=stream_seq,
        connection_seq=connection_seq,
        generation_epoch=generation_epoch,
        schema_version=schema_version,
        legacy=legacy,
    )


def decode_conversation_event(
    event_or_frame: Any,
    payload: Any = _MISSING,
    meta: Any = _MISSING,
    connection_seq: Any = _MISSING,
) -> DecodedConversationEvent:
    """Decode either a full frame or an ``RpcClient.on`` event callback.

    The callback form keeps transport-specific arguments out of the returned
    object while still accepting the exact payload/meta values emitted by the
    current WebSocket client.
    """

    if payload is _MISSING and isinstance(event_or_frame, Mapping):
        if "event" not in event_or_frame:
            raise ConversationEventContractError(
                f"{CONVERSATION_EVENTS_EVENT} frame is missing event"
            )
        return decode_conversation_event_frame(event_or_frame)

    frame: dict[str, Any] = {
        "event": event_or_frame,
        "payload": None if payload is _MISSING else payload,
    }
    if meta is not _MISSING:
        frame["meta"] = meta
    if connection_seq is not _MISSING:
        frame["seq"] = connection_seq
    return decode_conversation_event_frame(frame)


def is_conversation_event_name(value: Any) -> bool:
    """Return whether a value is a valid v4 conversation event name."""

    try:
        canonical = canonical_event_name(value)
        # Reuse the generated pattern by validating a tiny frame.  This also
        # rejects unrelated gateway events such as ``presence``.
        _frame({"event": canonical})
    except ConversationEventContractError:
        return False
    return True


__all__ = [
    "ConversationEventContractError",
    "DecodedConversationEvent",
    "canonical_event_name",
    "decode_conversation_event",
    "decode_conversation_event_frame",
    "is_conversation_event_name",
]
