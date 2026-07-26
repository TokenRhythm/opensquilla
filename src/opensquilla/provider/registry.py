"""Metadata registry for LLM and coding provider capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .compat_policy import OpenAICompatPolicy, compat_policy_for_kind
from .context_capabilities import (
    ANTHROPIC_CONTEXT_PROFILE,
    OPENAI_RESPONSES_CONTEXT_PROFILE,
    OPENROUTER_CONTEXT_PROFILE,
    ProviderContextProfile,
)

# ---------------------------------------------------------------------------
# Named local-provider sets. ONE home, TWO deliberately distinct sets — they
# answer different questions and must not be merged:
#
# - KEYLESS_PROVIDERS: providers whose API key is optional even when an
#   env_key is declared — local runtimes plus the generic self-hosted
#   ``custom`` endpoint (many self-hosted servers run without
#   authentication). Drives ``ProviderSpec.requires_api_key``.
# - LOCAL_RUNTIME_PROVIDERS: the superset the model catalog's context-window
#   heuristic uses (model_catalog.py): runtimes that serve models from the
#   local machine, whose unqualified model ids miss every catalog and would
#   otherwise inherit the 200k cloud default. ``vllm`` and the bare
#   ``local`` alias belong here but stay OUT of KEYLESS_PROVIDERS: local
#   serving does not imply keyless onboarding (a vLLM deployment can front
#   real auth), so folding them in would silently weaken requires_api_key.
#
# The derivation is explicit (superset = keyless | extras) so the two sets
# can never drift apart by accident.
# ---------------------------------------------------------------------------

KEYLESS_PROVIDERS: frozenset[str] = frozenset({"ollama", "lm_studio", "ovms", "custom"})

LOCAL_RUNTIME_PROVIDERS: frozenset[str] = KEYLESS_PROVIDERS | {"vllm", "local"}

ProviderBackend = Literal[
    "openai_compat",
    "openai_responses",
    "anthropic",
    "ollama",
    "openai_codex",
    "unsupported_oauth",
    "unsupported_responses",
]

# How the provider authenticates the request. Consumed by the anthropic
# backend today (Anthropic proper wants ``x-api-key``; MiniMax's
# Anthropic-compatible endpoints want ``Authorization: Bearer``). "bearer"
# is the default because every other backend sends a Bearer header.
AuthHeaderStyle = Literal["x-api-key", "bearer"]

# Whether a provider's live listing has been verified as a safe source for
# user-selectable model ids. ``none`` is deliberately the default: several
# compatibility adapters expose static or protocol-family model rows that do
# not describe what the configured service actually serves.
SelectableModelCatalog = Literal["none", "verified_live"]


@dataclass(frozen=True)
class ProviderSpec:
    """Static provider metadata used for selection and capability display."""

    provider_id: str
    backend: ProviderBackend
    provider_kind: str
    env_key: str = ""
    default_base_url: str = ""
    required_fields: frozenset[str] = field(default_factory=lambda: frozenset({"api_key", "model"}))
    reasoning_shape: str = "none"
    failure_family: str = "openai_compat"
    metadata_supported: bool = True
    runtime_supported: bool = True
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))
    # Dialect quirks for OpenAI-compatible providers (display name, token
    # field, schema keyword strips, reasoning toggles, ...). Defaults to the
    # kind-keyed policy; only meaningful for backend == "openai_compat".
    compat: OpenAICompatPolicy = field(default_factory=OpenAICompatPolicy)
    # Auth header shape for the anthropic backend (see AuthHeaderStyle).
    auth_header_style: AuthHeaderStyle = "bearer"
    # Purely provider-keyed context/prompt-cache capabilities. None for
    # providers whose cache behavior is host-guarded (gemini, openai) or
    # unknown — those stay code branches in context_capabilities.
    context_profile: ProviderContextProfile | None = None
    # models.dev provider ids feeding the vendored model catalog snapshot
    # (merged in order; first source of a model id wins). Empty for
    # local/self-hosted or OAuth-only providers with no public catalog.
    catalog_source: tuple[str, ...] = ()
    # Keyless public model-listing endpoint + payload shape for the
    # boot-time provider-scoped live catalog ingest (see
    # provider/live_catalog.py). Set only for hosted aggregators whose
    # platform publishes per-model windows/limits without auth; the
    # OpenRouter live cache keeps its bespoke authenticated fetch.
    live_catalog_url: str = ""
    live_catalog_shape: str = ""
    selectable_model_catalog: SelectableModelCatalog = "none"

    def requires_api_key(self) -> bool:
        """True if onboarding must collect an API key for this provider."""
        if self.provider_id in KEYLESS_PROVIDERS:
            return False
        return bool(self.env_key) and self.env_key != "OAuth"

    def requires_base_url(self) -> bool:
        """True if onboarding must collect a base URL for this provider."""
        return self.runtime_supported and not self.default_base_url


class UnknownProviderError(ValueError):
    """Raised when a provider id is not present in the registry."""


_PROVIDER_SPECS: dict[str, ProviderSpec] = {}


def _register(spec: ProviderSpec) -> None:
    _PROVIDER_SPECS[spec.provider_id] = spec


def _spec(
    provider_id: str,
    backend: ProviderBackend,
    provider_kind: str,
    env_key: str = "",
    default_base_url: str = "",
    *,
    required_fields: frozenset[str] | None = None,
    reasoning_shape: str = "none",
    failure_family: str = "openai_compat",
    runtime_supported: bool = True,
    capabilities: frozenset[str] | None = None,
    auth_header_style: AuthHeaderStyle = "bearer",
    context_profile: ProviderContextProfile | None = None,
    catalog_source: tuple[str, ...] = (),
    live_catalog_url: str = "",
    live_catalog_shape: str = "",
    selectable_model_catalog: SelectableModelCatalog = "none",
) -> ProviderSpec:
    if required_fields is None:
        required_fields = frozenset({"api_key", "model"}) if env_key else frozenset({"model"})
    return ProviderSpec(
        provider_id=provider_id,
        backend=backend,
        provider_kind=provider_kind,
        env_key=env_key,
        default_base_url=default_base_url,
        required_fields=required_fields,
        reasoning_shape=reasoning_shape,
        failure_family=failure_family,
        runtime_supported=runtime_supported,
        capabilities=capabilities or frozenset({"chat"}),
        compat=compat_policy_for_kind(provider_kind),
        auth_header_style=auth_header_style,
        context_profile=context_profile,
        catalog_source=catalog_source,
        live_catalog_url=live_catalog_url,
        live_catalog_shape=live_catalog_shape,
        selectable_model_catalog=selectable_model_catalog,
    )


for _provider_spec in [
    _spec(
        "openrouter",
        "openai_compat",
        "openrouter",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1",
        context_profile=OPENROUTER_CONTEXT_PROFILE,
        catalog_source=("openrouter",),
        selectable_model_catalog="verified_live",
    ),
    _spec(
        "openai",
        "openai_compat",
        "openai",
        "OPENAI_API_KEY",
        "https://api.openai.com/v1",
        catalog_source=("openai",),
    ),
    _spec(
        "openai_responses",
        "openai_responses",
        "openai_responses",
        "OPENAI_API_KEY",
        "https://api.openai.com/v1",
        capabilities=frozenset({"chat", "responses"}),
        context_profile=OPENAI_RESPONSES_CONTEXT_PROFILE,
        catalog_source=("openai",),
    ),
    _spec(
        "azure",
        "openai_compat",
        "azure",
        "AZURE_OPENAI_API_KEY",
        catalog_source=("azure",),
    ),
    _spec(
        "anthropic",
        "anthropic",
        "anthropic",
        "ANTHROPIC_API_KEY",
        "https://api.anthropic.com",
        failure_family="anthropic",
        auth_header_style="x-api-key",
        context_profile=ANTHROPIC_CONTEXT_PROFILE,
        catalog_source=("anthropic",),
    ),
    _spec(
        "ollama",
        "ollama",
        "ollama",
        "OLLAMA_API_KEY",
        default_base_url="http://localhost:11434",
        # Local provider: the key is optional (only needed for Ollama Cloud or a
        # secured remote host), so keep onboarding from demanding it.
        required_fields=frozenset({"model"}),
        failure_family="ollama",
    ),
    _spec(
        "deepseek",
        "openai_compat",
        "deepseek",
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com",
        reasoning_shape="deepseek",
        catalog_source=("deepseek",),
    ),
    _spec(
        "gemini",
        "openai_compat",
        "gemini",
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        reasoning_shape="gemini",
        catalog_source=("google",),
    ),
    _spec(
        "dashscope",
        "openai_compat",
        "dashscope",
        "DASHSCOPE_API_KEY",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        catalog_source=("alibaba-cn", "alibaba"),
    ),
    _spec(
        "bailian_coding",
        "openai_compat",
        "bailian_coding",
        "BAILIAN_API_KEY",
        "https://coding-intl.dashscope.aliyuncs.com/v1",
        catalog_source=("alibaba", "alibaba-cn"),
    ),
    _spec(
        "bailian_coding_cn",
        "openai_compat",
        "bailian_coding",
        "BAILIAN_API_KEY",
        "https://coding.dashscope.aliyuncs.com/v1",
        catalog_source=("alibaba-cn", "alibaba"),
    ),
    _spec(
        "moonshot",
        "openai_compat",
        "moonshot",
        "MOONSHOT_API_KEY",
        "https://api.moonshot.ai/v1",
        catalog_source=("moonshotai",),
    ),
    _spec(
        "kimi_coding_openai",
        "openai_compat",
        "moonshot",
        "KIMI_CODING_API_KEY",
        "https://api.kimi.com/coding/v1",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "kimi_coding_anthropic",
        "anthropic",
        "moonshot",
        "KIMI_CODING_API_KEY",
        "https://api.kimi.com/coding",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "minimax",
        "anthropic",
        "minimax",
        "MINIMAX_API_KEY",
        "https://api.minimaxi.com/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        catalog_source=("minimax",),
    ),
    _spec(
        "minimax_openai",
        "openai_compat",
        "minimax",
        "MINIMAX_API_KEY",
        "https://api.minimax.io/v1",
        catalog_source=("minimax",),
    ),
    _spec(
        "minimax_coding_openai",
        "openai_compat",
        "minimax",
        "MINIMAX_CODING_API_KEY",
        "https://api.minimaxi.com/v1",
        capabilities=frozenset({"chat", "coding_plan"}),
        catalog_source=("minimax",),
    ),
    _spec(
        "minimax_coding_anthropic",
        "anthropic",
        "minimax",
        "MINIMAX_CODING_API_KEY",
        "https://api.minimaxi.com/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
        catalog_source=("minimax",),
    ),
    _spec(
        "minimax_cn",
        "anthropic",
        "minimax",
        "MINIMAX_CN_API_KEY",
        "https://api.minimaxi.com/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        catalog_source=("minimax",),
    ),
    _spec(
        "minimax_global",
        "anthropic",
        "minimax",
        "MINIMAX_API_KEY",
        "https://api.minimax.io/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        catalog_source=("minimax",),
    ),
    _spec(
        "mimo_openai",
        "openai_compat",
        "mimo",
        "MIMO_API_KEY",
        "https://token-plan-cn.xiaomimimo.com/v1",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "mimo_anthropic",
        "anthropic",
        "mimo",
        "MIMO_API_KEY",
        "https://token-plan-cn.xiaomimimo.com/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "mistral",
        "openai_compat",
        "mistral",
        "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1",
        catalog_source=("mistral",),
    ),
    _spec(
        "groq",
        "openai_compat",
        "groq",
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        catalog_source=("groq",),
    ),
    _spec(
        "zhipu",
        "openai_compat",
        "zhipu",
        "ZAI_API_KEY",
        "https://open.bigmodel.cn/api/paas/v4",
        reasoning_shape="zai",
        catalog_source=("zhipuai", "zai"),
    ),
    _spec(
        "qianfan",
        "openai_compat",
        "qianfan",
        "QIANFAN_API_KEY",
        "https://qianfan.baidubce.com/v2",
        catalog_source=("qianfan", "baidu"),
    ),
    _spec(
        "siliconflow",
        "openai_compat",
        "siliconflow",
        "SILICONFLOW_API_KEY",
        "https://api.siliconflow.cn/v1",
        catalog_source=("siliconflow",),
    ),
    _spec(
        "aihubmix",
        "openai_compat",
        "aihubmix",
        "AIHUBMIX_API_KEY",
        "https://aihubmix.com/v1",
    ),
    _spec(
        "volcengine",
        "openai_compat",
        "volcengine",
        "VOLCENGINE_API_KEY",
        "https://ark.cn-beijing.volces.com/api/v3",
        catalog_source=("volcengine",),
    ),
    _spec(
        "byteplus",
        "openai_compat",
        "byteplus",
        "BYTEPLUS_API_KEY",
        "https://ark.ap-southeast.bytepluses.com/api/v3",
        catalog_source=("byteplus",),
    ),
    # Tencent TokenHub — the current home of the Hunyuan hy3 family (the
    # legacy api.hunyuan.cloud.tencent.com platform is sunsetting and never
    # received hy3). Mainland endpoint; keys come from the CN TokenHub
    # console and use plain Bearer auth on the OpenAI protocol.
    _spec(
        "tencent_tokenhub",
        "openai_compat",
        "tencent_tokenhub",
        "TENCENT_TOKENHUB_API_KEY",
        "https://tokenhub.tencentmaas.com/v1",
        catalog_source=("tencent-tokenhub",),
    ),
    # TokenHub's Anthropic-compatible Messages endpoint lives on the bare
    # host (POST /v1/messages) and, unlike MiniMax's, signs with x-api-key.
    _spec(
        "tencent_tokenhub_anthropic",
        "anthropic",
        "tencent_tokenhub",
        "TENCENT_TOKENHUB_API_KEY",
        "https://tokenhub.tencentmaas.com",
        failure_family="anthropic",
        auth_header_style="x-api-key",
        catalog_source=("tencent-tokenhub",),
    ),
    # International TokenHub (Singapore). A separate Tencent Cloud account
    # and key system from the CN site — hence its own env key — and a
    # different model list (no hy3 there yet), so no catalog_source.
    _spec(
        "tencent_tokenhub_intl",
        "openai_compat",
        "tencent_tokenhub",
        "TENCENT_TOKENHUB_INTL_API_KEY",
        "https://tokenhub-intl.tencentcloudmaas.com/v1",
    ),
    # Tencent Token Plan (personal edition) — the CN subscription that
    # carries hy3/hy3-preview (plus the General-plan third-party models on
    # the same key, routed by model id). Dedicated sk-tp keys, not
    # interchangeable with pay-as-you-go TokenHub keys; chat completions
    # only (no Responses API), and Tencent's terms restrict plan keys to
    # interactive AI-tool use.
    _spec(
        "tencent_token_plan",
        "openai_compat",
        "tencent_tokenhub",
        "TENCENT_TOKEN_PLAN_API_KEY",
        "https://api.lkeap.cloud.tencent.com/plan/v3",
        capabilities=frozenset({"chat", "coding_plan"}),
        catalog_source=("tencent-token-plan",),
    ),
    # The Token Plan's Anthropic Messages endpoint. Tencent's own tool
    # guides authenticate it with a bearer token (ANTHROPIC_AUTH_TOKEN),
    # like MiniMax and unlike the pay-as-you-go TokenHub host.
    _spec(
        "tencent_token_plan_anthropic",
        "anthropic",
        "tencent_tokenhub",
        "TENCENT_TOKEN_PLAN_API_KEY",
        "https://api.lkeap.cloud.tencent.com/plan/anthropic",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
        catalog_source=("tencent-token-plan",),
    ),
    # TokenRhythm — hosted aggregator relaying the DeepSeek/GLM/MiniMax/
    # Kimi/MiMo/Qwen families behind one OpenAI-protocol host (Bearer auth,
    # chat completions only — no Responses API). Every served model streams
    # DeepSeek-style reasoning_content. Not on models.dev, so no
    # catalog_source; instead the platform's keyless public listing is
    # ingested at boot into the provider-scoped live layer (windows,
    # output limits, prices), with the catalog_overrides.toml rows as the
    # offline fallback beneath it.
    _spec(
        "tokenrhythm",
        "openai_compat",
        "tokenrhythm",
        "TOKENRHYTHM_API_KEY",
        "https://tokenrhythm.studio/v1",
        reasoning_shape="deepseek",
        live_catalog_url="https://tokenrhythm.studio/api/models",
        live_catalog_shape="tokenrhythm",
        selectable_model_catalog="verified_live",
    ),
    # First-class id for any self-hosted or otherwise unlisted
    # OpenAI-compatible endpoint (vLLM, SGLang, TGI, llama.cpp server, a
    # bespoke proxy, ...). Pure registry metadata — the openai_compat backend
    # and its "openai" dialect policy are reused unchanged. No
    # default_base_url on purpose: requires_base_url() must stay True, the
    # endpoint is defined by its operator-supplied URL. The API key is
    # optional (listed in KEYLESS_PROVIDERS); CUSTOM_LLM_API_KEY is read when
    # the endpoint enforces one.
    _spec(
        "custom",
        "openai_compat",
        "openai",
        "CUSTOM_LLM_API_KEY",
        required_fields=frozenset({"model", "base_url"}),
        failure_family="openai_compat",
    ),
    _spec("vllm", "openai_compat", "openai"),
    _spec("lm_studio", "openai_compat", "lm_studio", default_base_url="http://localhost:1234/v1"),
    _spec("ovms", "openai_compat", "ovms", default_base_url="http://localhost:8000/v3"),
    _spec(
        "litellm_proxy",
        "openai_compat",
        "litellm_proxy",
        "LITELLM_API_KEY",
        "http://localhost:4000/v1",
    ),
    _spec(
        "volcengine_coding_plan",
        "openai_responses",
        "volcengine_coding_plan",
        "VOLCENGINE_API_KEY",
        "https://ark.cn-beijing.volces.com/api/coding/v3",
        capabilities=frozenset({"chat", "coding_plan", "responses"}),
    ),
    _spec(
        "volcengine_coding_plan_anthropic",
        "anthropic",
        "volcengine_coding_plan_anthropic",
        "VOLCENGINE_API_KEY",
        "https://ark.cn-beijing.volces.com/api/coding",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "byteplus_coding_plan",
        "openai_responses",
        "byteplus_coding_plan",
        "BYTEPLUS_API_KEY",
        "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        capabilities=frozenset({"chat", "coding_plan", "responses"}),
    ),
    _spec(
        "byteplus_coding_plan_anthropic",
        "anthropic",
        "byteplus_coding_plan_anthropic",
        "BYTEPLUS_API_KEY",
        "https://ark.ap-southeast.bytepluses.com/api/coding",
        failure_family="anthropic",
        auth_header_style="bearer",
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "openai_codex",
        "openai_codex",
        "openai_codex",
        "OAuth",
        "https://chatgpt.com/backend-api",
        # OAuth via the Codex CLI's stored ChatGPT credentials — no API key
        # field; `codex login` owns credential creation.
        required_fields=frozenset({"model"}),
        capabilities=frozenset({"chat", "coding_plan"}),
    ),
    _spec(
        "github_copilot",
        "unsupported_oauth",
        "github_copilot",
        "OAuth",
        "https://api.githubcopilot.com",
        runtime_supported=False,
        capabilities=frozenset({"coding_plan"}),
    ),
]:
    _register(_provider_spec)


def list_provider_specs() -> tuple[ProviderSpec, ...]:
    """Return provider specs sorted by provider id for stable display/tests."""

    return tuple(_PROVIDER_SPECS[name] for name in sorted(_PROVIDER_SPECS))


def list_provider_names() -> tuple[str, ...]:
    """Return registered provider ids in stable order."""

    return tuple(spec.provider_id for spec in list_provider_specs())


def get_provider_spec(provider_id: str) -> ProviderSpec:
    """Return a provider spec or raise an actionable unknown-provider error."""

    try:
        return _PROVIDER_SPECS[provider_id]
    except KeyError as exc:
        available = ", ".join(list_provider_names())
        raise UnknownProviderError(
            f"Unknown provider '{provider_id}'. Available: {available}"
        ) from exc
