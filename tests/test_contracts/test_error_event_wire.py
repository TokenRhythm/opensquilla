"""Wire-contract freeze for the session.event.error payload.

The error payload keys are a public protocol contract (CLAUDE.md: public RPC
field names are stable). ``error_id`` joins a user-visible ``(ref: …)`` code
to its durable turn_errors row — clients and bug reports depend on it.

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key requires deliberately extending the frozen set in this file.
"""

from __future__ import annotations

from dataclasses import asdict

from opensquilla.engine.types import ErrorEvent
from opensquilla.gateway.rpc_sessions import _normalize_terminal_event_payload
from opensquilla.session.terminal_reply import append_error_ref

NORMALIZED_ERROR_KEYS = frozenset(
    {
        "message",
        "code",
        "error_id",
        "terminal_message",
        "terminal_reason",
        "error_message",
        "turn_outcome",
    }
)


def _synthetic_error_payload() -> dict:
    event = ErrorEvent(message="Agent error", code="agent_error", error_id="abcd1234")
    payload = asdict(event)
    payload.pop("kind")
    return payload


def test_error_event_dataclass_carries_error_id() -> None:
    payload = _synthetic_error_payload()
    assert payload["error_id"] == "abcd1234"


def test_normalized_error_payload_keys_are_frozen() -> None:
    normalized = _normalize_terminal_event_payload(
        "session.event.error", _synthetic_error_payload()
    )
    assert set(normalized) == NORMALIZED_ERROR_KEYS


def test_normalized_error_payload_message_carries_ref() -> None:
    normalized = _normalize_terminal_event_payload(
        "session.event.error", _synthetic_error_payload()
    )
    assert normalized["message"].endswith("(ref: abcd1234)")
    assert normalized["terminal_message"].endswith("(ref: abcd1234)")
    # Idempotent under the CLI client's re-normalization pass.
    assert append_error_ref(normalized["message"], "abcd1234") == normalized["message"]


def test_error_payload_without_ref_is_unchanged() -> None:
    event = ErrorEvent(message="Agent error", code="agent_error")
    payload = asdict(event)
    payload.pop("kind")
    normalized = _normalize_terminal_event_payload("session.event.error", payload)
    assert "(ref:" not in normalized["message"]


def test_channel_reply_carries_ref() -> None:
    # Pins the exact composition both channel_dispatch ErrorEvent sites use —
    # if a refactor stops threading the ref into channel replies, this fails.
    from opensquilla.gateway.channel_dispatch import _terminal_payload_from_error_event
    from opensquilla.session.terminal_reply import build_terminal_reply

    event = ErrorEvent(message="Agent error", code="agent_error", error_id="abcd1234")
    reply = append_error_ref(
        build_terminal_reply(_terminal_payload_from_error_event(event)),
        event.error_id or None,
    )
    assert reply == "The task failed before it could finish. (ref: abcd1234)"


def test_channel_reply_surfaces_actionable_ensemble_image_rejection() -> None:
    from opensquilla.gateway.channel_dispatch import _terminal_payload_from_error_event
    from opensquilla.session.terminal_reply import build_terminal_reply

    event = ErrorEvent(
        message=(
            "Ensemble does not support image input yet. "
            "Switch to a single-model routing mode and try again."
        ),
        code="ensemble_multimodal_unsupported",
    )

    assert build_terminal_reply(_terminal_payload_from_error_event(event)) == event.message
