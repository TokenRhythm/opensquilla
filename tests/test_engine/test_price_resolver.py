from __future__ import annotations

import pytest

from opensquilla.engine.pricing import PriceEntry, _endpoint_price, resolve_model_price
from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog


def test_local_provider_resolves_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    r = resolve_model_price("qwen3:4b", provider="lm_studio")
    assert r.source == "local_free"
    assert r.entry.input_per_m == 0.0 and r.entry.cache_read_per_m == 0.0


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
@pytest.mark.parametrize("model_id", ["unknown-custom-model", "gpt-4.1"])
def test_generic_custom_provider_defaults_to_zero_price(
    monkeypatch: pytest.MonkeyPatch, provider: str, model_id: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    resolved = resolve_model_price(model_id, provider=provider)

    assert resolved.source == "custom_free"
    assert resolved.entry == PriceEntry(0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
def test_generic_custom_provider_ignores_warmed_openrouter_catalog_price(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/warm-priced-model",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            }
        ]
    )
    set_shared_catalog(catalog)
    try:
        resolved = resolve_model_price("vendor/warm-priced-model", provider=provider)
    finally:
        set_shared_catalog(None)

    assert resolved.source == "custom_free"
    assert resolved.entry == PriceEntry(0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
def test_generic_custom_provider_uses_complete_user_price_override(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            f"{provider}/vendor/priced-model": {
                "input_cost_per_mtok": 1.25,
                "output_cost_per_mtok": 2.5,
                "cache_read_cost_per_mtok": 0.125,
                "cache_write_cost_per_mtok": 3.125,
            }
        }
    )
    set_shared_catalog(catalog)
    try:
        resolved = resolve_model_price("vendor/priced-model", provider=provider)
    finally:
        set_shared_catalog(None)

    assert resolved.source == "user_override"
    assert resolved.entry == PriceEntry(1.25, 2.5, 0.125, 3.125)


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
def test_generic_custom_provider_honors_explicit_zero_price_override(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            f"{provider}/vendor/zero-priced-model": {
                "input_cost_per_mtok": 0.0,
                "output_cost_per_mtok": 0.0,
            }
        }
    )
    set_shared_catalog(catalog)
    try:
        resolved = resolve_model_price("vendor/zero-priced-model", provider=provider)
    finally:
        set_shared_catalog(None)

    assert resolved.source == "user_override"
    assert resolved.entry == PriceEntry(0.0, 0.0)


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
@pytest.mark.parametrize(
    "fields",
    [
        {"context_window": 131_072},
        {"input_cost_per_mtok": 1.0},
        {"output_cost_per_mtok": 2.0},
    ],
)
def test_generic_custom_provider_ignores_incomplete_user_price_override(
    monkeypatch: pytest.MonkeyPatch, provider: str, fields: dict[str, float | int]
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/warm-priced-model",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            }
        ]
    )
    catalog.set_user_overrides({f"{provider}/vendor/warm-priced-model": fields})
    set_shared_catalog(catalog)
    try:
        resolved = resolve_model_price("vendor/warm-priced-model", provider=provider)
    finally:
        set_shared_catalog(None)

    assert resolved.source == "custom_free"
    assert resolved.entry == PriceEntry(0.0, 0.0, 0.0, 0.0)


def test_user_catalog_override_wins_over_static(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "deepseek/deepseek-v4-pro": {
                "input_cost_per_mtok": 0.2,
                "output_cost_per_mtok": 0.4,
                "cache_read_cost_per_mtok": 0.002,
            }
        }
    )
    set_shared_catalog(catalog)
    try:
        r = resolve_model_price("deepseek/deepseek-v4-pro", provider="deepseek")
        assert r.source == "user_override"
        assert r.entry.input_per_m == pytest.approx(0.2)
        assert r.entry.cache_read_per_m == pytest.approx(0.002)
    finally:
        set_shared_catalog(None)


def test_catalog_snapshot_wins_over_static_table_with_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refreshed models.dev snapshot now vendors deepseek-v4-pro's own
    cost keys, so a provider-qualified lookup resolves through the catalog
    layer (matching the static table's official rate) instead of falling
    all the way to the static table."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    r = resolve_model_price("deepseek/deepseek-v4-pro", provider="deepseek")
    assert r.source == "catalog"
    assert r.entry.input_per_m == pytest.approx(0.435)
    assert r.entry.cache_read_per_m == pytest.approx(0.003625)


def test_static_table_fallback_with_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model+provider pair verified ABSENT from the snapshot (a dated
    suffix models.dev does not carry) still falls through to the static
    table — the catalog-first change above only wins when the snapshot
    actually knows the exact (provider, model)."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    r = resolve_model_price("deepseek/deepseek-v4-pro-20260423", provider="deepseek")
    assert r.source == "static_table"
    assert r.entry.input_per_m == pytest.approx(0.435)


def test_unknown_model_resolves_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    r = resolve_model_price("totally-unknown-model-xyz", provider="mistral")
    assert r.source == "default"
    assert r.entry == PriceEntry(3.0, 15.0)


def test_live_fetch_skipped_for_non_openrouter_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A siliconflow 'deepseek-ai/...' id must not query the OpenRouter marketplace."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "1")
    called: list[object] = []
    monkeypatch.setattr(
        "opensquilla.engine.pricing._fetch_live_openrouter_price",
        lambda *a, **k: called.append(a) or None,
    )
    resolve_model_price("deepseek-ai/DeepSeek-V3.2", provider="siliconflow")
    assert called == []


def test_endpoint_price_parses_cache_rates_without_discount_inverse() -> None:
    """Prompt/completion take the discount-inverse; cache rates are passed through."""
    price = _endpoint_price(
        {
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000001",
                "input_cache_write": "0.00000125",
                "discount": 0.5,
            }
        }
    )
    assert price is not None
    # 1/(1 - 0.5) = 2x on prompt/completion; no inverse on the cache rates.
    assert price.input_per_m == pytest.approx(2.0)
    assert price.output_per_m == pytest.approx(4.0)
    assert price.cache_read_per_m == pytest.approx(0.1)
    assert price.cache_write_per_m == pytest.approx(1.25)


def test_endpoint_price_cache_rates_none_when_absent() -> None:
    price = _endpoint_price({"pricing": {"prompt": "0.000001", "completion": "0.000002"}})
    assert price is not None
    assert price.cache_read_per_m is None
    assert price.cache_write_per_m is None
