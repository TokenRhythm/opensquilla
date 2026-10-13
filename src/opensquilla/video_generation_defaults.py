"""Video endpoint and credential metadata without provider runtime imports."""

from __future__ import annotations

QWEN_TOKEN_PLAN_API_KEY_ENV = "QWEN_TOKEN_PLAN_API_KEY"
QWEN_TOKEN_PLAN_IMAGE_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"

VIDEO_GENERATION_OFFICIAL_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "qwen": "https://dashscope.aliyuncs.com/api/v1",
    "tokenrhythm": "https://tokenrhythm.studio/v1",
    "qwen_token_plan": QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
}

VIDEO_GENERATION_DEFAULT_ENV_KEYS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "tokenrhythm": "TOKENRHYTHM_API_KEY",
    "qwen_token_plan": QWEN_TOKEN_PLAN_API_KEY_ENV,
}


__all__ = [
    "QWEN_TOKEN_PLAN_API_KEY_ENV",
    "QWEN_TOKEN_PLAN_IMAGE_BASE_URL",
    "VIDEO_GENERATION_DEFAULT_ENV_KEYS",
    "VIDEO_GENERATION_OFFICIAL_BASE_URLS",
]
