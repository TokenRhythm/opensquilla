from __future__ import annotations

from copy import deepcopy

import pytest

from opensquilla.telemetry.contracts.common import EventSource
from opensquilla.telemetry.server.producer_auth import (
    PRODUCER_SIGNATURE_HEADER,
    GrowthProducerAuthenticator,
    ProducerCredentialError,
    sign_producer_request,
    validate_producer_secrets,
)

_NOW = 1_788_230_400
_BODY = b'{"batch_version":1}'
_WEBSITE_SECRET = b"w" * 32


def _headers(
    *,
    producer: EventSource = EventSource.WEBSITE,
    secret: bytes = _WEBSITE_SECRET,
    timestamp: int = _NOW,
    body: bytes = _BODY,
) -> dict[str, str]:
    return sign_producer_request(
        producer=producer,
        secret=secret,
        timestamp=timestamp,
        body=body,
    )


def test_exact_signed_request_authenticates_one_configured_source() -> None:
    authenticator = GrowthProducerAuthenticator(
        {EventSource.WEBSITE: _WEBSITE_SECRET},
        clock=lambda: float(_NOW),
    )

    assert (
        authenticator.authenticate(
            headers=_headers(),
            body=_BODY,
            method="POST",
            path="/v1/growth/events",
        )
        is EventSource.WEBSITE
    )
    assert authenticator.configured_sources == {EventSource.WEBSITE}


def test_no_signature_headers_means_client_owned_unsigned_request() -> None:
    authenticator = GrowthProducerAuthenticator({}, clock=lambda: float(_NOW))

    assert (
        authenticator.authenticate(
            headers={"Content-Type": "application/json"},
            body=_BODY,
            method="POST",
            path="/v1/growth/events",
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    ["body", "method", "path", "producer", "signature", "partial", "stale", "future"],
)
def test_signature_is_fail_closed_and_bound_to_request(mutation: str) -> None:
    headers = _headers()
    body = _BODY
    method = "POST"
    path = "/v1/growth/events"
    now = _NOW
    if mutation == "body":
        body += b" "
    elif mutation == "method":
        method = "PUT"
    elif mutation == "path":
        path += "/"
    elif mutation == "producer":
        headers["X-OpenSquilla-Producer"] = "cdn"
    elif mutation == "signature":
        headers[PRODUCER_SIGNATURE_HEADER] = "v1=" + "0" * 64
    elif mutation == "partial":
        del headers[PRODUCER_SIGNATURE_HEADER]
    elif mutation == "stale":
        now += 301
    elif mutation == "future":
        now -= 301

    authenticator = GrowthProducerAuthenticator(
        {
            EventSource.WEBSITE: _WEBSITE_SECRET,
            EventSource.CDN: b"c" * 32,
        },
        clock=lambda: float(now),
    )
    with pytest.raises(ProducerCredentialError):
        authenticator.authenticate(
            headers=headers,
            body=body,
            method=method,
            path=path,
        )


def test_secret_configuration_is_defensively_copied_and_distinct() -> None:
    mutable = {EventSource.WEBSITE: bytearray(b"w" * 32)}
    with pytest.raises(ProducerCredentialError, match="bytes"):
        validate_producer_secrets(mutable)  # type: ignore[arg-type]

    configured = {EventSource.WEBSITE: _WEBSITE_SECRET}
    normalized = validate_producer_secrets(configured)
    changed = deepcopy(configured)
    changed[EventSource.WEBSITE] = b"x" * 32
    assert normalized[EventSource.WEBSITE] == _WEBSITE_SECRET

    with pytest.raises(ProducerCredentialError, match="distinct"):
        validate_producer_secrets(
            {
                EventSource.WEBSITE: _WEBSITE_SECRET,
                EventSource.CDN: _WEBSITE_SECRET,
            }
        )


@pytest.mark.parametrize(
    "producer",
    [
        EventSource.DESKTOP,
        EventSource.GATEWAY,
        EventSource.RUNTIME,
        EventSource.INSTALLER,
        EventSource.UPDATER,
    ],
)
def test_client_or_non_growth_sources_cannot_receive_service_credentials(
    producer: EventSource,
) -> None:
    with pytest.raises(ProducerCredentialError, match="server-owned"):
        validate_producer_secrets({producer: b"x" * 32})
