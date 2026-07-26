"""Per-provider dialect policy for the OpenAI-compatible backend.

Twenty-plus providers share ``OpenAIProvider``. What tells them apart is not
code but *data*: which token-limit field the upstream accepts, which JSON
Schema keywords it rejects, whether it leaks MiniMax's plain-text tool
protocol, whether its billed cost can be trusted, which models need explicit
thinking toggles. ``OpenAICompatPolicy`` is that data — one frozen record per
``provider_kind``, consumed by the request builder and stream loop instead of
``provider_kind == ...`` branches scattered through them.

The registry attaches a policy to every ``ProviderSpec``; constructing an
``OpenAIProvider`` without one falls back to the kind-keyed default so
direct construction (tests, tooling) behaves identically to the registry
path.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

TextToolDialect = Literal["qwen_tag", "minimax_xml", "plain_json"]

TEXT_TOOL_DIALECT_QWEN_TAG: TextToolDialect = "qwen_tag"
TEXT_TOOL_DIALECT_MINIMAX_XML: TextToolDialect = "minimax_xml"
TEXT_TOOL_DIALECT_PLAIN_JSON: TextToolDialect = "plain_json"


@dataclass(frozen=True)
class TextToolModelRule:
    """Allow text-tool dialects only for explicitly matched model ids."""

    model_patterns: tuple[str, ...]
    dialects: frozenset[TextToolDialect]

    def matches(self, model: str) -> bool:
        normalized = model.strip().lower()
        return any(fnmatchcase(normalized, pattern.lower()) for pattern in self.model_patterns)


@dataclass(frozen=True)
class TextToolCompatProfile:
    """Trusted text-to-tool execution policy.

    Dialects are executable compatibility capabilities, not display filters.
    Provider-wide dialects apply to every model on the provider; model rules
    are additive and make aggregator policies explicit instead of granting a
    text protocol to every model behind the same endpoint.
    """

    dialects: frozenset[TextToolDialect] = frozenset()
    model_rules: tuple[TextToolModelRule, ...] = ()

    def dialects_for_model(self, model: str) -> frozenset[TextToolDialect]:
        enabled = set(self.dialects)
        for rule in self.model_rules:
            if rule.matches(model):
                enabled.update(rule.dialects)
        return frozenset(enabled)

    @property
    def enabled(self) -> bool:
        return bool(self.dialects or self.model_rules)


@dataclass(frozen=True)
class OpenAICompatPolicy:
    """Declarative quirks of one OpenAI-compatible provider dialect."""

    # Human-readable name used in error messages ("OpenRouter chat request
    # failed (HTTP 400): ...").
    display_name: str = "Provider"

    # Host marker gating quirks that only apply to the provider's official
    # endpoint (an OpenAI-compatible re-host of the same models usually does
    # not share them).
    official_host: str = ""

    # Models that take ``max_completion_tokens`` instead of ``max_tokens``
    # (matched on the model basename, official host only).
    max_completion_tokens_model_prefixes: tuple[str, ...] = ()

    # Models whose sampling is fixed upstream: a non-default temperature is
    # dropped rather than rejected by the API.
    fixed_sampling_model_prefixes: tuple[str, ...] = ()

    # Models that reject a temperature while extended thinking is active
    # (official host only).
    omit_temperature_when_thinking_model_prefixes: tuple[str, ...] = ()

    # JSON Schema keywords the upstream rejects in tool definitions.
    tool_schema_unsupported_keywords: frozenset[str] = frozenset()

    # Text-to-tool execution is deliberately dialect- and model-scoped.  This
    # record is trusted packaged metadata: an online model catalog must never
    # be able to grant text the authority to become an executable tool call.
    text_tool_profile: TextToolCompatProfile = TextToolCompatProfile()

    # Whether usage.cost from this upstream is authoritative billing data.
    trust_billed_cost: bool = False

    # OpenRouter-family request extras.
    sends_usage_include: bool = False
    supports_provider_routing_pin: bool = False
    supports_explicit_prompt_cache: bool = False
    anthropic_top_level_cache: bool = False
    stream_timeout_fallback: bool = False
    empty_stream_fallback: bool = False
    log_payload_cache_shape: bool = False

    # Some gateways repeat the already-observed terminal choice while
    # attaching usage metadata.  This is safe to ignore only when the choice
    # is an exact semantic no-op (index 0, no text/reasoning/tools, and no
    # new/different finish reason); providers must opt in explicitly.
    allow_post_terminal_noop_choice: bool = False

    # TokenRhythm may insert one semantically empty choice with ``usage: null``
    # between finish_reason and its real usage/billing trailer.  This is a
    # distinct, narrower opt-in: the decoder still rejects content, reasoning,
    # tools, a different index, or any changed finish reason on that frame.
    allow_post_terminal_null_usage_noop_choice: bool = False

    # Provider-specific top-level metadata keys that may accompany the
    # no-op terminal epilogue.  These fields are inert: the stream decoder
    # validates their location but never treats them as response content.
    post_terminal_metadata_keys: frozenset[str] = frozenset()

    # Gateway proxies with their own routing (LiteLLM): pin the requested
    # model by disabling the gateway's cross-model fallbacks per request, so
    # SquillaRouter stays the single routing authority.
    sends_disable_fallbacks: bool = False

    # Response headers that report which deployment actually served the
    # request (logged for attribution; a routing deviation must be visible).
    attribution_response_headers: tuple[str, ...] = ()

    # Reasoning continuity: replay assistant reasoning_content when the model
    # capabilities declare this reasoning format.
    replay_reasoning_format: str = ""

    # Reasoning format assumed when no model capabilities are available.
    default_reasoning_format: str = ""

    # Models that need an explicit thinking enable/disable payload even when
    # no capability profile is available (exact ids, lowercase).
    thinking_toggle_model_ids: frozenset[str] = frozenset()

    # Models that require reasoning_content on every assistant message —
    # including an empty string when there is none (exact ids, lowercase).
    require_reasoning_content_model_ids: frozenset[str] = frozenset()

    # Models that stream reasoning by default and need it explicitly disabled
    # when thinking is off (exact ids, lowercase).
    disable_reasoning_by_default_models: frozenset[str] = frozenset()

    @property
    def text_tool_synthesis(self) -> bool:
        """Deprecated read-only compatibility view.

        Older diagnostics inspected one provider-wide boolean.  Keep that
        observation surface without letting the boolean control execution;
        callers that need an answer for one model must use
        ``text_tool_profile.dialects_for_model(model)``.
        """

        return self.text_tool_profile.enabled


_ARK_UNSUPPORTED_TOOL_SCHEMA_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minContains",
        "maxContains",
    }
)

_DEEPSEEK_V4_MODEL_IDS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})

# TokenHub's hy3 family documents interleaved thinking: assistant turns must
# carry reasoning_content back (an empty string when there is none), or the
# reasoning context is lost across tool-call rounds.
_TOKENHUB_HY3_MODEL_IDS = frozenset({"hy3", "hy3-preview"})

# OpenRouter's reasoning controls are model/provider-specific: GLM can be
# stabilized by explicitly disabling reasoning when OpenSquilla has not
# requested thinking, while MiniMax reasoning endpoints reject that payload.
_OPENROUTER_DISABLE_REASONING_MODELS = frozenset(
    {
        "z-ai/glm-4.5",
        "z-ai/glm-4.5-air",
        "z-ai/glm-5",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }
)


_POLICIES_BY_KIND: dict[str, OpenAICompatPolicy] = {
    "openai": OpenAICompatPolicy(
        display_name="OpenAI",
        official_host="api.openai.com",
        max_completion_tokens_model_prefixes=("gpt-5", "o1", "o3", "o4"),
        omit_temperature_when_thinking_model_prefixes=("gpt-5.4", "gpt-5.5"),
    ),
    "openrouter": OpenAICompatPolicy(
        display_name="OpenRouter",
        official_host="openrouter.ai",
        text_tool_profile=TextToolCompatProfile(
            model_rules=(
                TextToolModelRule(
                    model_patterns=("minimax/*",),
                    dialects=frozenset({TEXT_TOOL_DIALECT_MINIMAX_XML}),
                ),
            ),
        ),
        trust_billed_cost=True,
        sends_usage_include=True,
        supports_provider_routing_pin=True,
        supports_explicit_prompt_cache=True,
        anthropic_top_level_cache=True,
        stream_timeout_fallback=True,
        log_payload_cache_shape=True,
        replay_reasoning_format="openrouter",
        disable_reasoning_by_default_models=_OPENROUTER_DISABLE_REASONING_MODELS,
        allow_post_terminal_noop_choice=True,
        post_terminal_metadata_keys=frozenset({"provider"}),
    ),
    "azure": OpenAICompatPolicy(display_name="Azure OpenAI"),
    "deepseek": OpenAICompatPolicy(
        display_name="DeepSeek",
        default_reasoning_format="deepseek",
        # Reasoning replay is gated on the exact V4 ids (below), not on the
        # capability format: non-V4 DeepSeek models must not get replay.
        thinking_toggle_model_ids=_DEEPSEEK_V4_MODEL_IDS,
        require_reasoning_content_model_ids=_DEEPSEEK_V4_MODEL_IDS,
    ),
    "gemini": OpenAICompatPolicy(display_name="Gemini"),
    "dashscope": OpenAICompatPolicy(
        display_name="DashScope",
        text_tool_profile=TextToolCompatProfile(
            model_rules=(
                TextToolModelRule(
                    model_patterns=("qwen*", "qwq*"),
                    dialects=frozenset({TEXT_TOOL_DIALECT_QWEN_TAG}),
                ),
            ),
        ),
        supports_explicit_prompt_cache=True,
        stream_timeout_fallback=True,
    ),
    "bailian_coding": OpenAICompatPolicy(display_name="Bailian Coding"),
    "moonshot": OpenAICompatPolicy(
        display_name="Moonshot",
        fixed_sampling_model_prefixes=("kimi-k2.5", "kimi-k2.6", "kimi-k2.7"),
        empty_stream_fallback=True,
    ),
    "minimax": OpenAICompatPolicy(
        display_name="MiniMax",
        text_tool_profile=TextToolCompatProfile(
            dialects=frozenset({TEXT_TOOL_DIALECT_MINIMAX_XML}),
        ),
    ),
    "mimo": OpenAICompatPolicy(display_name="MiMo"),
    "mistral": OpenAICompatPolicy(display_name="Mistral"),
    "groq": OpenAICompatPolicy(display_name="Groq"),
    "zhipu": OpenAICompatPolicy(display_name="Zhipu"),
    "qianfan": OpenAICompatPolicy(display_name="Qianfan"),
    "siliconflow": OpenAICompatPolicy(display_name="SiliconFlow"),
    "aihubmix": OpenAICompatPolicy(display_name="AiHubMix"),
    "volcengine": OpenAICompatPolicy(
        display_name="Volcengine",
        tool_schema_unsupported_keywords=_ARK_UNSUPPORTED_TOOL_SCHEMA_KEYWORDS,
    ),
    "byteplus": OpenAICompatPolicy(
        display_name="BytePlus",
        tool_schema_unsupported_keywords=_ARK_UNSUPPORTED_TOOL_SCHEMA_KEYWORDS,
    ),
    "tencent_tokenhub": OpenAICompatPolicy(
        display_name="Tencent TokenHub",
        replay_reasoning_format="tencent_tokenhub",
        require_reasoning_content_model_ids=_TOKENHUB_HY3_MODEL_IDS,
    ),
    # TokenRhythm relays the DeepSeek/GLM/MiniMax/Kimi/MiMo/Qwen families
    # behind one host, and every served model streams DeepSeek-style
    # reasoning_content unconditionally (the parse side needs no config).
    # The endpoint rejects unknown request fields — a DeepSeek ``thinking``
    # toggle is an UNKNOWN_FIELD 400 — so no default_reasoning_format and no
    # thinking_toggle_model_ids here, and the packaged catalog rows pin
    # reasoning_format="none" to keep dialect injection off. The V4 ids keep
    # only the reasoning_content replay requirement (live-verified accepted).
    "tokenrhythm": OpenAICompatPolicy(
        display_name="TokenRhythm",
        official_host="tokenrhythm.studio",
        text_tool_profile=TextToolCompatProfile(
            model_rules=(
                TextToolModelRule(
                    model_patterns=("minimax-*",),
                    dialects=frozenset({TEXT_TOOL_DIALECT_MINIMAX_XML}),
                ),
                TextToolModelRule(
                    model_patterns=("qwen*",),
                    dialects=frozenset({TEXT_TOOL_DIALECT_QWEN_TAG}),
                ),
            ),
        ),
        require_reasoning_content_model_ids=_DEEPSEEK_V4_MODEL_IDS,
        allow_post_terminal_noop_choice=True,
        allow_post_terminal_null_usage_noop_choice=True,
        post_terminal_metadata_keys=frozenset(
            {
                "billing_pending",
                "cost_cny",
                "reasoning_available",
                "trace_id",
            }
        ),
    ),
    "lm_studio": OpenAICompatPolicy(display_name="LM Studio"),
    "ovms": OpenAICompatPolicy(display_name="OpenVINO Model Server"),
    "litellm_proxy": OpenAICompatPolicy(
        display_name="LiteLLM Proxy",
        sends_disable_fallbacks=True,
        attribution_response_headers=(
            "x-litellm-model-id",
            "x-litellm-model-api-base",
            "x-litellm-model-group",
            "x-litellm-attempted-retries",
            "x-litellm-attempted-fallbacks",
        ),
    ),
}

_DEFAULT_POLICY = OpenAICompatPolicy()


def compat_policy_for_kind(provider_kind: str) -> OpenAICompatPolicy:
    """Return the dialect policy for a provider kind (default when unknown)."""
    return _POLICIES_BY_KIND.get(provider_kind, _DEFAULT_POLICY)


def known_policy_kinds() -> frozenset[str]:
    """Provider kinds with an explicit policy (for registry sync tests)."""
    return frozenset(_POLICIES_BY_KIND)
