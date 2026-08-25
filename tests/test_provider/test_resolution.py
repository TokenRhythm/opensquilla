"""Unit tests for provider.resolution — effective-LLM provenance per field class.

All configs and model ids are synthetic. The catalog cases inject models via
``ModelCatalog._populate_from_data`` (the same seam other catalog tests use)
so the tests stay offline and deterministic.
"""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig, is_sensitive_config_key
from opensquilla.provider.model_catalog import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    ModelCatalog,
)
from opensquilla.provider.resolution import ResolvedField, resolve_effective_llm

UNKNOWN_MODEL = "synthetic/unknown-model-z"
CATALOG_MODEL = "synthetic/model-x"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))


def _config(**kwargs) -> GatewayConfig:
    return GatewayConfig(**kwargs)


def _catalog_with_model() -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": CATALOG_MODEL,
                "context_length": 120_000,
                "top_provider": {"max_completion_tokens": 9_000},
            }
        ]
    )
    return catalog


# --- llm.provider / llm.model / llm.base_url --------------------------------


def test_default_llm_identity_fields_report_default() -> None:
    fields = resolve_effective_llm(_config(), ModelCatalog())
    assert fields["llm.provider"] == ResolvedField("tokenrhythm", "default")
    assert fields["llm.model"] == ResolvedField("deepseek-v4-pro-0813", "default")
    assert fields["llm.base_url"] == ResolvedField("https://tokenrhythm.studio/v1", "default")


def test_explicit_llm_identity_fields_report_config() -> None:
    # The model deliberately differs from the field default: value-vs-baseline
    # attribution cannot distinguish an explicit value that coincides with it.
    cfg = _config(
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://proxy.example/v1",
        )
    )
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["llm.provider"] == ResolvedField("deepseek", "config")
    assert fields["llm.model"] == ResolvedField("deepseek-chat", "config")
    assert fields["llm.base_url"] == ResolvedField("https://proxy.example/v1", "config")


def test_spec_default_base_url_reports_default() -> None:
    # Spec-default provenance: boot materializes an unset base_url from the
    # provider spec, so a value equal to the spec default was not
    # operator-chosen even though the live model always carries it.
    cfg = _config(
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
        )
    )
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["llm.base_url"] == ResolvedField("https://api.deepseek.com", "default")


# --- llm.max_tokens / llm.context_window (catalog delegation) ---------------


def test_max_tokens_config_override_wins_and_reports_config() -> None:
    cfg = _config(llm=LlmProviderConfig(model=CATALOG_MODEL, max_tokens=1_234))
    fields = resolve_effective_llm(cfg, _catalog_with_model())
    assert fields["llm.max_tokens"] == ResolvedField(1_234, "config")


def test_max_tokens_and_context_window_from_catalog_report_catalog() -> None:
    cfg = _config(llm=LlmProviderConfig(model=CATALOG_MODEL))
    fields = resolve_effective_llm(cfg, _catalog_with_model())
    assert fields["llm.max_tokens"] == ResolvedField(9_000, "catalog")
    assert fields["llm.context_window"] == ResolvedField(120_000, "catalog")


def test_unknown_model_budgets_report_default() -> None:
    cfg = _config(llm=LlmProviderConfig(model=UNKNOWN_MODEL))
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["llm.max_tokens"] == ResolvedField(DEFAULT_MAX_TOKENS, "default")
    assert fields["llm.context_window"] == ResolvedField(DEFAULT_CONTEXT_WINDOW, "default")


def test_with_source_variants_cannot_drift_from_plain_resolvers() -> None:
    # resolve_max_tokens / resolve_context_window delegate to the
    # *_with_source variants; the pairs must agree by construction.
    catalog = _catalog_with_model()
    for model, override, provider in (
        (CATALOG_MODEL, 0, "openrouter"),
        (CATALOG_MODEL, 1_234, "openrouter"),
        (UNKNOWN_MODEL, 0, "openrouter"),
        (UNKNOWN_MODEL, 0, "ollama"),
    ):
        value, _source = catalog.resolve_max_tokens_with_source(model, override, provider)
        assert value == catalog.resolve_max_tokens(model, override, provider)
        window, _source = catalog.resolve_context_window_with_source(model, provider)
        assert window == catalog.resolve_context_window(model, provider)


def test_catalog_source_labels_per_layer() -> None:
    catalog = _catalog_with_model()
    assert catalog.resolve_max_tokens_with_source(CATALOG_MODEL, 777)[1] == "override"
    assert catalog.resolve_max_tokens_with_source(CATALOG_MODEL)[1] == "catalog"
    assert catalog.resolve_max_tokens_with_source(UNKNOWN_MODEL)[1] == "default"
    assert catalog.resolve_context_window_with_source(CATALOG_MODEL)[1] == "catalog"
    assert catalog.resolve_context_window_with_source(UNKNOWN_MODEL)[1] == "default"
    # Local runtimes report their own default window — still a default layer.
    assert catalog.resolve_context_window_with_source(UNKNOWN_MODEL, "ollama")[1] == "default"
    # A positive [models.*] override is its own top layer.
    catalog.set_user_overrides({CATALOG_MODEL: {"context_window": 55_000}})
    assert catalog.resolve_context_window_with_source(CATALOG_MODEL) == (55_000, "override")


def test_context_window_model_override_reports_config() -> None:
    # The [models.*] per-model override is operator config; the provenance
    # vocabulary maps the catalog's "override" label to "config".
    cfg = _config(llm=LlmProviderConfig(model=CATALOG_MODEL))
    catalog = _catalog_with_model()
    catalog.set_user_overrides({CATALOG_MODEL: {"context_window": 55_000}})
    fields = resolve_effective_llm(cfg, catalog)
    assert fields["llm.context_window"] == ResolvedField(55_000, "config")


def test_context_window_global_config_beats_catalog_reports_config() -> None:
    cfg = _config(llm=LlmProviderConfig(model=CATALOG_MODEL, context_window_tokens=99_000))
    fields = resolve_effective_llm(cfg, _catalog_with_model())
    assert fields["llm.context_window"] == ResolvedField(99_000, "config")


def test_context_window_model_override_beats_global_config() -> None:
    cfg = _config(llm=LlmProviderConfig(model=CATALOG_MODEL, context_window_tokens=99_000))
    catalog = _catalog_with_model()
    catalog.set_user_overrides({CATALOG_MODEL: {"context_window": 55_000}})
    fields = resolve_effective_llm(cfg, catalog)
    assert fields["llm.context_window"] == ResolvedField(55_000, "config")


# --- squilla_router.tiers.* (preset vs config) -------------------------------


def test_default_tiers_report_preset() -> None:
    fields = resolve_effective_llm(_config(), ModelCatalog())
    tier_fields = {
        path: field
        for path, field in fields.items()
        if path.startswith("squilla_router.tiers.")
    }
    assert tier_fields, "expected per-tier provenance fields"
    assert {field.source for field in tier_fields.values()} == {"preset"}


def test_tier_override_reports_config_per_key() -> None:
    cfg = _config(
        squilla_router={"tiers": {"c1": {"provider": "openrouter", "model": "synthetic/custom"}}}
    )
    fields = resolve_effective_llm(cfg, ModelCatalog())
    # The overridden key is config; the untouched sibling key still matches
    # the preset baseline and stays preset.
    assert fields["squilla_router.tiers.c1.model"] == ResolvedField("synthetic/custom", "config")
    assert fields["squilla_router.tiers.c1.provider"] == ResolvedField("openrouter", "preset")


def test_tier_profile_preset_vs_override() -> None:
    cfg = _config(
        llm=LlmProviderConfig(provider="openai", model="gpt-5.5"),
        squilla_router={
            "tier_profile": "openai",
            "tiers": {"c2": {"model": "synthetic/custom-c2"}},
        },
    )
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["squilla_router.tiers.c2.model"] == ResolvedField(
        "synthetic/custom-c2", "config"
    )
    assert fields["squilla_router.tiers.c2.provider"] == ResolvedField("openai", "preset")
    assert fields["squilla_router.tiers.c0.model"].source == "preset"
    assert fields["squilla_router.tiers.c0.provider"] == ResolvedField("openai", "preset")


# --- llm_ensemble.* (fields_set semantics) -----------------------------------


def test_untouched_ensemble_reports_default() -> None:
    fields = resolve_effective_llm(_config(), ModelCatalog())
    assert fields["llm_ensemble.enabled"] == ResolvedField(False, "default")
    assert fields["llm_ensemble.selection_mode"] == ResolvedField(
        "static_openrouter_b5", "default"
    )


def test_explicit_ensemble_fields_report_config_even_at_default_value() -> None:
    # Documented limitation: from persisted state a materialized default is
    # indistinguishable from an explicitly written one, so a field present
    # in the live model reports config even when its value equals the
    # default (enabled=True here).
    cfg = _config(llm_ensemble={"enabled": True})
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["llm_ensemble.enabled"] == ResolvedField(True, "config")
    assert fields["llm_ensemble.selection_mode"].source == "default"


def test_ensemble_selection_mode_override_reports_config() -> None:
    cfg = _config(llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"})
    fields = resolve_effective_llm(cfg, ModelCatalog())
    assert fields["llm_ensemble.enabled"] == ResolvedField(False, "config")
    assert fields["llm_ensemble.selection_mode"] == ResolvedField("router_dynamic", "config")


# --- secrets excluded by construction ----------------------------------------


def test_no_emitted_path_has_a_secret_named_segment() -> None:
    # Couples the resolver's allowlist to the real redaction predicate so
    # the two cannot drift: if a secret-named field ever slips into the
    # resolver output, this fails before the RPC belt-and-braces layer.
    cfg = _config(llm=LlmProviderConfig(api_key="sk-test-000"))
    fields = resolve_effective_llm(cfg, ModelCatalog())
    for path in fields:
        assert not any(
            is_sensitive_config_key(segment) for segment in path.split(".")
        ), f"secret-named segment in {path}"
    assert "sk-test-000" not in repr(fields)
