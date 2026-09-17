from __future__ import annotations

import httpx
import pytest

from opensquilla.skills.hub.contracts import DiagnosticPhase
from opensquilla.skills.hub.source import (
    SkillSourceFetchError,
    raise_for_source_http_status,
)


class _UnreadErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"unconsumed provider error body"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "headers", "expected_code"),
    [
        (404, {}, "FETCH_NOT_FOUND"),
        (401, {}, "FETCH_AUTH_FAILED"),
        (403, {"x-ratelimit-remaining": "0"}, "FETCH_RATE_LIMITED"),
        (429, {"retry-after": "30"}, "FETCH_RATE_LIMITED"),
        (503, {}, "FETCH_SERVER_FAILED"),
    ],
)
async def test_unread_httpx_stream_preserves_fetch_status_diagnostic(
    status_code: int,
    headers: dict[str, str],
    expected_code: str,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status_code,
            headers=headers,
            stream=_UnreadErrorStream(),
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream("GET", "https://source.test/artifact") as response:
            assert response.is_stream_consumed is False

            with pytest.raises(SkillSourceFetchError) as raised:
                raise_for_source_http_status(
                    response,
                    phase=DiagnosticPhase.FETCH,
                    source_name="Test source",
                )

            assert response.is_stream_consumed is False

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == expected_code
    assert diagnostic.details["statusCode"] == status_code
    if status_code == 429:
        assert diagnostic.details["retryAfter"] == "30"


def test_consumed_response_keeps_body_based_rate_limit_detection() -> None:
    response = httpx.Response(
        403,
        content=b"API rate limit exceeded",
        request=httpx.Request("GET", "https://source.test/artifact"),
    )

    with pytest.raises(SkillSourceFetchError) as raised:
        raise_for_source_http_status(
            response,
            phase=DiagnosticPhase.SOURCE,
            source_name="Test source",
        )

    assert raised.value.diagnostics[0].code == "SOURCE_RATE_LIMITED"
