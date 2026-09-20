from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.provider.model_catalog import ModelCatalog, _corrections_budget_fallback


def test_deployment_context_window_distinguishes_unknown_from_operator_and_runtime() -> None:
    catalog = ModelCatalog()
    unknown = catalog.resolve_deployment_limits("synthetic-private-model", provider="private-api")
    assert unknown.context_window == 200_000
    assert unknown.context_window_known is False
    local = catalog.resolve_deployment_limits("synthetic-private-model", provider="ollama")
    assert local.context_window_known is True
    catalog.set_user_overrides({"private-api/synthetic-private-model": {"context_window": 48_000}})
    explicit = catalog.resolve_deployment_limits("synthetic-private-model", provider="private-api")
    assert explicit.context_window == 48_000
    assert explicit.context_window_known is True


def test_user_override_price_fields_keep_qualified_precedence_and_bare_fallback() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "vendor/priced-model": {
                "input_cost_per_mtok": 1.0,
                "output_cost_per_mtok": 2.0,
                "cache_write_cost_per_mtok": 4.0,
                "context_window": 131_072,
            },
            "custom/vendor/priced-model": {
                "input_cost_per_mtok": 0.0,
                "cache_read_cost_per_mtok": 0.1,
            },
        }
    )

    assert catalog.user_override_price_fields(
        "VENDOR/PRICED-MODEL", provider="CUSTOM"
    ) == {
        "input_cost_per_mtok": 0.0,
        "output_cost_per_mtok": 2.0,
        "cache_read_cost_per_mtok": 0.1,
        "cache_write_cost_per_mtok": 4.0,
    }


def test_deepseek_direct_current_flash_alias_and_pro_limits() -> None:
    # Official direct API metadata: current Flash and its retired V4 alias
    # share the V4.1 deployment, while Pro remains a text-only deployment.
    catalog = ModelCatalog()

    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"):
        assert catalog.resolve_context_window(model, "deepseek") == 1_000_000
        assert catalog.resolve_context_window_with_source(model, "deepseek")[1] == "catalog"
        assert catalog.resolve_max_tokens(model, provider="deepseek") == 384_000
        caps = catalog.get_capabilities(model, provider_name="deepseek")
        assert caps.supports_reasoning is True
        assert caps.supports_tools is True
        assert caps.supports_vision is (model != "deepseek-v4-pro")
        assert caps.reasoning_format == "deepseek"


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash"])
def test_deepseek_flash_prices_use_official_peak_estimates(model: str) -> None:
    entry = ModelCatalog().resolve_entry(model, provider="deepseek")

    assert entry.input_cost_per_mtok == pytest.approx(0.3)
    assert entry.output_cost_per_mtok == pytest.approx(1.2)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.006)


@pytest.mark.parametrize(
    ("provider", "model", "input_cost", "output_cost", "cache_read_cost"),
    [
        ("tokenrhythm", "deepseek-flash", 0.2867383512544803, 1.1469534050179212,
         0.005734767025089606),
        ("tokenrhythm", "deepseek-v4-flash", 0.14336917562724014, 0.2867383512544803,
         0.02867383512544803),
        ("openrouter", "deepseek/deepseek-v4-flash", 0.09, 0.18, 0.018),
    ],
)
def test_deepseek_official_price_corrections_do_not_replace_other_provider_rates(
    provider: str, model: str, input_cost: float, output_cost: float, cache_read_cost: float,
) -> None:
    entry = ModelCatalog().resolve_entry(model, provider=provider)

    assert entry.input_cost_per_mtok == pytest.approx(input_cost)
    assert entry.output_cost_per_mtok == pytest.approx(output_cost)
    assert entry.cache_read_cost_per_mtok == pytest.approx(cache_read_cost)


def test_openrouter_c5_models_have_offline_budgets_and_capabilities() -> None:
    catalog = ModelCatalog()
    expected = {
        "deepseek/deepseek-v4.1-flash": (1_048_576, 384_000),
        "z-ai/glm-5.3-flash": (1_310_720, 131_072),
        "qwen/qwen3.8-flash": (1_000_000, 131_072),
        "qwen/qwen3.8-max-0902": (1_000_000, 131_072),
    }

    for model, (context_window, max_tokens) in expected.items():
        assert catalog.resolve_context_window_with_source(
            model, provider="openrouter"
        ) == (context_window, "catalog")
        assert catalog.resolve_max_tokens(model, provider="openrouter") == max_tokens
        capabilities = catalog.get_capabilities(model, provider_name="openrouter")
        assert capabilities.supports_reasoning is True
        assert capabilities.supports_tools is True
        assert capabilities.supports_vision is True
        assert capabilities.reasoning_format == "openrouter"


@pytest.mark.parametrize(
    ("model", "window", "output"),
    [
        ("deepseek/deepseek-v4-flash", 1_024_000, 384_000),
        ("deepseek/deepseek-v4-flash-0731", 1_048_576, 943_718),
        ("deepseek/deepseek-v4-pro", 1_048_576, 393_216),
        ("z-ai/glm-5.2", 1_048_576, 131_072),
        ("z-ai/glm-5.1", 200_000, 128_000),
        ("moonshotai/kimi-k2.6", 262_144, 235_929),
    ],
)
def test_openrouter_public_physical_limits_are_available_offline(
    model: str, window: int, output: int,
) -> None:
    catalog = ModelCatalog()
    entry = catalog.resolve_entry(model, provider="openrouter")
    assert (entry.context_window, entry.max_output_tokens) == (window, output)
    assert catalog.resolve_context_window_with_source(model, "openrouter") == (window, "catalog")
    assert catalog.resolve_max_tokens_with_source(
        model, provider="openrouter", capacity_only=True
    ) == (output, "catalog")
    assert entry.supports_tools is True
    assert entry.supports_reasoning is True


@pytest.mark.parametrize(
    ("provider", "expected_limits"),
    [
        ("openrouter", [(1_000_000, 65_536), (1_048_576, 943_718),
                        (1_024_000, 384_000), (1_048_576, 131_072)]),
        ("tokenrhythm", [(1_000_000, 65_536), (1_000_000, 384_000),
                         (1_000_000, 384_000), (1_048_576, 131_072)]),
    ],
)
def test_recommended_router_tiers_have_known_offline_capacity_and_vision(
    provider: str, expected_limits: list[tuple[int, int]],
) -> None:
    """Cold boots preserve physical facts and safe execution limits."""
    from opensquilla.provider.preset_registry import get_preset

    catalog = ModelCatalog()
    preset = get_preset(provider)
    assert preset is not None
    for tier, (window, output) in zip(("c0", "c1", "c2", "c3"), expected_limits, strict=True):
        model = preset.tiers[tier]["model"]
        limits = catalog.resolve_deployment_limits(model, provider=provider)
        expected_request_output = (
            window // 2
            if provider == "openrouter" and output >= (window * 9) // 10
            else output
        )
        assert (limits.context_window, limits.max_output_tokens) == (
            window,
            expected_request_output,
        )
        assert limits.context_window_known is True
        assert limits.max_output_tokens_known is True
        assert catalog.resolve_context_window_with_source(model, provider) == (window, "catalog")
        assert catalog.resolve_max_tokens_with_source(model, provider=provider) == (
            expected_request_output, "catalog"
        )
        assert catalog.resolve_max_tokens_with_source(
            model, provider=provider, capacity_only=True
        ) == (
            output, "catalog"
        )
        assert catalog.resolve_vision_support(model, provider_name=provider) == (
            "supported"
            if tier == "c0" or (provider == "tokenrhythm" and tier == "c1")
            else "unsupported"
        )
        capabilities = catalog.get_capabilities(model, provider_name=provider)
        assert capabilities.supports_tools is True
        assert capabilities.supports_reasoning is (provider == "openrouter")
        assert capabilities.reasoning_format == (
            "openrouter" if provider == "openrouter" else "none"
        )


def test_openrouter_ninety_percent_completion_reserves_half_window() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "provider/large-output-model",
                "context_length": 1_048_576,
                "top_provider": {"max_completion_tokens": 943_718},
            }
        ]
    )

    assert catalog.resolve_max_tokens_with_source(
        "provider/large-output-model",
        provider="openrouter",
        capacity_only=True,
    ) == (943_718, "catalog")
    assert catalog.resolve_max_tokens(
        "provider/large-output-model", provider="openrouter"
    ) == 524_288
    assert catalog.resolve_max_tokens(
        "provider/large-output-model",
        user_override=700_000,
        provider="openrouter",
    ) == 700_000


@pytest.mark.parametrize("top_window", [None, -1, 100_000, 250_000])
def test_openrouter_live_context_respects_the_smaller_positive_top_provider_limit(
    top_window: int | None,
) -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data([{
        "id": "vendor/synthetic-model", "context_length": 200_000,
        "top_provider": {"context_length": top_window, "max_completion_tokens": 8_192},
    }])
    expected = 100_000 if top_window == 100_000 else 200_000
    assert catalog.resolve_context_window("vendor/synthetic-model", "openrouter") == expected


def test_provider_scoped_corrections_budget_outranks_snapshot_merge() -> None:
    """tokenrhythm has no models.dev table: without the provider-scoped
    corrections layer, the snapshot's cross-provider bare-id merge would
    serve the origin providers' budgets (202_752 zhipu glm-5, 262_144
    moonshot kimi) instead of the relay's own published limits. The rows
    mirror the platform listing (catalog_overrides.toml block comment);
    when the listing is reachable the boot-time live ingest supersedes
    them (see test_provider/test_live_catalog.py)."""
    catalog = ModelCatalog()

    expected_c5_budgets = {
        "deepseek-flash": (1_000_000, 384_000),
        "glm-5.3-flash": (1_048_576, 131_072),
        "qwen3.8-flash": (1_000_000, 131_072),
        "qwen3.8-max": (1_000_000, 131_072),
    }
    for model, (context_window, max_tokens) in expected_c5_budgets.items():
        assert catalog.resolve_context_window_with_source(
            model, provider="tokenrhythm"
        ) == (context_window, "catalog")
        assert (
            catalog.resolve_max_tokens(model, provider="tokenrhythm")
            == max_tokens
        )

    assert catalog.resolve_context_window_with_source(
        "deepseek-v4-flash", provider="tokenrhythm"
    ) == (1_000_000, "catalog")
    assert catalog.resolve_max_tokens("deepseek-v4-flash", provider="tokenrhythm") == 384_000
    assert catalog.resolve_context_window_with_source(
        "deepseek-v4-flash-0731", provider="tokenrhythm"
    ) == (1_000_000, "catalog")
    assert (
        catalog.resolve_max_tokens(
            "deepseek-v4-flash-0731", provider="tokenrhythm"
        )
        == 384_000
    )
    assert catalog.resolve_context_window_with_source(
        "qwen3.7-flash", provider="tokenrhythm"
    ) == (1_000_000, "catalog")
    assert catalog.resolve_max_tokens("qwen3.7-flash", provider="tokenrhythm") == 65_536
    qwen_flash_caps = catalog.get_capabilities(
        "qwen3.7-flash", provider_name="tokenrhythm"
    )
    # TokenRhythm streams reasoning content but rejects thinking-toggle
    # request fields, so the effective capability intentionally keeps the
    # injectable reasoning dialect disabled.
    assert qwen_flash_caps.supports_reasoning is False
    assert qwen_flash_caps.reasoning_format == "none"
    assert qwen_flash_caps.supports_tools is True
    assert qwen_flash_caps.supports_vision is True
    # Discriminating rows — the bare-id merge would report the origin
    # providers' windows here, not the relay's published ones.
    assert catalog.resolve_context_window("glm-5", provider="tokenrhythm") == 1_000_000
    assert catalog.resolve_context_window("kimi-k2.5", provider="tokenrhythm") == 256_000
    assert catalog.resolve_context_window("kimi-k2.7-code", provider="tokenrhythm") == 256_000
    assert catalog.resolve_max_tokens("kimi-k2.7-code", provider="tokenrhythm") == 16_000
    assert catalog.resolve_context_window("qwen3.7-max", provider="tokenrhythm") == 1_000_000
    assert catalog.resolve_max_tokens("qwen3.7-max", provider="tokenrhythm") == 131_072
    # The correction is scoped to TokenRhythm. Direct/provider-less snapshot
    # routes retain their own 65,536-token output limit.
    assert catalog.resolve_max_tokens("qwen3.7-max") == 65_536
    assert catalog.resolve_max_tokens("qwen3.7-max", provider="dashscope") == 65_536
    assert catalog.resolve_max_tokens("qwen3.7-max", provider="openrouter") == 65_536
    # The same bare id on direct DeepSeek keeps its own snapshot-table
    # budgets — the provider-scoped layer never leaks across providers.
    assert catalog.resolve_context_window("deepseek-v4-flash", "deepseek") == 1_000_000


def test_tokenrhythm_v4_flash_0731_offline_metadata_is_exact_and_scoped() -> None:
    catalog = ModelCatalog()

    entry = catalog.resolve_entry("deepseek-v4-flash-0731", provider="tokenrhythm")
    assert entry.context_window == 1_000_000
    assert entry.max_output_tokens == 384_000
    assert entry.supports_reasoning is True
    assert entry.supports_tools is True
    assert entry.supports_vision is False
    assert entry.reasoning_format == "none"
    assert entry.status == "testing"
    assert entry.input_cost_per_mtok == pytest.approx(0.14336917562724014)
    assert entry.output_cost_per_mtok == pytest.approx(0.2867383512544803)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.002867383512544803)
    assert catalog.resolve_context_window(
        "deepseek-v4-flash-0731", provider="tokenrhythm"
    ) == 1_000_000
    assert catalog.resolve_max_tokens(
        "deepseek-v4-flash-0731", provider="tokenrhythm"
    ) == 384_000

    direct = catalog.resolve_entry("deepseek-v4-flash-0731", provider="deepseek")
    assert direct.input_cost_per_mtok is None
    assert direct.output_cost_per_mtok is None
    assert direct.cache_read_cost_per_mtok is None


def test_tokenrhythm_v4_pro_0813_offline_metadata_is_exact_and_scoped() -> None:
    catalog = ModelCatalog()

    entry = catalog.resolve_entry("deepseek-v4-pro-0813", provider="tokenrhythm")
    assert entry.context_window == 1_000_000
    assert entry.max_output_tokens == 384_000
    assert entry.supports_reasoning is True
    assert entry.supports_tools is True
    assert entry.supports_vision is False
    assert entry.reasoning_format == "none"
    assert entry.input_cost_per_mtok == pytest.approx(1.2903225806451613)
    assert entry.output_cost_per_mtok == pytest.approx(3.870967741935484)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.043010752688172046)

    direct = catalog.resolve_entry("deepseek-v4-pro-0813", provider="deepseek")
    assert direct.input_cost_per_mtok is None
    assert direct.output_cost_per_mtok is None
    assert direct.cache_read_cost_per_mtok is None


def test_direct_profile_windows_resolve_from_models_dev_snapshot() -> None:
    catalog = ModelCatalog()

    expected_windows = {
        "gpt-5.4-nano": 400_000,
        "gpt-5.4-mini": 400_000,
        "gpt-5.5": 1_050_000,
        "glm-4.7-flashx": 200_000,
        # Real budget (202k) instead of the former 80k conservative placeholder.
        "glm-5": 202_752,
        "glm-5.1": 200_000,
        "z-ai/glm-5.2": 1_000_000,
        "moonshot-v1-8k": 8_192,
        "moonshot-v1-128k": 131_072,
        "kimi-k2.5": 262_144,
        "kimi-k2.6": 262_144,
    }

    for model_id, context_window in expected_windows.items():
        assert catalog.resolve_context_window(model_id) == context_window
        max_tokens = catalog.resolve_max_tokens(model_id)
        assert max_tokens > 0
        assert max_tokens <= context_window


def test_corrections_budget_fallback_is_provider_agnostic_by_basename() -> None:
    # The retired static table's budget slot now resolves from the packaged
    # corrections rows, keyed PROVIDER-AGNOSTICALLY by basename. The moonshot
    # window rows carry the exact values the static table did.
    assert _corrections_budget_fallback("moonshot-v1-8k") == (8_192, 8_192)
    assert _corrections_budget_fallback("moonshot-v1-32k") == (32_768, 32_768)
    assert _corrections_budget_fallback("moonshot-v1-128k") == (131_072, 131_072)
    # The vendor-qualified router-tier rows the snapshot only knows by their
    # slash id resolve for the bare basename too (grok's static tuple was
    # DEFAULT_MAX_TOKENS output / 1M window).
    assert _corrections_budget_fallback("grok-4.3") == (16_384, 1_000_000)
    assert _corrections_budget_fallback("step-3.5-flash") == (16_384, 256_000)
    # Glob capability-ladder rows are never consulted for budgets, and an
    # unknown basename yields None.
    assert _corrections_budget_fallback("model-nobody-knows") is None


def test_corrections_budget_qualified_and_unqualified_resolve_identically() -> None:
    catalog = ModelCatalog()
    for qualified, bare in (
        ("z-ai/glm-5", "glm-5"),
        ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ("moonshot/moonshot-v1-8k", "moonshot-v1-8k"),
    ):
        assert catalog.resolve_context_window(qualified) == catalog.resolve_context_window(bare)
        assert catalog.resolve_max_tokens(qualified) == catalog.resolve_max_tokens(bare)


# Retirement parity net for the 25 keys of the deleted static fallback table.
# Expectations are LITERALS captured from the pre-change tree (both resolvers
# run against staging/provider-overhaul before the table was removed), for
# the two call shapes production uses: no provider (router-decision path)
# and the key's natural provider (turn-runner path). Any drift here means
# the retirement changed a resolution the static table used to decide.
#
# key → (natural provider,
#        (max_tokens, context_window) with provider="",
#        (max_tokens, context_window) with the natural provider)
_STATIC_RETIREMENT_PARITY: dict[str, tuple[str, tuple[int, int], tuple[int, int]]] = {
    "claude-opus-4.8": ("anthropic", (128_000, 1_000_000), (128_000, 1_000_000)),
    "claude-sonnet-4.6": ("anthropic", (128_000, 1_000_000), (128_000, 1_000_000)),
    "gemini-3.5-flash": ("gemini", (65_536, 1_048_576), (65_536, 1_048_576)),
    "gpt-5.4-nano": ("openai", (128_000, 400_000), (128_000, 400_000)),
    "gpt-5.4-mini": ("openai", (128_000, 400_000), (128_000, 400_000)),
    "gpt-5.5": ("openai", (128_000, 1_050_000), (128_000, 1_050_000)),
    "qwen3-coder-plus": ("dashscope", (65_536, 1_048_576), (65_536, 1_048_576)),
    "grok-4.3": ("xai", (16_384, 1_000_000), (16_384, 1_000_000)),
    "glm-4.5-air": ("zhipu", (98_304, 131_072), (98_304, 131_072)),
    "glm-4.6": ("zhipu", (131_072, 204_800), (131_072, 204_800)),
    "glm-4.7-flashx": ("zhipu", (131_072, 200_000), (131_072, 200_000)),
    "glm-5": ("zhipu", (16_384, 202_752), (131_072, 204_800)),
    "glm-5.1": ("zhipu", (128_000, 200_000), (131_072, 200_000)),
    "glm-5.2": ("zhipu", (128_000, 1_000_000), (131_072, 1_000_000)),
    "minimax-m2.5": ("minimax", (131_072, 204_800), (131_072, 204_800)),
    "minimax-m2.7": ("minimax", (131_072, 204_800), (131_072, 204_800)),
    "step-3.5-flash": ("stepfun", (16_384, 256_000), (16_384, 256_000)),
    "deepseek-v4-flash": ("deepseek", (384_000, 1_000_000), (384_000, 1_000_000)),
    "deepseek-v4-pro": ("deepseek", (384_000, 1_000_000), (384_000, 1_000_000)),
    "deepseek-v3.2": ("deepseek", (8_192, 128_000), (8_192, 128_000)),
    "moonshot-v1-8k": ("moonshot", (8_192, 8_192), (8_192, 8_192)),
    "moonshot-v1-32k": ("moonshot", (8_192, 32_768), (8_192, 32_768)),
    "moonshot-v1-128k": ("moonshot", (8_192, 131_072), (8_192, 131_072)),
    "kimi-k2.5": ("moonshot", (32_768, 262_144), (8_192, 262_144)),
    "kimi-k2.6": ("moonshot", (16_384, 262_144), (8_192, 262_144)),
}


def test_static_table_retirement_keeps_all_25_key_resolutions_identical() -> None:
    assert len(_STATIC_RETIREMENT_PARITY) == 25
    catalog = ModelCatalog()
    for model, (natural, bare_expected, natural_expected) in _STATIC_RETIREMENT_PARITY.items():
        observed_bare = (
            catalog.resolve_max_tokens(model),
            catalog.resolve_context_window(model),
        )
        assert observed_bare == bare_expected, (model, "")
        observed_natural = (
            catalog.resolve_max_tokens(model, provider=natural),
            catalog.resolve_context_window(model, natural),
        )
        assert observed_natural == natural_expected, (model, natural)


def test_populate_from_data_parses_openrouter_pricing() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/priced-model",
                "context_length": 100_000,
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            },
            {"id": "vendor/free-model", "context_length": 8_192},
            {"id": "vendor/bad-pricing", "pricing": {"prompt": "n/a", "completion": None}},
        ]
    )

    priced = catalog.get("vendor/priced-model")
    assert priced is not None
    assert priced.input_cost_per_1k == pytest.approx(0.0025)
    assert priced.output_cost_per_1k == pytest.approx(0.01)
    # Missing pricing block → 0.0
    assert catalog.get("vendor/free-model").input_cost_per_1k == 0.0
    # Non-numeric / None → 0.0 (no crash)
    bad = catalog.get("vendor/bad-pricing")
    assert bad.input_cost_per_1k == 0.0
    assert bad.output_cost_per_1k == 0.0


def test_openrouter_near_context_completion_window_uses_safe_default() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "provider/vision-model",
                "context_length": 262_144,
                "top_provider": {"max_completion_tokens": 262_142},
            }
        ]
    )

    assert catalog.resolve_context_window("provider/vision-model") == 262_144
    assert catalog.resolve_max_tokens("provider/vision-model") == 8192


def test_openrouter_safe_default_never_raises_smaller_provider_limit() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "provider/smaller-output-model",
                "context_length": 12_000,
                "top_provider": {"max_completion_tokens": 4096},
            }
        ]
    )

    assert catalog.resolve_context_window("provider/smaller-output-model") == 12_000
    assert catalog.resolve_max_tokens("provider/smaller-output-model") == 4096


def _catalog_with_live_reasoning_model() -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/reasoning-model",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 128_000},
                "supported_parameters": ["reasoning", "tools", "tool_choice"],
                "architecture": {"input_modalities": ["text", "image"]},
            }
        ]
    )
    return catalog


def test_get_capabilities_honors_user_reasoning_override() -> None:
    catalog = _catalog_with_live_reasoning_model()
    catalog.set_user_overrides(
        {
            "vendor/reasoning-model": {
                "supports_reasoning": False,
                "reasoning_format": "none",
            }
        }
    )

    caps = catalog.get_capabilities("vendor/reasoning-model", provider_name="openrouter")

    assert caps.supports_reasoning is False
    assert caps.reasoning_format == "none"


def test_get_capabilities_honors_user_vision_override_on_live_reasoning_model() -> None:
    catalog = _catalog_with_live_reasoning_model()
    catalog.set_user_overrides({"vendor/reasoning-model": {"supports_vision": False}})

    caps = catalog.get_capabilities("vendor/reasoning-model", provider_name="openrouter")

    assert caps.supports_reasoning is True
    assert caps.supports_vision is False
    assert catalog.resolve_vision_support(
        "vendor/reasoning-model",
        provider_name="openrouter",
    ) == "unsupported"


def test_vision_support_distinguishes_live_evidence_from_synthesized_default() -> None:
    catalog = _catalog_with_live_reasoning_model()

    assert catalog.resolve_vision_support(
        "vendor/reasoning-model",
        provider_name="openrouter",
    ) == "supported"
    assert catalog.resolve_vision_support(
        "vendor/unknown-model",
        provider_name="openrouter",
    ) == "unknown"
    assert catalog.resolve_vision_support(
        "vendor/reasoning-model",
        provider_name="synthetic-provider",
    ) == "unknown"


@pytest.mark.parametrize(
    ("architecture", "expected"),
    [
        ({}, "unknown"),
        ({"input_modalities": None}, "unknown"),
        ({"input_modalities": []}, "unknown"),
        ({"input_modalities": "text"}, "unknown"),
        ({"input_modalities": [None]}, "unknown"),
        ({"input_modalities": [""]}, "unknown"),
        ({"input_modalities": ["text"]}, "unsupported"),
        ({"input_modalities": ["text", "image"]}, "supported"),
        ({"input_modalities": [" TEXT ", " IMAGE "]}, "supported"),
        ({"output_modalities": ["image"]}, "unknown"),
    ],
)
def test_live_vision_requires_explicit_input_modality_evidence(
    architecture: dict, expected: str,
) -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [{"id": "synthetic/capability-check", "architecture": architecture}]
    )

    assert catalog.resolve_deployment_vision_support(
        "synthetic/capability-check", provider="openrouter"
    ) == expected


def test_missing_live_vision_does_not_mask_known_catalog_input_capability() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data([{"id": "synthetic/catalog-vision"}])
    with patch(
        "opensquilla.provider.model_catalog._snapshot_layer_fields",
        return_value={"supports_vision": True},
    ):
        assert catalog.resolve_vision_support(
            "synthetic/catalog-vision", provider_name="openrouter"
        ) == "supported"


@pytest.mark.parametrize(
    ("reasoning_format", "supports_reasoning"),
    [("deepseek", True), ("none", False)],
)
def test_get_capabilities_honors_user_reasoning_format_override_on_live_model(
    reasoning_format: str,
    supports_reasoning: bool,
) -> None:
    catalog = _catalog_with_live_reasoning_model()
    catalog.set_user_overrides(
        {"vendor/reasoning-model": {"reasoning_format": reasoning_format}}
    )

    caps = catalog.get_capabilities("vendor/reasoning-model", provider_name="openrouter")

    assert caps.supports_reasoning is supports_reasoning
    assert caps.reasoning_format == reasoning_format


def test_resolve_context_window_honors_user_override() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides({"vllm/self-hosted-model": {"context_window": 131_072}})

    window, source = catalog.resolve_context_window_with_source("self-hosted-model", "vllm")

    assert window == 131_072
    assert source == "override"


def test_resolve_max_tokens_honors_user_override() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "vllm/self-hosted-model": {
                "context_window": 131_072,
                "max_output_tokens": 32_768,
            }
        }
    )

    effective, source = catalog.resolve_max_tokens_with_source(
        "self-hosted-model", provider="vllm"
    )

    assert effective == 32_768
    assert source == "override"


@pytest.mark.asyncio
async def test_fetch_openrouter_adds_app_attribution_headers() -> None:
    captured: dict[str, object] = {}
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "context_length": 128_000,
                "top_provider": {"max_completion_tokens": 16_384},
            }
        ]
    }

    with patch("opensquilla.provider.model_catalog.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def capture_get(url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return mock_response

        mock_client.get = AsyncMock(side_effect=capture_get)
        mock_client_cls.return_value = mock_client

        catalog = ModelCatalog()
        await catalog.fetch_openrouter(api_key="test-key", base_url="https://openrouter.ai/api")

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }
    model = catalog.get("openai/gpt-4o")
    assert model is not None
    assert model.context_window == 128_000


def test_local_provider_context_window_uses_runtime_default_not_cloud() -> None:
    from opensquilla.provider.model_catalog import (
        _LOCAL_CONTEXT_WINDOW,
        DEFAULT_CONTEXT_WINDOW,
    )

    catalog = ModelCatalog()
    # Without provider context a bare ollama id falls to the 200k cloud default.
    assert catalog.resolve_context_window("qwen3:4b") == DEFAULT_CONTEXT_WINDOW
    # With the local provider it reports the runtime window instead.
    assert catalog.resolve_context_window("qwen3:4b", provider="ollama") == _LOCAL_CONTEXT_WINDOW
    assert _LOCAL_CONTEXT_WINDOW < DEFAULT_CONTEXT_WINDOW


def test_local_provider_max_tokens_clamped_to_local_window() -> None:
    from opensquilla.provider.model_catalog import _LOCAL_CONTEXT_WINDOW

    catalog = ModelCatalog()
    # max_tokens cannot exceed the (smaller) local context window.
    assert catalog.resolve_max_tokens("llama3.2:3b", provider="ollama") <= _LOCAL_CONTEXT_WINDOW


def test_cloud_provider_context_window_unchanged() -> None:
    from opensquilla.provider.model_catalog import DEFAULT_CONTEXT_WINDOW

    catalog = ModelCatalog()
    # An unknown cloud model id is unaffected by the provider argument.
    assert catalog.resolve_context_window("some-cloud-model", provider="openai") == (
        DEFAULT_CONTEXT_WINDOW
    )
