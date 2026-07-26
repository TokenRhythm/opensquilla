"""Consistency tests for the ProviderSpec substrate fields.

Covers the spec-carried ``catalog_source`` mapping (migrated from the
models.dev snapshot refresh script) and the ``auth_header_style`` values the
anthropic backend consumes.
"""

from __future__ import annotations

from opensquilla.provider.registry import get_provider_spec, list_provider_specs

# Runtime-supported providers legitimately absent from the models.dev
# catalog mapping. Every id must carry a reason; anything else that is
# runtime-supported needs a non-empty catalog_source.
_CATALOG_SOURCE_WAIVERS: frozenset[str] = frozenset(
    {
        # Local/self-hosted runtimes: the model list comes from the local
        # server itself, not a public catalog.
        "ollama",
        "lm_studio",
        "ovms",
        "vllm",
        # Generic self-hosted OpenAI-compatible endpoint id: the model set is
        # whatever the operator serves; no models.dev source exists.
        "custom",
        # Deployment-defined aggregation proxy: the model set is whatever
        # the operator's LiteLLM instance routes; no stable public catalog.
        "litellm_proxy",
        # Private coding-plan endpoints expose their own model lists, but
        # models.dev has no stable source id for these plan surfaces.
        "kimi_coding_openai",
        "kimi_coding_anthropic",
        "mimo_openai",
        "mimo_anthropic",
        # Hosted aggregator with no models.dev source mapped; the vendored
        # snapshot has never carried aihubmix rows.
        "aihubmix",
        # Hosted aggregator not on models.dev; per-model metadata ships as
        # catalog_overrides.toml corrections. Mapping family sources
        # (deepseek, zhipuai, ...) here would vendor entire foreign tables
        # with the origin providers' prices under this id.
        "tokenrhythm",
        # OAuth-only ChatGPT-backend provider: models are fixed by the
        # Codex subscription, not a public catalog.
        "openai_codex",
        # Coding-plan subscription endpoints expose a fixed subscription
        # surface rather than a models.dev-backed catalog.
        "volcengine_coding_plan",
        "volcengine_coding_plan_anthropic",
        "byteplus_coding_plan",
        "byteplus_coding_plan_anthropic",
        # International TokenHub is a separate deployment with its own model
        # list (no hy3 there yet); models.dev only catalogs the CN TokenHub.
        "tencent_tokenhub_intl",
    }
)

# Frozen copy of the mapping that previously lived out-of-band as
# PROVIDER_SOURCES in scripts/refresh_models_dev_snapshot.py. The script now
# derives its mapping from the registry; this literal proves the migration
# moved the data verbatim.
_EXPECTED_CATALOG_SOURCES: dict[str, tuple[str, ...]] = {
    "openrouter": ("openrouter",),
    "openai": ("openai",),
    "openai_responses": ("openai",),
    "anthropic": ("anthropic",),
    "deepseek": ("deepseek",),
    "gemini": ("google",),
    "dashscope": ("alibaba-cn", "alibaba"),
    "bailian_coding": ("alibaba", "alibaba-cn"),
    "bailian_coding_cn": ("alibaba-cn", "alibaba"),
    "moonshot": ("moonshotai",),
    "zhipu": ("zhipuai", "zai"),
    "minimax": ("minimax",),
    "minimax_openai": ("minimax",),
    "minimax_coding_openai": ("minimax",),
    "minimax_coding_anthropic": ("minimax",),
    "minimax_cn": ("minimax",),
    "minimax_global": ("minimax",),
    "mistral": ("mistral",),
    "groq": ("groq",),
    "siliconflow": ("siliconflow",),
    "volcengine": ("volcengine",),
    "byteplus": ("byteplus",),
    "tencent_tokenhub": ("tencent-tokenhub",),
    "tencent_tokenhub_anthropic": ("tencent-tokenhub",),
    "tencent_token_plan": ("tencent-token-plan",),
    "tencent_token_plan_anthropic": ("tencent-token-plan",),
    "qianfan": ("qianfan", "baidu"),
    "azure": ("azure",),
}


def test_every_runtime_supported_spec_has_catalog_source_or_waiver() -> None:
    for spec in list_provider_specs():
        if not spec.runtime_supported:
            continue
        assert spec.catalog_source or spec.provider_id in _CATALOG_SOURCE_WAIVERS, (
            f"Provider '{spec.provider_id}' is runtime-supported but declares no "
            "catalog_source. Map it to its models.dev source ids, or add it to "
            "_CATALOG_SOURCE_WAIVERS with a reason."
        )


def test_waivers_only_cover_specs_without_catalog_sources() -> None:
    for provider_id in sorted(_CATALOG_SOURCE_WAIVERS):
        spec = get_provider_spec(provider_id)  # also fails on stale waiver ids
        assert not spec.catalog_source, (
            f"Provider '{provider_id}' declares a catalog_source; drop it from "
            "_CATALOG_SOURCE_WAIVERS."
        )


def test_catalog_sources_match_the_migrated_script_mapping() -> None:
    actual = {
        spec.provider_id: spec.catalog_source
        for spec in list_provider_specs()
        if spec.catalog_source
    }
    assert actual == _EXPECTED_CATALOG_SOURCES


def test_selectable_model_catalog_is_enabled_only_for_verified_providers() -> None:
    """A picker must never turn an unverified/static adapter list into truth."""
    trusted = {
        spec.provider_id
        for spec in list_provider_specs()
        if spec.selectable_model_catalog == "verified_live"
    }
    assert trusted == {"openrouter", "tokenrhythm"}

    assert get_provider_spec("openrouter").compat.official_host == "openrouter.ai"
    assert get_provider_spec("tokenrhythm").compat.official_host == "tokenrhythm.studio"


def test_anthropic_backend_auth_header_styles() -> None:
    """Anthropic proper signs with x-api-key; the MiniMax Anthropic-compatible
    endpoints require Authorization: Bearer. The request goldens freeze the
    wire effect; this pins the spec values that drive it."""
    assert get_provider_spec("anthropic").auth_header_style == "x-api-key"
    for provider_id in (
        "minimax",
        "minimax_coding_anthropic",
        "minimax_cn",
        "minimax_global",
        "kimi_coding_anthropic",
        "mimo_anthropic",
        "volcengine_coding_plan_anthropic",
        "byteplus_coding_plan_anthropic",
        # Tencent's Token Plan tool guides authenticate the Anthropic
        # endpoint with a bearer token (ANTHROPIC_AUTH_TOKEN).
        "tencent_token_plan_anthropic",
    ):
        spec = get_provider_spec(provider_id)
        assert spec.backend == "anthropic"
        assert spec.auth_header_style == "bearer"
    # TokenHub's Anthropic-compatible Messages endpoint documents x-api-key
    # (anthropic-version is accepted and ignored), unlike the bearer group.
    tokenhub_anthropic = get_provider_spec("tencent_tokenhub_anthropic")
    assert tokenhub_anthropic.backend == "anthropic"
    assert tokenhub_anthropic.auth_header_style == "x-api-key"


def test_coding_plan_specs_expose_protocol_specific_runtime_surfaces() -> None:
    """Coding-plan provider ids map the official protocol-specific URLs."""
    expected = {
        "volcengine_coding_plan": (
            "openai_responses",
            "https://ark.cn-beijing.volces.com/api/coding/v3",
            frozenset({"chat", "coding_plan", "responses"}),
        ),
        "volcengine_coding_plan_anthropic": (
            "anthropic",
            "https://ark.cn-beijing.volces.com/api/coding",
            frozenset({"chat", "coding_plan"}),
        ),
        "byteplus_coding_plan": (
            "openai_responses",
            "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
            frozenset({"chat", "coding_plan", "responses"}),
        ),
        "byteplus_coding_plan_anthropic": (
            "anthropic",
            "https://ark.ap-southeast.bytepluses.com/api/coding",
            frozenset({"chat", "coding_plan"}),
        ),
        # Tencent's Token Plan subscription speaks Chat Completions (no
        # Responses API) plus Anthropic Messages, both on the lkeap host.
        "tencent_token_plan": (
            "openai_compat",
            "https://api.lkeap.cloud.tencent.com/plan/v3",
            frozenset({"chat", "coding_plan"}),
        ),
        "tencent_token_plan_anthropic": (
            "anthropic",
            "https://api.lkeap.cloud.tencent.com/plan/anthropic",
            frozenset({"chat", "coding_plan"}),
        ),
    }
    for provider_id, (backend, base_url, capabilities) in expected.items():
        spec = get_provider_spec(provider_id)
        assert spec.backend == backend
        assert spec.default_base_url == base_url
        assert spec.runtime_supported is True
        assert capabilities <= spec.capabilities
