"""Opt-in live acceptance for TokenRhythm catalog and output limits.

This module never embeds or persists credentials.  It runs only when the
operator explicitly opts in with a freshly rotated key in the environment;
default CI deselects the live LLM markers.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from opensquilla.env import trust_env
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_catalog_refresh import (
    TokenRhythmCatalogCoordinator,
    discover_tokenrhythm_models,
    install_tokenrhythm_catalog_coordinator,
)
from opensquilla.provider.app_attribution import provider_app_headers
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.tokenrhythm_catalog import (
    TOKENRHYTHM_API_BASE_URL,
    TOKENRHYTHM_PUBLIC_CATALOG_URL,
    merge_tokenrhythm_catalog,
    parse_tokenrhythm_declared,
    parse_tokenrhythm_published,
)
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message
from opensquilla.secrets import clean_header_secret
from scripts.live_harness_security import report_contains_secret

pytestmark = [pytest.mark.llm, pytest.mark.llm_costly, pytest.mark.llm_gateway]

_RESPONSE_STATE_FIELDS = (
    "stream",
    "tools",
    "background",
    "compact",
    "webSearch",
    "mcp",
    "codeInterpreter",
    "imageGeneration",
    "fileSearch",
    "cancel",
)
_PRICING_SOURCE_FIELDS = frozenset(
    {
        "currency",
        "billingMode",
        "billingUnit",
        "hasDiscount",
        "pricePerImage",
        "inputPrice",
        "outputPrice",
        "cacheReadPrice",
        "discountInputPrice",
        "discountOutputPrice",
        "discountCacheReadPrice",
        "effectiveInputPrice",
        "effectiveOutputPrice",
        "effectiveCacheReadPrice",
    }
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_bool(*values: object) -> bool | None:
    for value in values:
        parsed = _bool(value)
        if parsed is not None:
            return parsed
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer() or not math.isfinite(value):
            return None
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    return value if isinstance(value, int) and value > 0 else None


def _decimal_string(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or (parsed and parsed.adjusted() > 308):
        return None
    return raw


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _text(item)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _expected_capabilities(row: Mapping[str, Any]) -> dict[str, bool | None]:
    raw_capabilities = row.get("capabilities")
    capabilities = _mapping(raw_capabilities)
    capability_names = (
        {str(item) for item in raw_capabilities}
        if isinstance(raw_capabilities, list)
        else set()
    )
    protocols = _mapping(row.get("protocolCapabilities"))
    anthropic = _mapping(protocols.get("anthropic"))
    responses = _mapping(protocols.get("responses"))
    raw_supported = row.get("supported_parameters")
    supported_names = (
        {str(item) for item in raw_supported}
        if isinstance(raw_supported, list)
        else set()
    )

    tools = _first_bool(
        capabilities.get("tools"),
        row.get("supportsTools"),
        row.get("supports_tools"),
    )
    if tools is None and {"tools", "tool_choice"}.intersection(supported_names):
        tools = True
    if tools is None and "tools" in capability_names:
        tools = True

    reasoning = _first_bool(
        capabilities.get("reasoning"),
        row.get("supportsReasoning"),
        row.get("supports_reasoning"),
    )
    if reasoning is None and {"reasoning", "reasoning_effort"}.intersection(
        supported_names
    ):
        reasoning = True
    if reasoning is None and "reasoning" in capability_names:
        reasoning = True

    vision = _first_bool(
        capabilities.get("vision"),
        row.get("supportsVision"),
        row.get("supports_vision"),
    )
    if vision is None and "vision" in capability_names:
        vision = True

    return {
        "tools": tools,
        "reasoning": reasoning,
        "vision": vision,
        "anthropic": _first_bool(
            capabilities.get("anthropic"),
            anthropic.get("available"),
            row.get("supportsAnthropic"),
            row.get("supports_anthropic"),
            True if "anthropic" in capability_names else None,
        ),
        "responses": _first_bool(
            capabilities.get("responses"),
            responses.get("available"),
            row.get("supportsResponses"),
            row.get("supports_responses"),
            True if "responses" in capability_names else None,
        ),
        "streaming": _first_bool(
            capabilities.get("stream"),
            capabilities.get("streaming"),
            row.get("supportsStream"),
            row.get("supports_streaming"),
            (
                True
                if {"stream", "streaming"}.intersection(capability_names)
                else None
            ),
        ),
    }


def _expected_responses(row: Mapping[str, Any]) -> dict[str, Any] | None:
    protocols = _mapping(row.get("protocolCapabilities"))
    protocol_responses = protocols.get("responses")
    direct_responses = row.get("responses")
    if isinstance(protocol_responses, Mapping):
        response_row = protocol_responses
        response_present = True
    elif isinstance(direct_responses, Mapping):
        response_row = direct_responses
        response_present = True
    else:
        response_row = {}
        response_present = False

    top_modes = _string_list(row.get("responses_modes"))
    top_capabilities = _string_list(row.get("responses_capabilities"))
    if not response_present and top_modes is None and top_capabilities is None:
        return None

    response_modes = (
        _string_list(response_row.get("modes")) if "modes" in response_row else None
    )
    modes = response_modes if response_modes is not None else (top_modes or [])
    response_capabilities = (
        _string_list(response_row.get("capabilities"))
        if "capabilities" in response_row
        else None
    )
    enabled = (
        response_capabilities
        if response_capabilities is not None
        else (top_capabilities or [])
    )
    enabled_set = set(enabled)
    states = {
        name: (
            _bool(response_row.get(name))
            if name in response_row
            else (True if name in enabled_set else None)
        )
        for name in _RESPONSE_STATE_FIELDS
    }
    capabilities = [name for name in _RESPONSE_STATE_FIELDS if states[name] is True]
    for name in enabled:
        if name not in _RESPONSE_STATE_FIELDS and name not in capabilities:
            capabilities.append(name)
    return {
        "modes": modes,
        "capabilities": capabilities,
        "capabilityStates": states,
    }


def _expected_pricing(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _PRICING_SOURCE_FIELDS.intersection(row):
        return None
    return {
        "currency": _text(row.get("currency")),
        "billingMode": _text(row.get("billingMode")),
        "billingUnit": _positive_int(row.get("billingUnit")),
        "hasDiscount": _bool(row.get("hasDiscount")),
        "pricePerImage": _decimal_string(row.get("pricePerImage")),
        "standard": {
            "input": _decimal_string(row.get("inputPrice")),
            "output": _decimal_string(row.get("outputPrice")),
            "cacheRead": _decimal_string(row.get("cacheReadPrice")),
        },
        "discount": {
            "input": _decimal_string(row.get("discountInputPrice")),
            "output": _decimal_string(row.get("discountOutputPrice")),
            "cacheRead": _decimal_string(row.get("discountCacheReadPrice")),
        },
        "effective": {
            "input": _decimal_string(row.get("effectiveInputPrice")),
            "output": _decimal_string(row.get("effectiveOutputPrice")),
            "cacheRead": _decimal_string(row.get("effectiveCacheReadPrice")),
        },
    }


def _expected_published_wire(
    model_id: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "name": _text(row.get("name")) or model_id,
        "providerDisplayName": _text(row.get("providerDisplayName")),
        "modelType": _text(row.get("type")),
        "status": _text(row.get("status")),
        "modalities": _string_list(row.get("modalities")),
        "contextWindow": _positive_int(row.get("contextWindow")),
        "maxOutputTokens": _positive_int(row.get("maxOutputTokens")),
        "reasoningMode": _text(row.get("reasoningMode")),
        "reasoningDefault": _text(row.get("reasoningDefault")),
        "reasoningSupportedEfforts": _string_list(
            row.get("reasoningSupportedEfforts")
        ),
        "reasoningSupportsMaxTokens": _bool(
            row.get("reasoningSupportsMaxTokens")
        ),
        "capabilities": _expected_capabilities(row),
        "responses": _expected_responses(row),
        "pricing": _expected_pricing(row),
    }


def _expected_declared_wire(
    model_id: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    context_window = None
    for key in ("context_length", "context_window", "contextWindow"):
        if key in row:
            context_window = _positive_int(row.get(key))
            if context_window is not None:
                break
    max_output_tokens = (
        _positive_int(row.get("max_completion_tokens"))
        if "max_completion_tokens" in row
        else None
    )
    if max_output_tokens is None:
        max_output_tokens = _positive_int(
            _mapping(row.get("top_provider")).get("max_completion_tokens")
        )
    return {
        "displayName": _text(row.get("name")) or model_id,
        "modelType": _text(row.get("type")),
        "status": _text(row.get("status")),
        "contextWindow": context_window,
        "maxOutputTokens": max_output_tokens,
        "capabilities": _expected_capabilities(row),
        "responses": _expected_responses(row),
        "pricing": _expected_pricing(row),
    }


def _validate_live_payload(
    payload: object, *, source: str
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        pytest.fail(f"TokenRhythm {source} returned a non-object envelope", pytrace=False)
    data = payload.get("data")
    if not isinstance(data, list) or not all(
        isinstance(row, Mapping) and _text(row.get("id")) is not None
        for row in data
    ):
        pytest.fail(f"TokenRhythm {source} returned an invalid model list", pytrace=False)
    raw_code = payload.get("code")
    successful = "code" not in payload or raw_code == "0" or (
        isinstance(raw_code, int) and not isinstance(raw_code, bool) and raw_code == 0
    )
    if not successful:
        pytest.fail(f"TokenRhythm {source} returned a failed envelope", pytrace=False)
    return payload


async def _fetch_raw_json(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: Mapping[str, str],
    source: str,
    timeout: float,
) -> Mapping[str, Any]:
    try:
        response = await client.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        pytest.fail(f"TokenRhythm {source} request failed", pytrace=False)
    if response.is_error:
        pytest.fail(
            f"TokenRhythm {source} returned HTTP {response.status_code}",
            pytrace=False,
        )
    try:
        payload = response.json(parse_float=Decimal)
    except (TypeError, ValueError):
        pytest.fail(f"TokenRhythm {source} returned invalid JSON", pytrace=False)
    return _validate_live_payload(payload, source=source)


async def _fetch_raw_catalog_sources(
    key: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    public_headers = provider_app_headers(TOKENRHYTHM_PUBLIC_CATALOG_URL)
    assert "Authorization" not in public_headers
    declared_headers = {
        "Authorization": (
            f"Bearer {clean_header_secret(key, label='TokenRhythm API key')}"
        ),
        **provider_app_headers(TOKENRHYTHM_API_BASE_URL),
    }
    async with httpx.AsyncClient(trust_env=trust_env()) as client:
        async with asyncio.timeout(10.0):
            return await asyncio.gather(
                _fetch_raw_json(
                    client,
                    url=TOKENRHYTHM_PUBLIC_CATALOG_URL,
                    headers=public_headers,
                    source="published catalog",
                    timeout=5.0,
                ),
                _fetch_raw_json(
                    client,
                    url=f"{TOKENRHYTHM_API_BASE_URL}/models",
                    headers=declared_headers,
                    source="declared catalog",
                    timeout=10.0,
                ),
            )


def _raw_model_rows(
    payload: Mapping[str, Any], *, source: str
) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in payload["data"]:
        assert isinstance(raw_row, Mapping)
        model_id = _text(raw_row.get("id"))
        assert model_id is not None
        assert model_id not in rows, f"duplicate model id in {source}: {model_id}"
        rows[model_id] = raw_row
    return rows


def _rotated_live_key() -> str:
    if os.environ.get("OPENSQUILLA_LIVE_TOKENRHYTHM") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_TOKENRHYTHM=1 for TokenRhythm live checks")
    key = os.environ.get("TOKENRHYTHM_API_KEY", "").strip()
    if not key:
        pytest.skip("set TOKENRHYTHM_API_KEY to a freshly rotated key")
    return key


@pytest.mark.asyncio
async def test_live_catalog_sources_align_for_qwen_max(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _rotated_live_key()
    with capture_logs() as upstream_logs:
        raw_published, raw_declared = await _fetch_raw_catalog_sources(key)
        published = parse_tokenrhythm_published(raw_published)
        declared = parse_tokenrhythm_declared(raw_declared)

    raw_published_rows = _raw_model_rows(raw_published, source="published catalog")
    raw_declared_rows = _raw_model_rows(raw_declared, source="declared catalog")
    assert set(published) == set(raw_published_rows)
    assert set(declared) == set(raw_declared_rows)
    for model_id, raw_row in raw_published_rows.items():
        assert published[model_id].to_wire() == _expected_published_wire(
            model_id, raw_row
        ), f"published known-field normalization drift for {model_id!r}"
    for model_id, raw_row in raw_declared_rows.items():
        assert declared[model_id].to_wire() == _expected_declared_wire(
            model_id, raw_row
        ), f"declared known-field normalization drift for {model_id!r}"

    # Every price remains a validated decimal string. This catches accidental
    # float conversion independently of the per-row equality checks above.
    for model in (*published.values(), *declared.values()):
        pricing = model.to_wire()["pricing"]
        if pricing is None:
            continue
        for bucket in ("standard", "discount", "effective"):
            assert all(
                value is None or isinstance(value, str)
                for value in pricing[bucket].values()
            )

    schema_drift_detected = any(
        row.get("event") == "tokenrhythm_catalog.schema_drift"
        for row in upstream_logs
    )
    assert schema_drift_detected is False, (
        "TokenRhythm added an unknown catalog field; extend schemaVersion explicitly"
    )

    public_qwen = published.get("qwen3.8-max")
    declared_qwen = declared.get("qwen3.8-max")
    assert public_qwen is not None
    assert declared_qwen is not None
    assert public_qwen.context_window == 1_000_000
    assert public_qwen.max_output_tokens == 131_072
    assert declared_qwen.max_output_tokens == 131_072

    selectable = merge_tokenrhythm_catalog(published, declared)
    published_by_lower = {model_id.lower(): row for model_id, row in published.items()}

    def eligible(model_id: str) -> bool:
        declared_row = declared[model_id]
        if (declared_row.model_type or "chat").lower() != "chat":
            return False
        if (declared_row.status or "online").lower() not in {"online", "testing"}:
            return False
        public = published_by_lower.get(model_id.lower())
        return public is None or (
            (public.model_type or "chat").lower() == "chat"
            and (public.status or "online").lower() in {"online", "testing"}
        )

    expected_selectable = {model_id for model_id in declared if eligible(model_id)}
    assert set(selectable) == expected_selectable
    assert "qwen3.8-max" in selectable
    for model_id, row in selectable.items():
        public = row.metadata.published
        assert row.metadata.declared is declared[model_id]
        if public is not None:
            assert (public.model_type or "chat").lower() == "chat"
            assert (public.status or "online").lower() in {"online", "testing"}
        else:
            assert model_id.lower() not in published_by_lower

    authorized_testing = {
        model_id
        for model_id in declared
        if (public := published_by_lower.get(model_id.lower())) is not None
        and (public.status or "").lower() == "testing"
        and eligible(model_id)
    }
    assert authorized_testing.issubset(selectable)

    # Re-publish the live normalized documents through the real coordinator so
    # state/RPC/log artifacts can be scanned without making a second request or
    # sending the credential through a synthetic proxy.
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    async def use_live_published(**_kwargs):
        return published

    async def use_live_declared(*_args, **_kwargs):
        return declared

    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_published",
        use_live_published,
    )
    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_declared",
        use_live_declared,
    )
    proxy_sentinel = "http://synthetic-proxy.invalid:65535"
    config = GatewayConfig(
        state_dir=str(tmp_path),
        llm={
            "provider": "tokenrhythm",
            "model": "qwen3.8-max",
            "api_key": key,
            "base_url": TOKENRHYTHM_API_BASE_URL,
            "proxy": proxy_sentinel,
        },
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    install_tokenrhythm_catalog_coordinator(coordinator)
    try:
        with capture_logs() as coordinator_logs:
            result = await discover_tokenrhythm_models(
                provider_id="tokenrhythm",
                api_key=key,
                base_url=TOKENRHYTHM_API_BASE_URL,
                proxy=proxy_sentinel,
                force=True,
                persist_entitlement=True,
                config=config,
            )
        rpc_payload = result.to_payload()
        state_text = (
            tmp_path / "model_catalog" / "tokenrhythm-v1.json"
        ).read_text(encoding="utf-8")
    finally:
        await coordinator.close()
        install_tokenrhythm_catalog_coordinator(None)

    artifact_report = {
        "rpc": rpc_payload,
        "logs": [*upstream_logs, *coordinator_logs],
        "state": state_text,
    }
    contains_secret = report_contains_secret(
        artifact_report,
        [key, proxy_sentinel],
    )
    serialized_artifacts = repr(artifact_report)
    assert contains_secret is False, "credential or proxy leaked into catalog artifacts"
    assert "Authorization" not in serialized_artifacts


@pytest.mark.asyncio
async def test_live_qwen_accepts_131072_max_tokens() -> None:
    provider = OpenAIProvider(
        api_key=_rotated_live_key(),
        model="qwen3.8-max",
        base_url=TOKENRHYTHM_API_BASE_URL,
        provider_kind="tokenrhythm",
    )

    async with asyncio.timeout(90.0):
        events = [
            event
            async for event in provider.chat(
                [Message(role="user", content="Reply with exactly: OK")],
                tools=None,
                config=ChatConfig(max_tokens=131_072, timeout=60.0),
            )
        ]

    has_error = any(isinstance(event, ErrorEvent) for event in events)
    has_done = any(isinstance(event, DoneEvent) for event in events)
    assert has_error is False, "TokenRhythm returned an error for max_tokens=131072"
    assert has_done is True, "TokenRhythm stream did not complete"
