from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.onboarding.provider_specs import get_provider_setup_spec
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.registry import get_provider_spec


def test_tsubasa_uses_compatible_transport_without_live_catalog_verification() -> None:
    spec = get_provider_spec("tsubasa")
    assert spec.backend == "openai_compat"
    assert spec.default_base_url == "https://api.tsubasa.sh/v1"
    assert spec.env_key == "TSUBASA_API_KEY"
    assert spec.requires_api_key()
    assert spec.selectable_model_catalog == "none"
    assert not spec.compat.supports_native_json_schema_output
    assert get_provider_setup_spec("tsubasa").verification == "experimental"


@pytest.mark.parametrize(
    ("model", "max_output", "input_cost", "output_cost"),
    [("tsubasa-fast", 8192, 0.2, 1.0), ("tsubasa-pro", 16384, 0.5, 4.0)],
)
def test_tsubasa_catalog_keeps_service_limits_and_text_capabilities(
    model: str, max_output: int, input_cost: float, output_cost: float,
) -> None:
    catalog = ModelCatalog()
    entry = catalog.resolve_entry(model, provider="tsubasa")
    assert (entry.context_window, entry.max_output_tokens) == (32768, max_output)
    assert entry.input_cost_per_mtok == pytest.approx(input_cost)
    assert entry.output_cost_per_mtok == pytest.approx(output_cost)
    assert catalog.resolve_context_window(model, "tsubasa") == 32768
    assert catalog.resolve_max_tokens_with_source(
        model, provider="tsubasa", capacity_only=True,
    ) == (max_output, "catalog")
    assert catalog.resolve_max_tokens(model, provider="tsubasa") == 8192
    capabilities = catalog.get_capabilities(model, provider_name="tsubasa")
    assert not capabilities.supports_tools
    assert not capabilities.supports_reasoning
    assert not capabilities.supports_vision


def test_tsubasa_named_credential_does_not_fall_back_to_other_keys(monkeypatch) -> None:
    def configured() -> GatewayConfig:
        return GatewayConfig(llm=LlmProviderConfig(
            provider="tsubasa", model="tsubasa-fast", api_key_env="TSUBASA_API_KEY",
        ))

    monkeypatch.setenv("TSUBASA_API_KEY", "synthetic-tsubasa-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-key")
    config = configured()
    runtime = resolve_llm_runtime_config(config)
    assert runtime.api_key == "synthetic-tsubasa-key"
    assert runtime.base_url == "https://api.tsubasa.sh/v1"

    monkeypatch.delenv("TSUBASA_API_KEY")
    config = configured()
    assert resolve_llm_runtime_config(config).api_key == ""
