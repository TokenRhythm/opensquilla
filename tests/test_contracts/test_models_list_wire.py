"""Wire-contract freeze for ``models.list`` RPC rows.

The Web UI model picker and external control clients index into these rows
by key name, so the row shape is a public protocol contract (see CLAUDE.md:
public RPC field names are stable).

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key requires deliberately extending the frozen sets in this file —
  that friction is the point: wire additions should be a conscious decision.

The row and error shapes are frozen at ``_model_info_to_wire`` /
``_list_error_to_wire``, the pure builders the ``models.list`` handler maps
over selector results; the envelope is frozen by driving the handler with a
fully synthetic in-memory selector stub — zero network either way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_models import (
    _handle_models_list,
    _list_error_to_wire,
    _model_info_to_wire,
)
from opensquilla.provider.selector import (
    ModelListResult,
    ModelSelector,
    ProviderConfig,
    ProviderListError,
    SelectorConfig,
)
from opensquilla.provider.types import ModelInfo

# Additive wire evolution: ``source`` (catalog provenance) and
# ``reasoningFormat`` (reasoning dialect) were added deliberately. Extending
# this frozen set is the conscious decision the friction is meant to force —
# renaming or removing any existing key must still fail here.
MODEL_ROW_KEYS = frozenset(
    {
        "id",
        "name",
        "provider",
        "contextWindow",
        "maxOutputTokens",
        "capabilities",
        "pricing",
        "source",
        "reasoningFormat",
        "metadata",
    }
)
MODEL_PRICING_KEYS = frozenset({"inputPer1k", "outputPer1k"})
# Additive top-level envelope key: ``errors`` carries classified, redacted
# per-provider listing failures alongside ``models``. Each error row is frozen
# to exactly these keys.
MODEL_ERROR_KEYS = frozenset({"provider", "kind", "detail"})


def _synthetic_model(**overrides) -> dict:
    kwargs: dict = {
        "provider": "test-provider",
        "model_id": "test-provider/test-model",
        "display_name": "Test Model",
        "context_window": 32_000,
        "max_output_tokens": 4_096,
        "supports_tools": True,
        "input_cost_per_1k": 0.001,
        "output_cost_per_1k": 0.002,
    }
    kwargs.update(overrides)
    return ModelInfo(**kwargs).model_dump()


def test_model_row_keys_are_frozen() -> None:
    row = _model_info_to_wire(_synthetic_model())
    assert set(row) == MODEL_ROW_KEYS
    assert set(row["pricing"]) == MODEL_PRICING_KEYS


def test_model_row_values_map_from_model_info() -> None:
    # Field-name mapping (snake_case ModelInfo -> camelCase wire) is part of
    # the contract: clients read contextWindow/pricing.inputPer1k literally.
    row = _model_info_to_wire(_synthetic_model())
    assert row["id"] == "test-provider/test-model"
    assert row["name"] == "Test Model"
    assert row["provider"] == "test-provider"
    assert row["contextWindow"] == 32_000
    assert row["maxOutputTokens"] == 4_096
    assert row["pricing"] == {"inputPer1k": 0.001, "outputPer1k": 0.002}
    assert row["metadata"] is None


def test_model_row_carries_normalized_provider_metadata() -> None:
    metadata = {
        "schemaVersion": 1,
        "published": None,
        "declared": {
            "contextWindow": 64_000,
            "maxOutputTokens": 16_384,
            "capabilities": {
                "tools": False,
                "reasoning": True,
                "vision": None,
                "anthropic": None,
                "responses": None,
                "streaming": True,
            },
            "responses": None,
            "pricing": None,
        },
    }

    row = _model_info_to_wire(_synthetic_model(metadata=metadata))

    assert row["metadata"] == metadata


def test_tokenrhythm_wire_prefers_declared_values_and_explicit_false(monkeypatch) -> None:
    class _Catalog:
        def resolve_entry(self, _model_id, *, provider):
            assert provider == "tokenrhythm"
            return SimpleNamespace(
                context_window=32_000,
                max_output_tokens=8_192,
                source="corrections",
                reasoning_format="qwen",
            )

        def get_capabilities(self, _model_id, provider):
            assert provider == "tokenrhythm"
            return SimpleNamespace(
                supports_tools=True,
                supports_reasoning=True,
                supports_vision=True,
            )

    monkeypatch.setattr("opensquilla.gateway.rpc_models._catalog", _Catalog())
    metadata = {
        "schemaVersion": 1,
        "declared": {
            "contextWindow": 1_000_000,
            "maxOutputTokens": 131_072,
            "capabilities": {"tools": False, "reasoning": False, "vision": False},
        },
        "published": {
            "contextWindow": 200_000,
            "maxOutputTokens": 65_536,
            "capabilities": {"tools": True, "reasoning": True, "vision": True},
        },
    }

    row = _model_info_to_wire(
        _synthetic_model(
            provider="tokenrhythm",
            model_id="qwen3.8-max",
            context_window=0,
            max_output_tokens=0,
            metadata=metadata,
        )
    )

    assert row["contextWindow"] == 1_000_000
    assert row["maxOutputTokens"] == 131_072
    assert row["capabilities"] == ["chat"]
    assert row["metadata"] == metadata


def test_tokenrhythm_wire_uses_authority_resolved_model_info_without_rewriting_metadata(
    monkeypatch,
) -> None:
    class _Catalog:
        def resolve_entry(self, _model_id, *, provider):
            assert provider == "tokenrhythm"
            return SimpleNamespace(
                context_window=1_000_000,
                max_output_tokens=131_072,
                source="corrections",
                reasoning_format="none",
            )

    monkeypatch.setattr("opensquilla.gateway.rpc_models._catalog", _Catalog())
    metadata = {
        "schemaVersion": 1,
        "declared": {
            "contextWindow": None,
            "maxOutputTokens": None,
            "capabilities": {"tools": None, "reasoning": True, "vision": None},
        },
        "published": None,
    }

    row = _model_info_to_wire(
        _synthetic_model(
            provider="tokenrhythm",
            model_id="qwen3.8-max",
            context_window=64_000,
            max_output_tokens=8_192,
            supports_tools=False,
            metadata=metadata,
        )
    )

    assert row["contextWindow"] == 64_000
    assert row["maxOutputTokens"] == 8_192
    assert row["capabilities"] == ["chat"]
    assert row["metadata"] == metadata


def test_model_row_carries_catalog_provenance() -> None:
    # A model unknown to every catalog layer still resolves to a synthesized
    # entry, so ``source``/``reasoningFormat`` are always renderable strings.
    row = _model_info_to_wire(_synthetic_model())
    assert isinstance(row["source"], str) and row["source"]
    assert isinstance(row["reasoningFormat"], str) and row["reasoningFormat"]


def test_error_row_keys_are_frozen() -> None:
    err = _list_error_to_wire(
        ProviderListError(
            provider="test-provider",
            model_hint="test-provider/test-model",
            kind="auth_invalid",
            detail="invalid api key",
        )
    )
    assert set(err) == MODEL_ERROR_KEYS
    # ``model_hint`` is selector-internal operator context; it stays off the
    # wire on purpose.
    assert "model_hint" not in err
    assert err == {
        "provider": "test-provider",
        "kind": "auth_invalid",
        "detail": "invalid api key",
    }



def test_model_row_capability_strings_are_frozen() -> None:
    # Capability strings are matched verbatim by the handler's
    # ``capabilities`` filter and by client-side capability badges.
    with_tools = _model_info_to_wire(_synthetic_model())
    assert with_tools["capabilities"] == ["chat", "tools"]

    without_tools = _model_info_to_wire(_synthetic_model(supports_tools=False))
    assert without_tools["capabilities"] == ["chat"]


def test_model_row_name_falls_back_to_the_model_id() -> None:
    # Clients rely on ``name`` always being renderable even when a provider
    # returns no display name.
    row = _model_info_to_wire(_synthetic_model(display_name=""))
    assert row["name"] == "test-provider/test-model"


MODEL_ENVELOPE_KEYS = frozenset({"models", "errors"})


class _StubSelector:
    """Zero-network selector stub returning a fixed ModelListResult."""

    def __init__(self, result: ModelListResult) -> None:
        self._result = result

    async def list_models_detailed(self) -> ModelListResult:
        return self._result


async def test_models_list_envelope_keys_are_frozen() -> None:
    result = ModelListResult(
        models=[_synthetic_model()],
        errors=[
            ProviderListError(
                provider="test-provider",
                model_hint="test-provider/test-model",
                kind="auth_invalid",
                detail="invalid api key",
            )
        ],
    )
    ctx = RpcContext(conn_id="test", provider_selector=_StubSelector(result))
    envelope = await _handle_models_list({}, ctx)

    assert set(envelope) == MODEL_ENVELOPE_KEYS
    assert set(envelope["models"][0]) == MODEL_ROW_KEYS
    assert envelope["errors"] == [
        {"provider": "test-provider", "kind": "auth_invalid", "detail": "invalid api key"}
    ]


async def test_tokenrhythm_models_list_is_snapshot_only(monkeypatch) -> None:
    from opensquilla.gateway import model_catalog_refresh

    metadata = {"schemaVersion": 1, "published": None, "declared": None}

    class _SnapshotOnlySelector:
        is_configured = True
        current_config = SimpleNamespace(provider="tokenrhythm")

        async def list_models_detailed(self):  # pragma: no cover - regression guard
            raise AssertionError("models.list must not perform TokenRhythm provider I/O")

    monkeypatch.setattr(
        model_catalog_refresh,
        "cached_tokenrhythm_models",
        lambda config: [
            ModelInfo(
                provider="tokenrhythm",
                model_id="qwen-test",
                max_output_tokens=131_072,
                metadata=metadata,
            )
        ],
        raising=False,
    )

    envelope = await _handle_models_list(
        {},
        RpcContext(
            conn_id="test",
            config=SimpleNamespace(),
            provider_selector=_SnapshotOnlySelector(),
        ),
    )

    assert set(envelope) == MODEL_ENVELOPE_KEYS
    assert envelope["errors"] == []
    assert envelope["models"][0]["maxOutputTokens"] == 131_072
    assert envelope["models"][0]["metadata"] == metadata


@pytest.mark.parametrize("tokenrhythm_first", [True, False])
async def test_models_list_mixed_chain_uses_tokenrhythm_snapshot_per_leg(
    monkeypatch,
    tmp_path,
    tokenrhythm_first: bool,
) -> None:
    from opensquilla.gateway import model_catalog_refresh
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.tokenrhythm_catalog import (
        parse_tokenrhythm_declared,
        parse_tokenrhythm_published,
    )

    built: list[str] = []
    fetches: list[str] = []

    class _HealthyProvider:
        async def list_models(self):
            return [ModelInfo(provider="ollama", model_id="ollama-live")]

    class _FailingProvider:
        async def list_models(self):
            raise RuntimeError("HTTP 401: invalid api key synthetic-secret")

    def fake_build_provider(config):
        built.append(config.provider)
        if config.provider == "tokenrhythm":  # pragma: no cover - regression guard
            raise AssertionError("TokenRhythm models.list must remain snapshot-only")
        if config.provider == "openrouter":
            return _FailingProvider()
        return _HealthyProvider()

    async def fetch_published(**_kwargs):
        fetches.append("published")
        return parse_tokenrhythm_published(
            {
                "data": [
                    {
                        "id": "qwen3.8-max",
                        "name": "Qwen 3.8 Max",
                        "type": "chat",
                        "status": "online",
                        "contextWindow": 1_000_000,
                        "maxOutputTokens": 131_072,
                    }
                ]
            }
        )

    async def fetch_declared(*_args, **_kwargs):
        fetches.append("declared")
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "qwen3.8-max",
                        "context_length": 1_000_000,
                        "max_completion_tokens": 131_072,
                    }
                ]
            }
        )

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    monkeypatch.setattr(
        model_catalog_refresh,
        "fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        model_catalog_refresh,
        "fetch_tokenrhythm_declared",
        fetch_declared,
    )
    persisted_config = GatewayConfig(state_dir=str(tmp_path))
    persisted_config.llm.provider = "tokenrhythm"
    persisted_config.llm.model = "qwen3.8-max"
    persisted_config.llm.api_key = "sk_tr_synthetic_models_list_key"
    persisted_config.llm.base_url = "https://tokenrhythm.studio/v1"
    coordinator = model_catalog_refresh.TokenRhythmCatalogCoordinator(ModelCatalog())
    await coordinator.refresh_active(persisted_config)
    monkeypatch.setattr(model_catalog_refresh, "_coordinator", coordinator)
    initial_fetches = list(fetches)

    tokenrhythm = ProviderConfig(
        provider="tokenrhythm",
        model="qwen3.8-max",
        api_key="sk_tr_synthetic_models_list_key",
        # Runtime selectors normalize the official endpoint to its origin;
        # the persisted snapshot above intentionally retains ``/v1``.
        base_url="https://tokenrhythm.studio",
    )
    ollama = ProviderConfig(provider="ollama", model="ollama-live")
    failed = ProviderConfig(
        provider="openrouter",
        model="openrouter/auth-locked",
        api_key="sk-or-synthetic-models-list-key",
    )
    primary, fallbacks = (
        (tokenrhythm, [ollama, failed])
        if tokenrhythm_first
        else (ollama, [tokenrhythm, failed])
    )
    selector = ModelSelector(SelectorConfig(primary=primary, fallbacks=fallbacks))

    try:
        envelope = await _handle_models_list(
            {},
            RpcContext(conn_id="test", provider_selector=selector),
        )
    finally:
        await coordinator.close()

    assert fetches == initial_fetches
    assert built == ["ollama", "openrouter"]
    assert {row["id"] for row in envelope["models"]} == {
        "qwen3.8-max",
        "ollama-live",
    }
    assert envelope["errors"] == [
        {
            "provider": "openrouter",
            "kind": "auth_invalid",
            "detail": "HTTP 401: invalid api key synthetic-secret",
        }
    ]


async def test_models_list_same_tokenrhythm_model_keeps_each_keys_snapshot(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway import model_catalog_refresh
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.tokenrhythm_catalog import (
        parse_tokenrhythm_declared,
        parse_tokenrhythm_published,
    )

    async def fetch_published(**_kwargs):
        return parse_tokenrhythm_published({"data": []})

    async def fetch_declared(*_args, api_key: str, **_kwargs):
        ceiling = 131_072 if api_key.endswith("-a") else 8_192
        context = 1_000_000 if api_key.endswith("-a") else 64_000
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "shared/model",
                        "context_length": context,
                        "max_completion_tokens": ceiling,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        model_catalog_refresh,
        "fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        model_catalog_refresh,
        "fetch_tokenrhythm_declared",
        fetch_declared,
    )
    config_a = GatewayConfig(
        state_dir=str(tmp_path),
        llm={
            "provider": "tokenrhythm",
            "model": "shared/model",
            "api_key": "synthetic-model-list-key-a",
            "base_url": "https://tokenrhythm.studio/v1",
        },
    )
    coordinator = model_catalog_refresh.TokenRhythmCatalogCoordinator(ModelCatalog())
    await coordinator.refresh_active(config_a, force=True)
    request_b = model_catalog_refresh._tokenrhythm_request(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-model-list-key-b",
        proxy="",
    )
    assert request_b is not None
    await coordinator.discover(
        request_b,
        force=True,
        persist_entitlement=True,
        activate=False,
    )
    monkeypatch.setattr(model_catalog_refresh, "_coordinator", coordinator)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="tokenrhythm",
                model="shared/model",
                api_key="synthetic-model-list-key-a",
                base_url="https://tokenrhythm.studio/v1",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="tokenrhythm",
                    model="shared/model",
                    api_key="synthetic-model-list-key-b",
                    base_url="https://tokenrhythm.studio/v1",
                )
            ],
        )
    )

    try:
        envelope = await _handle_models_list(
            {},
            RpcContext(conn_id="test", provider_selector=selector),
        )
    finally:
        await coordinator.close()

    assert [row["id"] for row in envelope["models"]] == [
        "shared/model",
        "shared/model",
    ]
    assert [row["maxOutputTokens"] for row in envelope["models"]] == [
        131_072,
        8_192,
    ]
    assert [row["contextWindow"] for row in envelope["models"]] == [
        1_000_000,
        64_000,
    ]
