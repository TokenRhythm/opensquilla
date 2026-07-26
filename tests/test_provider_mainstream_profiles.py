from __future__ import annotations

import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderBuildError, ProviderConfig, _build_provider


@pytest.mark.parametrize(
    ("provider", "provider_kind"),
    [
        ("deepseek", "deepseek"),
        ("gemini", "gemini"),
        ("dashscope", "dashscope"),
        ("bailian_coding", "bailian_coding"),
        ("bailian_coding_cn", "bailian_coding"),
        ("moonshot", "moonshot"),
        ("kimi_coding_openai", "moonshot"),
        ("minimax_coding_openai", "minimax"),
        ("mimo_openai", "mimo"),
        ("mistral", "mistral"),
        ("groq", "groq"),
        ("zhipu", "zhipu"),
        ("siliconflow", "siliconflow"),
        ("volcengine", "volcengine"),
        ("byteplus", "byteplus"),
        ("tencent_tokenhub", "tencent_tokenhub"),
        ("tencent_tokenhub_intl", "tencent_tokenhub"),
        ("tencent_token_plan", "tencent_tokenhub"),
        ("tokenrhythm", "tokenrhythm"),
        ("qianfan", "qianfan"),
        ("aihubmix", "aihubmix"),
        ("lm_studio", "lm_studio"),
        ("ovms", "ovms"),
    ],
)
def test_new_openai_compatible_profiles_have_vendor_provider_kind(
    provider: str,
    provider_kind: str,
) -> None:
    assert get_provider_spec(provider).provider_kind == provider_kind


@pytest.mark.parametrize(
    ("provider", "env_key", "base_url"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
        (
            "gemini",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("dashscope", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        (
            "bailian_coding",
            "BAILIAN_API_KEY",
            "https://coding-intl.dashscope.aliyuncs.com/v1",
        ),
        (
            "bailian_coding_cn",
            "BAILIAN_API_KEY",
            "https://coding.dashscope.aliyuncs.com/v1",
        ),
        ("moonshot", "MOONSHOT_API_KEY", "https://api.moonshot.ai/v1"),
        (
            "kimi_coding_openai",
            "KIMI_CODING_API_KEY",
            "https://api.kimi.com/coding/v1",
        ),
        (
            "minimax_coding_openai",
            "MINIMAX_CODING_API_KEY",
            "https://api.minimaxi.com/v1",
        ),
        (
            "mimo_openai",
            "MIMO_API_KEY",
            "https://token-plan-cn.xiaomimimo.com/v1",
        ),
        ("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
        ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
        ("zhipu", "ZAI_API_KEY", "https://open.bigmodel.cn/api/paas/v4"),
        ("siliconflow", "SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1"),
        ("volcengine", "VOLCENGINE_API_KEY", "https://ark.cn-beijing.volces.com/api/v3"),
        ("byteplus", "BYTEPLUS_API_KEY", "https://ark.ap-southeast.bytepluses.com/api/v3"),
        ("tencent_tokenhub", "TENCENT_TOKENHUB_API_KEY", "https://tokenhub.tencentmaas.com/v1"),
        (
            "tencent_tokenhub_intl",
            "TENCENT_TOKENHUB_INTL_API_KEY",
            "https://tokenhub-intl.tencentcloudmaas.com/v1",
        ),
        (
            "tencent_token_plan",
            "TENCENT_TOKEN_PLAN_API_KEY",
            "https://api.lkeap.cloud.tencent.com/plan/v3",
        ),
        ("tokenrhythm", "TOKENRHYTHM_API_KEY", "https://tokenrhythm.studio/v1"),
        ("qianfan", "QIANFAN_API_KEY", "https://qianfan.baidubce.com/v2"),
        ("aihubmix", "AIHUBMIX_API_KEY", "https://aihubmix.com/v1"),
        ("lm_studio", "", "http://localhost:1234/v1"),
        ("ovms", "", "http://localhost:8000/v3"),
    ],
)
def test_openai_compatible_profiles_have_documented_config(
    provider: str,
    env_key: str,
    base_url: str,
) -> None:
    spec = get_provider_spec(provider)

    assert spec.backend == "openai_compat"
    assert spec.env_key == env_key
    assert spec.default_base_url == base_url
    expected_required = frozenset({"model"}) if not env_key else frozenset({"api_key", "model"})
    assert spec.required_fields == expected_required


def test_tencent_tokenhub_profiles_pin_documented_endpoints() -> None:
    """The TokenHub trio maps Tencent's documented per-protocol endpoints:
    OpenAI protocol on <host>/v1, Anthropic Messages on the bare host
    (x-api-key auth), and the international deployment on the separate
    tencentcloudmaas.com domain with its own account/key system."""
    cn = get_provider_spec("tencent_tokenhub")
    assert cn.backend == "openai_compat"
    assert cn.provider_kind == "tencent_tokenhub"
    assert cn.env_key == "TENCENT_TOKENHUB_API_KEY"
    assert cn.default_base_url == "https://tokenhub.tencentmaas.com/v1"
    assert cn.catalog_source == ("tencent-tokenhub",)

    anthropic_compat = get_provider_spec("tencent_tokenhub_anthropic")
    assert anthropic_compat.backend == "anthropic"
    assert anthropic_compat.provider_kind == "tencent_tokenhub"
    assert anthropic_compat.env_key == "TENCENT_TOKENHUB_API_KEY"
    assert anthropic_compat.default_base_url == "https://tokenhub.tencentmaas.com"
    assert anthropic_compat.failure_family == "anthropic"
    assert anthropic_compat.auth_header_style == "x-api-key"

    intl = get_provider_spec("tencent_tokenhub_intl")
    assert intl.backend == "openai_compat"
    assert intl.provider_kind == "tencent_tokenhub"
    assert intl.env_key == "TENCENT_TOKENHUB_INTL_API_KEY"
    assert intl.default_base_url == "https://tokenhub-intl.tencentcloudmaas.com/v1"
    assert intl.catalog_source == ()


def test_tencent_token_plan_profiles_pin_documented_endpoints() -> None:
    """The Token Plan subscription lives on the lkeap host with dedicated
    sk-tp keys: Chat Completions at /plan/v3 and Anthropic Messages at
    /plan/anthropic (bearer auth per Tencent's tool guides)."""
    plan = get_provider_spec("tencent_token_plan")
    assert plan.backend == "openai_compat"
    assert plan.provider_kind == "tencent_tokenhub"
    assert plan.env_key == "TENCENT_TOKEN_PLAN_API_KEY"
    assert plan.default_base_url == "https://api.lkeap.cloud.tencent.com/plan/v3"
    assert plan.capabilities == frozenset({"chat", "coding_plan"})
    assert plan.catalog_source == ("tencent-token-plan",)

    plan_anthropic = get_provider_spec("tencent_token_plan_anthropic")
    assert plan_anthropic.backend == "anthropic"
    assert plan_anthropic.provider_kind == "tencent_tokenhub"
    assert plan_anthropic.env_key == "TENCENT_TOKEN_PLAN_API_KEY"
    assert plan_anthropic.default_base_url == "https://api.lkeap.cloud.tencent.com/plan/anthropic"
    assert plan_anthropic.failure_family == "anthropic"
    assert plan_anthropic.auth_header_style == "bearer"
    assert plan_anthropic.capabilities == frozenset({"chat", "coding_plan"})


def test_model_selector_builds_tencent_token_plan_providers() -> None:
    chat = _build_provider(
        ProviderConfig(provider="tencent_token_plan", model="hy3", api_key="test-key")
    )
    assert isinstance(chat, OpenAIProvider)

    messages = _build_provider(
        ProviderConfig(provider="tencent_token_plan_anthropic", model="hy3", api_key="test-key")
    )
    assert isinstance(messages, AnthropicProvider)


def test_model_selector_builds_tencent_tokenhub_anthropic_provider() -> None:
    built = _build_provider(
        ProviderConfig(provider="tencent_tokenhub_anthropic", model="hy3", api_key="test-key")
    )

    assert isinstance(built, AnthropicProvider)


def test_minimax_mainland_profile_uses_anthropic_compatible_endpoint() -> None:
    spec = get_provider_spec("minimax")

    assert spec.backend == "anthropic"
    assert spec.provider_kind == "minimax"
    assert spec.env_key == "MINIMAX_API_KEY"
    assert spec.default_base_url == "https://api.minimaxi.com/anthropic"
    assert spec.failure_family == "anthropic"


def test_minimax_region_profiles_are_explicit_anthropic_compatible_endpoints() -> None:
    mainland = get_provider_spec("minimax_cn")
    global_ = get_provider_spec("minimax_global")

    assert mainland.backend == "anthropic"
    assert mainland.provider_kind == "minimax"
    assert mainland.env_key == "MINIMAX_CN_API_KEY"
    assert mainland.default_base_url == "https://api.minimaxi.com/anthropic"
    assert mainland.failure_family == "anthropic"

    assert global_.backend == "anthropic"
    assert global_.provider_kind == "minimax"
    assert global_.env_key == "MINIMAX_API_KEY"
    assert global_.default_base_url == "https://api.minimax.io/anthropic"
    assert global_.failure_family == "anthropic"


def test_kimi_coding_anthropic_profile_is_explicit_anthropic_compatible() -> None:
    spec = get_provider_spec("kimi_coding_anthropic")

    assert spec.backend == "anthropic"
    assert spec.provider_kind == "moonshot"
    assert spec.env_key == "KIMI_CODING_API_KEY"
    assert spec.default_base_url == "https://api.kimi.com/coding"
    assert spec.failure_family == "anthropic"
    assert spec.auth_header_style == "bearer"


def test_minimax_coding_anthropic_profile_is_explicit_anthropic_compatible() -> None:
    spec = get_provider_spec("minimax_coding_anthropic")

    assert spec.backend == "anthropic"
    assert spec.provider_kind == "minimax"
    assert spec.env_key == "MINIMAX_CODING_API_KEY"
    assert spec.default_base_url == "https://api.minimaxi.com/anthropic"
    assert spec.failure_family == "anthropic"
    assert spec.auth_header_style == "bearer"


def test_mimo_anthropic_profile_is_explicit_anthropic_compatible() -> None:
    spec = get_provider_spec("mimo_anthropic")

    assert spec.backend == "anthropic"
    assert spec.provider_kind == "mimo"
    assert spec.env_key == "MIMO_API_KEY"
    assert spec.default_base_url == "https://token-plan-cn.xiaomimimo.com/anthropic"
    assert spec.failure_family == "anthropic"
    assert spec.auth_header_style == "bearer"


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("deepseek", "deepseek-chat"),
        ("gemini", "gemini-2.5-flash"),
        ("dashscope", "qwen-plus"),
        ("bailian_coding", "kimi-k2.5"),
        ("bailian_coding_cn", "qwen3.7-plus"),
        ("moonshot", "kimi-k2.5"),
        ("kimi_coding_openai", "kimi-for-coding"),
        ("mistral", "mistral-large-latest"),
        ("groq", "llama-3.3-70b-versatile"),
        ("zhipu", "glm-4.5"),
        ("siliconflow", "deepseek-ai/DeepSeek-V3"),
        ("volcengine", "ark-model-id"),
        ("byteplus", "ark-endpoint-id"),
        ("qianfan", "ernie-4.5-turbo-128k"),
        ("aihubmix", "openai/gpt-5-mini"),
        ("minimax_openai", "MiniMax-M2.7"),
        ("minimax_coding_openai", "MiniMax-M2.7"),
        ("mimo_openai", "mimo-v2.5"),
        ("tencent_tokenhub", "hy3"),
        ("tencent_tokenhub_intl", "deepseek-v3.2"),
        ("tencent_token_plan", "hy3"),
        ("lm_studio", "local-model"),
        ("ovms", "llama3"),
    ],
)
def test_model_selector_builds_registered_openai_compatible_providers(
    provider: str,
    model: str,
) -> None:
    built = _build_provider(ProviderConfig(provider=provider, model=model, api_key="test-key"))

    assert isinstance(built, OpenAIProvider)


def test_model_selector_preserves_volcengine_coding_plan_responses_contract() -> None:
    spec = get_provider_spec("volcengine_coding_plan")

    assert spec.backend == "openai_responses"
    assert spec.provider_kind == "volcengine_coding_plan"
    assert spec.env_key == "VOLCENGINE_API_KEY"
    assert spec.default_base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert spec.capabilities == frozenset({"chat", "coding_plan", "responses"})

    built = _build_provider(
        ProviderConfig(
            provider="volcengine_coding_plan",
            model="doubao-seed-2.0-pro",
            api_key="test-key",
        )
    )

    assert isinstance(built, OpenAIResponsesProvider)


def test_model_selector_builds_minimax_mainland_anthropic_provider() -> None:
    built = _build_provider(
        ProviderConfig(provider="minimax", model="MiniMax-M2.7", api_key="test-key")
    )

    assert isinstance(built, AnthropicProvider)


def test_model_selector_builds_minimax_openai_compatible_provider() -> None:
    spec = get_provider_spec("minimax_openai")
    assert spec.backend == "openai_compat"
    assert spec.provider_kind == "minimax"
    assert spec.env_key == "MINIMAX_API_KEY"
    assert spec.default_base_url == "https://api.minimax.io/v1"

    built = _build_provider(
        ProviderConfig(provider="minimax_openai", model="MiniMax-M2.7", api_key="test-key")
    )

    assert isinstance(built, OpenAIProvider)


@pytest.mark.parametrize("provider", ["minimax_cn", "minimax_global"])
def test_model_selector_builds_explicit_minimax_region_anthropic_providers(
    provider: str,
) -> None:
    built = _build_provider(
        ProviderConfig(provider=provider, model="MiniMax-M2.7", api_key="test-key")
    )

    assert isinstance(built, AnthropicProvider)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("kimi_coding_anthropic", "kimi-for-coding"),
        ("minimax_coding_anthropic", "MiniMax-M2.7"),
        ("mimo_anthropic", "mimo-v2.5-pro"),
    ],
)
def test_model_selector_builds_coding_plan_anthropic_providers(
    provider: str,
    model: str,
) -> None:
    built = _build_provider(
        ProviderConfig(provider=provider, model=model, api_key="test-key")
    )

    assert isinstance(built, AnthropicProvider)


def test_vllm_requires_explicit_base_url() -> None:
    with pytest.raises(ProviderBuildError, match="requires an explicit base_url"):
        _build_provider(ProviderConfig(provider="vllm", model="served-model", api_key="unused"))

    built = _build_provider(
        ProviderConfig(
            provider="vllm",
            model="served-model",
            api_key="unused",
            base_url="http://localhost:8001/v1",
        )
    )

    assert isinstance(built, OpenAIProvider)


def test_azure_default_construction_is_outside_a_stage_support() -> None:
    with pytest.raises(ProviderBuildError, match="requires an explicit base_url"):
        _build_provider(
            ProviderConfig(provider="azure", model="deployment-name", api_key="test-key")
        )
