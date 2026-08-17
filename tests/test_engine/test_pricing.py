from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from opensquilla.engine.pricing import (
    PriceEntry,
    PricingCache,
    lookup_price,
    reset_live_price_cache_for_tests,
    resolve_model_price,
    seed_live_price_cache_for_tests,
)


@pytest.fixture(autouse=True)
def reset_pricing_cache() -> Iterator[None]:
    reset_live_price_cache_for_tests()
    yield
    reset_live_price_cache_for_tests()


def test_default_test_session_disables_live_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_fetch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("default tests must not fetch live OpenRouter pricing")

    monkeypatch.setattr(
        "opensquilla.engine.pricing._fetch_live_openrouter_price",
        unexpected_fetch,
    )

    resolved = resolve_model_price("vendor/synthetic-model", provider="openrouter")

    assert resolved.source == "default"


def test_deepseek_v4_pro_static_price_matches_current_official(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Official price since 2026-05-31: $0.435/M in (miss), $0.87/M out, $0.003625/M cache hit."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("deepseek/deepseek-v4-pro")

    assert price.input_per_m == pytest.approx(0.435)
    assert price.output_per_m == pytest.approx(0.87)
    assert price.cache_read_per_m == pytest.approx(0.003625)


@pytest.mark.asyncio
async def test_pricing_cache_refresh_adds_openrouter_app_attribution() -> None:
    import httpx as _httpx

    cache = PricingCache(api_key="test-key", ttl_seconds=60)
    captured: dict[str, object] = {}
    mock_response = _httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "openai/gpt-4o",
                    "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                }
            ]
        },
        request=_httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
    )

    with patch("opensquilla.engine.pricing.httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()

        async def capture_get(url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return mock_response

        mock_instance.get = AsyncMock(side_effect=capture_get)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        await cache.refresh()

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }
    price = cache.get_price_sync("openai/gpt-4o")
    assert price is not None
    assert price.input_per_token == 0.0000025
    assert price.output_per_token == 0.00001


def test_deepseek_v4_pro_live_price_wins_over_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hardcoded pin must no longer block live-price self-correction."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "1")
    reset_live_price_cache_for_tests()
    seed_live_price_cache_for_tests("deepseek/deepseek-v4-pro", PriceEntry(0.5, 1.0))

    price = lookup_price("deepseek/deepseek-v4-pro")

    assert price.input_per_m == pytest.approx(0.5)
    assert price.output_per_m == pytest.approx(1.0)


def test_versioned_deepseek_id_prefix_matches_static_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("deepseek/deepseek-v4-pro-20260423")

    assert price.input_per_m == pytest.approx(0.435)


def test_price_entry_cache_fields_default_none() -> None:
    entry = PriceEntry(3.0, 15.0)
    assert entry.cache_read_per_m is None
    assert entry.cache_write_per_m is None


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("moonshotai/kimi-k2.7-code-20260612", 0.95, 4.0),
        ("kimi-k2.7-code", 0.95, 4.0),
        ("claude-haiku-4-5-20251001", 1.0, 5.0),
        ("deepseek-chat", 0.14, 0.28),
        ("deepseek-reasoner", 0.26, 0.38),
        ("glm-5.2", 1.40, 4.40),
        ("qwen/qwen3.7-flash", 0.03, 0.13),
        ("qwen3.7-flash", 0.03, 0.13),
        ("qwen3.7-max", 1.25, 3.75),
        ("qwen3.7-plus", 0.40, 1.60),
        ("deepseek-v4-flash-0731", 0.14, 0.28),
    ],
)
def test_previously_missing_ids_now_have_static_entries(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


@pytest.mark.parametrize("model", ["z-ai/glm-5.1", "z-ai/glm-5.2"])
def test_glm_5_static_price_matches_openrouter_native_provider(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(1.40)
    assert price.output_per_m == pytest.approx(4.40)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("qwen/qwen3.7-plus-20260602", 0.40, 1.60),
        ("qwen/qwen3.7-max", 1.25, 3.75),
        ("google/gemini-3-flash-preview-20251217", 0.50, 3.0),
        ("mistralai/mistral-large-2512", 0.50, 1.50),
        ("meta-llama/llama-4-maverick", 0.15, 0.60),
    ],
)
def test_g8_ensemble_static_fallback_prices_do_not_use_generic_default(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_claude_opus_4_8_static_price_matches_openrouter_model_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("anthropic/claude-opus-4.8")

    assert price.input_per_m == pytest.approx(5.0)
    assert price.output_per_m == pytest.approx(25.0)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("qwen-plus", 0.115, 0.287),
        ("qwen-flash", 0.022, 0.216),
        ("qwen-turbo", 0.044, 0.087),
        ("qwen-max", 0.345, 1.377),
    ],
)
def test_dashscope_beijing_qwen_static_prices_match_official_model_studio_pricing(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_dashscope_beijing_qwen_plus_smoke_usage_estimates_cost_from_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    price = lookup_price("qwen-plus")

    estimated_cost = (31 * price.input_per_m + 6 * price.output_per_m) / 1_000_000

    assert estimated_cost == pytest.approx(0.000005287)


def test_provider_profile_models_do_not_use_default_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    default = PriceEntry(3.0, 15.0)
    models = [
        "qwen3.6-flash",
        "qwen3.7-flash",
        "qwen3.7-plus",
        "qwen3.7-max",
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gpt-4.1-nano",
        "gpt-4.1-mini",
        "gpt-4.1",
        "glm-5-turbo",
        "glm-5.2",
        "kimi-k2.6",
        "kimi-k2.7-code",
        "doubao-seed-1-6-flash-250828",
        "doubao-seed-1-6-251015",
        "doubao-seed-1-6-thinking-250715",
        "doubao-seed-2-0-mini-260215",
        "doubao-seed-2-0-lite-260215",
        "doubao-seed-2-0-pro-260215",
        "doubao-seed-2-0-code-preview-260215",
    ]

    for model in models:
        assert lookup_price(model) != default, model


def test_local_embedding_model_does_not_fetch_openrouter_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "1")

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("local embedding models should not hit OpenRouter pricing")

    monkeypatch.setattr("opensquilla.engine.pricing._fetch_openrouter_json_sync", fail_fetch)

    price = lookup_price("BAAI/bge-small-zh-v1.5")

    assert price.input_per_m == 0
    assert price.output_per_m == 0


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("gpt-4.1-nano", 0.10, 0.40),
        ("gpt-4.1-mini", 0.40, 1.60),
        ("gpt-4.1", 2.0, 8.0),
        ("gpt-5.4-nano", 0.20, 1.25),
        ("gpt-5.4-mini", 0.75, 4.50),
        ("gpt-5.5", 5.0, 30.0),
        ("glm-5", 1.0, 3.20),
        ("glm-5-turbo", 1.20, 4.0),
        ("glm-5.1", 1.40, 4.40),
        ("kimi-k2.6", 0.95, 4.0),
        ("kimi-k2.7-code", 0.95, 4.0),
        ("gemini-3.1-flash-lite", 0.25, 1.50),
        ("gemini-3.5-flash", 1.50, 9.0),
        ("gemini-3.1-pro-preview", 2.0, 12.0),
    ],
)
def test_direct_provider_profile_estimate_prices_match_approved_static_entries(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("doubao-seed-2-0-mini-260215", 0.029, 0.287),
        ("doubao-seed-2-0-lite-260215", 0.086, 0.516),
        ("doubao-seed-2-0-pro-260215", 0.459, 2.294),
        ("doubao-seed-2-0-code-preview-260215", 0.459, 2.294),
    ],
)
def test_volcengine_seed_2_static_prices_match_under_32k_online_inference_pricing(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("gpt-4.1", 2.0, 8.0),
        ("glm-4.5", 0.115, 0.287),
        ("kimi-k2.6", 0.95, 4.0),
        ("MiniMax-M2.7", 0.118, 0.99),
    ],
)
def test_direct_openai_zhipu_kimi_and_minimax_prices_do_not_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_local_provider_is_free_even_for_unqualified_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    # Bare ollama model id misses the "ollama/" table entry -> cloud default.
    assert lookup_price("qwen3:4b").input_per_m == pytest.approx(3.0)
    # Passing the local provider makes it free regardless of the model id.
    free = lookup_price("qwen3:4b", provider="ollama")
    assert free.input_per_m == 0.0
    assert free.output_per_m == 0.0


def test_local_provider_case_insensitive_and_covers_local_runtimes() -> None:
    for prov in ("ollama", "OLLAMA", "lm_studio", "vllm", "ovms", "local"):
        price = lookup_price("some-model", provider=prov)
        assert (price.input_per_m, price.output_per_m) == (0.0, 0.0)


def test_cloud_provider_arg_does_not_zero_priced_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    price = lookup_price("claude-sonnet-4", provider="anthropic")
    assert price.input_per_m == pytest.approx(3.0)
