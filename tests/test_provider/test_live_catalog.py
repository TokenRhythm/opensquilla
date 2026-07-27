"""Provider-scoped live catalog ingest (provider/live_catalog.py).

Covers the TokenRhythm listing parser, the catalog's scoped live layer
(authority order and cross-provider isolation), and the registry-driven
warm helper the gateway boot calls. Everything is offline: HTTP is patched,
payloads are synthetic mirrors of the platform shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.provider.live_catalog import (
    _TOKENRHYTHM_CNY_PER_USD,
    fetch_live_catalog_entries,
    parse_tokenrhythm_models,
    warm_live_provider_catalogs,
)
from opensquilla.provider.model_catalog import ModelCatalog


def _tokenrhythm_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "type": "chat",
        "status": "online",
        "contextWindow": 900_000,
        "maxOutputTokens": 300_000,
        "capabilities": {"tools": True, "vision": False},
        "billingUnit": 1_000_000,
        "currency": "CNY",
        "inputPrice": "12",
        "outputPrice": "24",
        "cacheReadPrice": "1",
    }
    row.update(overrides)
    return row


def test_parse_tokenrhythm_models_maps_published_fields() -> None:
    entries = parse_tokenrhythm_models({"code": 0, "data": [_tokenrhythm_row()]})

    fields = entries["deepseek-v4-pro"]
    assert fields["context_window"] == 900_000
    assert fields["max_output_tokens"] == 300_000
    assert fields["display_name"] == "DeepSeek V4 Pro"
    assert fields["supports_tools"] is True
    assert fields["supports_vision"] is False
    # CNY per billingUnit tokens → USD per Mtok at the documented ~6.975
    # conversion (matches the packaged corrections rows).
    assert fields["input_cost_per_mtok"] == pytest.approx(12 / _TOKENRHYTHM_CNY_PER_USD, abs=1e-4)
    assert fields["output_cost_per_mtok"] == pytest.approx(24 / _TOKENRHYTHM_CNY_PER_USD, abs=1e-4)
    assert fields["cache_read_cost_per_mtok"] == pytest.approx(
        1 / _TOKENRHYTHM_CNY_PER_USD, abs=1e-4
    )
    # The listing has no reasoning knowledge — the corrections ladder keeps
    # owning supports_reasoning/reasoning_format, so the parser must never
    # emit either field.
    assert "supports_reasoning" not in fields
    assert "reasoning_format" not in fields


def test_tokenrhythm_prices_use_per_bucket_effective_discount_and_standard_fallbacks() -> None:
    row = _tokenrhythm_row(
        hasDiscount=True,
        effectiveInputPrice="3",
        discountInputPrice="6",
        inputPrice="12",
        effectiveOutputPrice="invalid",
        discountOutputPrice="8",
        outputPrice="24",
        effectiveCacheReadPrice=True,
        discountCacheReadPrice="0",
        cacheReadPrice="1",
    )

    fields = parse_tokenrhythm_models({"data": [row]})["deepseek-v4-pro"]

    assert fields["input_cost_per_mtok"] == pytest.approx(
        3 / _TOKENRHYTHM_CNY_PER_USD, rel=1e-12
    )
    assert fields["output_cost_per_mtok"] == pytest.approx(
        8 / _TOKENRHYTHM_CNY_PER_USD, rel=1e-12
    )
    # A legitimate free cache bucket must not fall through to the list price.
    assert fields["cache_read_cost_per_mtok"] == 0.0


def test_tokenrhythm_zero_effective_price_remains_authoritative_after_catalog_merge() -> None:
    parsed = parse_tokenrhythm_models(
        {
            "data": [
                _tokenrhythm_row(
                    effectiveInputPrice="0",
                    inputPrice="7",
                )
            ]
        }
    )
    catalog = ModelCatalog()
    catalog.set_live_provider_entries("tokenrhythm", parsed)

    resolved = catalog.resolve_entry("deepseek-v4-pro", provider="tokenrhythm")

    assert resolved.source == "live"
    assert resolved.input_cost_per_mtok == 0.0


def test_tokenrhythm_discount_prices_are_ignored_without_exact_discount_flag() -> None:
    fields = parse_tokenrhythm_models(
        {
            "data": [
                _tokenrhythm_row(
                    hasDiscount="true",
                    discountInputPrice="2",
                    effectiveInputPrice="bad",
                    inputPrice="5",
                )
            ]
        }
    )["deepseek-v4-pro"]

    assert fields["input_cost_per_mtok"] == pytest.approx(
        5 / _TOKENRHYTHM_CNY_PER_USD, rel=1e-12
    )


@pytest.mark.parametrize("billing_unit", [None, 0, -1, True, "bad", "NaN", "Infinity"])
def test_tokenrhythm_invalid_billing_unit_drops_only_prices(billing_unit: object) -> None:
    fields = parse_tokenrhythm_models(
        {"data": [_tokenrhythm_row(billingUnit=billing_unit)]}
    )["deepseek-v4-pro"]

    assert fields["context_window"] == 900_000
    assert not any(field.endswith("_cost_per_mtok") for field in fields)


def test_tokenrhythm_non_million_billing_unit_preserves_full_precision() -> None:
    fields = parse_tokenrhythm_models(
        {"data": [_tokenrhythm_row(billingUnit=1000, inputPrice="0.00123456789")]}
    )["deepseek-v4-pro"]

    assert fields["input_cost_per_mtok"] == pytest.approx(
        (0.00123456789 * 1000) / _TOKENRHYTHM_CNY_PER_USD,
        rel=1e-12,
    )


@pytest.mark.parametrize(
    "invalid",
    [True, -1, "-1", "NaN", "Infinity", "1e999999", float("nan")],
)
def test_tokenrhythm_invalid_effective_price_falls_back_to_standard(invalid: object) -> None:
    fields = parse_tokenrhythm_models(
        {"data": [_tokenrhythm_row(effectiveInputPrice=invalid, inputPrice="7")]}
    )["deepseek-v4-pro"]

    assert fields["input_cost_per_mtok"] == pytest.approx(
        7 / _TOKENRHYTHM_CNY_PER_USD, rel=1e-12
    )


def test_parse_tokenrhythm_models_skips_offline_and_malformed_rows() -> None:
    payload = {
        "code": 0,
        "data": [
            _tokenrhythm_row(),
            _tokenrhythm_row(id="glm-5", status="offline"),
            _tokenrhythm_row(id=""),
            "not-a-model-row",
            {"name": "row with no id"},
        ],
    }

    assert set(parse_tokenrhythm_models(payload)) == {"deepseek-v4-pro"}
    assert parse_tokenrhythm_models({"code": 0, "data": "junk"}) == {}
    assert parse_tokenrhythm_models({}) == {}


def test_parse_tokenrhythm_models_coerces_string_and_float_budget_fields() -> None:
    # The platform demonstrably serves numbers loosely (prices arrive as
    # strings); windows/outputs must survive the same shape drift instead
    # of silently zeroing the exact fields this module exists to track.
    entries = parse_tokenrhythm_models(
        {
            "code": 0,
            "data": [_tokenrhythm_row(contextWindow="1000000", maxOutputTokens=384000.0)],
        }
    )

    fields = entries["deepseek-v4-pro"]
    assert fields["context_window"] == 1_000_000
    assert fields["max_output_tokens"] == 384_000


def test_parse_tokenrhythm_qwen_max_output_is_exact_128_kib_tokens() -> None:
    entries = parse_tokenrhythm_models(
        {
            "code": 0,
            "data": [
                _tokenrhythm_row(
                    id="qwen3.7-max",
                    name="Qwen3.7 Max",
                    contextWindow=1_000_000,
                    maxOutputTokens=131_072,
                )
            ],
        }
    )

    catalog = ModelCatalog()
    catalog.set_live_provider_entries("tokenrhythm", entries)

    assert entries["qwen3.7-max"]["max_output_tokens"] == 131_072
    assert catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm").source == "live"
    assert catalog.resolve_max_tokens("qwen3.7-max", provider="tokenrhythm") == 131_072


def test_parse_tokenrhythm_models_halves_near_window_output_caps() -> None:
    # A published output cap at/near the whole window (input and output
    # share it) would trip resolve_max_tokens' request-safety clamp down to
    # 8192; the parser halves it to the engine's output-reserve ceiling.
    entries = parse_tokenrhythm_models(
        {
            "code": 0,
            "data": [
                _tokenrhythm_row(id="minimax-m2.5", contextWindow=200_000,
                                 maxOutputTokens=200_000),
                _tokenrhythm_row(id="minimax-m2.7", contextWindow=200_000,
                                 maxOutputTokens=192_000),
            ],
        }
    )

    assert entries["minimax-m2.5"]["max_output_tokens"] == 100_000
    assert entries["minimax-m2.7"]["max_output_tokens"] == 100_000

    catalog = ModelCatalog()
    catalog.set_live_provider_entries("tokenrhythm", entries)
    # The resolved value survives resolve_max_tokens un-clamped — the whole
    # point of halving at ingest time.
    assert catalog.resolve_max_tokens("minimax-m2.5", provider="tokenrhythm") == 100_000


def test_refreshed_corrections_rows_do_not_trip_the_near_window_clamp() -> None:
    # Offline fallback parity for the same hazard: every packaged
    # tokenrhythm row must resolve max_tokens above the 8192 safe-default
    # collapse the clamp would apply to a near-window output cap.
    catalog = ModelCatalog()

    for model, expected in (
        ("minimax-m2.5", 100_000),
        ("minimax-m2.7", 100_000),
        ("mimo-v2.5-pro", 128_000),
    ):
        assert catalog.resolve_max_tokens(model, provider="tokenrhythm") == expected


def test_tokenrhythm_offline_corrections_use_effective_prices() -> None:
    catalog = ModelCatalog()

    pro = catalog.resolve_entry("deepseek-v4-pro", provider="tokenrhythm")
    qwen = catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm")
    kimi_code = catalog.resolve_entry("kimi-k2.7-code", provider="tokenrhythm")

    assert pro.input_cost_per_mtok == pytest.approx(3 / _TOKENRHYTHM_CNY_PER_USD)
    assert pro.output_cost_per_mtok == pytest.approx(6 / _TOKENRHYTHM_CNY_PER_USD)
    assert pro.cache_read_cost_per_mtok == pytest.approx(0.025 / _TOKENRHYTHM_CNY_PER_USD)
    assert qwen.input_cost_per_mtok == pytest.approx(6 / _TOKENRHYTHM_CNY_PER_USD)
    assert qwen.output_cost_per_mtok == pytest.approx(18 / _TOKENRHYTHM_CNY_PER_USD)
    assert kimi_code.cache_read_cost_per_mtok == pytest.approx(
        1.3 / _TOKENRHYTHM_CNY_PER_USD
    )


def test_parse_tokenrhythm_models_unknown_currency_emits_no_costs() -> None:
    entries = parse_tokenrhythm_models(
        {"code": 0, "data": [_tokenrhythm_row(currency="USD")]}
    )

    fields = entries["deepseek-v4-pro"]
    assert "input_cost_per_mtok" not in fields
    assert "output_cost_per_mtok" not in fields
    assert fields["context_window"] == 900_000


def test_scoped_live_entries_outrank_corrections_without_leaking() -> None:
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm",
        parse_tokenrhythm_models({"code": 0, "data": [_tokenrhythm_row()]}),
    )

    # Scoped live beats the packaged corrections row (1M/384k) for the
    # ingested provider…
    assert catalog.resolve_context_window_with_source(
        "deepseek-v4-pro", provider="tokenrhythm"
    ) == (900_000, "catalog")
    assert catalog.resolve_max_tokens("deepseek-v4-pro", provider="tokenrhythm") == 300_000
    assert catalog.resolve_entry("deepseek-v4-pro", provider="tokenrhythm").source == "live"
    # …while the same bare id on other providers (and provider-less
    # lookups) never sees the relay's rows.
    assert catalog.resolve_context_window("deepseek-v4-pro", "deepseek") == 1_000_000
    assert catalog.resolve_max_tokens("deepseek-v4-pro", provider="deepseek") == 384_000
    assert catalog.resolve_context_window("deepseek-v4-pro") == 1_000_000


def test_scoped_live_keeps_corrections_reasoning_dialect() -> None:
    # The relay rejects thinking-toggle payloads, so its corrections rows
    # pin reasoning_format="none". Live ingest claims no reasoning fields;
    # capabilities must stay exactly as the ladder decided them.
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm",
        parse_tokenrhythm_models({"code": 0, "data": [_tokenrhythm_row()]}),
    )

    caps = catalog.get_capabilities("deepseek-v4-pro", provider_name="tokenrhythm")
    assert caps.supports_reasoning is False
    assert caps.reasoning_format == "none"
    assert caps.supports_tools is True


def test_user_overrides_still_beat_scoped_live() -> None:
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", {"deepseek-v4-pro": {"context_window": 900_000}}
    )
    catalog.set_user_overrides(
        {"tokenrhythm/deepseek-v4-pro": {"context_window": 555_000}}
    )

    assert catalog.resolve_context_window_with_source(
        "deepseek-v4-pro", provider="tokenrhythm"
    ) == (555_000, "override")


def test_set_live_provider_entries_drops_bad_fields_and_replaces_table() -> None:
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "tokenrhythm",
        {
            "deepseek-v4-pro": {
                "context_window": 900_000,
                "not_a_field": 1,
                "max_output_tokens": "mistyped",
            },
            "all-fields-invalid": {"bogus": True},
        },
    )

    # Valid fields survive, invalid ones degrade like packaged corrections.
    assert catalog.resolve_context_window("deepseek-v4-pro", "tokenrhythm") == 900_000
    # max_output_tokens was dropped → the corrections row still supplies it.
    assert catalog.resolve_max_tokens("deepseek-v4-pro", provider="tokenrhythm") == 384_000

    # A re-warm replaces the whole provider table — stale rows cannot linger.
    catalog.set_live_provider_entries(
        "tokenrhythm", {"glm-5": {"context_window": 777_000}}
    )
    assert catalog.resolve_context_window("glm-5", "tokenrhythm") == 777_000
    assert catalog.resolve_context_window("deepseek-v4-pro", "tokenrhythm") == 1_000_000


async def test_fetch_live_catalog_entries_uses_url_verbatim_without_auth() -> None:
    captured: dict[str, Any] = {}
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"code": 0, "data": [_tokenrhythm_row()]}

    with patch("opensquilla.provider.live_catalog.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def capture_get(url: str, **kwargs: Any) -> Any:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return mock_response

        mock_client.get = AsyncMock(side_effect=capture_get)
        mock_client_cls.return_value = mock_client

        entries = await fetch_live_catalog_entries(
            "https://tokenrhythm.studio/api/models", "tokenrhythm"
        )

    assert captured["url"] == "https://tokenrhythm.studio/api/models"
    # The listing stays keyless while carrying fixed public app attribution.
    headers = captured["kwargs"]["headers"]
    assert headers == {
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }
    assert "Authorization" not in headers
    assert entries["deepseek-v4-pro"]["context_window"] == 900_000


async def test_fetch_live_catalog_entries_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError, match="unknown live catalog shape"):
        await fetch_live_catalog_entries("https://example.invalid/models", "no-such-shape")


class _RecordingCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, dict[str, Any]]]] = []

    def set_live_provider_entries(
        self, provider_id: str, entries: dict[str, dict[str, Any]]
    ) -> None:
        self.calls.append((provider_id, entries))


async def test_warm_ingests_only_providers_with_live_catalog_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[tuple[str, str]] = []

    async def fake_fetch(url: str, shape: str, *, proxy: str = "", timeout: float = 5.0) -> dict:
        fetched.append((url, shape))
        return {"deepseek-v4-pro": {"context_window": 900_000}}

    monkeypatch.setattr(
        "opensquilla.provider.live_catalog.fetch_live_catalog_entries", fake_fetch
    )
    catalog = _RecordingCatalog()

    counts = await warm_live_provider_catalogs(
        catalog,
        # duplicates, casing, blanks, unknown ids, and providers without
        # live-catalog metadata are all skipped without error
        ["tokenrhythm", "TokenRhythm", "", "openai", "no-such-provider"],
    )

    assert counts == {"tokenrhythm": 1}
    assert fetched == [("https://tokenrhythm.studio/api/models", "tokenrhythm")]
    assert catalog.calls == [
        ("tokenrhythm", {"deepseek-v4-pro": {"context_window": 900_000}})
    ]


async def test_warm_degrades_per_provider_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_fetch(*args: Any, **kwargs: Any) -> dict:
        raise OSError("network unreachable")

    monkeypatch.setattr(
        "opensquilla.provider.live_catalog.fetch_live_catalog_entries", failing_fetch
    )
    catalog = _RecordingCatalog()

    counts = await warm_live_provider_catalogs(catalog, ["tokenrhythm"])

    assert counts == {}
    assert catalog.calls == []
