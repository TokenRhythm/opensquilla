import pytest

from opensquilla.provider.app_attribution import (
    OPENSQUILLA_APP_REFERER,
    OPENSQUILLA_APP_TITLE,
    is_provider_app_host,
    provider_app_headers,
)

EXPECTED_HEADERS = {
    "HTTP-Referer": "https://opensquilla.ai",
    "X-Title": "OpenSquilla",
}


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1",
        "https://api.openrouter.ai/v1",
        "https://tokenrhythm.studio/v1",
        "https://api.tokenrhythm.studio/v1",
        "tokenrhythm.studio/v1",
        "https://tokenrhythm.example/v1",
        "https://api.tokenrhythm.example/v1",
        "https://api-tokenrhythm.example/v1",
    ],
)
def test_provider_app_headers_accept_supported_hosts(url: str) -> None:
    assert provider_app_headers(url) == EXPECTED_HEADERS


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "https://api.openai.com/v1",
        "http://localhost:4000/v1",
        "https://openrouter.ai.example.com/v1",
        "https://evilopenrouter.ai/v1",
        "https://example.com/tokenrhythm/v1",
        "https://example.com/v1?provider=tokenrhythm",
        "https://[tokenrhythm.studio",
        "http://[::1%.openrouter.ai]/v1",
        "http://[::1%25.openrouter.ai]/v1",
        "http://[fe80::1%.tokenrhythm.studio]/v1",
        "ftp://tokenrhythm.studio/v1",
    ],
)
def test_provider_app_headers_reject_untrusted_or_malformed_urls(
    url: str | None,
) -> None:
    assert provider_app_headers(url) == {}


def test_provider_app_identity_constants_are_fixed() -> None:
    assert OPENSQUILLA_APP_REFERER == "https://opensquilla.ai"
    assert OPENSQUILLA_APP_TITLE == "OpenSquilla"


def test_is_provider_app_host_rejects_non_allowlisted_root() -> None:
    assert not is_provider_app_host("https://example.com/v1", "example.com")
