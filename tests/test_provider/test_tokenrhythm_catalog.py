from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.tokenrhythm_catalog import (
    TokenRhythmModelMetadata,
    canonical_tokenrhythm_base_url,
    fetch_tokenrhythm_declared,
    fetch_tokenrhythm_published,
    merge_tokenrhythm_catalog,
    parse_tokenrhythm_declared,
    parse_tokenrhythm_published,
    tokenrhythm_authority_identity,
    tokenrhythm_declared_from_wire,
    tokenrhythm_declared_to_wire,
    tokenrhythm_published_catalog_entries,
    tokenrhythm_published_from_wire,
    tokenrhythm_published_to_wire,
)
from opensquilla.provider.types import ModelInfo


def _published_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "qwen3.8-max",
        "name": "Qwen3.8 Max",
        "type": "chat",
        "status": "testing",
        "contextWindow": 1_000_000,
        "maxOutputTokens": 131_072,
        "providerDisplayName": "Synthetic Provider",
        "modalities": ["text", "image"],
        "reasoningMode": "optional",
        "reasoningDefault": "provider",
        "reasoningSupportedEfforts": [],
        "reasoningSupportsMaxTokens": False,
        "capabilities": {
            "tools": True,
            "reasoning": True,
            "vision": True,
            "anthropic": False,
            "responses": True,
            "stream": True,
        },
        "protocolCapabilities": {
            "responses": {
                "available": True,
                "modes": ["native"],
                "stream": True,
                "tools": True,
                "background": False,
                "compact": False,
                "webSearch": False,
                "mcp": False,
                "codeInterpreter": False,
                "imageGeneration": False,
                "fileSearch": False,
                "cancel": False,
            }
        },
        "currency": "CNY",
        "billingMode": "per_1m_tokens",
        "billingUnit": 1_000_000,
        "pricePerImage": None,
        "hasDiscount": False,
        "inputPrice": "12.00",
        "outputPrice": "36",
        "cacheReadPrice": "1.5",
        "effectiveInputPrice": "12.00",
        "effectiveOutputPrice": "36",
        "effectiveCacheReadPrice": "1.5",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://tokenrhythm.studio", "https://tokenrhythm.studio/v1"),
        ("HTTPS://TOKENRHYTHM.STUDIO:443/v1/", "https://tokenrhythm.studio/v1"),
        ("https://tokenrhythm.studio/v1", "https://tokenrhythm.studio/v1"),
    ],
)
def test_canonical_tokenrhythm_base_url_accepts_only_registry_root(
    value: str,
    expected: str,
) -> None:
    assert canonical_tokenrhythm_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://api.tokenrhythm.studio/v1",
        "https://tokenrhythm.studio/v2",
        "https://tokenrhythm.studio/custom",
        "https://tokenrhythm.studio/v1?tenant=x",
        "https://tokenrhythm.studio/v1#fragment",
        "https://user@tokenrhythm.studio/v1",
        "https://tokenrhythm.studio:8443/v1",
        "http://tokenrhythm.studio/v1",
        "https://[malformed/v1",
        "not a url",
    ],
)
def test_canonical_tokenrhythm_base_url_rejects_non_registry_roots(value: str) -> None:
    assert canonical_tokenrhythm_base_url(value) == ""


def test_tokenrhythm_authority_identity_normalizes_equivalent_official_roots() -> None:
    key = "synthetic-authority-key-a"
    origin = tokenrhythm_authority_identity(
        provider="TokenRhythm",
        base_url="https://TOKENRHYTHM.studio:443",
        api_key=key,
    )
    versioned = tokenrhythm_authority_identity(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1/",
        api_key=key,
    )
    assert origin is not None
    assert origin == versioned
    assert tokenrhythm_authority_identity(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/custom",
        api_key=key,
    ) is None


def test_deployment_limits_and_capabilities_are_isolated_by_authority() -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    declared_a = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "qwen3.8-max",
                    "context_length": 1_000_000,
                    "max_completion_tokens": 131_072,
                    "supports_tools": True,
                    "supports_streaming": True,
                    "supports_vision": True,
                }
            ]
        }
    )
    declared_b = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "qwen3.8-max",
                    "context_length": 64_000,
                    "max_completion_tokens": 8_192,
                    "supports_tools": False,
                    "supports_streaming": False,
                    "supports_vision": False,
                }
            ]
        }
    )
    authority_a = tokenrhythm_authority_identity(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-authority-key-a",
    )
    authority_b = tokenrhythm_authority_identity(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-authority-key-b",
    )
    assert authority_a is not None and authority_b is not None
    catalog = ModelCatalog()
    catalog.set_tokenrhythm_snapshot_sidecars(
        published=published,
        declared_by_authority={
            authority_a: declared_a,
            authority_b: declared_b,
        },
    )

    limits_a = catalog.resolve_deployment_limits(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-a",
        base_url="https://tokenrhythm.studio/v1",
    )
    limits_b = catalog.resolve_deployment_limits(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-b",
        base_url="https://tokenrhythm.studio/v1",
    )
    assert (limits_a.context_window, limits_a.max_output_tokens) == (
        1_000_000,
        131_072,
    )
    assert (limits_b.context_window, limits_b.max_output_tokens) == (64_000, 8_192)
    caps_a = catalog.resolve_deployment_capabilities(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-a",
        base_url="https://tokenrhythm.studio/v1",
    )
    caps_b = catalog.resolve_deployment_capabilities(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-b",
        base_url="https://tokenrhythm.studio/v1",
    )
    assert caps_a.supports_tools is True
    assert caps_a.supports_streaming is True
    assert caps_b.supports_tools is False
    assert caps_b.supports_streaming is False
    assert catalog.resolve_deployment_vision_support(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-a",
        base_url="https://tokenrhythm.studio/v1",
    ) == "supported"
    assert catalog.resolve_deployment_vision_support(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-authority-key-b",
        base_url="https://tokenrhythm.studio/v1",
    ) == "unsupported"


def test_custom_tokenrhythm_deployment_never_uses_website_projection() -> None:
    published = parse_tokenrhythm_published(
        {
            "data": [
                {
                    "id": "website-only-model",
                    "type": "chat",
                    "status": "online",
                    "contextWindow": 900_000,
                    "maxOutputTokens": 77_777,
                    "capabilities": {"tools": False, "vision": True},
                }
            ]
        }
    )
    catalog = ModelCatalog()
    catalog.set_tokenrhythm_snapshot_sidecars(
        published=published,
        declared_by_authority={},
    )

    official = catalog.resolve_deployment_limits(
        "website-only-model",
        provider="tokenrhythm",
        api_key="synthetic-key",
        base_url="https://tokenrhythm.studio/v1",
    )
    custom = catalog.resolve_deployment_limits(
        "website-only-model",
        provider="tokenrhythm",
        api_key="synthetic-key",
        base_url="https://mirror.example/v1",
    )
    custom_caps = catalog.resolve_deployment_capabilities(
        "website-only-model",
        provider="tokenrhythm",
        api_key="synthetic-key",
        base_url="https://mirror.example/v1",
    )
    assert (official.context_window, official.max_output_tokens) == (900_000, 77_777)
    assert official.max_output_tokens_known is True
    assert (custom.context_window, custom.max_output_tokens) == (200_000, 16_384)
    assert custom.max_output_tokens_known is False
    assert custom_caps.supports_tools is True
    assert custom_caps.supports_vision is False


def test_model_info_omits_absent_metadata_but_serializes_typed_projection() -> None:
    base = ModelInfo(provider="synthetic", model_id="model")
    enriched = ModelInfo(
        provider="tokenrhythm",
        model_id="qwen3.8-max",
        metadata={"schemaVersion": 1},
    )

    assert "metadata" not in base.model_dump()
    assert "metadata" not in base.model_dump(exclude_none=False)
    assert enriched.model_dump(include={"provider", "metadata"}) == {
        "provider": "tokenrhythm",
        "metadata": {"schemaVersion": 1},
    }
    validation_schema = ModelInfo.model_json_schema(mode="validation")
    serialization_schema = ModelInfo.model_json_schema(mode="serialization")
    assert serialization_schema["type"] == "object"
    assert set(serialization_schema["properties"]) == set(
        validation_schema["properties"]
    )


def test_published_normalization_preserves_raw_values_false_and_decimal_strings() -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})

    wire = published["qwen3.8-max"].to_wire()
    assert set(wire) == {
        "name",
        "providerDisplayName",
        "modelType",
        "status",
        "modalities",
        "contextWindow",
        "maxOutputTokens",
        "reasoningMode",
        "reasoningDefault",
        "reasoningSupportedEfforts",
        "reasoningSupportsMaxTokens",
        "capabilities",
        "responses",
        "pricing",
    }
    assert wire["status"] == "testing"
    assert wire["maxOutputTokens"] == 131_072
    assert wire["reasoningDefault"] == "provider"
    assert wire["reasoningSupportedEfforts"] == []
    assert wire["reasoningSupportsMaxTokens"] is False
    assert wire["capabilities"]["anthropic"] is False
    assert wire["capabilities"]["vision"] is True
    assert wire["pricing"]["standard"]["input"] == "12.00"
    assert wire["pricing"]["billingMode"] == "per_1m_tokens"
    assert wire["pricing"]["pricePerImage"] is None
    assert wire["responses"] == {
        "modes": ["native"],
        "capabilities": ["stream", "tools"],
        "capabilityStates": {
            "stream": True,
            "tools": True,
            "background": False,
            "compact": False,
            "webSearch": False,
            "mcp": False,
            "codeInterpreter": False,
            "imageGeneration": False,
            "fileSearch": False,
            "cancel": False,
        },
    }


def test_response_states_preserve_false_and_explicit_empty_modes() -> None:
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "explicit-response-shape",
                    "protocolCapabilities": {
                        "responses": {
                            "modes": [],
                            "stream": True,
                            "tools": False,
                            "background": None,
                        }
                    },
                    "responses_modes": ["native"],
                    "responses_capabilities": ["stream", "tools", "background"],
                }
            ]
        }
    )["explicit-response-shape"]

    assert declared.responses is not None
    wire = declared.responses.to_wire()
    assert wire["modes"] == []
    assert wire["capabilities"] == ["stream"]
    assert wire["capabilityStates"]["stream"] is True
    assert wire["capabilityStates"]["tools"] is False
    assert wire["capabilityStates"]["background"] is None


@pytest.mark.parametrize("shape", ["direct", "protocol"])
def test_nested_response_capabilities_override_top_level_compatibility_list(
    shape: str,
) -> None:
    nested = {
        "capabilities": ["stream", "mcp", "futureCapability"],
        "stream": False,
    }
    row: dict[str, Any] = {
        "id": f"nested-response-{shape}",
        "responses_capabilities": ["tools"],
    }
    if shape == "direct":
        row["responses"] = nested
    else:
        row["protocolCapabilities"] = {"responses": nested}

    declared = parse_tokenrhythm_declared({"data": [row]})[row["id"]]

    assert declared.responses is not None
    wire = declared.responses.to_wire()
    assert wire["capabilities"] == ["mcp", "futureCapability"]
    assert wire["capabilityStates"]["stream"] is False
    assert wire["capabilityStates"]["mcp"] is True
    assert wire["capabilityStates"]["tools"] is None


def test_price_normalization_rejects_float_but_preserves_decimal_exactly() -> None:
    published = parse_tokenrhythm_published(
        {
            "data": [
                _published_row(id="float-price", inputPrice=0.1, pricePerImage=0.2),
                _published_row(
                    id="decimal-price",
                    inputPrice=Decimal("0.10000000000000001"),
                    pricePerImage=Decimal("0.200"),
                ),
            ]
        }
    )

    assert published["float-price"].pricing is not None
    assert published["float-price"].pricing.standard.input is None
    assert published["float-price"].pricing.price_per_image is None
    assert published["decimal-price"].pricing is not None
    assert published["decimal-price"].pricing.standard.input == "0.10000000000000001"
    assert published["decimal-price"].pricing.price_per_image == "0.200"


def test_schema_drift_logs_only_unknown_field_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_catalog.log.debug",
        lambda event, **fields: events.append((event, fields)),
    )
    secret_value = "DO_NOT_LOG_SECRET_VALUE"

    published_row = _published_row(futurePublishedField=secret_value)
    published_row["capabilities"]["futureCapability"] = secret_value
    published_row["protocolCapabilities"]["responses"]["futureResponseField"] = (
        secret_value
    )
    parse_tokenrhythm_published(
        {"data": [published_row], "futurePublishedEnvelope": secret_value}
    )
    parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "qwen3.8-max",
                    "futureDeclaredField": secret_value,
                    "top_provider": {
                        "max_completion_tokens": 131_072,
                        "futureTopProviderField": secret_value,
                    },
                }
            ],
            "futureDeclaredEnvelope": secret_value,
        }
    )

    assert {event for event, _fields in events} == {
        "tokenrhythm_catalog.schema_drift"
    }
    observed_fields = {
        (fields["source"], fields["scope"], tuple(fields["fields"]))
        for _, fields in events
    }
    assert observed_fields == {
        ("published", "envelope", ("futurePublishedEnvelope",)),
        ("published", "row", ("futurePublishedField",)),
        ("published", "row.capabilities", ("futureCapability",)),
        (
            "published",
            "row.protocolCapabilities.responses",
            ("futureResponseField",),
        ),
        ("declared", "envelope", ("futureDeclaredEnvelope",)),
        ("declared", "row", ("futureDeclaredField",)),
        ("declared", "row.top_provider", ("futureTopProviderField",)),
    }
    assert secret_value not in repr(events)


def test_published_and_declared_wire_round_trip_without_raw_payload() -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "qwen3.8-max",
                    "name": "Qwen3.8 Max declared",
                    "type": "chat",
                    "status": "testing",
                    "context_length": 1_000_000,
                    "max_completion_tokens": 131_072,
                    "supports_tools": False,
                }
            ]
        }
    )

    assert tokenrhythm_published_from_wire(
        tokenrhythm_published_to_wire(published)
    )["qwen3.8-max"].to_wire() == published["qwen3.8-max"].to_wire()
    assert tokenrhythm_declared_from_wire(
        tokenrhythm_declared_to_wire(declared)
    )["qwen3.8-max"].to_wire() == declared["qwen3.8-max"].to_wire()
    declared_wire = tokenrhythm_declared_to_wire(declared)["qwen3.8-max"]
    assert declared_wire["displayName"] == "Qwen3.8 Max declared"
    assert declared_wire["modelType"] == "chat"
    assert declared_wire["status"] == "testing"


def test_declared_top_level_max_wins_nested_and_unknown_booleans_stay_none() -> None:
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "qwen3.8-max",
                    "max_completion_tokens": 131_072,
                    "top_provider": {"max_completion_tokens": 8_192},
                    "supports_anthropic": False,
                    "supports_responses": True,
                    "supports_streaming": False,
                    "responses_modes": ["native"],
                    "responses_capabilities": ["stream", "tools"],
                }
            ]
        }
    )["qwen3.8-max"]

    assert declared.max_output_tokens == 131_072
    assert declared.capabilities.tools is None
    assert declared.capabilities.anthropic is False
    assert declared.capabilities.responses is True
    assert declared.capabilities.streaming is False
    assert declared.responses is not None
    assert declared.responses.to_wire() == {
        "modes": ["native"],
        "capabilities": ["stream", "tools"],
        "capabilityStates": {
            "stream": True,
            "tools": True,
            "background": None,
            "compact": None,
            "webSearch": None,
            "mcp": None,
            "codeInterpreter": None,
            "imageGeneration": None,
            "fileSearch": None,
            "cancel": None,
        },
    }


def test_declared_zero_limits_fall_through_and_published_zero_is_unknown() -> None:
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "zero-top-level",
                    "context_length": 0,
                    "context_window": 200_000,
                    "max_completion_tokens": 0,
                    "top_provider": {"max_completion_tokens": 8_192},
                }
            ]
        }
    )["zero-top-level"]
    published = parse_tokenrhythm_published(
        {"data": [_published_row(id="zero-published", contextWindow=0, maxOutputTokens=0)]}
    )["zero-published"]

    assert declared.context_window == 200_000
    assert declared.max_output_tokens == 8_192
    assert published.context_window is None
    assert published.max_output_tokens is None


def test_declared_capabilities_array_is_positive_evidence_only() -> None:
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {
                    "id": "array-shape-model",
                    "capabilities": ["tools", "reasoning", "streaming"],
                }
            ]
        }
    )["array-shape-model"]

    assert declared.capabilities.tools is True
    assert declared.capabilities.reasoning is True
    assert declared.capabilities.streaming is True
    assert declared.capabilities.vision is None


def test_merge_uses_auth_as_entitlement_and_filters_known_non_chat_or_offline() -> None:
    published = parse_tokenrhythm_published(
        {
            "data": [
                _published_row(),
                _published_row(id="public-only"),
                _published_row(id="image-model", type="image"),
                _published_row(id="offline-model", status="offline"),
                _published_row(id="paused-model", status="maintenance"),
                _published_row(id="declared-image"),
            ]
        }
    )
    declared = parse_tokenrhythm_declared(
        {
            "data": [
                {"id": "qwen3.8-max"},
                {"id": "auth-only", "max_completion_tokens": 4_096},
                {"id": "image-model"},
                {"id": "offline-model"},
                {"id": "paused-model"},
                {"id": "auth-only-image", "type": "image"},
                {"id": "auth-only-offline", "status": "offline"},
                {"id": "declared-image", "type": "image"},
            ]
        }
    )

    merged = merge_tokenrhythm_catalog(published, declared)
    runtime_entries = tokenrhythm_published_catalog_entries(published)

    assert set(merged) == {"qwen3.8-max", "auth-only"}
    assert "image-model" not in runtime_entries
    assert "offline-model" not in runtime_entries
    assert merged["qwen3.8-max"].metadata.published is not None
    assert merged["auth-only"].metadata.published is None


def test_v4_flash_0731_public_metadata_does_not_grant_entitlement() -> None:
    model_id = "deepseek-v4-flash-0731"
    published = parse_tokenrhythm_published(
        {"data": [_published_row(id=model_id, name="DeepSeek V4 Flash 0731")]}
    )

    assert merge_tokenrhythm_catalog(published, {}) == {}

    declared = parse_tokenrhythm_declared({"data": [{"id": model_id}]})
    merged = merge_tokenrhythm_catalog(published, declared)
    assert set(merged) == {model_id}
    assert merged[model_id].metadata.published is published[model_id]
    assert merged[model_id].metadata.declared is declared[model_id]


def test_testing_model_projects_raw_catalog_value_but_runtime_policy_is_scoped() -> None:
    published = parse_tokenrhythm_published(
        {
            "data": [
                _published_row(
                    id="minimax-m2.7",
                    contextWindow=200_000,
                    maxOutputTokens=192_000,
                )
            ]
        }
    )
    entries = tokenrhythm_published_catalog_entries(published)
    catalog = ModelCatalog()
    catalog.set_live_provider_entries("tokenrhythm", entries)

    assert entries["minimax-m2.7"]["max_output_tokens"] == 192_000
    assert catalog.resolve_entry(
        "minimax-m2.7", provider="tokenrhythm"
    ).max_output_tokens == 192_000
    assert catalog.resolve_max_tokens("minimax-m2.7", provider="tokenrhythm") == 100_000
    # The provider-scoped rule must not replace OpenRouter's existing contract.
    catalog._populate_from_data(
        [
            {
                "id": "minimax-m2.7",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 192_000},
            }
        ]
    )
    assert catalog.resolve_max_tokens("minimax-m2.7", provider="openrouter") == 8_192


def test_qwen38_auto_resolves_to_published_131072() -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )

    assert catalog.resolve_max_tokens("qwen3.8-max", provider="tokenrhythm") == 131_072


def test_explicit_override_is_not_clamped_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )

    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog.log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    first = catalog.resolve_max_tokens(
        "qwen3.8-max", user_override=200_000, provider="tokenrhythm"
    )
    second = catalog.resolve_max_tokens(
        "qwen3.8-max", user_override=200_000, provider="tokenrhythm"
    )

    assert first == second == 200_000
    assert warnings == [
        (
            "model_catalog.max_tokens_override_exceeds_provider_cap",
            {
                "provider": "tokenrhythm",
                "model": "qwen3.8-max",
                "configured_max_tokens": 200_000,
                "provider_cap": 131_072,
                "declared_max_tokens": None,
                "published_max_tokens": 131_072,
            },
        )
    ]


def test_deployment_lookup_warns_for_oversized_logical_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog = ModelCatalog()
    catalog.set_tokenrhythm_snapshot_sidecars(
        published=published,
        declared_by_authority={},
    )
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog.log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    limits = catalog.resolve_deployment_limits(
        "qwen3.8-max",
        provider="tokenrhythm",
        api_key="synthetic-warning-key",
        base_url="https://tokenrhythm.studio/v1",
        logical_max_tokens_override=200_000,
    )

    assert limits.max_output_tokens == 131_072
    assert warnings == [
        (
            "model_catalog.max_tokens_override_exceeds_provider_cap",
            {
                "provider": "tokenrhythm",
                "model": "qwen3.8-max",
                "configured_max_tokens": 200_000,
                "provider_cap": 131_072,
                "declared_max_tokens": None,
                "published_max_tokens": 131_072,
            },
        )
    ]


def test_per_model_override_is_not_clamped_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )
    catalog.set_user_overrides(
        {"tokenrhythm/qwen3.8-max": {"max_output_tokens": 200_000}}
    )
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog.log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    assert catalog.resolve_max_tokens("qwen3.8-max", provider="tokenrhythm") == 200_000
    assert warnings[0][0] == "model_catalog.max_tokens_override_exceeds_provider_cap"


def test_runtime_uses_conservative_public_declared_conflict_without_losing_raw() -> None:
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    declared = parse_tokenrhythm_declared(
        {"data": [{"id": "qwen3.8-max", "max_completion_tokens": 200_000}]}
    )
    merged = merge_tokenrhythm_catalog(published, declared)
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )
    catalog.set_provider_model_metadata(
        "tokenrhythm", {model_id: model.metadata for model_id, model in merged.items()}
    )

    assert merged["qwen3.8-max"].max_output_tokens == 200_000
    assert merged["qwen3.8-max"].metadata.published.max_output_tokens == 131_072
    assert merged["qwen3.8-max"].metadata.declared.max_output_tokens == 200_000
    assert catalog.resolve_max_tokens("qwen3.8-max", provider="tokenrhythm") == 131_072


@pytest.mark.asyncio
async def test_typed_fetch_helpers_return_normalized_records_and_gate_auth_host() -> None:
    public_response = MagicMock()
    public_response.raise_for_status = MagicMock()
    public_response.json.return_value = {"data": [_published_row()]}
    auth_response = MagicMock()
    auth_response.raise_for_status = MagicMock()
    auth_response.json.return_value = {
        "data": [{"id": "qwen3.8-max", "max_completion_tokens": 131_072}]
    }
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=[public_response, auth_response])

    with (
        patch(
            "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
            return_value=client,
        ) as client_cls,
        patch(
            "opensquilla.provider.tokenrhythm_catalog.tokenrhythm_install_id_headers",
            return_value={"X-OpenSquilla-Install-Id": "synthetic-install-id"},
        ) as install_headers,
    ):
        published = await fetch_tokenrhythm_published()
        declared = await fetch_tokenrhythm_declared(
            api_key="sk-tr-synthetic-not-a-real-secret"
        )

    assert published["qwen3.8-max"].max_output_tokens == 131_072
    assert declared["qwen3.8-max"].max_output_tokens == 131_072
    public_response.json.assert_called_once_with(parse_float=Decimal)
    auth_response.json.assert_called_once_with(parse_float=Decimal)
    public_call, auth_call = client.get.await_args_list
    assert "Authorization" not in public_call.kwargs["headers"]
    assert public_call.kwargs["headers"]["X-OpenSquilla-Install-Id"] == (
        "synthetic-install-id"
    )
    assert auth_call.args[0] == "https://tokenrhythm.studio/v1/models"
    assert auth_call.kwargs["headers"]["Authorization"].startswith("Bearer sk-tr-synthetic")
    assert auth_call.kwargs["headers"]["X-OpenSquilla-Install-Id"] == (
        "synthetic-install-id"
    )
    assert client_cls.call_count == 2
    assert all(
        call.kwargs["follow_redirects"] is False
        for call in client_cls.call_args_list
    )
    assert [call.args[:2] for call in install_headers.call_args_list] == [
        ("tokenrhythm", "https://tokenrhythm.studio/api/models"),
        ("tokenrhythm", "https://tokenrhythm.studio/v1/models"),
    ]
    assert all(call.kwargs["proxy"] is None for call in install_headers.call_args_list)
    with patch(
        "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient"
    ) as forbidden_client:
        with pytest.raises(ValueError, match="official HTTPS host"):
            await fetch_tokenrhythm_declared(
                base_url="https://example.invalid/v1",
                api_key="sk-tr-synthetic-not-a-real-secret",
            )
        for unsafe_base_url in (
            "https://catalog.tokenrhythm.studio/v1",
            "https://tokenrhythm.studio:8443/v1",
            "https://user@tokenrhythm.studio/v1",
        ):
            with pytest.raises(ValueError, match="official HTTPS host"):
                await fetch_tokenrhythm_declared(
                    base_url=unsafe_base_url,
                    api_key="sk-tr-synthetic-not-a-real-secret",
                )
        forbidden_client.assert_not_called()


@pytest.mark.asyncio
async def test_typed_fetch_passes_explicit_proxy_to_install_id_gate() -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": []}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    proxy = "http://127.0.0.1:9999"

    with (
        patch(
            "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
            return_value=client,
        ) as client_cls,
        patch(
            "opensquilla.provider.tokenrhythm_catalog.tokenrhythm_install_id_headers",
            return_value={},
        ) as install_headers,
    ):
        assert await fetch_tokenrhythm_published(proxy=proxy) == {}

    assert client_cls.call_args.kwargs["proxy"] == proxy
    install_headers.assert_called_once_with(
        "tokenrhythm",
        "https://tokenrhythm.studio/api/models",
        proxy=proxy,
    )
    assert "X-OpenSquilla-Install-Id" not in client.get.await_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_typed_declared_fetch_redacts_schema_drift_secret_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_id = "synthetic-install-id-frame-boundary"
    api_key = "sk-tr-synthetic-schema-secret"
    events: list[tuple[str, dict[str, Any]]] = []
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "qwen3.8-max",
                install_id: "install-id echo as key",
                api_key: "API key echo as key",
            }
        ]
    }
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_catalog.log.debug",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_catalog.redact_tokenrhythm_install_ids",
        lambda text: text.replace(install_id, "***"),
    )

    with (
        patch(
            "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
            return_value=client,
        ),
        patch(
            "opensquilla.provider.tokenrhythm_catalog.tokenrhythm_install_id_headers",
            return_value={"X-OpenSquilla-Install-Id": install_id},
        ),
    ):
        declared = await fetch_tokenrhythm_declared(api_key=api_key)

    assert set(declared) == {"qwen3.8-max"}
    assert events
    assert install_id not in repr(events)
    assert api_key not in repr(events)
    assert "***" in repr(events)


@pytest.mark.asyncio
async def test_typed_fetch_distinguishes_empty_from_malformed_envelope() -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.side_effect = [{"data": []}, {"data": "not-a-list"}]
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with patch(
        "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
        return_value=client,
    ):
        assert await fetch_tokenrhythm_published() == {}
        with pytest.raises(ValueError, match="invalid TokenRhythm declared"):
            await fetch_tokenrhythm_declared(
                api_key="sk-tr-synthetic-not-a-real-secret"
            )


@pytest.mark.parametrize(
    "malformed_data",
    [[{}], ["qwen3.8-max"], [{"id": "  "}]],
)
@pytest.mark.asyncio
async def test_nonempty_malformed_catalog_rows_are_not_legal_empty_lists(
    malformed_data: list[object],
) -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": malformed_data}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with patch(
        "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
        return_value=client,
    ):
        with pytest.raises(ValueError, match="invalid TokenRhythm published"):
            await fetch_tokenrhythm_published()


@pytest.mark.parametrize("invalid_success_code", [False, Decimal("0.0"), 0.0])
@pytest.mark.asyncio
async def test_non_exact_success_code_cannot_clear_catalog_lkg(
    invalid_success_code: object,
) -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"code": invalid_success_code, "data": []}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with patch(
        "opensquilla.provider.tokenrhythm_catalog.httpx.AsyncClient",
        return_value=client,
    ):
        with pytest.raises(ValueError, match="unsuccessful TokenRhythm published"):
            await fetch_tokenrhythm_published()


@pytest.mark.asyncio
async def test_tokenrhythm_list_models_merges_public_and_auth_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "sk-tr-synthetic-not-a-real-secret"
    schema_events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_catalog.log.debug",
        lambda event, **fields: schema_events.append((event, fields)),
    )
    catalog = ModelCatalog()
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )
    set_shared_catalog(catalog)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "qwen3.8-max",
                "max_completion_tokens": 131_072,
                "top_provider": {"max_completion_tokens": 8_192},
                "capabilities": {"tools": False, "vision": False},
                api_key: "API key echoed as an unknown field name",
            }
        ]
    }
    try:
        with patch("opensquilla.provider.openai.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=response)
            client_cls.return_value = client
            provider = OpenAIProvider(
                api_key=api_key,
                model="qwen3.8-max",
                base_url="https://tokenrhythm.studio/v1",
                provider_kind="tokenrhythm",
            )

            models = await provider.list_models(raise_on_error=True)
    finally:
        set_shared_catalog(None)

    assert len(models) == 1
    assert models[0].max_output_tokens == 131_072
    assert models[0].supports_tools is False
    assert models[0].supports_vision is False
    assert models[0].metadata is not None
    assert models[0].metadata["declared"]["capabilities"]["tools"] is False
    assert models[0].metadata["published"]["status"] == "testing"
    assert models[0].metadata["declared"]["maxOutputTokens"] == 131_072
    assert schema_events
    assert api_key not in repr(schema_events)
    assert "***" in repr(schema_events)
    response.json.assert_called_once_with(parse_float=Decimal)
    stored = catalog.get_provider_model_metadata("qwen3.8-max", "tokenrhythm")
    assert isinstance(stored, TokenRhythmModelMetadata)
    assert stored.declared is None


@pytest.mark.asyncio
async def test_tokenrhythm_custom_endpoint_does_not_merge_or_mutate_official_metadata() -> None:
    catalog = ModelCatalog()
    published = parse_tokenrhythm_published({"data": [_published_row()]})
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published)
    )
    set_shared_catalog(catalog)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "qwen3.8-max",
                "max_completion_tokens": 65_536,
                "capabilities": {"tools": False},
            }
        ]
    }
    try:
        with patch("opensquilla.provider.openai.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=response)
            client_cls.return_value = client
            provider = OpenAIProvider(
                api_key="sk-tr-synthetic-not-a-real-secret",
                model="qwen3.8-max",
                base_url="https://mirror.example/v1",
                provider_kind="tokenrhythm",
            )

            models = await provider.list_models(raise_on_error=True)
    finally:
        set_shared_catalog(None)

    assert len(models) == 1
    assert models[0].max_output_tokens == 65_536
    assert models[0].context_window == 0
    assert models[0].metadata is not None
    assert models[0].metadata["published"] is None
    assert models[0].metadata["declared"]["maxOutputTokens"] == 65_536
    stored = catalog.get_provider_model_metadata("qwen3.8-max", "tokenrhythm")
    assert isinstance(stored, TokenRhythmModelMetadata)
    assert stored.declared is None


@pytest.mark.asyncio
async def test_direct_listing_missing_fields_never_reads_another_authority() -> None:
    catalog = ModelCatalog()
    authority_a = tokenrhythm_authority_identity(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-direct-key-a",
    )
    assert authority_a is not None
    catalog.set_tokenrhythm_snapshot_sidecars(
        published={},
        declared_by_authority={
            authority_a: parse_tokenrhythm_declared(
                {
                    "data": [
                        {
                            "id": "authority-only-model",
                            "context_length": 900_000,
                            "max_completion_tokens": 77_777,
                            "supports_tools": False,
                            "supports_vision": True,
                        }
                    ]
                }
            )
        },
    )
    set_shared_catalog(catalog)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"id": "authority-only-model"}]}
    try:
        with patch("opensquilla.provider.openai.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=response)
            client_cls.return_value = client
            provider = OpenAIProvider(
                api_key="synthetic-direct-key-b",
                model="authority-only-model",
                base_url="https://tokenrhythm.studio/v1",
                provider_kind="tokenrhythm",
            )

            models = await provider.list_models(raise_on_error=True)
    finally:
        set_shared_catalog(None)

    assert len(models) == 1
    assert models[0].context_window == 200_000
    assert models[0].max_output_tokens == 16_384
    assert models[0].supports_tools is True
    assert models[0].supports_vision is False


@pytest.mark.asyncio
async def test_non_tokenrhythm_listing_keeps_nested_max_output_precedence() -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "synthetic-openrouter-model",
                "max_completion_tokens": 131_072,
                "top_provider": {"max_completion_tokens": 8_192},
            }
        ]
    }
    with patch("opensquilla.provider.openai.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=response)
        client_cls.return_value = client
        provider = OpenAIProvider(
            api_key="synthetic-key",
            model="synthetic-openrouter-model",
            base_url="https://openrouter.ai/api/v1",
            provider_kind="openrouter",
        )

        models = await provider.list_models(raise_on_error=True)

    assert len(models) == 1
    assert models[0].max_output_tokens == 8_192
    response.json.assert_called_once_with()
