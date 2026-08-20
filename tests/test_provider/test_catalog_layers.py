"""Layered model-catalog resolution (ModelCatalog.resolve_entry).

Covers per-field authority merging (user > live > corrections > snapshot >
synthesized), glob corrections matching, per-1k → per-Mtok cost adaptation,
cold-instance resolution, override validation — plus a parity net asserting
the legacy resolve paths still return today's exact values.
"""

from __future__ import annotations

import tomllib
from importlib import resources

import pytest

from opensquilla.provider import model_catalog as model_catalog_module
from opensquilla.provider.catalog_types import ModelCatalogEntry
from opensquilla.provider.model_catalog import (
    ModelCatalog,
    resolve_effective_context_window,
)


def _install_corrections(monkeypatch: pytest.MonkeyPatch, toml_text: str) -> None:
    """Route the corrections layer at parsed TOML text (packaged file is empty)."""
    tables = model_catalog_module._normalize_corrections(tomllib.loads(toml_text))
    monkeypatch.setattr(model_catalog_module, "_corrections_tables", lambda: tables)


# ---------------------------------------------------------------------------
# Cold instance — a bare ModelCatalog() with no network warmup must resolve
# through corrections + snapshot + synthesized (ensemble.py builds bare
# instances per member).
# ---------------------------------------------------------------------------


def test_cold_instance_resolves_snapshot_known_model() -> None:
    entry = ModelCatalog().resolve_entry("gpt-5.5", provider="openai")

    assert entry.source == "snapshot"
    assert entry.provider_id == "openai"
    assert entry.model_id == "gpt-5.5"
    assert entry.context_window == 1_050_000
    assert entry.max_output_tokens == 128_000
    assert entry.supports_reasoning is True
    assert entry.supports_tools is True
    assert entry.supports_vision is True
    # The snapshot never knows the streaming dialect.
    assert entry.reasoning_format == "none"
    # The snapshot vendors models.dev cost keys verbatim.
    assert entry.input_cost_per_mtok == pytest.approx(5.0)
    assert entry.output_cost_per_mtok == pytest.approx(30.0)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.5)


def test_cold_instance_synthesizes_unknown_model() -> None:
    entry = ModelCatalog().resolve_entry("model-that-does-not-exist-anywhere")

    assert entry.source == "synthesized"
    assert entry.context_window == 32_768
    assert entry.max_output_tokens == 8_192
    assert entry.supports_tools is True
    assert entry.supports_reasoning is False
    assert entry.supports_vision is False
    assert entry.input_cost_per_mtok is None
    assert entry.output_cost_per_mtok is None
    assert entry.quality_prior is None


def test_artifact_tool_capability_requires_an_explicit_catalog_fact() -> None:
    catalog = ModelCatalog()
    assert not catalog.tool_capability_is_verified(
        "unknown-writer-model",
        provider_name="custom",
    )

    catalog.set_user_overrides(
        {
            "custom/unknown-writer-model": {
                "supports_tools": True,
            }
        }
    )

    assert catalog.tool_capability_is_verified(
        "unknown-writer-model",
        provider_name="custom",
    )


# ---------------------------------------------------------------------------
# Per-field authority merging
# ---------------------------------------------------------------------------


def _populated_catalog() -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/model-x",
                "name": "Vendor Model X",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 30_000},
                "supported_parameters": ["tools", "reasoning"],
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            }
        ]
    )
    return catalog


def test_user_overrides_beat_live_per_field() -> None:
    catalog = _populated_catalog()
    catalog.set_user_overrides(
        {"openrouter/vendor/model-x": {"context_window": 111, "quality_prior": 0.9}}
    )

    entry = catalog.resolve_entry("vendor/model-x", provider="openrouter")

    assert entry.source == "user"
    # User-set fields win outright…
    assert entry.context_window == 111
    assert entry.quality_prior == 0.9
    # …while the live layer still fills everything the user left unset.
    assert entry.max_output_tokens == 30_000
    assert entry.display_name == "Vendor Model X"
    assert entry.supports_reasoning is True
    assert entry.supports_vision is True
    assert entry.reasoning_format == "openrouter"


def test_live_beats_corrections_and_corrections_fill_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "vendor/model-y",
                "context_length": 100_000,
                "top_provider": {"max_completion_tokens": 2_048},
                "supported_parameters": ["tools"],
            }
        ]
    )
    _install_corrections(
        monkeypatch,
        """
        [openrouter."vendor/model-y"]
        max_output_tokens = 9999
        supports_reasoning = true
        reasoning_format = "deepseek"
        quality_prior = 0.5
        """,
    )

    entry = catalog.resolve_entry("vendor/model-y", provider="openrouter")

    assert entry.source == "live"
    # Live knows these — the correction must NOT override them…
    assert entry.max_output_tokens == 2_048
    assert entry.supports_reasoning is False
    # …but fields live left unset are filled from corrections.
    assert entry.reasoning_format == "deepseek"
    assert entry.quality_prior == 0.5


def test_corrections_beat_snapshot_and_snapshot_fills_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_corrections(
        monkeypatch,
        """
        [openai."gpt-4"]
        context_window = 7777
        supports_vision = true
        """,
    )

    entry = ModelCatalog().resolve_entry("gpt-4", provider="openai")

    assert entry.source == "corrections"
    assert entry.context_window == 7_777  # corrections beat snapshot's 8192
    assert entry.supports_vision is True  # corrections beat snapshot's False
    assert entry.max_output_tokens == 8_192  # snapshot fills unset fields
    assert entry.supports_tools is True
    assert entry.supports_reasoning is False


def test_snapshot_beats_synthesized_but_floor_fills_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A correction that carries only metadata still gets the synthesized
    # budget floor for the numeric fields nothing knows.
    _install_corrections(
        monkeypatch,
        """
        [zhipu."glm-9-experimental"]
        family = "glm"
        """,
    )

    entry = ModelCatalog().resolve_entry("glm-9-experimental", provider="zhipu")

    assert entry.source == "corrections"
    assert entry.family == "glm"
    assert entry.context_window == 32_768
    assert entry.max_output_tokens == 8_192


# ---------------------------------------------------------------------------
# Glob corrections matching
# ---------------------------------------------------------------------------


def test_glob_corrections_match_lowercased_model_and_exact_key_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_corrections(
        monkeypatch,
        """
        [zhipu."glm-9-pro"]
        display_name = "Exact GLM-9 Pro"
        context_window = 123456

        [zhipu."glm-9*"]
        display_name = "Glob GLM-9"
        max_output_tokens = 4242
        supports_reasoning = true
        """,
    )
    catalog = ModelCatalog()

    # Uppercase spelling still matches: model ids are lowercased for lookup.
    exact = catalog.resolve_entry("GLM-9-Pro", provider="zhipu")
    assert exact.display_name == "Exact GLM-9 Pro"  # exact key beats the glob
    assert exact.context_window == 123_456
    assert exact.max_output_tokens == 4_242  # glob fills what exact left unset
    assert exact.supports_reasoning is True

    globbed = catalog.resolve_entry("glm-9-mini", provider="zhipu")
    assert globbed.source == "corrections"
    assert globbed.display_name == "Glob GLM-9"
    assert globbed.context_window == 32_768  # synthesized floor fills the rest

    # Corrections are provider-scoped: same model id, other provider → none.
    other = catalog.resolve_entry("glm-9-mini", provider="openai")
    assert other.source == "synthesized"
    assert other.display_name == ""


def test_corrections_drop_unknown_and_mistyped_fields() -> None:
    tables = model_catalog_module._normalize_corrections(
        {
            "vendor": {
                "model-z": {
                    "context_window": 5_000,
                    "supports_reasoning": "true",  # string, not bool → dropped
                    "bogus_field": 1,  # unknown → dropped
                }
            }
        }
    )

    assert tables == {"vendor": {"model-z": {"context_window": 5_000}}}


def test_packaged_corrections_file_parses_with_expected_tables() -> None:
    text = (
        resources.files("opensquilla.provider")
        .joinpath("catalog_overrides.toml")
        .read_text(encoding="utf-8")
    )
    payload = tomllib.loads(text)
    # Window/pricing corrections (retired static tables) + the transcribed
    # capability ladder (see the "capability ladder migration" section).
    assert set(payload) == {
        "moonshot",
        "anthropic",
        "openrouter",
        "dashscope",
        "volcengine",
        "volcengine_coding_plan",
        "byteplus",
        "deepseek",
        "gemini",
        "zhipu",
        "qianfan",
        "tencent_tokenhub",
        "tencent_token_plan",
        "qwen_token_plan",
        "tokenrhythm",
        "kimi_coding_openai",
        "kimi_coding_anthropic",
        "minimax",
        "minimax_cn",
        "minimax_global",
        "minimax_openai",
        "minimax_coding_openai",
        "minimax_coding_anthropic",
        "mimo_openai",
        "mimo_anthropic",
    }
    assert set(payload["moonshot"]) == {
        "moonshot-v1-8k",
        "moonshot-v1-32k",
        "moonshot-v1-128k",
        "kimi-k2.5*",
        "kimi-k2.6*",
        "kimi-k2.7*",
        "kimi-k2-thinking*",
        "*",
    }
    assert set(payload["anthropic"]) == {
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    }
    assert set(payload["openrouter"]) == {
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-4.6",
        "x-ai/grok-4.3",
        "stepfun/step-3.5-flash",
    }
    # Every packaged row survives normalization — no unknown field names,
    # no mistyped values (a dropped field would silently weaken a layer).
    tables = model_catalog_module._normalize_corrections(payload)
    assert {p: set(t) for p, t in tables.items()} == {p: set(t) for p, t in payload.items()}
    assert all(fields for table in tables.values() for fields in table.values())


def test_ladder_glob_rows_keep_specific_before_general_file_order() -> None:
    """FILE ORDER of the transcribed ladder rows is load-bearing.

    The corrections loader matches globs in file order, each filling only
    fields still unset — so a more specific prefix must appear before the
    general one it overlaps (qwen3.5* before qwen3*, doubao-seed-1-6*
    before *thinking*) and the "*" catch-all must be the LAST key of every
    ladder provider table. Reordering rows would silently change matching.
    """
    payload = tomllib.loads(
        resources.files("opensquilla.provider")
        .joinpath("catalog_overrides.toml")
        .read_text(encoding="utf-8")
    )
    for provider in (
        "dashscope",
        "moonshot",
        "volcengine",
        "byteplus",
        "gemini",
        "zhipu",
        "tencent_tokenhub",
        "tencent_token_plan",
        "tokenrhythm",
    ):
        keys = list(payload[provider])
        assert keys[-1] == "*", provider
        assert keys.count("*") == 1, provider
    dashscope = list(payload["dashscope"])
    assert dashscope.index("qwen3.5*") < dashscope.index("qwen3*")
    assert dashscope.index("qwen3.6*") < dashscope.index("qwen3*")
    volcengine = list(payload["volcengine"])
    assert volcengine.index("doubao-seed-1-6*") < volcengine.index("*thinking*")
    byteplus = list(payload["byteplus"])
    assert byteplus.index("kimi-k2-*") < byteplus.index("*thinking*")
    # deepseek is a single catch-all (reasoning_shape transcription).
    assert list(payload["deepseek"]) == ["*"]


# ---------------------------------------------------------------------------
# Packaged corrections data rows (windows/pricing from in-repo static tables)
# ---------------------------------------------------------------------------


def test_moonshot_v1_windows_resolve_from_packaged_corrections() -> None:
    # moonshot-v1-* is absent from the snapshot's moonshot table, so without
    # these rows resolve_entry would fall to the synthesized floor (32k/8k).
    # The corrections rows are the canonical source of these windows (the
    # in-repo static fallback table they were copied from has been retired);
    # the literals below pin the retired table's exact values.
    catalog = ModelCatalog()
    expected_windows = {
        "moonshot-v1-8k": (8_192, 8_192),
        "moonshot-v1-32k": (32_768, 32_768),
        "moonshot-v1-128k": (131_072, 131_072),
    }
    for model, (max_output, context_window) in expected_windows.items():
        entry = catalog.resolve_entry(model, provider="moonshot")
        assert entry.source == "corrections", model
        assert entry.context_window == context_window, model
        assert entry.max_output_tokens == max_output, model
    # The 8k SKU is 8192/8192, not the synthesized 32k/8k.
    eight_k = catalog.resolve_entry("moonshot-v1-8k", provider="moonshot")
    assert (eight_k.max_output_tokens, eight_k.context_window) == (8_192, 8_192)


def test_anthropic_listing_models_priced_via_packaged_corrections() -> None:
    # The corrections rows are the canonical metadata for the SKUs the
    # Anthropic adapter lists (the adapter's _KNOWN_MODELS table has been
    # retired; list_models resolves these rows through the shared catalog).
    # Values are verified against Anthropic's published per-model pricing
    # page (input/output per Mtok). The opus-4-6 and sonnet-4-6 rows carry
    # no cache pricing, so their cache_read/write fall through to the
    # snapshot's vendored models.dev cache rates for the same SKU; the
    # haiku-4-5 row now carries its own cache rates directly.
    listing_rows = (
        ("claude-opus-4-6", "Claude Opus 4.6", 200_000, 32_000, 5.0, 25.0, 0.5, 6.25),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", 200_000, 16_000, 3.0, 15.0, 0.3, 3.75),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 200_000, 8_192, 1.0, 5.0, 0.1, 1.25),
    )
    catalog = ModelCatalog()
    for (
        model_id,
        display_name,
        context_window,
        max_output,
        in_mtok,
        out_mtok,
        cr_mtok,
        cw_mtok,
    ) in listing_rows:
        entry = catalog.resolve_entry(model_id, provider="anthropic")
        assert entry.source == "corrections", model_id
        assert entry.display_name == display_name, model_id
        # Windows come from the same rows; corrections beat the snapshot.
        assert entry.context_window == context_window, model_id
        assert entry.max_output_tokens == max_output, model_id
        assert entry.input_cost_per_mtok == pytest.approx(in_mtok)
        assert entry.output_cost_per_mtok == pytest.approx(out_mtok)
        # The retired table carried no cache pricing — corrections stays
        # silent on it, so the snapshot's vendored rate fills the gap.
        assert entry.cache_read_cost_per_mtok == pytest.approx(cr_mtok)
        assert entry.cache_write_cost_per_mtok == pytest.approx(cw_mtok)


# ---------------------------------------------------------------------------
# Live-layer cost adaptation (per-1k cache → per-Mtok canonical)
# ---------------------------------------------------------------------------


def test_live_costs_adapt_per_1k_to_per_mtok() -> None:
    entry = _populated_catalog().resolve_entry("vendor/model-x", provider="openrouter")

    # OpenRouter per-token "0.0000025" → cached 0.0025/1k → canonical 2.5/Mtok.
    assert entry.input_cost_per_mtok == pytest.approx(2.5)
    assert entry.output_cost_per_mtok == pytest.approx(10.0)


def test_live_zero_price_stays_unknown_not_free() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_data([{"id": "vendor/free-model", "context_length": 8_192}])

    entry = catalog.resolve_entry("vendor/free-model", provider="openrouter")

    # The live cache's 0.0 means "free or unknown" — never claim a known $0.
    assert entry.input_cost_per_mtok is None
    assert entry.output_cost_per_mtok is None


# ---------------------------------------------------------------------------
# User overrides — keying and validation
# ---------------------------------------------------------------------------


def test_user_override_qualified_key_beats_bare_key_per_field() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "deepseek/some-model": {"context_window": 1_000},
            "Some-Model": {"context_window": 2_000, "max_output_tokens": 500},
        }
    )

    qualified = catalog.resolve_entry("some-model", provider="deepseek")
    assert qualified.context_window == 1_000  # qualified key wins the field
    assert qualified.max_output_tokens == 500  # bare key fills what it left unset

    bare = catalog.resolve_entry("some-model")
    assert bare.context_window == 2_000


def test_set_user_overrides_rejects_unknown_fields_and_bad_types() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides({"kept-model": {"context_window": 4_096}})

    with pytest.raises(ValueError, match="bogus_field"):
        catalog.set_user_overrides({"m": {"bogus_field": 1}})
    with pytest.raises(ValueError, match="context_window"):
        catalog.set_user_overrides({"m": {"context_window": "big"}})
    with pytest.raises(ValueError, match="supports_tools"):
        catalog.set_user_overrides({"m": {"supports_tools": "false"}})
    # Identity / derived fields are not overridable.
    with pytest.raises(ValueError, match="source"):
        catalog.set_user_overrides({"m": {"source": "user"}})

    # A rejected replacement leaves the previous overrides installed.
    assert catalog.resolve_entry("kept-model").context_window == 4_096


def test_explicit_false_and_zero_overrides_win() -> None:
    catalog = _populated_catalog()
    catalog.set_user_overrides(
        {"vendor/model-x": {"supports_reasoning": False, "max_output_tokens": 0}}
    )

    entry = catalog.resolve_entry("vendor/model-x", provider="openrouter")

    # Presence in a layer = knowledge: False/0 beat the live layer's values.
    assert entry.supports_reasoning is False
    assert entry.max_output_tokens == 0


def test_context_window_override_governs_legacy_resolver_with_source() -> None:
    # The [models.*] user-override layer is the top layer of the legacy
    # resolve_context_window chain too, attributed as "override"; models
    # without an override keep resolving through live/snapshot/corrections
    # exactly as the parity net below pins.
    catalog = _populated_catalog()
    catalog.set_user_overrides({"openrouter/vendor/model-x": {"context_window": 111}})

    assert catalog.resolve_context_window_with_source(
        "vendor/model-x", "openrouter"
    ) == (111, "override")
    assert catalog.resolve_context_window("vendor/model-x", "openrouter") == 111
    assert catalog.user_context_window_override("vendor/model-x", "openrouter") == 111
    # Other models are untouched by the installed override.
    assert catalog.resolve_context_window_with_source("gpt-5.5", "openai") == (
        1_050_000,
        "catalog",
    )
    assert catalog.user_context_window_override("gpt-5.5", "openai") is None


def test_context_window_override_zero_is_not_a_budgeting_window() -> None:
    # A 0 override wins per-field in resolve_entry (presence = knowledge) but
    # cannot serve as a budgeting window, so the legacy chain resolves past it.
    catalog = _populated_catalog()
    catalog.set_user_overrides({"openrouter/vendor/model-x": {"context_window": 0}})

    assert catalog.user_context_window_override("vendor/model-x", "openrouter") is None
    assert catalog.resolve_context_window_with_source("vendor/model-x", "openrouter") == (
        200_000,
        "catalog",
    )


def test_resolve_effective_context_window_layers() -> None:
    # Shared implementation of "[models.*] override > global config window >
    # catalog > default" used by the engine budgeting and RPC paths.
    catalog = _populated_catalog()
    catalog.set_user_overrides({"openrouter/vendor/model-x": {"context_window": 111}})

    assert resolve_effective_context_window(
        catalog, "vendor/model-x", provider="openrouter", global_override=999_000
    ) == (111, "override")
    assert resolve_effective_context_window(
        catalog, "gpt-5.5", provider="openai", global_override=999_000
    ) == (999_000, "config")
    assert resolve_effective_context_window(catalog, "gpt-5.5", provider="openai") == (
        1_050_000,
        "catalog",
    )
    # Junk/non-positive global values count as unset.
    assert resolve_effective_context_window(
        catalog, "gpt-5.5", provider="openai", global_override=-5
    ) == (1_050_000, "catalog")


def test_resolve_effective_context_window_duck_types_plain_catalogs() -> None:
    class _PlainCatalog:
        def resolve_context_window(self, model_id: str, provider: str = "") -> int:
            return 42_000

    # Without *_with_source the value is attributed "catalog" — never
    # "override" — so the global config value still applies over it.
    assert resolve_effective_context_window(_PlainCatalog(), "m") == (42_000, "catalog")
    assert resolve_effective_context_window(_PlainCatalog(), "m", global_override=50_000) == (
        50_000,
        "config",
    )


# ---------------------------------------------------------------------------
# Snapshot-layer cost emission (compact in/out/cr/cw_mtok keys → per-Mtok
# fields). The committed snapshot carries no cost keys yet, so these drive
# the layer through a synthetic entry.
# ---------------------------------------------------------------------------


def test_snapshot_cost_keys_emit_per_mtok_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_catalog_module,
        "_models_dev_model",
        lambda provider_id, model_id: {
            "ctx": 200_000,
            "out": 32_000,
            "reasoning": True,
            "tools": True,
            "vision": False,
            "in_mtok": 2.5,
            "out_mtok": 10,  # int leaf — coerced to float
            "cr_mtok": 0.25,
            "cw_mtok": 3.125,
        },
    )

    entry = ModelCatalog().resolve_entry("priced-model", provider="acme")

    assert entry.source == "snapshot"
    assert entry.input_cost_per_mtok == pytest.approx(2.5)
    assert entry.output_cost_per_mtok == pytest.approx(10.0)
    assert isinstance(entry.output_cost_per_mtok, float)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.25)
    assert entry.cache_write_cost_per_mtok == pytest.approx(3.125)


def test_snapshot_partial_cost_keys_leave_missing_fields_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_catalog_module,
        "_models_dev_model",
        lambda provider_id, model_id: {"ctx": 8_192, "out": 4_096, "in_mtok": 0.5},
    )

    entry = ModelCatalog().resolve_entry("partial-model", provider="acme")

    assert entry.input_cost_per_mtok == pytest.approx(0.5)
    assert entry.output_cost_per_mtok is None
    assert entry.cache_read_cost_per_mtok is None
    assert entry.cache_write_cost_per_mtok is None


# ---------------------------------------------------------------------------
# PARITY NET — the legacy resolve paths must keep returning today's exact
# values (captured as literals from the pre-change tree). get_capabilities
# now resolves through resolve_entry + the transcribed ladder rows, and
# resolve_max_tokens / resolve_context_window remain independent; any drift
# here is an accidental behavior change. The exhaustive provider x model
# sweep lives in test_capability_ladder_parity.py.
# ---------------------------------------------------------------------------

# (model, provider) → (resolve_max_tokens, resolve_context_window)
_LEGACY_LIMITS: dict[tuple[str, str], tuple[int, int]] = {
    ("gpt-5.5", "openai"): (128_000, 1_050_000),
    ("gpt-5.4-mini", "openai"): (128_000, 400_000),
    ("deepseek-v4-pro", "deepseek"): (384_000, 1_000_000),
    ("kimi-k2.5", "moonshot"): (8_192, 262_144),
    ("glm-5", "zhipu"): (131_072, 204_800),
    ("glm-5", "zai"): (16_384, 202_752),
    # models.dev's 2026-07-08 refresh lowered openrouter z-ai/glm-5.2 max
    # output from 131_072 to 32_768 (the cross-provider min); the window is
    # unchanged.
    ("z-ai/glm-5.2", ""): (32_768, 1_000_000),
    ("gemini-3.5-flash", "gemini"): (65_536, 1_048_576),
    ("minimax-m2.7", ""): (131_072, 204_800),
    ("step-3.5-flash", ""): (16_384, 256_000),
    ("moonshot-v1-32k", "moonshot"): (8_192, 32_768),
    ("grok-4.3", ""): (16_384, 1_000_000),
    ("totally-unknown-model", ""): (16_384, 200_000),
    ("mystery-local", "ollama"): (8_192, 8_192),
    ("some-cloud-model", "openai"): (16_384, 200_000),
}

# (model, provider, base_url) →
#   (supports_reasoning, supports_tools, supports_vision, reasoning_format)
_LEGACY_CAPS: dict[tuple[str, str, str], tuple[bool, bool, bool, str]] = {
    ("claude-opus-4.8", "anthropic", ""): (False, True, False, "none"),
    ("llama3.2:3b", "ollama", ""): (False, True, False, "none"),
    ("gpt-5.5", "openai", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("deepseek-v4-pro", "deepseek", ""): (True, True, False, "deepseek"),
    ("glm-5", "zhipu", ""): (True, True, False, "zai"),
    ("glm-4.6", "zhipu", ""): (False, True, False, "none"),
    ("qwen3-coder-plus", "dashscope", ""): (True, True, False, "dashscope"),
    ("kimi-k2.5", "moonshot", ""): (True, True, True, "moonshot"),
    ("gemini-3.5-flash", "gemini", ""): (True, True, True, "gemini"),
    ("doubao-seed-1-6-251015", "volcengine", ""): (True, True, True, "volcengine"),
    ("totally-unknown-model", "openrouter", ""): (False, True, False, "none"),
    ("gpt-4", "openai", ""): (False, True, False, "none"),
}


def test_parity_legacy_limits_unchanged() -> None:
    catalog = ModelCatalog()
    for (model, provider), (max_tokens, context_window) in _LEGACY_LIMITS.items():
        assert catalog.resolve_max_tokens(model, provider=provider) == max_tokens, (
            model,
            provider,
        )
        assert catalog.resolve_context_window(model, provider) == context_window, (
            model,
            provider,
        )


def test_parity_legacy_capabilities_unchanged() -> None:
    catalog = ModelCatalog()
    for (model, provider, base_url), expected in _LEGACY_CAPS.items():
        caps = catalog.get_capabilities(model, provider_name=provider, base_url=base_url)
        observed = (
            caps.supports_reasoning,
            caps.supports_tools,
            caps.supports_vision,
            caps.reasoning_format,
        )
        assert observed == expected, (model, provider, base_url)


def test_full_entry_literals_snapshot_and_transcribed_ladder() -> None:
    # Full-literal entry pins (frozen-dataclass equality covers every field,
    # including the four cost fields — the snapshot now vendors models.dev
    # cost keys, so these are no longer all None). openai has NO ladder
    # rows — the api.openai.com branch is host-guarded and stays code — so
    # its snapshot resolution is byte-identical to the pre-ladder tree:
    # supports_reasoning is snapshot data but reasoning_format stays "none"
    # (the snapshot never knows the dialect). deepseek/gemini entries now
    # layer the transcribed capability-ladder rows (reasoning_format,
    # ladder-scoped capability booleans) over snapshot windows, so their
    # source moved to "corrections" — deliberately, as part of the
    # capability-ladder migration; the corrections rows carry no cost
    # fields, so cost still comes from the snapshot layer underneath.
    catalog = ModelCatalog()
    expected_entries = (
        ModelCatalogEntry(
            provider_id="openai",
            model_id="gpt-5.5",
            context_window=1_050_000,
            max_output_tokens=128_000,
            supports_reasoning=True,
            supports_tools=True,
            supports_vision=True,
            input_cost_per_mtok=5.0,
            output_cost_per_mtok=30.0,
            cache_read_cost_per_mtok=0.5,
            source="snapshot",
        ),
        ModelCatalogEntry(
            provider_id="deepseek",
            model_id="deepseek-chat",
            context_window=1_000_000,
            max_output_tokens=384_000,
            # The ladder's deepseek shape: every model of the provider
            # reasons through the deepseek dialect (snapshot said False).
            supports_reasoning=True,
            supports_tools=True,
            supports_vision=False,
            reasoning_format="deepseek",
            input_cost_per_mtok=0.14,
            output_cost_per_mtok=0.28,
            cache_read_cost_per_mtok=0.0028,
            source="corrections",
        ),
        ModelCatalogEntry(
            provider_id="gemini",
            model_id="gemini-2.5-pro",
            context_window=1_048_576,
            max_output_tokens=65_536,
            supports_reasoning=True,
            supports_tools=True,
            supports_vision=True,
            reasoning_format="gemini",
            input_cost_per_mtok=1.25,
            output_cost_per_mtok=10.0,
            cache_read_cost_per_mtok=0.125,
            source="corrections",
        ),
        ModelCatalogEntry(
            provider_id="zhipu",
            model_id="glm-4.6",
            context_window=204_800,
            max_output_tokens=131_072,
            # Ladder semantics win over the snapshot's reasoning=True:
            # the zai shape reasons only for glm-4.5/glm-4.7/glm-5.
            supports_reasoning=False,
            supports_tools=True,
            supports_vision=False,
            reasoning_format="none",
            input_cost_per_mtok=0.6,
            output_cost_per_mtok=2.2,
            cache_read_cost_per_mtok=0.11,
            cache_write_cost_per_mtok=0.0,
            source="corrections",
        ),
    )
    for want in expected_entries:
        got = catalog.resolve_entry(want.model_id, provider=want.provider_id)
        assert got == want, (want.provider_id, want.model_id)
