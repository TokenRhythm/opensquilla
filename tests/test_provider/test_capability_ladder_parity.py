"""Exhaustive parity sweep for the capability-ladder migration.

ModelCatalog.get_capabilities used to be a hardcoded per-provider prefix
ladder. That ladder's DATA now lives in catalog_overrides.toml corrections
rows and resolves through resolve_entry (only host-trust branches remain
code). This module freezes the pre-migration outputs as literals and asserts
the post-migration get_capabilities is byte-identical for EVERY registered
provider id crossed with a battery of model ids — each ladder prefix, a
miss, snapshot-known ids, and unknown ids — plus the openai host-guard
matrix (trusted host vs untrusted proxy) and the deepseek base-url sniff.

The expected literals were transcribed by running get_capabilities on the
UNMODIFIED tree (staging/provider-overhaul@43d6475c) via a one-off harness;
a sample is cross-checked against the pre-change ladder logic in
test_parity_legacy_capabilities_unchanged (test_catalog_layers.py). Do NOT
edit an expected tuple to make a test pass — a diff here is a real behavior
change in the migration.

Also freezes the two named provider sets moved into registry.py
(KEYLESS_PROVIDERS drives requires_api_key; LOCAL_RUNTIME_PROVIDERS drives
the catalog context-window heuristic) so unifying them — which would flip
requires_api_key("vllm") — fails loudly.
"""

from __future__ import annotations

from opensquilla.provider.model_catalog import DEFAULT_CONTEXT_WINDOW, ModelCatalog
from opensquilla.provider.registry import (
    KEYLESS_PROVIDERS,
    LOCAL_RUNTIME_PROVIDERS,
    get_provider_spec,
    list_provider_names,
    list_provider_specs,
)

# (provider, model, base_url) ->
#   (supports_reasoning, supports_tools, supports_vision, reasoning_format)
# Frozen from the pre-migration get_capabilities. supports_streaming is
# always True and asserted separately below.
_EXPECTED_CAPS: dict[tuple[str, str, str], tuple[bool, bool, bool, str]] = {
    ("aihubmix", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("aihubmix", "gpt-4o", ""): (False, True, True, "none"),
    ("aihubmix", "deepseek-r1", ""): (False, False, False, "none"),
    ("aihubmix", "glm-4.6", ""): (False, True, False, "none"),
    ("aihubmix", "qwen3-max", ""): (False, True, False, "none"),
    ("aihubmix", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("aihubmix", "kimi-k2.5", ""): (False, True, True, "none"),
    ("aihubmix", "kimi-latest", ""): (False, True, False, "none"),
    ("aihubmix", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("aihubmix", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("aihubmix", "model-x-thinking", ""): (False, True, False, "none"),
    ("aihubmix", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("aihubmix", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("aihubmix", "o3-mini", ""): (False, True, False, "none"),
    ("aihubmix", "gpt-5.5", ""): (False, True, True, "none"),
    ("anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("anthropic", "gpt-4o", ""): (False, True, False, "none"),
    ("anthropic", "deepseek-r1", ""): (False, True, False, "none"),
    ("anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("anthropic", "kimi-k2.5", ""): (False, True, False, "none"),
    ("anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("anthropic", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("anthropic", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("anthropic", "gpt-5.5", ""): (False, True, False, "none"),
    ("anthropic", "claude-opus-4-6", ""): (False, True, False, "none"),
    ("azure", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("azure", "gpt-4o", ""): (False, True, True, "none"),
    ("azure", "deepseek-r1", ""): (False, False, False, "none"),
    ("azure", "glm-4.6", ""): (False, True, False, "none"),
    ("azure", "qwen3-max", ""): (False, True, False, "none"),
    ("azure", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("azure", "kimi-k2.5", ""): (False, True, True, "none"),
    ("azure", "kimi-latest", ""): (False, True, False, "none"),
    ("azure", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("azure", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("azure", "model-x-thinking", ""): (False, True, False, "none"),
    ("azure", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("azure", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("azure", "o3-mini", ""): (False, True, False, "none"),
    ("azure", "gpt-5.5", ""): (False, True, True, "none"),
    ("bailian_coding", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("bailian_coding", "gpt-4o", ""): (False, True, True, "none"),
    ("bailian_coding", "deepseek-r1", ""): (False, True, False, "none"),
    ("bailian_coding", "glm-4.6", ""): (False, True, False, "none"),
    ("bailian_coding", "qwen3-max", ""): (False, True, False, "none"),
    ("bailian_coding", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("bailian_coding", "kimi-k2.5", ""): (False, True, True, "none"),
    ("bailian_coding", "kimi-latest", ""): (False, True, False, "none"),
    ("bailian_coding", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("bailian_coding", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("bailian_coding", "model-x-thinking", ""): (False, True, False, "none"),
    ("bailian_coding", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("bailian_coding", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("bailian_coding", "o3-mini", ""): (False, True, False, "none"),
    ("bailian_coding", "gpt-5.5", ""): (False, True, True, "none"),
    ("bailian_coding_cn", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "gpt-4o", ""): (False, True, True, "none"),
    ("bailian_coding_cn", "deepseek-r1", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "glm-4.6", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "qwen3-max", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "kimi-k2.5", ""): (False, True, True, "none"),
    ("bailian_coding_cn", "kimi-latest", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "model-x-thinking", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("bailian_coding_cn", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("bailian_coding_cn", "o3-mini", ""): (False, True, False, "none"),
    ("bailian_coding_cn", "gpt-5.5", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "gpt-4o", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "deepseek-r1", ""): (False, False, False, "none"),
    ("kimi_coding_anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "kimi-k2.5", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("kimi_coding_anthropic", "gpt-5.5", ""): (False, True, True, "none"),
    ("kimi_coding_anthropic", "kimi-for-coding", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "gpt-4o", ""): (False, True, True, "none"),
    ("kimi_coding_openai", "deepseek-r1", ""): (False, False, False, "none"),
    ("kimi_coding_openai", "glm-4.6", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "qwen3-max", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "kimi-k2.5", ""): (False, True, True, "none"),
    ("kimi_coding_openai", "kimi-latest", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "model-x-thinking", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("kimi_coding_openai", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("kimi_coding_openai", "o3-mini", ""): (False, True, False, "none"),
    ("kimi_coding_openai", "gpt-5.5", ""): (False, True, True, "none"),
    ("kimi_coding_openai", "kimi-for-coding", ""): (False, True, False, "none"),
    ("byteplus", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("byteplus", "gpt-4o", ""): (False, True, False, "none"),
    ("byteplus", "deepseek-r1", ""): (False, True, False, "none"),
    ("byteplus", "glm-4.6", ""): (False, True, False, "none"),
    ("byteplus", "qwen3-max", ""): (False, True, False, "none"),
    ("byteplus", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("byteplus", "kimi-k2.5", ""): (True, True, True, "volcengine"),
    ("byteplus", "kimi-latest", ""): (False, True, False, "none"),
    ("byteplus", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("byteplus", "seed-1-8-flash", ""): (True, True, True, "volcengine"),
    ("byteplus", "model-x-thinking", ""): (True, True, False, "volcengine"),
    ("byteplus", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("byteplus", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("byteplus", "o3-mini", ""): (False, True, False, "none"),
    ("byteplus", "gpt-5.5", ""): (False, True, False, "none"),
    ("byteplus", "seed-1-6", ""): (True, True, True, "volcengine"),
    ("byteplus", "seed-1-6-250915", ""): (True, True, True, "volcengine"),
    ("byteplus", "seed-2-flash", ""): (True, True, True, "volcengine"),
    ("byteplus", "kimi-k2-250905", ""): (True, True, True, "volcengine"),
    ("byteplus", "kimi-k2.6", ""): (True, True, True, "volcengine"),
    ("byteplus", "kimi-k2-thinking", ""): (True, True, True, "volcengine"),
    ("byteplus", "skylark-pro", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "gpt-4o", ""): (False, True, True, "none"),
    ("byteplus_coding_plan", "deepseek-r1", ""): (False, False, False, "none"),
    ("byteplus_coding_plan", "glm-4.6", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "qwen3-max", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "kimi-k2.5", ""): (False, True, True, "none"),
    ("byteplus_coding_plan", "kimi-latest", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "model-x-thinking", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("byteplus_coding_plan", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("byteplus_coding_plan", "o3-mini", ""): (False, True, False, "none"),
    ("byteplus_coding_plan", "gpt-5.5", ""): (False, True, True, "none"),
    (
        "byteplus_coding_plan_anthropic",
        "totally-unknown-model-x1",
        "",
    ): (False, True, False, "none"),
    ("custom", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("custom", "gpt-4o", ""): (False, True, True, "none"),
    ("custom", "deepseek-r1", ""): (False, False, False, "none"),
    ("custom", "glm-4.6", ""): (False, True, False, "none"),
    ("custom", "qwen3-max", ""): (False, True, False, "none"),
    ("custom", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("custom", "kimi-k2.5", ""): (False, True, True, "none"),
    ("custom", "kimi-latest", ""): (False, True, False, "none"),
    ("custom", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("custom", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("custom", "model-x-thinking", ""): (False, True, False, "none"),
    ("custom", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("custom", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("custom", "o3-mini", ""): (False, True, False, "none"),
    ("custom", "gpt-5.5", ""): (False, True, True, "none"),
    ("dashscope", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("dashscope", "gpt-4o", ""): (False, True, False, "none"),
    ("dashscope", "deepseek-r1", ""): (False, True, False, "none"),
    ("dashscope", "glm-4.6", ""): (False, True, False, "none"),
    ("dashscope", "qwen3-max", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("dashscope", "kimi-k2.5", ""): (False, True, False, "none"),
    ("dashscope", "kimi-latest", ""): (False, True, False, "none"),
    ("dashscope", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("dashscope", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("dashscope", "model-x-thinking", ""): (False, True, False, "none"),
    ("dashscope", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("dashscope", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("dashscope", "o3-mini", ""): (False, True, False, "none"),
    ("dashscope", "gpt-5.5", ""): (False, True, False, "none"),
    ("dashscope", "qwen3", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen3.5-vl-plus", ""): (True, True, True, "dashscope"),
    ("dashscope", "qwen3.6-omni", ""): (True, True, True, "dashscope"),
    ("dashscope", "qwen-plus-latest", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen-flash-2025", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen-turbo-x", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen-max-latest", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwq-32b", ""): (True, True, False, "dashscope"),
    ("dashscope", "qwen-vl-ocr", ""): (False, True, True, "none"),
    ("dashscope", "qwen-vl-max", ""): (False, True, True, "none"),
    ("dashscope", "tongyi-intent-detect-v3", ""): (False, True, False, "none"),
    ("deepseek", "totally-unknown-model-x1", ""): (True, True, False, "deepseek"),
    ("deepseek", "gpt-4o", ""): (True, True, False, "deepseek"),
    ("deepseek", "deepseek-r1", ""): (True, True, False, "deepseek"),
    ("deepseek", "glm-4.6", ""): (True, True, False, "deepseek"),
    ("deepseek", "qwen3-max", ""): (True, True, False, "deepseek"),
    ("deepseek", "qwen2.5-14b-instruct", ""): (True, True, False, "deepseek"),
    ("deepseek", "kimi-k2.5", ""): (True, True, False, "deepseek"),
    ("deepseek", "kimi-latest", ""): (True, True, False, "deepseek"),
    ("deepseek", "doubao-seed-1-6-251015", ""): (True, True, False, "deepseek"),
    ("deepseek", "seed-1-8-flash", ""): (True, True, False, "deepseek"),
    ("deepseek", "model-x-thinking", ""): (True, True, False, "deepseek"),
    ("deepseek", "gemini-2.5-flash", ""): (True, True, False, "deepseek"),
    ("deepseek", "gemini-3-pro-preview", ""): (True, True, False, "deepseek"),
    ("deepseek", "o3-mini", ""): (True, True, False, "deepseek"),
    ("deepseek", "gpt-5.5", ""): (True, True, False, "deepseek"),
    ("deepseek", "deepseek-chat", ""): (True, True, False, "deepseek"),
    ("deepseek", "deepseek-reasoner", ""): (True, True, False, "deepseek"),
    ("deepseek", "deepseek-v4-flash", ""): (True, True, False, "deepseek"),
    ("gemini", "totally-unknown-model-x1", ""): (False, True, True, "none"),
    ("gemini", "gpt-4o", ""): (False, True, True, "none"),
    ("gemini", "deepseek-r1", ""): (False, True, True, "none"),
    ("gemini", "glm-4.6", ""): (False, True, True, "none"),
    ("gemini", "qwen3-max", ""): (False, True, True, "none"),
    ("gemini", "qwen2.5-14b-instruct", ""): (False, True, True, "none"),
    ("gemini", "kimi-k2.5", ""): (False, True, True, "none"),
    ("gemini", "kimi-latest", ""): (False, True, True, "none"),
    ("gemini", "doubao-seed-1-6-251015", ""): (False, True, True, "none"),
    ("gemini", "seed-1-8-flash", ""): (False, True, True, "none"),
    ("gemini", "model-x-thinking", ""): (False, True, True, "none"),
    ("gemini", "gemini-2.5-flash", ""): (True, True, True, "gemini"),
    ("gemini", "gemini-3-pro-preview", ""): (True, True, True, "gemini"),
    ("gemini", "o3-mini", ""): (False, True, True, "none"),
    ("gemini", "gpt-5.5", ""): (False, True, True, "none"),
    ("gemini", "gemini-2.5-pro", ""): (True, True, True, "gemini"),
    ("gemini", "gemini-2.0-flash", ""): (False, True, True, "none"),
    ("gemini", "gemini-2.5-flash-image", ""): (True, True, True, "gemini"),
    ("github_copilot", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("github_copilot", "gpt-4o", ""): (False, True, True, "none"),
    ("github_copilot", "deepseek-r1", ""): (False, False, False, "none"),
    ("github_copilot", "glm-4.6", ""): (False, True, False, "none"),
    ("github_copilot", "qwen3-max", ""): (False, True, False, "none"),
    ("github_copilot", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("github_copilot", "kimi-k2.5", ""): (False, True, True, "none"),
    ("github_copilot", "kimi-latest", ""): (False, True, False, "none"),
    ("github_copilot", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("github_copilot", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("github_copilot", "model-x-thinking", ""): (False, True, False, "none"),
    ("github_copilot", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("github_copilot", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("github_copilot", "o3-mini", ""): (False, True, False, "none"),
    ("github_copilot", "gpt-5.5", ""): (False, True, True, "none"),
    ("groq", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("groq", "gpt-4o", ""): (False, True, True, "none"),
    ("groq", "deepseek-r1", ""): (False, False, False, "none"),
    ("groq", "glm-4.6", ""): (False, True, False, "none"),
    ("groq", "qwen3-max", ""): (False, True, False, "none"),
    ("groq", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("groq", "kimi-k2.5", ""): (False, True, True, "none"),
    ("groq", "kimi-latest", ""): (False, True, False, "none"),
    ("groq", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("groq", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("groq", "model-x-thinking", ""): (False, True, False, "none"),
    ("groq", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("groq", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("groq", "o3-mini", ""): (False, True, False, "none"),
    ("groq", "gpt-5.5", ""): (False, True, True, "none"),
    ("litellm_proxy", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("litellm_proxy", "gpt-4o", ""): (False, True, True, "none"),
    ("litellm_proxy", "deepseek-r1", ""): (False, False, False, "none"),
    ("litellm_proxy", "glm-4.6", ""): (False, True, False, "none"),
    ("litellm_proxy", "qwen3-max", ""): (False, True, False, "none"),
    ("litellm_proxy", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("litellm_proxy", "kimi-k2.5", ""): (False, True, True, "none"),
    ("litellm_proxy", "kimi-latest", ""): (False, True, False, "none"),
    ("litellm_proxy", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("litellm_proxy", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("litellm_proxy", "model-x-thinking", ""): (False, True, False, "none"),
    ("litellm_proxy", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("litellm_proxy", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("litellm_proxy", "o3-mini", ""): (False, True, False, "none"),
    ("litellm_proxy", "gpt-5.5", ""): (False, True, True, "none"),
    ("lm_studio", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("lm_studio", "gpt-4o", ""): (False, True, True, "none"),
    ("lm_studio", "deepseek-r1", ""): (False, False, False, "none"),
    ("lm_studio", "glm-4.6", ""): (False, True, False, "none"),
    ("lm_studio", "qwen3-max", ""): (False, True, False, "none"),
    ("lm_studio", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("lm_studio", "kimi-k2.5", ""): (False, True, True, "none"),
    ("lm_studio", "kimi-latest", ""): (False, True, False, "none"),
    ("lm_studio", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("lm_studio", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("lm_studio", "model-x-thinking", ""): (False, True, False, "none"),
    ("lm_studio", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("lm_studio", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("lm_studio", "o3-mini", ""): (False, True, False, "none"),
    ("lm_studio", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax", "o3-mini", ""): (False, True, False, "none"),
    ("minimax", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_cn", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax_cn", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax_cn", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax_cn", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax_cn", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax_cn", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax_cn", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax_cn", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax_cn", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax_cn", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax_cn", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax_cn", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax_cn", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax_cn", "o3-mini", ""): (False, True, False, "none"),
    ("minimax_cn", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_global", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax_global", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax_global", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax_global", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax_global", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax_global", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax_global", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax_global", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax_global", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax_global", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax_global", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax_global", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax_global", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax_global", "o3-mini", ""): (False, True, False, "none"),
    ("minimax_global", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_openai", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax_openai", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax_openai", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax_openai", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax_openai", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax_openai", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax_openai", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax_openai", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax_openai", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax_openai", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax_openai", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax_openai", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax_openai", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax_openai", "o3-mini", ""): (False, True, False, "none"),
    ("minimax_openai", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax_coding_anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_coding_anthropic", "MiniMax-M2.7", ""): (False, True, False, "none"),
    ("minimax_coding_anthropic", "MiniMax-M3", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "gpt-4o", ""): (False, True, True, "none"),
    ("minimax_coding_openai", "deepseek-r1", ""): (False, False, False, "none"),
    ("minimax_coding_openai", "glm-4.6", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "qwen3-max", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "kimi-k2.5", ""): (False, True, True, "none"),
    ("minimax_coding_openai", "kimi-latest", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "model-x-thinking", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("minimax_coding_openai", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("minimax_coding_openai", "o3-mini", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "gpt-5.5", ""): (False, True, True, "none"),
    ("minimax_coding_openai", "MiniMax-M2.7", ""): (False, True, False, "none"),
    ("minimax_coding_openai", "MiniMax-M3", ""): (False, True, False, "none"),
    ("mimo_anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("mimo_anthropic", "gpt-4o", ""): (False, True, True, "none"),
    ("mimo_anthropic", "deepseek-r1", ""): (False, False, False, "none"),
    ("mimo_anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("mimo_anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("mimo_anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("mimo_anthropic", "kimi-k2.5", ""): (False, True, True, "none"),
    ("mimo_anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("mimo_anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("mimo_anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("mimo_anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("mimo_anthropic", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("mimo_anthropic", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("mimo_anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("mimo_anthropic", "gpt-5.5", ""): (False, True, True, "none"),
    ("mimo_anthropic", "mimo-v2.5", ""): (False, True, False, "none"),
    ("mimo_anthropic", "mimo-v2.5-pro", ""): (False, True, False, "none"),
    ("mimo_openai", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("mimo_openai", "gpt-4o", ""): (False, True, True, "none"),
    ("mimo_openai", "deepseek-r1", ""): (False, False, False, "none"),
    ("mimo_openai", "glm-4.6", ""): (False, True, False, "none"),
    ("mimo_openai", "qwen3-max", ""): (False, True, False, "none"),
    ("mimo_openai", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("mimo_openai", "kimi-k2.5", ""): (False, True, True, "none"),
    ("mimo_openai", "kimi-latest", ""): (False, True, False, "none"),
    ("mimo_openai", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("mimo_openai", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("mimo_openai", "model-x-thinking", ""): (False, True, False, "none"),
    ("mimo_openai", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("mimo_openai", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("mimo_openai", "o3-mini", ""): (False, True, False, "none"),
    ("mimo_openai", "gpt-5.5", ""): (False, True, True, "none"),
    ("mimo_openai", "mimo-v2.5", ""): (False, True, False, "none"),
    ("mimo_openai", "mimo-v2.5-pro", ""): (False, True, False, "none"),
    ("mistral", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("mistral", "gpt-4o", ""): (False, True, True, "none"),
    ("mistral", "deepseek-r1", ""): (False, False, False, "none"),
    ("mistral", "glm-4.6", ""): (False, True, False, "none"),
    ("mistral", "qwen3-max", ""): (False, True, False, "none"),
    ("mistral", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("mistral", "kimi-k2.5", ""): (False, True, True, "none"),
    ("mistral", "kimi-latest", ""): (False, True, False, "none"),
    ("mistral", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("mistral", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("mistral", "model-x-thinking", ""): (False, True, False, "none"),
    ("mistral", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("mistral", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("mistral", "o3-mini", ""): (False, True, False, "none"),
    ("mistral", "gpt-5.5", ""): (False, True, True, "none"),
    ("moonshot", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("moonshot", "gpt-4o", ""): (False, True, False, "none"),
    ("moonshot", "deepseek-r1", ""): (False, True, False, "none"),
    ("moonshot", "glm-4.6", ""): (False, True, False, "none"),
    ("moonshot", "qwen3-max", ""): (False, True, False, "none"),
    ("moonshot", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("moonshot", "kimi-k2.5", ""): (True, True, True, "moonshot"),
    ("moonshot", "kimi-latest", ""): (False, True, False, "none"),
    ("moonshot", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("moonshot", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("moonshot", "model-x-thinking", ""): (False, True, False, "none"),
    ("moonshot", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("moonshot", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("moonshot", "o3-mini", ""): (False, True, False, "none"),
    ("moonshot", "gpt-5.5", ""): (False, True, False, "none"),
    ("moonshot", "kimi-k2.6-preview", ""): (True, True, True, "moonshot"),
    ("moonshot", "kimi-k2-thinking", ""): (True, True, False, "moonshot"),
    ("moonshot", "kimi-k2-thinking-turbo", ""): (True, True, False, "moonshot"),
    ("moonshot", "kimi-k2-0905-preview", ""): (False, True, False, "none"),
    ("moonshot", "moonshot-v1-8k", ""): (False, True, False, "none"),
    ("moonshot", "moonshot-v1-128k", ""): (False, True, False, "none"),
    ("ollama", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("ollama", "gpt-4o", ""): (False, True, False, "none"),
    ("ollama", "deepseek-r1", ""): (False, True, False, "none"),
    ("ollama", "glm-4.6", ""): (False, True, False, "none"),
    ("ollama", "qwen3-max", ""): (False, True, False, "none"),
    ("ollama", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("ollama", "kimi-k2.5", ""): (False, True, False, "none"),
    ("ollama", "kimi-latest", ""): (False, True, False, "none"),
    ("ollama", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("ollama", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("ollama", "model-x-thinking", ""): (False, True, False, "none"),
    ("ollama", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("ollama", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("ollama", "o3-mini", ""): (False, True, False, "none"),
    ("ollama", "gpt-5.5", ""): (False, True, False, "none"),
    ("ollama", "llama3.2:3b", ""): (False, True, False, "none"),
    ("openai", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("openai", "gpt-4o", ""): (False, True, True, "none"),
    ("openai", "deepseek-r1", ""): (False, False, False, "none"),
    ("openai", "glm-4.6", ""): (False, True, False, "none"),
    ("openai", "qwen3-max", ""): (False, True, False, "none"),
    ("openai", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("openai", "kimi-k2.5", ""): (False, True, True, "none"),
    ("openai", "kimi-latest", ""): (False, True, False, "none"),
    ("openai", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("openai", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("openai", "model-x-thinking", ""): (False, True, False, "none"),
    ("openai", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("openai", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("openai", "o3-mini", ""): (False, True, False, "none"),
    ("openai", "gpt-5.5", ""): (False, True, True, "none"),
    ("openai", "gpt-3.5-turbo", ""): (False, False, False, "none"),
    ("openai_codex", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("openai_codex", "gpt-4o", ""): (False, True, True, "none"),
    ("openai_codex", "deepseek-r1", ""): (False, False, False, "none"),
    ("openai_codex", "glm-4.6", ""): (False, True, False, "none"),
    ("openai_codex", "qwen3-max", ""): (False, True, False, "none"),
    ("openai_codex", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("openai_codex", "kimi-k2.5", ""): (False, True, True, "none"),
    ("openai_codex", "kimi-latest", ""): (False, True, False, "none"),
    ("openai_codex", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("openai_codex", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("openai_codex", "model-x-thinking", ""): (False, True, False, "none"),
    ("openai_codex", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("openai_codex", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("openai_codex", "o3-mini", ""): (False, True, False, "none"),
    ("openai_codex", "gpt-5.5", ""): (False, True, True, "none"),
    ("openai_responses", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("openai_responses", "gpt-4o", ""): (False, True, True, "none"),
    ("openai_responses", "deepseek-r1", ""): (False, False, False, "none"),
    ("openai_responses", "glm-4.6", ""): (False, True, False, "none"),
    ("openai_responses", "qwen3-max", ""): (False, True, False, "none"),
    ("openai_responses", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("openai_responses", "kimi-k2.5", ""): (False, True, True, "none"),
    ("openai_responses", "kimi-latest", ""): (False, True, False, "none"),
    ("openai_responses", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("openai_responses", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("openai_responses", "model-x-thinking", ""): (False, True, False, "none"),
    ("openai_responses", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("openai_responses", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("openai_responses", "o3-mini", ""): (False, True, False, "none"),
    ("openai_responses", "gpt-5.5", ""): (False, True, True, "none"),
    ("openrouter", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("openrouter", "gpt-4o", ""): (False, True, True, "none"),
    ("openrouter", "deepseek-r1", ""): (False, False, False, "none"),
    ("openrouter", "glm-4.6", ""): (False, True, False, "none"),
    ("openrouter", "qwen3-max", ""): (False, True, False, "none"),
    ("openrouter", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("openrouter", "kimi-k2.5", ""): (False, True, True, "none"),
    ("openrouter", "kimi-latest", ""): (False, True, False, "none"),
    ("openrouter", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("openrouter", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("openrouter", "model-x-thinking", ""): (False, True, False, "none"),
    ("openrouter", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("openrouter", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("openrouter", "o3-mini", ""): (False, True, False, "none"),
    ("openrouter", "gpt-5.5", ""): (False, True, True, "none"),
    # Snapshot-known reasoning model without tools. Originally
    # aion-labs/aion-1.0; models.dev dropped that id (2026-07-08 refresh), so
    # the same scenario is pinned on another snapshot row with the same shape
    # (reasoning=true suppressed without a dialect, tools=false vendored).
    ("openrouter", "tencent/hunyuan-a13b-instruct", ""): (False, False, False, "none"),
    ("openrouter", "amazon/nova-2-lite-v1", ""): (False, True, True, "none"),
    ("openrouter", "z-ai/glm-5", ""): (False, True, False, "none"),
    ("ovms", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("ovms", "gpt-4o", ""): (False, True, True, "none"),
    ("ovms", "deepseek-r1", ""): (False, False, False, "none"),
    ("ovms", "glm-4.6", ""): (False, True, False, "none"),
    ("ovms", "qwen3-max", ""): (False, True, False, "none"),
    ("ovms", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("ovms", "kimi-k2.5", ""): (False, True, True, "none"),
    ("ovms", "kimi-latest", ""): (False, True, False, "none"),
    ("ovms", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("ovms", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("ovms", "model-x-thinking", ""): (False, True, False, "none"),
    ("ovms", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("ovms", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("ovms", "o3-mini", ""): (False, True, False, "none"),
    ("ovms", "gpt-5.5", ""): (False, True, True, "none"),
    ("qianfan", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("qianfan", "gpt-4o", ""): (False, True, True, "none"),
    ("qianfan", "deepseek-r1", ""): (False, False, False, "none"),
    ("qianfan", "glm-4.6", ""): (False, True, False, "none"),
    ("qianfan", "qwen3-max", ""): (False, True, False, "none"),
    ("qianfan", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("qianfan", "kimi-k2.5", ""): (False, True, True, "none"),
    ("qianfan", "kimi-latest", ""): (False, True, False, "none"),
    ("qianfan", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("qianfan", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("qianfan", "model-x-thinking", ""): (False, True, False, "none"),
    ("qianfan", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("qianfan", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("qianfan", "o3-mini", ""): (False, True, False, "none"),
    ("qianfan", "gpt-5.5", ""): (False, True, True, "none"),
    ("siliconflow", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("siliconflow", "gpt-4o", ""): (False, True, True, "none"),
    ("siliconflow", "deepseek-r1", ""): (False, False, False, "none"),
    ("siliconflow", "glm-4.6", ""): (False, True, False, "none"),
    ("siliconflow", "qwen3-max", ""): (False, True, False, "none"),
    ("siliconflow", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("siliconflow", "kimi-k2.5", ""): (False, True, True, "none"),
    ("siliconflow", "kimi-latest", ""): (False, True, False, "none"),
    ("siliconflow", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("siliconflow", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("siliconflow", "model-x-thinking", ""): (False, True, False, "none"),
    ("siliconflow", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("siliconflow", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("siliconflow", "o3-mini", ""): (False, True, False, "none"),
    ("siliconflow", "gpt-5.5", ""): (False, True, True, "none"),
    ("tencent_token_plan", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tencent_token_plan", "gpt-4o", ""): (False, True, False, "none"),
    ("tencent_token_plan", "deepseek-r1", ""): (False, True, False, "none"),
    ("tencent_token_plan", "glm-4.6", ""): (False, True, False, "none"),
    ("tencent_token_plan", "qwen3-max", ""): (False, True, False, "none"),
    ("tencent_token_plan", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tencent_token_plan", "kimi-k2.5", ""): (False, True, False, "none"),
    ("tencent_token_plan", "kimi-latest", ""): (False, True, False, "none"),
    ("tencent_token_plan", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tencent_token_plan", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tencent_token_plan", "model-x-thinking", ""): (False, True, False, "none"),
    ("tencent_token_plan", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("tencent_token_plan", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("tencent_token_plan", "o3-mini", ""): (False, True, False, "none"),
    ("tencent_token_plan", "gpt-5.5", ""): (False, True, False, "none"),
    ("tencent_token_plan", "hy3", ""): (True, True, False, "tencent_tokenhub"),
    ("tencent_token_plan", "hy3-preview", ""): (True, True, False, "tencent_tokenhub"),
    ("tencent_token_plan_anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "gpt-4o", ""): (False, True, True, "none"),
    ("tencent_token_plan_anthropic", "deepseek-r1", ""): (False, False, False, "none"),
    ("tencent_token_plan_anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "kimi-k2.5", ""): (False, True, True, "none"),
    ("tencent_token_plan_anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("tencent_token_plan_anthropic", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("tencent_token_plan_anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "gpt-5.5", ""): (False, True, True, "none"),
    ("tencent_token_plan_anthropic", "hy3", ""): (False, True, False, "none"),
    ("tencent_token_plan_anthropic", "hy3-preview", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "gpt-4o", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "deepseek-r1", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "glm-4.6", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "qwen3-max", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "kimi-k2.5", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "kimi-latest", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "model-x-thinking", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "o3-mini", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "gpt-5.5", ""): (False, True, False, "none"),
    ("tencent_tokenhub", "hy3", ""): (True, True, False, "tencent_tokenhub"),
    ("tencent_tokenhub", "hy3-preview", ""): (True, True, False, "tencent_tokenhub"),
    ("tencent_tokenhub_anthropic", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "gpt-4o", ""): (False, True, True, "none"),
    ("tencent_tokenhub_anthropic", "deepseek-r1", ""): (False, False, False, "none"),
    ("tencent_tokenhub_anthropic", "glm-4.6", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "qwen3-max", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "kimi-k2.5", ""): (False, True, True, "none"),
    ("tencent_tokenhub_anthropic", "kimi-latest", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "model-x-thinking", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("tencent_tokenhub_anthropic", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("tencent_tokenhub_anthropic", "o3-mini", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "gpt-5.5", ""): (False, True, True, "none"),
    ("tencent_tokenhub_anthropic", "hy3", ""): (False, True, False, "none"),
    ("tencent_tokenhub_anthropic", "hy3-preview", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "gpt-4o", ""): (False, True, True, "none"),
    ("tencent_tokenhub_intl", "deepseek-r1", ""): (False, False, False, "none"),
    ("tencent_tokenhub_intl", "glm-4.6", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "qwen3-max", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "kimi-k2.5", ""): (False, True, True, "none"),
    ("tencent_tokenhub_intl", "kimi-latest", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "model-x-thinking", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("tencent_tokenhub_intl", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("tencent_tokenhub_intl", "o3-mini", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "gpt-5.5", ""): (False, True, True, "none"),
    ("tencent_tokenhub_intl", "hy3", ""): (False, True, False, "none"),
    ("tencent_tokenhub_intl", "hy3-preview", ""): (False, True, False, "none"),
    # tokenrhythm: the [tokenrhythm.*] corrections rows pin
    # reasoning_format="none" (the relay streams reasoning_content on its
    # own but rejects thinking toggles), so effective supports_reasoning is
    # False everywhere; vision only on the live-verified kimi rows.
    ("tokenrhythm", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("tokenrhythm", "gpt-4o", ""): (False, True, False, "none"),
    ("tokenrhythm", "deepseek-r1", ""): (False, True, False, "none"),
    ("tokenrhythm", "glm-4.6", ""): (False, True, False, "none"),
    ("tokenrhythm", "qwen3-max", ""): (False, True, False, "none"),
    ("tokenrhythm", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("tokenrhythm", "kimi-k2.5", ""): (False, True, True, "none"),
    ("tokenrhythm", "kimi-latest", ""): (False, True, False, "none"),
    ("tokenrhythm", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("tokenrhythm", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("tokenrhythm", "model-x-thinking", ""): (False, True, False, "none"),
    ("tokenrhythm", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("tokenrhythm", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("tokenrhythm", "o3-mini", ""): (False, True, False, "none"),
    ("tokenrhythm", "gpt-5.5", ""): (False, True, False, "none"),
    ("tokenrhythm", "deepseek-v4-flash", ""): (False, True, False, "none"),
    ("tokenrhythm", "kimi-k2.7-code", ""): (False, True, True, "none"),
    ("vllm", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("vllm", "gpt-4o", ""): (False, True, True, "none"),
    ("vllm", "deepseek-r1", ""): (False, False, False, "none"),
    ("vllm", "glm-4.6", ""): (False, True, False, "none"),
    ("vllm", "qwen3-max", ""): (False, True, False, "none"),
    ("vllm", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("vllm", "kimi-k2.5", ""): (False, True, True, "none"),
    ("vllm", "kimi-latest", ""): (False, True, False, "none"),
    ("vllm", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("vllm", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("vllm", "model-x-thinking", ""): (False, True, False, "none"),
    ("vllm", "gemini-2.5-flash", ""): (False, True, True, "none"),
    ("vllm", "gemini-3-pro-preview", ""): (False, True, True, "none"),
    ("vllm", "o3-mini", ""): (False, True, False, "none"),
    ("vllm", "gpt-5.5", ""): (False, True, True, "none"),
    ("volcengine", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("volcengine", "gpt-4o", ""): (False, True, False, "none"),
    ("volcengine", "deepseek-r1", ""): (False, True, False, "none"),
    ("volcengine", "glm-4.6", ""): (False, True, False, "none"),
    ("volcengine", "qwen3-max", ""): (False, True, False, "none"),
    ("volcengine", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("volcengine", "kimi-k2.5", ""): (False, True, False, "none"),
    ("volcengine", "kimi-latest", ""): (False, True, False, "none"),
    ("volcengine", "doubao-seed-1-6-251015", ""): (True, True, True, "volcengine"),
    ("volcengine", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("volcengine", "model-x-thinking", ""): (True, True, False, "volcengine"),
    ("volcengine", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("volcengine", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("volcengine", "o3-mini", ""): (False, True, False, "none"),
    ("volcengine", "gpt-5.5", ""): (False, True, False, "none"),
    ("volcengine", "doubao-seed-1-8-250915", ""): (True, True, True, "volcengine"),
    ("volcengine", "doubao-seed-2-pro", ""): (True, True, True, "volcengine"),
    ("volcengine", "doubao-1-5-thinking-pro-250415", ""): (True, True, False, "volcengine"),
    ("volcengine", "doubao-pro-32k", ""): (False, True, False, "none"),
    ("volcengine", "kimi-k2-thinking", ""): (True, True, False, "volcengine"),
    ("volcengine_coding_plan", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "gpt-4o", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "deepseek-r1", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "glm-4.6", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "qwen3-max", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "kimi-k2.5", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "kimi-latest", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "model-x-thinking", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "o3-mini", ""): (False, True, False, "none"),
    ("volcengine_coding_plan", "gpt-5.5", ""): (False, True, False, "none"),
    (
        "volcengine_coding_plan_anthropic",
        "totally-unknown-model-x1",
        "",
    ): (False, True, False, "none"),
    ("volcengine_coding_plan", "doubao-seed-2.0-lite", ""): (True, True, True, "volcengine"),
    ("volcengine_coding_plan", "doubao-seed-2.0-pro", ""): (True, True, True, "volcengine"),
    (
        "volcengine_coding_plan",
        "doubao-seed-2.0-code",
        "",
    ): (True, True, True, "volcengine"),
    ("zhipu", "totally-unknown-model-x1", ""): (False, True, False, "none"),
    ("zhipu", "gpt-4o", ""): (False, True, False, "none"),
    ("zhipu", "deepseek-r1", ""): (False, True, False, "none"),
    ("zhipu", "glm-4.6", ""): (False, True, False, "none"),
    ("zhipu", "qwen3-max", ""): (False, True, False, "none"),
    ("zhipu", "qwen2.5-14b-instruct", ""): (False, True, False, "none"),
    ("zhipu", "kimi-k2.5", ""): (False, True, False, "none"),
    ("zhipu", "kimi-latest", ""): (False, True, False, "none"),
    ("zhipu", "doubao-seed-1-6-251015", ""): (False, True, False, "none"),
    ("zhipu", "seed-1-8-flash", ""): (False, True, False, "none"),
    ("zhipu", "model-x-thinking", ""): (False, True, False, "none"),
    ("zhipu", "gemini-2.5-flash", ""): (False, True, False, "none"),
    ("zhipu", "gemini-3-pro-preview", ""): (False, True, False, "none"),
    ("zhipu", "o3-mini", ""): (False, True, False, "none"),
    ("zhipu", "gpt-5.5", ""): (False, True, False, "none"),
    ("zhipu", "glm-4.5-air", ""): (True, True, False, "zai"),
    ("zhipu", "glm-4.5v", ""): (True, True, False, "zai"),
    ("zhipu", "glm-4.7-flashx", ""): (True, True, False, "zai"),
    ("zhipu", "glm-5", ""): (True, True, False, "zai"),
    ("zhipu", "glm-5.2", ""): (True, True, False, "zai"),
    ("zhipu", "glm-4", ""): (False, True, False, "none"),
    ("openai", "gpt-5.5", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "gpt-5.4", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "gpt-5", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "o1", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "o1-preview", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "o3", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "o3-mini", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "o4-mini", "https://api.openai.com/v1"): (True, True, False, "openai"),
    ("openai", "gpt-4o", "https://api.openai.com/v1"): (False, True, True, "none"),
    ("openai", "gpt-4.1", "https://api.openai.com/v1"): (False, True, True, "none"),
    ("openai", "gpt-3.5-turbo", "https://api.openai.com/v1"): (False, False, False, "none"),
    ("openai", "omni-model", "https://api.openai.com/v1"): (False, True, False, "none"),
    ("openai", "gpt-5.5", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "gpt-5.4", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "gpt-5", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "o1", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "o1-preview", "https://proxy.example/v1"): (False, True, False, "none"),
    ("openai", "o3", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "o3-mini", "https://proxy.example/v1"): (False, True, False, "none"),
    ("openai", "o4-mini", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "gpt-4o", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "gpt-4.1", "https://proxy.example/v1"): (False, True, True, "none"),
    ("openai", "gpt-3.5-turbo", "https://proxy.example/v1"): (False, False, False, "none"),
    ("openai", "omni-model", "https://proxy.example/v1"): (False, True, False, "none"),
    ("openai", "deepseek-chat", "https://api.deepseek.com"): (True, True, False, "deepseek"),
    ("openai", "any-model-at-all", "https://api.deepseek.com/v1"): (True, True, False, "deepseek"),
}

# ProviderSpec.requires_api_key() per provider id — frozen pre-migration.
_EXPECTED_REQUIRES_API_KEY: dict[str, bool] = {
    "aihubmix": True,
    "anthropic": True,
    "azure": True,
    "bailian_coding": True,
    "bailian_coding_cn": True,
    "byteplus": True,
    "byteplus_coding_plan": True,
    "byteplus_coding_plan_anthropic": True,
    "custom": False,
    "dashscope": True,
    "deepseek": True,
    "gemini": True,
    "github_copilot": False,
    "groq": True,
    "kimi_coding_anthropic": True,
    "kimi_coding_openai": True,
    "litellm_proxy": True,
    "lm_studio": False,
    "minimax": True,
    "minimax_coding_anthropic": True,
    "minimax_coding_openai": True,
    "minimax_cn": True,
    "minimax_global": True,
    "minimax_openai": True,
    "mimo_anthropic": True,
    "mimo_openai": True,
    "mistral": True,
    "moonshot": True,
    "ollama": False,
    "openai": True,
    "openai_codex": False,
    "openai_responses": True,
    "openrouter": True,
    "ovms": False,
    "qianfan": True,
    "siliconflow": True,
    "tencent_token_plan": True,
    "tencent_token_plan_anthropic": True,
    "tencent_tokenhub": True,
    "tencent_tokenhub_anthropic": True,
    "tencent_tokenhub_intl": True,
    "tokenrhythm": True,
    "vllm": False,
    "volcengine": True,
    "volcengine_coding_plan": True,
    "volcengine_coding_plan_anthropic": True,
    "zhipu": True,
}

# resolve_context_window() for a model id unknown to every layer — frozen
# pre-migration. Providers in LOCAL_RUNTIME_PROVIDERS report the local
# runtime window; everything else reports the cloud default.
_EXPECTED_UNKNOWN_CONTEXT_WINDOW: dict[str, int] = {
    "aihubmix": 200000,
    "anthropic": 200000,
    "azure": 200000,
    "bailian_coding": 200000,
    "bailian_coding_cn": 200000,
    "byteplus": 200000,
    "byteplus_coding_plan": 200000,
    "byteplus_coding_plan_anthropic": 200000,
    "custom": 8192,
    "dashscope": 200000,
    "deepseek": 200000,
    "gemini": 200000,
    "github_copilot": 200000,
    "groq": 200000,
    "kimi_coding_anthropic": 200000,
    "kimi_coding_openai": 200000,
    "litellm_proxy": 200000,
    "lm_studio": 8192,
    "minimax": 200000,
    "minimax_coding_anthropic": 200000,
    "minimax_coding_openai": 200000,
    "minimax_cn": 200000,
    "minimax_global": 200000,
    "minimax_openai": 200000,
    "mimo_anthropic": 200000,
    "mimo_openai": 200000,
    "mistral": 200000,
    "moonshot": 200000,
    "ollama": 8192,
    "openai": 200000,
    "openai_codex": 200000,
    "openai_responses": 200000,
    "openrouter": 200000,
    "ovms": 8192,
    "qianfan": 200000,
    "siliconflow": 200000,
    "tencent_token_plan": 200000,
    "tencent_token_plan_anthropic": 200000,
    "tencent_tokenhub": 200000,
    "tencent_tokenhub_anthropic": 200000,
    "tencent_tokenhub_intl": 200000,
    "tokenrhythm": 200000,
    "vllm": 8192,
    "volcengine": 200000,
    "volcengine_coding_plan": 200000,
    "volcengine_coding_plan_anthropic": 200000,
    "zhipu": 200000,
    "local": 8192,
    "": 200000,
}


def test_get_capabilities_parity_full_sweep() -> None:
    catalog = ModelCatalog()
    for (provider, model, base_url), expected in _EXPECTED_CAPS.items():
        caps = catalog.get_capabilities(model, provider_name=provider, base_url=base_url)
        observed = (
            caps.supports_reasoning,
            caps.supports_tools,
            caps.supports_vision,
            caps.reasoning_format,
        )
        assert observed == expected, (provider, model, base_url)
        assert caps.supports_streaming is True, (provider, model, base_url)


def test_sweep_covers_every_registered_provider() -> None:
    swept = {provider for provider, _model, _base in _EXPECTED_CAPS}
    assert set(list_provider_names()) <= swept


def test_requires_api_key_parity_every_provider() -> None:
    for spec in list_provider_specs():
        assert spec.requires_api_key() == _EXPECTED_REQUIRES_API_KEY[spec.provider_id], (
            spec.provider_id
        )


def test_unknown_model_context_window_parity_every_provider() -> None:
    catalog = ModelCatalog()
    for provider, window in _EXPECTED_UNKNOWN_CONTEXT_WINDOW.items():
        assert catalog.resolve_context_window("model-unknown-everywhere-zz9", provider) == window, (
            provider
        )


def test_named_provider_sets_stay_distinct() -> None:
    # Unifying membership would flip requires_api_key("vllm") — these MUST
    # stay two distinct sets with an explicit superset derivation.
    assert KEYLESS_PROVIDERS == frozenset({"ollama", "lm_studio", "ovms", "custom"})
    assert LOCAL_RUNTIME_PROVIDERS == KEYLESS_PROVIDERS | {"vllm", "local"}
    # vllm/local are local runtimes for the context heuristic but NOT keyless.
    assert "vllm" in LOCAL_RUNTIME_PROVIDERS
    assert "vllm" not in KEYLESS_PROVIDERS
    assert get_provider_spec("vllm").requires_api_key() is False  # no env_key, not keyless
    # A vLLM-style deployment CAN carry auth; keyless status is not implied by
    # being a local runtime. The custom endpoint is keyless AND local.
    assert "custom" in KEYLESS_PROVIDERS and "custom" in LOCAL_RUNTIME_PROVIDERS


def test_local_runtime_window_matches_membership() -> None:
    from opensquilla.provider.model_catalog import _LOCAL_CONTEXT_WINDOW

    catalog = ModelCatalog()
    for provider in LOCAL_RUNTIME_PROVIDERS:
        assert (
            catalog.resolve_context_window("unknown-local-model-zz9", provider)
            == _LOCAL_CONTEXT_WINDOW
        ), provider
    # A cloud provider id falls to the cloud default instead.
    assert catalog.resolve_context_window("unknown-cloud-model-zz9", "openai") == (
        DEFAULT_CONTEXT_WINDOW
    )


# ---------------------------------------------------------------------------
# Flag gate (decision OQ#5): anthropic/ollama early-return-empty stays the
# default for one release; flipping the flag routes both through the
# layered catalog.
# ---------------------------------------------------------------------------


def test_anthropic_ollama_flag_defaults_off_and_returns_empty() -> None:
    from opensquilla.provider import model_catalog as model_catalog_module
    from opensquilla.provider.types import ModelCapabilities

    assert model_catalog_module.CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA is False
    catalog = ModelCatalog()
    for provider, model in (("anthropic", "claude-opus-4-6"), ("ollama", "llama3.2:3b")):
        assert catalog.get_capabilities(model, provider_name=provider) == ModelCapabilities()


def test_anthropic_ollama_flag_on_resolves_through_catalog(monkeypatch) -> None:
    from opensquilla.provider import model_catalog as model_catalog_module

    monkeypatch.setattr(
        model_catalog_module, "CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA", True
    )
    catalog = ModelCatalog()
    # Snapshot knows claude-opus-4-6 (tools+vision); reasoning stays off
    # because no layer names an anthropic streaming dialect yet. THIS is the
    # engine-visible change the one-release gate defers: supports_vision
    # flips False -> True, which stops engine-level image stripping.
    claude = catalog.get_capabilities("claude-opus-4-6", provider_name="anthropic")
    assert (
        claude.supports_reasoning,
        claude.supports_tools,
        claude.supports_vision,
        claude.reasoning_format,
    ) == (False, True, True, "none")
    # An unqualified local model is unknown to every layer -> synthesized
    # floor, which happens to match the historical empty capabilities.
    local = catalog.get_capabilities("llama3.2:3b", provider_name="ollama")
    assert (
        local.supports_reasoning,
        local.supports_tools,
        local.supports_vision,
        local.reasoning_format,
    ) == (False, True, False, "none")


# ---------------------------------------------------------------------------
# Ladder ordering that resolve_entry cannot express is kept as code; freeze
# it here: a live OpenRouter reasoning hit outranks the api.openai.com host
# guard, and host trust never leaks to data rows.
# ---------------------------------------------------------------------------


def _catalog_with_live_row(model_id: str, *, reasoning: bool) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": model_id,
                "name": "Synthetic live row",
                "context_length": 128_000,
                "top_provider": {"max_completion_tokens": 32_768},
                "supported_parameters": ["tools", "reasoning"] if reasoning else ["tools"],
                "architecture": {"input_modalities": ["text", "image"]},
            }
        ]
    )
    return catalog


def test_live_reasoning_hit_outranks_openai_host_guard() -> None:
    catalog = _catalog_with_live_row("gpt-5-proxy-row", reasoning=True)
    caps = catalog.get_capabilities(
        "gpt-5-proxy-row",
        provider_name="openai",
        base_url="https://api.openai.com/v1",
    )
    # Pre-migration order: the live-catalog reasoning branch ran BEFORE the
    # api.openai.com prefix branch, so the OpenRouter dialect wins.
    assert caps.supports_reasoning is True
    assert caps.reasoning_format == "openrouter"
    assert caps.supports_vision is True  # live row carries image modality


def test_live_non_reasoning_hit_falls_through_to_layers() -> None:
    catalog = _catalog_with_live_row("vendor/plain-model", reasoning=False)
    caps = catalog.get_capabilities("vendor/plain-model", provider_name="openrouter")
    # Same as the legacy final fallback: live tools/vision, no reasoning.
    assert caps.supports_reasoning is False
    assert caps.reasoning_format == "none"
    assert caps.supports_tools is True
    assert caps.supports_vision is True


def test_deepseek_base_url_sniff_stays_code_for_openai_provider_only() -> None:
    catalog = ModelCatalog()
    sniffed = catalog.get_capabilities(
        "some-model", provider_name="openai", base_url="https://api.deepseek.com/v1"
    )
    assert sniffed.supports_reasoning is True
    assert sniffed.reasoning_format == "deepseek"
    # Another provider id with the same base URL does not trigger the sniff.
    other = catalog.get_capabilities(
        "some-model", provider_name="groq", base_url="https://api.deepseek.com/v1"
    )
    assert other.supports_reasoning is False
    assert other.reasoning_format == "none"


# ---------------------------------------------------------------------------
# New, deliberate capability of the migration: the user-override layer now
# reaches get_capabilities (highest authority), so operators can fix a
# wrong capability row without waiting for a release.
# ---------------------------------------------------------------------------


def test_user_overrides_now_reach_get_capabilities() -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "dashscope/qwen3-max": {"supports_reasoning": False},
            "brand-new-model": {
                "supports_reasoning": True,
                "reasoning_format": "deepseek",
                "supports_vision": True,
            },
        }
    )
    # An explicit False beats the transcribed ladder row...
    downgraded = catalog.get_capabilities("qwen3-max", provider_name="dashscope")
    assert downgraded.supports_reasoning is False
    assert downgraded.reasoning_format == "none"
    # ...and a full user row can enable reasoning for an unknown model,
    # because it names the dialect itself.
    upgraded = catalog.get_capabilities("brand-new-model", provider_name="siliconflow")
    assert upgraded.supports_reasoning is True
    assert upgraded.reasoning_format == "deepseek"
    assert upgraded.supports_vision is True


def test_reasoning_without_dialect_is_never_invented() -> None:
    # A user row (or snapshot row) claiming supports_reasoning WITHOUT a
    # reasoning_format must not enable reasoning: the adapter refuses to
    # invent a streaming dialect (the legacy fallback's exact semantics).
    catalog = ModelCatalog()
    catalog.set_user_overrides({"formatless-model": {"supports_reasoning": True}})
    caps = catalog.get_capabilities("formatless-model", provider_name="siliconflow")
    assert caps.supports_reasoning is False
    assert caps.reasoning_format == "none"
