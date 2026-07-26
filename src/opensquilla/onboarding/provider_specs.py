"""Onboarding-friendly provider catalog derived from provider.registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.provider.preset_registry import ProviderPreset, get_preset
from opensquilla.provider.registry import ProviderSpec, list_provider_specs

FieldType = Literal["text", "password", "select", "bool"]
Deployment = Literal["cloud", "local", "custom", "oauth"]
# "verified": the full agent stack (tools, reasoning, replay) has been
# exercised against this provider. "experimental": registered and
# runtime-capable, offered with a visible caveat instead of being hidden.
Verification = Literal["verified", "experimental"]


@dataclass(frozen=True)
class ProviderSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | bool | None = None
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class ProviderSetupSpec:
    provider_id: str
    label: str
    backend: str
    provider_kind: str
    runtime_supported: bool
    verification: Verification
    env_key: str
    default_base_url: str
    accepts_api_key: bool
    requires_api_key: bool
    requires_base_url: bool
    router_supported: bool
    deployment: Deployment
    blocking: bool
    can_probe: bool
    readme_scenarios: tuple[str, ...]
    what_you_need: tuple[str, ...]
    default_direct_model: str
    capabilities: tuple[str, ...]
    fields: tuple[ProviderSetupField, ...]


_PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "azure": "Azure OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama (local)",
    "deepseek": "DeepSeek",
    "gemini": "Google Gemini",
    "dashscope": "Aliyun DashScope",
    "bailian_coding": "Bailian Coding (International)",
    "bailian_coding_cn": "Bailian Coding (Mainland China)",
    "moonshot": "Moonshot AI",
    "kimi_coding_openai": "Kimi Coding OpenAI-compatible",
    "kimi_coding_anthropic": "Kimi Coding Anthropic-compatible",
    "minimax": "MiniMax",
    "minimax_openai": "MiniMax OpenAI-compatible",
    "minimax_coding_openai": "MiniMax Coding OpenAI-compatible",
    "minimax_coding_anthropic": "MiniMax Coding Anthropic-compatible",
    "minimax_cn": "MiniMax Mainland",
    "minimax_global": "MiniMax Global",
    "mimo_openai": "MiMo OpenAI-compatible",
    "mimo_anthropic": "MiMo Anthropic-compatible",
    "mistral": "Mistral",
    "groq": "Groq",
    "zhipu": "Zhipu (Z.AI)",
    "qianfan": "Baidu Qianfan",
    "siliconflow": "SiliconFlow",
    "aihubmix": "AIHubMix",
    "volcengine": "Volcengine Ark",
    "byteplus": "BytePlus Ark",
    "tencent_tokenhub": "Tencent TokenHub",
    "tencent_tokenhub_anthropic": "Tencent TokenHub (Anthropic)",
    "tencent_tokenhub_intl": "Tencent TokenHub International",
    "tencent_token_plan": "Tencent Token Plan",
    "tencent_token_plan_anthropic": "Tencent Token Plan (Anthropic)",
    "tokenrhythm": "TokenRhythm",
    "vllm": "vLLM (self-hosted)",
    "custom": "Custom OpenAI-compatible endpoint",
    "litellm_proxy": "LiteLLM Proxy",
    "lm_studio": "LM Studio (local)",
    "ovms": "OpenVINO Model Server",
    "volcengine_coding_plan": "Volcengine Coding Plan (OpenAI Responses)",
    "volcengine_coding_plan_anthropic": "Volcengine Coding Plan (Anthropic)",
    "byteplus_coding_plan": "BytePlus Coding Plan (OpenAI Responses)",
    "byteplus_coding_plan_anthropic": "BytePlus Coding Plan (Anthropic)",
    "openai_codex": "OpenAI Codex (OAuth)",
    "github_copilot": "GitHub Copilot (OAuth)",
    "openai_responses": "OpenAI (Responses API)",
}

# Catalog display order: TokenRhythm is the recommended first pick, then
# OpenRouter; everything else sorts by label. This one map orders the Web UI
# dropdown, CLI ``providers list``, and the interactive onboarding picker —
# every surface renders the server order.
_CATALOG_RANK = {
    "tokenrhythm": 0,
    "openrouter": 1,
}

_INLINE_ROUTER_SUPPORTED_PROVIDER_IDS: frozenset[str] = frozenset(
    {
        "qianfan",
        "volcengine_coding_plan",
        "kimi_coding_openai",
        "kimi_coding_anthropic",
        "minimax",
        "minimax_cn",
        "minimax_global",
        "minimax_coding_openai",
        "minimax_coding_anthropic",
        "mimo_openai",
        "mimo_anthropic",
    }
)

_ONBOARDING_VERIFIED_PROVIDER_IDS = frozenset(
    {
        "openrouter",
        "openai",
        "openai_responses",
        "anthropic",
        "ollama",
        "deepseek",
        "gemini",
        "dashscope",
        "moonshot",
        "zhipu",
        "qianfan",
        "volcengine",
        "byteplus",
        "tokenrhythm",
    }
)

_LOCAL_PROVIDER_IDS = frozenset({"ollama", "vllm", "lm_studio", "ovms"})
_OAUTH_PROVIDER_IDS = frozenset({"openai_codex", "github_copilot"})
_BAILIAN_CODING_PROVIDER_IDS = frozenset({"bailian_coding", "bailian_coding_cn"})
_DIRECT_MODEL_DEFAULTS = {
    "bailian_coding": "qwen3.7-plus",
    "bailian_coding_cn": "qwen3.7-plus",
}


def _deployment_for(spec: ProviderSpec) -> Deployment:
    if spec.provider_id in _LOCAL_PROVIDER_IDS:
        return "local"
    if spec.provider_id in _OAUTH_PROVIDER_IDS:
        return "oauth"
    if spec.requires_base_url():
        return "custom"
    return "cloud"


def _has_curated_router_ladder(provider_id: str) -> bool:
    """True when the provider ships a curated tier ladder (packaged or inline).

    Synthesized presets carry no per-tier model ladder, so they do not count:
    their providers still need an operator-supplied model id.
    """
    preset = get_preset(provider_id)
    return preset is not None and not preset.synthesized


def _is_router_supported_provider(provider_id: str) -> bool:
    return (
        _has_curated_router_ladder(provider_id)
        or provider_id in _INLINE_ROUTER_SUPPORTED_PROVIDER_IDS
    )


def _what_you_need(spec: ProviderSpec) -> tuple[str, ...]:
    needs: list[str] = []
    if not _is_router_supported_provider(spec.provider_id):
        needs.append(
            "A local model name available from your model server."
            if spec.provider_id in _LOCAL_PROVIDER_IDS
            else "A provider model id."
        )
    if spec.requires_api_key():
        if spec.provider_id in _BAILIAN_CODING_PROVIDER_IDS:
            needs.append("A dedicated Coding Plan API key starting with sk-sp-.")
        else:
            needs.append(
                f"API key via {spec.env_key} or a one-time paste."
                if spec.env_key
                else "Provider API key."
            )
    if spec.requires_base_url():
        needs.append("Provider base URL.")
    if spec.provider_id in _LOCAL_PROVIDER_IDS:
        needs.append("A reachable local model server.")
    if not needs:
        needs.append("No API key required for the default local path.")
    return tuple(needs)


def _default_direct_model(provider_id: str) -> str:
    if default_model := _DIRECT_MODEL_DEFAULTS.get(provider_id):
        return default_model
    preset = get_preset(provider_id)
    if preset is None or preset.synthesized:
        if provider_id in _INLINE_ROUTER_SUPPORTED_PROVIDER_IDS and preset is not None:
            return preset.default_model
        return ""
    if _has_curated_router_ladder(provider_id):
        tiers = preset.tier_defaults()
        tier = tiers.get("c1") or tiers.get("c0") or {}
        return str(tier.get("model") or "")
    if provider_id in _INLINE_ROUTER_SUPPORTED_PROVIDER_IDS:
        return preset.default_model
    return ""


def _model_description(spec: ProviderSpec, *, router_supported: bool) -> str:
    if router_supported:
        return (
            "Optional direct fallback model. Leave blank to use this provider's "
            "default direct model. SquillaRouter tiers are configured separately."
        )
    if spec.provider_id in _LOCAL_PROVIDER_IDS:
        return "Required local model id. Use a model available from your local model server."
    return "Required model id for this provider."


def _fields_for(spec: ProviderSpec) -> tuple[ProviderSetupField, ...]:
    router_supported = _is_router_supported_provider(spec.provider_id)
    api_key_description = (
        (
            "Use the dedicated Coding Plan API key starting with sk-sp-. "
            "Standard Model Studio keys are not interchangeable. "
        )
        if spec.provider_id in _BAILIAN_CODING_PROVIDER_IDS
        else ""
    )
    api_key_description += (
        "Saved as plaintext api_key in the config file and used "
        f"ahead of {spec.env_key}. Leave blank to read "
        f"{spec.env_key} from the environment instead."
        if spec.env_key
        else "Saved as plaintext api_key in the config file."
    )
    return (
        ProviderSetupField(
            name="model",
            label="Model id",
            field_type="text",
            required=not router_supported,
            default=_default_direct_model(spec.provider_id),
            description=_model_description(spec, router_supported=router_supported),
        ),
        ProviderSetupField(
            name="api_key",
            label="API key",
            field_type="password",
            required=spec.requires_api_key(),
            default="",
            description=api_key_description,
            secret=True,
        ),
        *(
            (
                ProviderSetupField(
                    name="api_key_env",
                    label="API key env",
                    field_type="text",
                    required=False,
                    default=spec.env_key,
                    description="Environment variable name the gateway reads for this key.",
                ),
            )
            if spec.requires_api_key()
            else ()
        ),
        ProviderSetupField(
            name="base_url",
            label="Base URL",
            field_type="text",
            required=spec.requires_base_url(),
            default=spec.default_base_url,
            description="Override the upstream HTTP base URL.",
        ),
        ProviderSetupField(
            name="proxy",
            label="HTTP proxy",
            field_type="text",
            required=False,
            default="",
            description=(
                "Optional explicit HTTP proxy URL "
                "(e.g. http://127.0.0.1:7890)."
            ),
        ),
    )


def _to_setup_spec(spec: ProviderSpec) -> ProviderSetupSpec:
    # Registry runtime support decides availability; the verified set only
    # decides the tier badge. Hiding runtime-capable providers behind an
    # invisible gate produced unexplainable blank dropdowns for operators who
    # configured them via TOML.
    runtime_supported = spec.runtime_supported
    verification: Verification = (
        "verified"
        if spec.provider_id in _ONBOARDING_VERIFIED_PROVIDER_IDS
        else "experimental"
    )
    label = _PROVIDER_LABELS.get(spec.provider_id, spec.provider_id)
    if runtime_supported and verification == "experimental":
        label = f"{label} (experimental)"
    return ProviderSetupSpec(
        provider_id=spec.provider_id,
        label=label,
        backend=spec.backend,
        provider_kind=spec.provider_kind,
        runtime_supported=runtime_supported,
        verification=verification,
        env_key=spec.env_key,
        default_base_url=spec.default_base_url,
        # ``requires_api_key`` answers whether setup is blocked without a
        # key; it does not answer whether the transport accepts an optional
        # Bearer credential.  Local/custom OpenAI-compatible endpoints often
        # support both authenticated and unauthenticated deployments.  OAuth
        # is the one registry credential mode that is not an API-key field.
        accepts_api_key=spec.env_key != "OAuth",
        requires_api_key=spec.requires_api_key(),
        requires_base_url=spec.requires_base_url(),
        router_supported=_is_router_supported_provider(spec.provider_id),
        deployment=_deployment_for(spec),
        blocking=True,
        # Runtime-supported providers can be probed live (a small,
        # provider-bounded chat via onboarding.provider.probe) before the
        # config is saved.
        can_probe=runtime_supported,
        readme_scenarios=("first-run setup", "quick terminal install"),
        what_you_need=_what_you_need(spec),
        default_direct_model=_default_direct_model(spec.provider_id),
        capabilities=tuple(sorted(spec.capabilities)),
        fields=_fields_for(spec),
    )


def list_provider_setup_specs() -> list[ProviderSetupSpec]:
    specs = [_to_setup_spec(s) for s in list_provider_specs()]
    return sorted(
        specs,
        key=lambda s: (
            _CATALOG_RANK.get(s.provider_id, len(_CATALOG_RANK)),
            s.label.lower(),
            s.provider_id,
        ),
    )


def get_provider_setup_spec(provider_id: str) -> ProviderSetupSpec:
    for spec in list_provider_setup_specs():
        if spec.provider_id == provider_id:
            return spec
    raise KeyError(f"unknown provider: {provider_id!r}")


def _preset_payload(preset: ProviderPreset) -> dict[str, Any]:
    """Wire view of one registry preset (packaged or synthesized).

    Tier rows reuse the router catalog's camelCase tier shape so preset
    pickers and router profile cards render through the same component.
    """
    from opensquilla.onboarding.router_specs import _tier_payload

    return {
        "presetId": preset.preset_id,
        "label": preset.label,
        "description": preset.description,
        "synthesized": preset.synthesized,
        "defaultModel": preset.default_model,
        "tiers": {
            name: _tier_payload(tier)
            for name, tier in preset.tier_defaults().items()
        },
    }


def _provider_presets_payload(provider_id: str) -> list[dict[str, Any]]:
    """Registry presets for one provider — exactly one per provider today.

    A list on the wire on purpose: multiple curated presets per provider is
    the expected evolution, and clients should already iterate.
    """
    preset = get_preset(provider_id)
    return [_preset_payload(preset)] if preset is not None else []


def _provider_entry_payload(s: ProviderSetupSpec) -> dict[str, Any]:
    preset = get_preset(s.provider_id)
    return {
        "providerId": s.provider_id,
        "label": s.label,
        "backend": s.backend,
        "providerKind": s.provider_kind,
        "runtimeSupported": s.runtime_supported,
        "verification": s.verification,
        "envKey": s.env_key,
        "defaultBaseUrl": s.default_base_url,
        "acceptsApiKey": s.accepts_api_key,
        "requiresApiKey": s.requires_api_key,
        "requiresBaseUrl": s.requires_base_url,
        "routerSupported": s.router_supported,
        "deployment": s.deployment,
        "blocking": s.blocking,
        "canProbe": s.can_probe,
        "readmeScenarios": list(s.readme_scenarios),
        "whatYouNeed": list(s.what_you_need),
        "defaultDirectModel": s.default_direct_model,
        # Preset surface (additive): the provider's registry preset(s) and the
        # preset-declared default model. defaultDirectModel stays the
        # legacy-derived direct-model hint; defaultModel is the preset's.
        "defaultModel": preset.default_model if preset is not None else "",
        "presets": _provider_presets_payload(s.provider_id),
        "capabilities": list(s.capabilities),
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.field_type,
                "required": f.required,
                "default": f.default,
                "description": f.description,
                "secret": f.secret,
            }
            for f in s.fields
        ],
    }


def provider_catalog_payload() -> list[dict[str, Any]]:
    return [
        _provider_entry_payload(s)
        for s in list_provider_setup_specs()
        if s.runtime_supported
    ]
