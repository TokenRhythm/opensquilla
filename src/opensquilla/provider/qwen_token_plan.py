"""Stable Qwen Token Plan protocol and model identifiers.

The subscription exposes one model allowlist through both OpenAI-compatible
Chat Completions and Anthropic Messages endpoints.  Keep identifiers shared
between registry metadata, request compatibility, and listing code so the
two protocol profiles cannot drift.
"""

from __future__ import annotations

QWEN_TOKEN_PLAN_API_KEY_ENV = "QWEN_TOKEN_PLAN_API_KEY"
QWEN_TOKEN_PLAN_OPENAI_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
QWEN_TOKEN_PLAN_ANTHROPIC_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"
)
QWEN_TOKEN_PLAN_IMAGE_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
)

# Team is the superset of the personal plan.  Exact spellings matter: the
# upstream rejects aliases and differently cased model ids.
QWEN_TOKEN_PLAN_MODEL_IDS: tuple[str, ...] = (
    "qwen3.8-max-preview",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.6-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v3.2",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "kimi-k2.5",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "MiniMax-M2.5",
)
QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS: tuple[str, ...] = (
    "wan2.7-image",
    "wan2.7-image-pro",
)

QWEN_TOKEN_PLAN_DEEPSEEK_V4_MODEL_IDS = frozenset(
    {"deepseek-v4-pro", "deepseek-v4-flash"}
)
QWEN_TOKEN_PLAN_KIMI_MODEL_IDS = frozenset(
    {"kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"}
)
QWEN_TOKEN_PLAN_GLM_MODEL_IDS = frozenset({"glm-5.2", "glm-5.1", "glm-5"})

# These models are documented or verified as reasoning-only on the plan
# endpoint.  A local "thinking off" preference must not serialize an
# upstream-rejected disable payload.
QWEN_TOKEN_PLAN_FORCE_THINKING_MODEL_IDS = frozenset(
    {"qwen3.8-max-preview", "kimi-k2.7-code", "minimax-m2.5"}
)

# preserve_thinking is accepted only by this documented subset. Kimi K2.5 is
# intentionally absent even though it can reason.
QWEN_TOKEN_PLAN_PRESERVE_THINKING_MODEL_IDS = frozenset(
    {
        "qwen3.8-max-preview",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "kimi-k2.7-code",
        "kimi-k2.6",
    }
)


def qwen_token_plan_model_id(model: str) -> str:
    """Normalize a configured model id for exact Token Plan comparisons."""

    return model.rsplit("/", 1)[-1].strip().lower()
