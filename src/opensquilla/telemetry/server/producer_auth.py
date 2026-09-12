"""Request authentication for server-owned growth event producers.

Desktop, Gateway, Runtime, and installer facts originate on end-user devices and
therefore cannot safely carry a shared service secret.  In contrast, website,
CDN, and account-service facts claim server-side authority.  Those sources use
independent HMAC credentials so an unauthenticated client cannot manufacture a
server-owned funnel milestone.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from typing import Final

from opensquilla.telemetry.contracts.common import EventSource

PRODUCER_HEADER: Final = "X-OpenSquilla-Producer"
PRODUCER_TIMESTAMP_HEADER: Final = "X-OpenSquilla-Timestamp"
PRODUCER_SIGNATURE_HEADER: Final = "X-OpenSquilla-Signature"
PRODUCER_SIGNATURE_VERSION: Final = "v1"

SERVER_OWNED_GROWTH_SOURCES: Final = frozenset(
    {
        EventSource.WEBSITE,
        EventSource.CDN,
        EventSource.ACCOUNT_SERVICE,
    }
)
CLIENT_OWNED_GROWTH_SOURCES: Final = frozenset(
    {
        EventSource.INSTALLER,
        EventSource.DESKTOP,
        EventSource.GATEWAY,
        EventSource.RUNTIME,
    }
)

_MIN_SECRET_BYTES = 32
_MAX_SECRET_BYTES = 64
_DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300
_HEX_DIGITS = frozenset("0123456789abcdef")


class ProducerCredentialError(ValueError):
    """A producer credential or signed request is invalid."""


def validate_producer_secrets(
    secrets: Mapping[EventSource, bytes],
) -> dict[EventSource, bytes]:
    """Return a defensive copy after validating source and secret invariants."""

    if not isinstance(secrets, Mapping):
        raise ProducerCredentialError("producer secrets must be a mapping")
    normalized: dict[EventSource, bytes] = {}
    for source, secret in secrets.items():
        if source not in SERVER_OWNED_GROWTH_SOURCES:
            raise ProducerCredentialError("producer source is not server-owned")
        if not isinstance(secret, bytes):
            raise ProducerCredentialError("producer secret must be bytes")
        if not _MIN_SECRET_BYTES <= len(secret) <= _MAX_SECRET_BYTES:
            raise ProducerCredentialError("producer secret must contain 32 to 64 bytes")
        normalized[source] = bytes(secret)
    if len(set(normalized.values())) != len(normalized):
        raise ProducerCredentialError("producer secrets must be distinct")
    return normalized


def _signature_payload(
    *,
    method: str,
    path: str,
    producer: EventSource,
    timestamp: str,
    body: bytes,
) -> bytes:
    body_digest = hashlib.sha256(body).hexdigest()
    return (
        f"{PRODUCER_SIGNATURE_VERSION}\n{method}\n{path}\n"
        f"{producer.value}\n{timestamp}\n{body_digest}"
    ).encode("ascii")


def sign_producer_request(
    *,
    secret: bytes,
    producer: EventSource,
    timestamp: int,
    body: bytes,
    method: str = "POST",
    path: str = "/v1/growth/events",
) -> dict[str, str]:
    """Create the three detached-signature headers for one exact request body."""

    validated = validate_producer_secrets({producer: secret})
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise ProducerCredentialError("producer timestamp must be a non-negative integer")
    if not isinstance(body, bytes):
        raise ProducerCredentialError("producer body must be bytes")
    if method != "POST" or path != "/v1/growth/events":
        raise ProducerCredentialError("producer signature target is invalid")
    rendered_timestamp = str(timestamp)
    signature = hmac.new(
        validated[producer],
        _signature_payload(
            method=method,
            path=path,
            producer=producer,
            timestamp=rendered_timestamp,
            body=body,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        PRODUCER_HEADER: producer.value,
        PRODUCER_TIMESTAMP_HEADER: rendered_timestamp,
        PRODUCER_SIGNATURE_HEADER: f"{PRODUCER_SIGNATURE_VERSION}={signature}",
    }


class GrowthProducerAuthenticator:
    """Verify bounded, replay-safe request signatures for configured producers."""

    def __init__(
        self,
        secrets: Mapping[EventSource, bytes],
        *,
        clock: Callable[[], float] = time.time,
        max_clock_skew_seconds: int = _DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    ) -> None:
        self._secrets = validate_producer_secrets(secrets)
        if (
            not isinstance(max_clock_skew_seconds, int)
            or isinstance(max_clock_skew_seconds, bool)
            or max_clock_skew_seconds < 1
            or max_clock_skew_seconds > 900
        ):
            raise ProducerCredentialError("producer clock skew must be 1 to 900 seconds")
        self._clock = clock
        self._max_clock_skew_seconds = max_clock_skew_seconds

    @property
    def configured_sources(self) -> frozenset[EventSource]:
        return frozenset(self._secrets)

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        method: str,
        path: str,
    ) -> EventSource | None:
        """Return the authenticated source, ``None`` for unsigned, or raise."""

        supplied = {
            PRODUCER_HEADER: headers.get(PRODUCER_HEADER),
            PRODUCER_TIMESTAMP_HEADER: headers.get(PRODUCER_TIMESTAMP_HEADER),
            PRODUCER_SIGNATURE_HEADER: headers.get(PRODUCER_SIGNATURE_HEADER),
        }
        if all(value is None for value in supplied.values()):
            return None
        if any(value is None for value in supplied.values()):
            raise ProducerCredentialError("producer signature headers are incomplete")

        producer_value = supplied[PRODUCER_HEADER]
        timestamp_value = supplied[PRODUCER_TIMESTAMP_HEADER]
        signature_value = supplied[PRODUCER_SIGNATURE_HEADER]
        assert producer_value is not None
        assert timestamp_value is not None
        assert signature_value is not None

        try:
            producer = EventSource(producer_value)
        except ValueError:
            raise ProducerCredentialError("producer is invalid") from None
        secret = self._secrets.get(producer)
        if secret is None:
            raise ProducerCredentialError("producer is not configured")
        if (
            not timestamp_value
            or not timestamp_value.isascii()
            or not timestamp_value.isdigit()
            or len(timestamp_value) > 10
        ):
            raise ProducerCredentialError("producer timestamp is invalid")
        timestamp = int(timestamp_value, 10)
        now = int(self._clock())
        if abs(now - timestamp) > self._max_clock_skew_seconds:
            raise ProducerCredentialError("producer timestamp is stale")

        prefix = f"{PRODUCER_SIGNATURE_VERSION}="
        if not signature_value.startswith(prefix):
            raise ProducerCredentialError("producer signature version is invalid")
        signature = signature_value[len(prefix) :]
        if len(signature) != 64 or any(char not in _HEX_DIGITS for char in signature):
            raise ProducerCredentialError("producer signature is invalid")
        expected = hmac.new(
            secret,
            _signature_payload(
                method=method,
                path=path,
                producer=producer,
                timestamp=timestamp_value,
                body=body,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ProducerCredentialError("producer signature is invalid")
        return producer


__all__ = [
    "CLIENT_OWNED_GROWTH_SOURCES",
    "PRODUCER_HEADER",
    "PRODUCER_SIGNATURE_HEADER",
    "PRODUCER_TIMESTAMP_HEADER",
    "SERVER_OWNED_GROWTH_SOURCES",
    "GrowthProducerAuthenticator",
    "ProducerCredentialError",
    "sign_producer_request",
    "validate_producer_secrets",
]
