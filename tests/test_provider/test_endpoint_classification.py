"""Provider defaults follow parsed endpoint hosts, not URL substrings."""

from __future__ import annotations

import pytest

from opensquilla.endpoint_identity import base_url_hostname
from opensquilla.provider.context_capabilities import (
    PromptCacheSupport,
    provider_context_capabilities,
)
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import _dashscope_endpoint_family
from opensquilla.provider.openai_codex import OpenAICodexProvider

_URL_FORMS = [
    "https://{host}.example.invalid/v1",
    "https://prefix-{host}/v1",
    "https://example.invalid/{host}/v1",
    "https://example.invalid/v1?endpoint={host}",
    "https://example.invalid/v1#{host}",
    "https://{host}@example.invalid/v1",
    "https://user@{host}/v1",
    "https://{host}:invalid/v1",
    "https://{host}\\@example.invalid/v1",
]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (" https://API.OpenAI.COM.:443/v1/ ", "api.openai.com"),
        ("http://api.openai.com:8080/custom/path", "api.openai.com"),
        ("https://api.openai.com/v1?query=value", "api.openai.com"),
        ("https://proxy.example/v1", "proxy.example"),
        ("", ""),
        ("api.openai.com/v1", ""),
        ("ftp://api.openai.com/v1", ""),
        ("https://[invalid", ""),
        ("https://api.openai.com:65536/v1", ""),
        ("https://api.openai.com\t/v1", ""),
        ("https://user:password@api.openai.com/v1", ""),
    ],
)
def test_endpoint_hostname_validation(url: str, expected: str) -> None:
    assert base_url_hostname(url) == expected


@pytest.mark.parametrize("url_form", _URL_FORMS)
@pytest.mark.parametrize("host", ["api.openai.com", "generativelanguage.googleapis.com"])
def test_url_substrings_do_not_enable_provider_cache_defaults(url_form: str, host: str) -> None:
    caps = provider_context_capabilities(
        provider_kind="openai", model="gemini-2.5-flash", base_url=url_form.format(host=host),
    )
    assert caps.prompt_cache == PromptCacheSupport.NONE


@pytest.mark.parametrize(
    ("url", "cache"),
    [
        ("https://API.OPENAI.COM.:443/v1", PromptCacheSupport.AUTOMATIC),
        ("https://api.openai.com:8443/custom/path", PromptCacheSupport.AUTOMATIC),
        ("https://generativelanguage.googleapis.com/v1beta/openai", PromptCacheSupport.IMPLICIT),
    ],
)
def test_official_hosts_keep_provider_cache_defaults(url: str, cache: PromptCacheSupport) -> None:
    caps = provider_context_capabilities(provider_kind="openai", model="example", base_url=url)
    assert caps.prompt_cache == cache


def test_explicit_gemini_provider_keeps_custom_proxy_capabilities() -> None:
    caps = provider_context_capabilities(
        provider_kind="gemini", model="gemini-2.5-flash", base_url="https://proxy.example/v1",
    )
    assert caps.prompt_cache == PromptCacheSupport.IMPLICIT
    assert caps.min_cache_tokens == 1024


@pytest.mark.parametrize("url_form", _URL_FORMS)
def test_url_substrings_do_not_grant_openai_catalog_provenance(url_form: str) -> None:
    catalog = ModelCatalog()
    model = "gpt-5-endpoint-test"
    url = url_form.format(host="api.openai.com")
    assert catalog.get_capabilities(model, "openai", url) == catalog.get_capabilities(
        model, "openai", "https://proxy.example/v1",
    )
    assert not catalog.tool_capability_is_verified(model, provider_name="openai", base_url=url)


@pytest.mark.parametrize("url", ["https://API.OPENAI.COM.:443/v1", "https://api.openai.com:8443/v1"])
def test_official_openai_host_keeps_catalog_provenance(url: str) -> None:
    catalog = ModelCatalog()
    model = "gpt-5-endpoint-test"
    assert catalog.get_capabilities(model, "openai", url).reasoning_format == "openai"
    assert catalog.tool_capability_is_verified(model, provider_name="openai", base_url=url)


@pytest.mark.parametrize(
    ("host", "family"),
    [
        ("coding-intl.dashscope.aliyuncs.com", "coding_global"),
        ("coding.dashscope.aliyuncs.com", "coding_cn"),
        ("dashscope-intl.aliyuncs.com", "standard_global"),
        ("dashscope.aliyuncs.com", "standard_cn"),
    ],
)
def test_dashscope_family_uses_exact_hostname(host: str, family: str) -> None:
    assert _dashscope_endpoint_family(f"https://{host.upper()}.:443/compatible-mode/v1") == family
    for url_form in _URL_FORMS:
        assert _dashscope_endpoint_family(url_form.format(host=host)) == "custom"


@pytest.mark.parametrize("host", ["chatgpt.com", "chat.openai.com"])
def test_codex_backend_path_only_added_to_official_root(host: str) -> None:
    normalize = OpenAICodexProvider._normalize_base_url
    assert normalize(f"https://{host}/") == f"https://{host}/backend-api"
    assert normalize(f"https://{host.upper()}.:443/") == f"https://{host.upper()}.:443/backend-api"
    for path in ("/backend-api", "/backend-api/custom", "/custom", "?x=1", "#backend-api"):
        url = f"https://{host}{path}"
        assert normalize(url) == url
    for url_form in _URL_FORMS:
        url = url_form.format(host=host)
        assert normalize(url) == url


@pytest.mark.parametrize("url", ["https://proxy.example/custom/", "https://[invalid", ""])
def test_codex_keeps_custom_endpoint_and_default_behavior(url: str) -> None:
    expected = url.rstrip("/") if url else "https://chatgpt.com/backend-api"
    assert OpenAICodexProvider._normalize_base_url(url) == expected
