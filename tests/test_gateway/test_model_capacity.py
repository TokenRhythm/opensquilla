"""Offline capacity contract, discovery and runtime-catalog regression coverage."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opensquilla.gateway.boot import apply_model_catalog_overrides
from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.probe import discover_selectable_provider_models
from opensquilla.provider.model_capacity import (
    custom_capacity_identity,
    custom_listing_capacity,
    install_custom_capacity,
    resolve_model_capacities,
)
from opensquilla.provider.model_catalog import ModelCatalog

MODEL = "example.vendor/unknown.v1:latest"


@pytest.fixture
def configured(monkeypatch):
    config = GatewayConfig.model_validate(
        {
            "llm": {
                "provider": "custom",
                "model": MODEL,
                "base_url": "https://capacity.invalid/v1",
                "api_key": "synthetic-test-key",
            }
        }
    )
    catalog = ModelCatalog()
    apply_model_catalog_overrides(catalog, config)
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    return config, catalog


def resolve(config, catalog, provider="custom", model=MODEL):
    return resolve_model_capacities(catalog, config, [{"provider": provider, "model": model}])[
        "models"
    ][0]


def test_unknown_keeps_local_default_and_separates_cloud_default(configured):
    config, catalog = configured
    row = resolve(config, catalog)
    assert row["contextWindow"] == {
        "automatic": 8192,
        "automaticSource": "default",
        "override": None,
        "value": 8192,
        "source": "default",
        "editable": True,
    }
    assert resolve(config, catalog, "openai")["contextWindow"]["value"] == 200000
    assert resolve(config, catalog, "custom_anthropic")["contextWindow"]["value"] == 8192
    assert not row["localRuntime"]
    assert resolve(config, catalog, "ollama")["localRuntime"]
    assert not resolve(config, catalog, "openai_codex")["maxOutputTokens"]["editable"]


def test_override_global_and_automatic_sources_preserve_catalog(configured):
    config, catalog = configured
    config.llm.context_window_tokens = 65536
    values = config.model_dump()
    values["models"] = {"custom": {MODEL: {"context_window": 131072, "max_output_tokens": 32000}}}
    # Parse the same structure accepted by config.patch, including punctuation in the id.
    config = GatewayConfig.model_validate(values)
    apply_model_catalog_overrides(catalog, config)
    before = dict(catalog._user_overrides)
    row = resolve(config, catalog)
    assert row["contextWindow"] == {
        "automatic": 65536,
        "automaticSource": "config",
        "override": 131072,
        "value": 131072,
        "source": "override",
        "editable": True,
    }
    assert row["maxOutputTokens"]["value"] == 32000
    assert catalog._user_overrides == before
    assert resolve(config, catalog, "custom_anthropic")["contextWindow"]["source"] == "default"


def test_capacity_is_not_the_request_output_reservation(configured):
    config, catalog = configured
    catalog.set_live_provider_entries(
        "custom",
        {
            MODEL: {
                "context_window": 128000,
                "max_output_tokens": 128000,
            }
        },
    )
    assert resolve(config, catalog)["maxOutputTokens"]["value"] == 128000
    assert catalog.resolve_max_tokens(MODEL, provider="custom") < 128000


def test_restore_automatic_retains_existing_unqualified_override(configured):
    config, catalog = configured
    catalog.set_user_overrides(
        {
            MODEL: {"context_window": 65536},
            f"custom/{MODEL}": {"context_window": 131072, "supports_tools": True},
        }
    )
    context = resolve(config, catalog)["contextWindow"]
    assert context["override"] == 131072
    assert context["automatic"] == 65536
    assert context["automaticSource"] == "override"
    assert context["value"] == 131072
    assert catalog._user_override_fields(MODEL, "custom")["supports_tools"] is True


def test_explicit_metadata_only_and_conflicting_declarations_take_minimum():
    assert custom_listing_capacity({"context_window": True, "max_output_tokens": "32000"}) == {}
    assert custom_listing_capacity({"contextWindow": 0, "max_output_tokens": -1}) == {}
    assert custom_listing_capacity(
        {
            "context_length": 262144,
            "contextWindow": 131072,
            "max_output_tokens": 65536,
            "top_provider": {"max_completion_tokens": 32000},
        }
    ) == {"context_window": 131072, "max_output_tokens": 32000}


def test_endpoint_change_invalidates_and_rejects_stale_listing(configured):
    config, catalog = configured
    identity = custom_capacity_identity(config, "custom")
    rows = [{"id": MODEL, "metadata": {"capacity": {"context_window": 131072}}}]
    install_custom_capacity(catalog, identity, "custom", rows)
    assert resolve(config, catalog)["contextWindow"]["source"] == "catalog"
    config.llm.base_url = "https://different.invalid/v1"
    apply_model_catalog_overrides(catalog, config)
    install_custom_capacity(catalog, identity, "custom", rows)
    assert resolve(config, catalog)["contextWindow"]["source"] == "default"


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
@pytest.mark.parametrize("persist", [True, False])
async def test_synthetic_discovery_metadata_enters_only_saved_runtime(
    configured, monkeypatch, provider, persist
):
    config, catalog = configured
    config.llm.provider = provider
    apply_model_catalog_overrides(catalog, config)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": MODEL, "context_length": 262144, "max_output_tokens": 65536},
                    {"id": "another.unknown/model:v2"},
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    result = await discover_selectable_provider_models(
        provider_id=provider,
        api_key="synthetic-test-key",
        base_url=config.llm.base_url,
        persist_catalog=persist,
        catalog_config=config,
    )
    assert result.ok and len(requests) == 1
    assert requests[0].url.path == "/v1/models"
    assert result.models[0]["contextWindow"] == 262144
    row = resolve(config, catalog, provider)
    assert row["contextWindow"]["value"] == (262144 if persist else 8192)
    assert (
        resolve(config, catalog, provider, "another.unknown/model:v2")["contextWindow"]["source"]
        == "default"
    )


async def test_models_list_and_readonly_capacity_share_runtime_metadata(configured):
    from opensquilla.gateway.adapters.provider_configuration import GatewayModelCatalogPort

    config, catalog = configured

    async def listing():
        return SimpleNamespace(
            models=[
                {
                    "provider": "custom",
                    "model_id": MODEL,
                    "context_window": 131072,
                    "max_output_tokens": 32000,
                    "metadata": {
                        "capacity": {"context_window": 131072, "max_output_tokens": 32000}
                    },
                }
            ],
            errors=[],
        )

    selector = SimpleNamespace(is_configured=True, list_models_detailed=listing)
    listed = await GatewayModelCatalogPort(selector, config).load_model_catalog()
    assert listed["models"][0]["contextWindow"] == 131072
    assert resolve(config, catalog)["contextWindow"]["value"] == 131072
    assert catalog.resolve_context_window(MODEL, "custom") == 131072
    assert catalog.resolve_max_tokens(MODEL, provider="custom") == 32000


async def test_capacity_rpc_is_batched_offline_and_rejects_invalid_targets(configured):
    from opensquilla.gateway.rpc_models import _PLATFORM_CONFIGURATION_CONTRACT_HANDLERS

    config, _ = configured
    handler = _PLATFORM_CONFIGURATION_CONTRACT_HANDLERS["models.capacity.resolve"]
    ctx = SimpleNamespace(config=config)
    result = await handler(
        {
            "models": [
                {"provider": "custom", "model": MODEL},
                {"provider": "custom", "model": MODEL},
                {"provider": "custom_anthropic", "model": MODEL},
            ]
        },
        ctx,
    )
    assert len(result["models"]) == 2
    for invalid in ("", "   "):
        with pytest.raises(Exception):
            await handler({"models": [{"provider": "custom", "model": invalid}]}, ctx)


@pytest.mark.parametrize("metadata", [True, False])
def test_issue_1561_large_synthetic_request_after_capacity_correction(configured, metadata):
    from opensquilla.context_budget import ContextBudgetGovernor
    from opensquilla.provider.request_proof import (
        ProviderRequestBudgetExceededError,
        prove_provider_payload,
    )

    config, catalog = configured
    payload = {"model": MODEL, "messages": [{"role": "system", "content": "synthetic " * 15000}]}

    def prove():
        budget = ContextBudgetGovernor.from_values(
            context_window_tokens=catalog.resolve_context_window(MODEL, "custom"),
            max_output_tokens=catalog.resolve_max_tokens(MODEL, provider="custom"),
            thinking_budget_tokens=0,
            context_overflow_threshold=0.85,
        ).snapshot()
        return prove_provider_payload(
            payload,
            projection_adapter="openai",
            proof_budget=budget.provider_request_max_chars,
        )

    with pytest.raises(ProviderRequestBudgetExceededError):
        prove()
    if metadata:
        install_custom_capacity(
            catalog,
            custom_capacity_identity(config, "custom"),
            "custom",
            [
                {
                    "id": MODEL,
                    "metadata": {
                        "capacity": {
                            "context_window": 262144,
                            "max_output_tokens": 65536,
                        }
                    },
                }
            ],
        )
    else:
        data = config.model_dump()
        data["models"] = {
            "custom": {
                MODEL: {
                    "context_window": 262144,
                    "max_output_tokens": 65536,
                }
            }
        }
        apply_model_catalog_overrides(catalog, GatewayConfig.model_validate(data))
    assert prove()["fits"]


def test_fixed_router_and_ensemble_roles_use_provider_scoped_capacity(configured):
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter
    from opensquilla.provider import ChatConfig
    from opensquilla.provider.ensemble import (
        EnsembleMemberConfig,
        _member_budget_key,
        _member_chat_config,
        _runtime_member_request_budget_bindings,
    )
    from opensquilla.provider.selector import ProviderConfig

    config, catalog = configured
    config.llm.context_window_tokens = 131072
    adapter = _TurnRunnerModelCatalogAdapter(
        SimpleNamespace(_model_catalog=catalog, _config=config)
    )
    assert adapter.lookup(MODEL, "custom").context_window == 131072
    assert adapter.lookup(MODEL, "custom_anthropic").context_window == 8192
    catalog.set_user_overrides(
        {
            f"custom/{MODEL}": {"context_window": 262144, "max_output_tokens": 65536},
            f"custom_anthropic/{MODEL}": {"context_window": 65536, "max_output_tokens": 8192},
        }
    )
    adapter = _TurnRunnerModelCatalogAdapter(
        SimpleNamespace(_model_catalog=catalog, _config=config)
    )
    assert adapter.lookup(MODEL, "custom").context_window == 262144
    assert adapter.lookup(MODEL, "custom_anthropic").context_window == 65536
    members = [
        EnsembleMemberConfig(
            provider_config=ProviderConfig(provider=provider, model=MODEL),
            max_tokens=limit,
        )
        for provider, limit in [("custom", 4000), ("custom_anthropic", 16000)]
    ]
    bindings = _runtime_member_request_budget_bindings(
        config=config,
        members=members,
        model_catalog=catalog,
        context_overflow_threshold=0.85,
    )
    # The same binding functions serve candidates, aggregator and fixed takeover.
    for role in ["proposer", "aggregator", "fallback"]:
        configs = [
            _member_chat_config(
                ChatConfig(thinking=False, provider_request_max_chars=999999),
                member,
                request_budget_binding=bindings[_member_budget_key(member)],
                role=role,
            )
            for member in members
        ]
        assert [item.max_tokens for item in configs] == [4000, 8192]
        assert configs[0].provider_request_max_chars > configs[1].provider_request_max_chars > 0


@pytest.mark.parametrize("reason", [
    "provider_request_budget_exhausted",
    "provider_system_prompt_too_large",
    "provider_tool_schema_too_large",
    "provider_protected_context_too_large",
])
def test_default_capacity_error_has_source_and_exact_model_target(configured, reason):
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.session.terminal_reply import (
        CONTEXT_PAYLOAD_TOO_LARGE_MESSAGES,
        build_terminal_reply,
    )

    _, _catalog = configured
    agent = object.__new__(Agent)
    agent.config = AgentConfig(provider_id="custom", model_id=MODEL, context_window_tokens=8192)
    agent._last_compaction_refusal_reason = reason
    event = agent._context_overflow_error()
    assert event.model_capacity == {
        "provider": "custom",
        "model": MODEL,
        "contextWindow": 8192,
        "source": "default",
    }
    text = build_terminal_reply({
        "error_class": event.code,
        "error_message": event.message,
        "model_capacity": event.model_capacity,
    })
    assert "system default of 8,192 tokens" in text
    assert "Model settings" in text
    if reason in CONTEXT_PAYLOAD_TOO_LARGE_MESSAGES:
        assert text.startswith(CONTEXT_PAYLOAD_TOO_LARGE_MESSAGES[reason])


def test_conflicting_listing_rows_take_smaller_limits(configured):
    config, catalog = configured
    install_custom_capacity(
        catalog,
        custom_capacity_identity(config, "custom"),
        "custom",
        [
            {"id": MODEL, "metadata": {"capacity": {"context_window": value}}}
            for value in (65536, 262144)
        ],
    )
    assert resolve(config, catalog)["contextWindow"]["value"] == 65536


async def test_capacity_patch_roundtrip_and_restore_preserve_other_model_fields(
    tmp_path, configured
):
    import tomllib

    import tomli_w

    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_config import _handle_config_patch

    path = tmp_path / "capacity.toml"
    path.write_text(
        tomli_w.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "model": MODEL,
                    "base_url": "https://capacity.invalid/v1",
                },
                "models": {"custom": {MODEL: {"supports_vision": False, "context_window": 8192}}},
            }
        )
    )
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="capacity-test", config=config)
    await _handle_config_patch(
        {
            "patch": {
                "models": {
                    "custom": {
                        MODEL: {
                            "context_window": 262144,
                            "max_output_tokens": 65536,
                        }
                    }
                }
            }
        },
        ctx,
    )
    data = tomllib.loads(path.read_text())
    assert data["models"]["custom"][MODEL] == {
        "supports_vision": False,
        "context_window": 262144,
        "max_output_tokens": 65536,
    }
    await _handle_config_patch(
        {
            "patch": {
                "models": {
                    "custom": {
                        MODEL: {
                            "context_window": None,
                            "max_output_tokens": None,
                        }
                    }
                }
            }
        },
        ctx,
    )
    data = tomllib.loads(path.read_text())
    assert data["models"]["custom"][MODEL] == {"supports_vision": False}
    assert config.llm.provider == "custom" and config.llm.model == MODEL


@pytest.mark.parametrize("auth_style", ["bearer", "x-api-key"])
async def test_custom_anthropic_listing_auth_and_malformed_response(monkeypatch, auth_style):
    from opensquilla.provider.anthropic import AnthropicProvider
    from opensquilla.provider.protocol import ProviderModelListingResponseError

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"unexpected": "synthetic-private-response"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    provider = AnthropicProvider(
        "synthetic-api-key",
        provider_id="custom_anthropic",
        base_url="https://capacity.invalid/v1",
        auth_header_style=auth_style,
    )
    with pytest.raises(ProviderModelListingResponseError) as error:
        await provider.list_models(raise_on_error=True)
    assert "synthetic-private-response" not in str(error.value)
    assert requests[0].url.path == "/v1/models"
    expected = "Bearer synthetic-api-key" if auth_style == "bearer" else "synthetic-api-key"
    assert (
        requests[0].headers["Authorization" if auth_style == "bearer" else "x-api-key"] == expected
    )


@pytest.mark.parametrize(
    "failure", ["missing_endpoint", "unauthorized", "unreachable", "malformed"],
)
async def test_custom_anthropic_listing_fallback_keeps_identity_without_declared_limits(
    monkeypatch, failure,
):
    from opensquilla.provider.anthropic import AnthropicProvider
    from opensquilla.provider.protocol import ProviderModelListingResponseError

    def respond(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        if failure == "unreachable":
            raise httpx.ConnectError("synthetic connection failure", request=request)
        status = {"missing_endpoint": 404, "unauthorized": 401, "malformed": 200}[failure]
        return httpx.Response(status, json={"unexpected": "synthetic response"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    provider = AnthropicProvider(
        "", model=MODEL, provider_id="custom_anthropic", base_url="https://capacity.invalid/v1",
    )
    rows = await provider.list_models()
    assert [(row.provider, row.model_id) for row in rows] == [("custom_anthropic", MODEL)]
    assert rows[0].context_window == rows[0].max_output_tokens == 0
    assert rows[0].metadata is None
    with pytest.raises((httpx.HTTPError, ProviderModelListingResponseError)):
        await provider.list_models(raise_on_error=True)
