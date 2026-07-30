"""Tests for onboarding search provider catalog."""

from __future__ import annotations

from opensquilla.onboarding.search_specs import (
    get_search_provider_setup_spec,
    list_search_provider_setup_specs,
    search_provider_catalog_payload,
)


def test_search_catalog_includes_known_providers():
    ids = {s.provider_id for s in list_search_provider_setup_specs()}
    assert {"bocha", "brave", "duckduckgo", "iqs", "tavily", "exa", "perplexity"} <= ids


def test_search_catalog_marks_runtime_providers_supported():
    specs = {s.provider_id: s for s in list_search_provider_setup_specs()}
    assert specs["bocha"].runtime_supported is True
    assert specs["brave"].runtime_supported is True
    assert specs["duckduckgo"].runtime_supported is True
    assert specs["iqs"].runtime_supported is True
    assert specs["tavily"].runtime_supported is True
    assert specs["exa"].runtime_supported is True
    assert specs["perplexity"].runtime_supported is False


def test_bocha_search_spec_requires_api_key():
    spec = get_search_provider_setup_spec("bocha")
    assert spec.requires_api_key is True
    assert spec.env_key == "BOCHA_SEARCH_API_KEY"
    assert "content" in spec.capabilities
    assert "freshness" in spec.capabilities


def test_brave_search_spec_requires_api_key():
    spec = get_search_provider_setup_spec("brave")
    assert spec.requires_api_key is True
    assert spec.env_key == "BRAVE_SEARCH_API_KEY"


def test_iqs_search_spec_requires_api_key():
    spec = get_search_provider_setup_spec("iqs")
    assert spec.requires_api_key is True
    assert spec.env_key == "IQS_SEARCH_API_KEY"
    assert spec.label == "Alibaba Cloud IQS"
    assert "content" in spec.capabilities
    assert "freshness" in spec.capabilities
    assert "domain_filter" in spec.capabilities


def test_tavily_search_spec_requires_api_key():
    spec = get_search_provider_setup_spec("tavily")
    assert spec.requires_api_key is True
    assert spec.env_key == "TAVILY_API_KEY"


def test_duckduckgo_search_spec_does_not_require_api_key():
    spec = get_search_provider_setup_spec("duckduckgo")
    assert spec.requires_api_key is False


def test_search_catalog_payload_is_web_safe_shape():
    payload = search_provider_catalog_payload()
    first = payload[0]
    assert "providerId" in first
    assert "runtimeSupported" in first
    assert "fields" in first
    assert "blocking" in first
    assert "whatYouNeed" in first


def test_search_catalog_explains_fallback_and_diagnostics_fields():
    spec = get_search_provider_setup_spec("brave")
    fields = {field.name: field for field in spec.fields}

    assert "DuckDuckGo" in fields["fallback_policy"].description
    assert "at most one additional" in fields["fallback_policy"].description
    assert "attempt/error details" in fields["diagnostics"].description


def test_search_payload_marks_search_as_optional_capability():
    payload = search_provider_catalog_payload()

    assert all(row["blocking"] is False for row in payload)
    assert all(row["canProbe"] is False for row in payload)
    assert all(row["readmeScenarios"] for row in payload)


def test_search_api_key_description_states_plaintext_storage_and_env_fallback():
    """The api_key hint must describe where the key actually goes: pasted
    keys persist as plaintext ``search_api_key`` in the config file (taking
    precedence over the env var); blank reads the env var instead. The old
    "Stored under env key X." wording claimed the opposite."""
    spec = get_search_provider_setup_spec("brave")
    api_field = next(f for f in spec.fields if f.name == "api_key")

    assert "Stored under env key" not in api_field.description
    assert "plaintext search_api_key" in api_field.description
    assert spec.env_key in api_field.description
    assert "Leave blank" in api_field.description


def test_search_api_key_description_empty_without_env_key():
    spec = get_search_provider_setup_spec("duckduckgo")
    api_field = next(f for f in spec.fields if f.name == "api_key")
    assert api_field.description == ""
