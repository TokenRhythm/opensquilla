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
from urllib.parse import urlsplit

from opensquilla.endpoint_identity import base_url_matches_official_api

from .model_identity import DEEPSEEK_V4_MODEL_IDS
from .qwen_token_plan import (
    QWEN_TOKEN_PLAN_DEEPSEEK_V4_MODEL_IDS,
    QWEN_TOKEN_PLAN_FORCE_THINKING_MODEL_IDS,
    QWEN_TOKEN_PLAN_GLM_MODEL_IDS,
    QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS,
    QWEN_TOKEN_PLAN_KIMI_MODEL_IDS,
    QWEN_TOKEN_PLAN_PRESERVE_THINKING_MODEL_IDS,
)

TextToolDialect = Literal["qwen_tag", "minimax_xml", "plain_json", "deepseek_dsml"]
ReasoningReplayScope = Literal["all_assistant", "tool_call_assistant"]

TEXT_TOOL_DIALECT_QWEN_TAG: TextToolDialect = "qwen_tag"
TEXT_TOOL_DIALECT_MINIMAX_XML: TextToolDialect = "minimax_xml"
TEXT_TOOL_DIALECT_PLAIN_JSON: TextToolDialect = "plain_json"
TEXT_TOOL_DIALECT_DEEPSEEK_DSML: TextToolDialect = "deepseek_dsml"


# DSML is executable syntax, so its authorization stays independent from
# reasoning/model-family helpers and names every trusted wire identity exactly.
_DEEPSEEK_DSML_MODEL_IDS = (
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
)
_TOKENRHYTHM_DSML_MODEL_IDS = (
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
    "tokenrhythm/deepseek-v4-flash",
    "tokenrhythm/deepseek-v4-flash-0731",
    "tokenrhythm/deepseek-v4-pro",
)
_TOKENRHYTHM_0813_MODEL_IDS = frozenset(
    {
        "deepseek-v4-pro-0813",
        "tokenrhythm/deepseek-v4-pro-0813",
    }
)
_TOKENRHYTHM_LEGACY_CUSTOM_REPLAY_MODEL_IDS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "tokenrhythm/deepseek-v4-flash",
        "tokenrhythm/deepseek-v4-flash-0731",
        "tokenrhythm/deepseek-v4-pro",
    }
)
_TOKENRHYTHM_V4_MODEL_IDS = (
    _TOKENRHYTHM_LEGACY_CUSTOM_REPLAY_MODEL_IDS | _TOKENRHYTHM_0813_MODEL_IDS
)
_TOKENRHYTHM_V4_LOW_EFFORT_MODEL_IDS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro-0813",
        "tokenrhythm/deepseek-v4-flash",
        "tokenrhythm/deepseek-v4-flash-0731",
        "tokenrhythm/deepseek-v4-pro-0813",
    }
)
_OPENROUTER_DSML_MODEL_IDS = (
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
)


def _matches_exact_https_endpoint(
    base_url: str,
    *,
    endpoint_hosts: frozenset[str],
    endpoint_paths: frozenset[str],
) -> bool:
    """Whether *base_url* is one of a rule's exact trusted API roots."""

    if not endpoint_hosts:
        return True
    normalized_url = str(base_url or "").strip()
    if "?" in normalized_url or "#" in normalized_url:
        return False
    try:
        parsed = urlsplit(normalized_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return False
    path = parsed.path.rstrip("/")
    return bool(
        parsed.scheme.lower() == "https"
        and host in endpoint_hosts
        and (port is None or port == 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and path in endpoint_paths
    )


@dataclass(frozen=True)
class TextToolModelRule:
    """Allow text-tool dialects only for explicitly matched model ids."""

    model_patterns: tuple[str, ...]
    dialects: frozenset[TextToolDialect]
    endpoint_hosts: frozenset[str] = frozenset()
    endpoint_paths: frozenset[str] = frozenset()

    def matches(self, model: str, base_url: str = "") -> bool:
        normalized = model.strip().lower()
        if not any(
            fnmatchcase(normalized, pattern.lower()) for pattern in self.model_patterns
        ):
            return False
        return _matches_exact_https_endpoint(
            base_url,
            endpoint_hosts=self.endpoint_hosts,
            endpoint_paths=self.endpoint_paths,
        )


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

    def dialects_for_model(
        self,
        model: str,
        base_url: str = "",
    ) -> frozenset[TextToolDialect]:
        enabled = set(self.dialects)
        for rule in self.model_rules:
            if rule.matches(model, base_url):
                enabled.update(rule.dialects)
        return frozenset(enabled)

    @property
    def enabled(self) -> bool:
        return bool(self.dialects or self.model_rules)


@dataclass(frozen=True)
class ReasoningModelRule:
    """Exact, endpoint-scoped reasoning request compatibility.

    Reasoning history is provider state, but it is still untrusted request
    data with provider-specific wire limits.  These rules only shape the
    physical request view; callers retain the original ``Message`` objects.
    """

    model_ids: frozenset[str]
    endpoint_hosts: frozenset[str] = frozenset()
    endpoint_paths: frozenset[str] = frozenset()
    replay_scope: ReasoningReplayScope = "all_assistant"
    require_reasoning_content: bool = False
    max_reasoning_content_utf16_units: int | None = None
    reasoning_format: str = ""
    low_effort_model_ids: frozenset[str] = frozenset()
    thinking_tool_choice_auto_only: bool = False
    prefer_pinned_tool_choice_over_thinking: bool = False

    def matches(self, model: str, base_url: str) -> bool:
        """Match only a trusted raw model id and an exact HTTPS API root."""

        if model.strip().lower() not in self.model_ids:
            return False
        return _matches_exact_https_endpoint(
            base_url,
            endpoint_hosts=self.endpoint_hosts,
            endpoint_paths=self.endpoint_paths,
        )


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

    # Tool names whose itemless arrays may be projected to string items on
    # this provider's official endpoint. This is deliberately allowlisted:
    # an itemless JSON Schema array accepts arbitrary values, so a string
    # fallback is safe only when the tool's wire semantics are textual.
    tool_schema_string_item_fallback_tools: frozenset[str] = frozenset()

    # Exact API root where the string-item projection is required. A tool
    # allowlist alone is not sufficient evidence for custom endpoints that
    # happen to use the same provider kind or hostname.
    tool_schema_string_item_fallback_api_root: str = ""

    # Whether the chat-completions endpoint reliably supports native
    # ``response_format.type=json_schema``.  When false, the OpenAI-compatible
    # adapter keeps the same provider/model and places the authoritative
    # schema in the system prompt instead; callers still validate the returned
    # artifact locally.
    supports_native_json_schema_output: bool = True

    # Whether the endpoint supports the less expressive
    # ``response_format.type=json_object`` mode.  This is useful when native
    # JSON Schema is unavailable: the schema remains in the trusted system
    # prompt while the request still asks the upstream for a JSON object.
    supports_json_object_output: bool = False

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

    # Exact model/endpoint rules take precedence over the legacy basename
    # fields below.  They are used when an aggregator has a narrower replay
    # contract than the upstream model family it exposes.
    reasoning_model_rules: tuple[ReasoningModelRule, ...] = ()

    # Reasoning format assumed when no model capabilities are available.
    default_reasoning_format: str = ""

    # One provider-native reasoning dialect may be valid only at an exact API
    # root. A custom endpoint using another explicitly configured dialect is
    # unaffected, and an empty pair leaves compatible relays unrestricted.
    official_reasoning_dialect: str = ""
    official_reasoning_api_root: str = ""

    # Models that need an explicit thinking enable/disable payload even when
    # no capability profile is available (exact ids, lowercase).
    thinking_toggle_model_ids: frozenset[str] = frozenset()

    # Models that require reasoning_content on every assistant message —
    # including an empty string when there is none (exact ids, lowercase).
    require_reasoning_content_model_ids: frozenset[str] = frozenset()

    # Models that stream reasoning by default and need it explicitly disabled
    # when thinking is off (exact ids, lowercase).
    disable_reasoning_by_default_models: frozenset[str] = frozenset()

    # Model id prefixes that reject enable_thinking=False (forced-thinking
    # endpoints). When a disable-thinking payload would be emitted for a model
    # matching one of these prefixes, the off-payload is omitted entirely so
    # the request still succeeds. Checked at the single wire emitter in
    # openai.py, covering all five agent-loop disable sites at once.
    thinking_required_model_prefixes: tuple[str, ...] = ()

    # Exact forced-thinking ids for multi-family endpoints where a prefix
    # would be too broad. These models receive enable_thinking=True even
    # when the local preference is off.
    force_thinking_model_ids: frozenset[str] = frozenset()

    # Models whose reasoning history can be replayed and whose request must
    # opt into that continuity with preserve_thinking=True.
    preserve_thinking_model_ids: frozenset[str] = frozenset()

    # Reasoning models that require an assistant reasoning_content key only
    # while thinking is enabled. The tool-call set is narrower: some models
    # require the key only on assistant tool-call turns.
    require_reasoning_content_when_thinking_model_ids: frozenset[str] = frozenset()
    require_tool_call_reasoning_content_when_thinking_model_ids: frozenset[str] = (
        frozenset()
    )

    # Thinking-mode tool choice accepts only auto/none on this endpoint.
    thinking_tool_choice_auto_only: bool = False
    # Models that are reasoning-only upstream even when the endpoint does not
    # accept or emit an explicit enable_thinking toggle.  Their tool selector
    # follows the same auto/none restriction as an explicitly enabled request.
    implicit_thinking_tool_choice_model_ids: frozenset[str] = frozenset()
    # When a non-forced model receives an explicit pinned tool selector,
    # preserve the selector by disabling thinking instead of silently changing
    # the selected function. Forced-thinking models still normalize to auto.
    prefer_pinned_tool_choice_over_thinking: bool = False

    # Models that require tool_stream=True whenever tools are present.
    tool_stream_model_ids: frozenset[str] = frozenset()

    # A thinking-only model may impose a minimum sampling temperature.
    temperature_floor_model_ids: frozenset[str] = frozenset()
    temperature_floor: float = 0.0

    # Provider listings can mix non-chat products into the same /models
    # response. Keep picker filtering declarative rather than branching in
    # the generic OpenAI transport.
    model_listing_excluded_ids: frozenset[str] = frozenset()

    # Omit a framework-default thinking budget so the service can apply its
    # own model default; an explicitly configured budget is still sent.
    omit_implicit_thinking_budget: bool = False

    @property
    def text_tool_synthesis(self) -> bool:
        """Deprecated read-only compatibility view.

        Older diagnostics inspected one provider-wide boolean.  Keep that
        observation surface without letting the boolean control execution;
        callers that need an answer for one deployment must use
        ``text_tool_profile.dialects_for_model(model, base_url)``.
        """

        return self.text_tool_profile.enabled

    def allows_string_item_schema_projection(self, tool_name: str, base_url: str) -> bool:
        """Return whether a trusted tool may use the lossy wire projection."""

        candidate_base_url = str(base_url or "")
        if not candidate_base_url or candidate_base_url != candidate_base_url.strip():
            return False
        return bool(
            tool_name in self.tool_schema_string_item_fallback_tools
            and self.tool_schema_string_item_fallback_api_root
            and base_url_matches_official_api(
                self.tool_schema_string_item_fallback_api_root,
                candidate_base_url,
            )
        )


def effective_reasoning_format(
    policy: OpenAICompatPolicy,
    reasoning_format: str,
    base_url: str,
) -> str:
    """Suppress a provider-native dialect outside its exact official API root."""

    restricted_dialect = policy.official_reasoning_dialect.strip().lower()
    if (
        restricted_dialect
        and reasoning_format.strip().lower() == restricted_dialect
        and not base_url_matches_official_api(
            policy.official_reasoning_api_root,
            base_url,
        )
    ):
        return ""
    return reasoning_format


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
                TextToolModelRule(
                    model_patterns=_OPENROUTER_DSML_MODEL_IDS,
                    dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
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
        supports_native_json_schema_output=False,
        supports_json_object_output=True,
        text_tool_profile=TextToolCompatProfile(
            model_rules=(
                TextToolModelRule(
                    model_patterns=_DEEPSEEK_DSML_MODEL_IDS,
                    dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
                ),
            ),
        ),
        # Reasoning replay is gated on the exact V4 ids (below), not on the
        # capability format: non-V4 DeepSeek models must not get replay.
        thinking_toggle_model_ids=DEEPSEEK_V4_MODEL_IDS,
        require_reasoning_content_model_ids=DEEPSEEK_V4_MODEL_IDS,
    ),
    "gemini": OpenAICompatPolicy(
        display_name="Gemini",
        official_host="generativelanguage.googleapis.com",
        tool_schema_string_item_fallback_tools=frozenset({"create_csv"}),
        tool_schema_string_item_fallback_api_root=(
            "https://generativelanguage.googleapis.com/v1beta/openai"
        ),
    ),
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
        thinking_required_model_prefixes=("qwen3.8-",),
        thinking_tool_choice_auto_only=True,
        implicit_thinking_tool_choice_model_ids=DEEPSEEK_V4_MODEL_IDS,
    ),
    "bailian_coding": OpenAICompatPolicy(display_name="Bailian Coding"),
    "qwen_token_plan": OpenAICompatPolicy(
        display_name="Qwen Token Plan",
        official_host="token-plan.cn-beijing.maas.aliyuncs.com",
        text_tool_profile=TextToolCompatProfile(
            model_rules=(
                TextToolModelRule(
                    model_patterns=("qwen*",),
                    dialects=frozenset({TEXT_TOOL_DIALECT_QWEN_TAG}),
                ),
            ),
        ),
        stream_timeout_fallback=True,
        force_thinking_model_ids=QWEN_TOKEN_PLAN_FORCE_THINKING_MODEL_IDS,
        preserve_thinking_model_ids=QWEN_TOKEN_PLAN_PRESERVE_THINKING_MODEL_IDS,
        require_reasoning_content_when_thinking_model_ids=(
            QWEN_TOKEN_PLAN_DEEPSEEK_V4_MODEL_IDS
        ),
        require_tool_call_reasoning_content_when_thinking_model_ids=(
            QWEN_TOKEN_PLAN_KIMI_MODEL_IDS
        ),
        thinking_tool_choice_auto_only=True,
        prefer_pinned_tool_choice_over_thinking=True,
        tool_stream_model_ids=QWEN_TOKEN_PLAN_GLM_MODEL_IDS,
        temperature_floor_model_ids=frozenset({"qwen3.8-max-preview"}),
        temperature_floor=0.6,
        model_listing_excluded_ids=frozenset(QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS),
        omit_implicit_thinking_budget=True,
    ),
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
    "zhipu": OpenAICompatPolicy(
        display_name="Zhipu",
        official_host="open.bigmodel.cn",
        official_reasoning_dialect="zai",
        official_reasoning_api_root="https://open.bigmodel.cn/api/paas/v4",
    ),
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
    # TokenRhythm relays several model families behind one host.  Its V4
    # request contract is deliberately endpoint- and raw-id-scoped: ordinary
    # assistant reasoning is withheld, tool-call reasoning is echoed only
    # within the gateway's field limit, and controls use the DeepSeek-shaped
    # wire dialect without changing catalog capabilities.
    "tokenrhythm": OpenAICompatPolicy(
        display_name="TokenRhythm",
        official_host="tokenrhythm.studio",
        supports_explicit_prompt_cache=True,
        supports_native_json_schema_output=False,
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
                TextToolModelRule(
                    model_patterns=_TOKENRHYTHM_DSML_MODEL_IDS,
                    dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
                ),
                TextToolModelRule(
                    model_patterns=tuple(sorted(_TOKENRHYTHM_0813_MODEL_IDS)),
                    dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
                    endpoint_hosts=frozenset({"tokenrhythm.studio"}),
                    endpoint_paths=frozenset({"", "/v1"}),
                ),
            ),
        ),
        reasoning_model_rules=(
            ReasoningModelRule(
                model_ids=_TOKENRHYTHM_V4_MODEL_IDS,
                endpoint_hosts=frozenset({"tokenrhythm.studio"}),
                endpoint_paths=frozenset({"", "/v1"}),
                replay_scope="tool_call_assistant",
                require_reasoning_content=True,
                max_reasoning_content_utf16_units=50_000,
                reasoning_format="deepseek",
                low_effort_model_ids=_TOKENRHYTHM_V4_LOW_EFFORT_MODEL_IDS,
                thinking_tool_choice_auto_only=True,
                prefer_pinned_tool_choice_over_thinking=True,
            ),
            # Custom endpoints keep the previous exact-id replay behavior but
            # do not inherit TokenRhythm's official controls or field limit.
            ReasoningModelRule(
                model_ids=_TOKENRHYTHM_LEGACY_CUSTOM_REPLAY_MODEL_IDS,
                require_reasoning_content=True,
            ),
        ),
        # TokenRhythm rejects a required selector for 0813 even when thinking
        # is explicitly disabled. The request constraint compares basenames,
        # so this exact entry covers both raw and provider-qualified ids.
        implicit_thinking_tool_choice_model_ids=frozenset(
            {"deepseek-v4-pro-0813"}
        ),
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
