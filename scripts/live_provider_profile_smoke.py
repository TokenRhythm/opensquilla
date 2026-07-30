#!/usr/bin/env python3
"""Live smoke selected provider profiles without printing secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opensquilla.engine.pricing import estimate_cost, resolve_model_price  # noqa: E402
from opensquilla.provider.model_catalog import ModelCatalog  # noqa: E402
from opensquilla.provider.openai import _versioned_api_url  # noqa: E402
from opensquilla.provider.reasoning_dialects import (  # noqa: E402
    ReasoningDisableArgs,
    apply_reasoning_disable,
)
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from opensquilla.provider.selector import ProviderConfig, _build_provider  # noqa: E402
from opensquilla.provider.types import (  # noqa: E402
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
)
from scripts.live_harness_security import (  # noqa: E402
    classify_failure,
    is_temporary_report_path,
    parse_secrets_file,
    provider_secret_names,
    redact_text,
    registry_endpoint,
    report_contains_secret,
    sanitize_report,
    write_safe_report,
)


@dataclass
class SmokeResult:
    provider: str
    model: str
    base_url: str
    env_key: str
    key_present: bool
    direct_status: str
    stream_status: str
    response_model: str
    content_match: str
    usage: dict[str, Any]
    cost: dict[str, Any]
    error: str
    latency_ms: int


_MODEL_ENV = {
    "anthropic": "ANTHROPIC_MODEL",
    "openai": "OPENAI_MODEL",
    "openai_responses": "OPENAI_MODEL",
    "openrouter": "OPENROUTER_MODEL",
    "dashscope": "DASHSCOPE_MODEL",
    "deepseek": "DEEPSEEK_MODEL",
    "gemini": "GEMINI_MODEL",
    "volcengine": "VOLCENGINE_MODEL",
    "volcengine_coding_plan": "VOLCENGINE_CODING_MODEL",
    "byteplus": "BYTEPLUS_MODEL",
    "bailian_coding": "BAILIAN_CODING_MODEL",
    "moonshot": "MOONSHOT_MODEL",
    "kimi_coding_openai": "KIMI_CODING_MODEL",
    "kimi_coding_anthropic": "KIMI_CODING_MODEL",
    "zhipu": "ZAI_MODEL",
    "qianfan": "QIANFAN_MODEL",
    "minimax": "MINIMAX_MODEL",
    "minimax_openai": "MINIMAX_MODEL",
    "minimax_coding_openai": "MINIMAX_CODING_MODEL",
    "minimax_coding_anthropic": "MINIMAX_CODING_MODEL",
    "minimax_cn": "MINIMAX_CN_MODEL",
    "minimax_global": "MINIMAX_GLOBAL_MODEL",
    "mimo_openai": "MIMO_MODEL",
    "mimo_anthropic": "MIMO_MODEL",
    "tencent_tokenhub": "TENCENT_TOKENHUB_MODEL",
    "tencent_tokenhub_anthropic": "TENCENT_TOKENHUB_MODEL",
    "tencent_tokenhub_intl": "TENCENT_TOKENHUB_INTL_MODEL",
    "tencent_token_plan": "TENCENT_TOKEN_PLAN_MODEL",
    "tencent_token_plan_anthropic": "TENCENT_TOKEN_PLAN_MODEL",
    "tokenrhythm": "TOKENRHYTHM_MODEL",
}

_BASE_ENV = {
    "anthropic": "ANTHROPIC_BASE_URL",
    "openai": "OPENAI_BASE_URL",
    "openai_responses": "OPENAI_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
    "dashscope": "DASHSCOPE_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "gemini": "GEMINI_BASE_URL",
    "volcengine": "VOLCENGINE_BASE_URL",
    "volcengine_coding_plan": "VOLCENGINE_CODING_BASE_URL",
    "byteplus": "BYTEPLUS_BASE_URL",
    "bailian_coding": "BAILIAN_CODING_BASE_URL",
    "moonshot": "MOONSHOT_BASE_URL",
    "kimi_coding_openai": "KIMI_CODING_OPENAI_BASE_URL",
    "kimi_coding_anthropic": "KIMI_CODING_ANTHROPIC_BASE_URL",
    "zhipu": "ZAI_BASE_URL",
    "qianfan": "QIANFAN_BASE_URL",
    "minimax": "MINIMAX_BASE_URL",
    "minimax_openai": "MINIMAX_OPENAI_BASE_URL",
    "minimax_coding_openai": "MINIMAX_CODING_OPENAI_BASE_URL",
    "minimax_coding_anthropic": "MINIMAX_CODING_ANTHROPIC_BASE_URL",
    "minimax_cn": "MINIMAX_CN_BASE_URL",
    "minimax_global": "MINIMAX_GLOBAL_BASE_URL",
    "mimo_openai": "MIMO_OPENAI_BASE_URL",
    "mimo_anthropic": "MIMO_ANTHROPIC_BASE_URL",
    "tencent_tokenhub": "TENCENT_TOKENHUB_BASE_URL",
    "tencent_tokenhub_anthropic": "TENCENT_TOKENHUB_ANTHROPIC_BASE_URL",
    "tencent_tokenhub_intl": "TENCENT_TOKENHUB_INTL_BASE_URL",
    "tencent_token_plan": "TENCENT_TOKEN_PLAN_BASE_URL",
    "tencent_token_plan_anthropic": "TENCENT_TOKEN_PLAN_ANTHROPIC_BASE_URL",
    "tokenrhythm": "TOKENRHYTHM_BASE_URL",
}

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-5.4-mini",
    "openai_responses": "gpt-5.4-mini",
    "openrouter": "deepseek/deepseek-v4-flash",
    "dashscope": "qwen3.7-plus",
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-3.5-flash",
    "volcengine": "doubao-seed-2-0-lite-260215",
    "volcengine_coding_plan": "doubao-seed-2.0-pro",
    "byteplus": "seed-2-0-lite-260228",
    "bailian_coding": "kimi-k2.5",
    "moonshot": "kimi-k2.6",
    "kimi_coding_openai": "kimi-for-coding",
    "kimi_coding_anthropic": "kimi-for-coding",
    "zhipu": "glm-5",
    "qianfan": "ernie-4.5-turbo-128k",
    "minimax": "MiniMax-M2.7",
    "minimax_openai": "MiniMax-M2.7",
    "minimax_coding_openai": "MiniMax-M2.7",
    "minimax_coding_anthropic": "MiniMax-M2.7",
    "minimax_cn": "MiniMax-M2.7",
    "minimax_global": "MiniMax-M2.7",
    "mimo_openai": "mimo-v2.5",
    "mimo_anthropic": "mimo-v2.5-pro",
    "tencent_tokenhub": "hy3",
    "tencent_tokenhub_anthropic": "hy3",
    "tencent_tokenhub_intl": "deepseek-v3.2",
    "tencent_token_plan": "hy3",
    "tencent_token_plan_anthropic": "hy3",
    "tokenrhythm": "deepseek-v4-flash",
}

# Providers whose models spend reasoning tokens out of max_tokens before any
# text: the CLI default budget of 64 would come back as empty content with
# finish_reason "length", failing the smoke for provider-independent reasons.
_MIN_MAX_TOKENS = {
    # MiniMax M2.7 honors the output cap exactly and may spend most of a
    # 32-token smoke on its answer preamble before reaching the marker.
    # Sixty-four remains inside the ordinary-smoke contract.
    "minimax": 64,
    "tokenrhythm": 1024,
}

_DIRECT_TIMEOUT_SECONDS = 60.0
_PUBLIC_RESULT_KEYS = frozenset(
    {
        "provider",
        "model",
        "status",
        "failure_class",
        "usage",
        "cost",
        "latency_ms",
    }
)


def _csv_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _load_env_quietly(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for key, value in parse_secrets_file(path).items():
        os.environ.setdefault(key, value)


def _headers_for_openai(api_key: str) -> dict[str, str]:
    # Keyless local providers must not send an empty Bearer value (httpx
    # rejects it as an illegal header).
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _headers_for_anthropic(
    api_key: str,
    auth_header_style: str = "x-api-key",
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if not api_key:
        return headers
    if auth_header_style == "x-api-key":
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _versioned_chat_url(base_url: str) -> str:
    return _versioned_api_url(base_url, "/v1/chat/completions")


def _versioned_responses_url(base_url: str) -> str:
    return _versioned_api_url(base_url, "/v1/responses")


def _versioned_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _direct_openai_temperature(provider: str, model: str) -> int:
    if provider == "kimi_coding_openai" and model == "kimi-for-coding":
        return 1
    if provider == "moonshot" and model.lower().startswith("kimi-k2."):
        return 1
    return 0


def _direct_openai_token_limit_field(provider: str, model: str) -> str:
    if provider == "openai" and model.lower().startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def _model_capabilities(provider: str, model: str, base_url: str) -> Any:
    return ModelCatalog().get_capabilities(
        model,
        provider_name=provider,
        base_url=base_url,
    )


def _apply_direct_reasoning_off(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
    base_url: str,
) -> None:
    """Make a bounded raw smoke comparable to the production adapter call."""

    caps = _model_capabilities(provider, model, base_url)
    if not caps.supports_reasoning:
        return
    spec = get_provider_spec(provider)
    apply_reasoning_disable(
        payload,
        caps.reasoning_format,
        ReasoningDisableArgs(
            model=model,
            disable_reasoning_by_default_models=(
                spec.compat.disable_reasoning_by_default_models
            ),
        ),
    )


async def _direct_openai(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    expected: str,
    max_tokens: int,
) -> tuple[str, str, str, dict[str, Any], int]:
    start = time.perf_counter()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"Reply exactly with: {expected}",
            }
        ],
        "temperature": _direct_openai_temperature(provider, model),
    }
    payload[_direct_openai_token_limit_field(provider, model)] = max_tokens
    _apply_direct_reasoning_off(
        payload,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    try:
        async with httpx.AsyncClient(
            timeout=_DIRECT_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            resp = await client.post(
                _versioned_chat_url(base_url),
                headers=_headers_for_openai(api_key),
                json=payload,
            )
        latency = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            return "failed", "", _error_summary(resp, secrets=(api_key,)), {}, latency
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        response_model = str(data.get("model") or "")
        status = "passed" if expected in content else "content_mismatch"
        return status, response_model, content, _usage_summary(data.get("usage")), latency
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostic
        latency = int((time.perf_counter() - start) * 1000)
        return "failed", "", redact_text(f"{type(exc).__name__}: {exc}", (api_key,)), {}, latency


def _responses_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                parts.append(str(block.get("text") or ""))
    return "".join(parts)


async def _direct_openai_responses(
    model: str,
    api_key: str,
    base_url: str,
    expected: str,
    max_tokens: int,
) -> tuple[str, str, str, dict[str, Any], int]:
    start = time.perf_counter()
    payload = {
        "model": model,
        "input": [{"role": "user", "content": f"Reply exactly with: {expected}"}],
        "max_output_tokens": max_tokens,
        "store": False,
    }
    try:
        async with httpx.AsyncClient(
            timeout=_DIRECT_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            resp = await client.post(
                _versioned_responses_url(base_url),
                headers=_headers_for_openai(api_key),
                json=payload,
            )
        latency = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            return "failed", "", _error_summary(resp, secrets=(api_key,)), {}, latency
        data = resp.json()
        content = _responses_text(data)
        response_model = str(data.get("model") or "")
        status = "passed" if expected in content else "content_mismatch"
        return status, response_model, content, _usage_summary(data.get("usage")), latency
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostic
        latency = int((time.perf_counter() - start) * 1000)
        return "failed", "", redact_text(f"{type(exc).__name__}: {exc}", (api_key,)), {}, latency


async def _direct_anthropic(
    model: str,
    api_key: str,
    base_url: str,
    expected: str,
    max_tokens: int,
    auth_header_style: str = "x-api-key",
) -> tuple[str, str, str, dict[str, Any], int]:
    start = time.perf_counter()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"Reply exactly with: {expected}"}],
        "max_tokens": max_tokens,
        "temperature": 1,
    }
    try:
        async with httpx.AsyncClient(
            timeout=_DIRECT_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            resp = await client.post(
                _versioned_messages_url(base_url),
                headers=_headers_for_anthropic(api_key, auth_header_style),
                json=payload,
            )
        latency = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            return "failed", "", _error_summary(resp, secrets=(api_key,)), {}, latency
        data = resp.json()
        text_parts = [
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        content = "".join(text_parts)
        response_model = str(data.get("model") or "")
        status = "passed" if expected in content else "content_mismatch"
        return status, response_model, content, _usage_summary(data.get("usage")), latency
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostic
        latency = int((time.perf_counter() - start) * 1000)
        return "failed", "", redact_text(f"{type(exc).__name__}: {exc}", (api_key,)), {}, latency


async def _stream_opensquilla(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    expected: str,
    max_tokens: int,
) -> tuple[str, str, dict[str, Any], int]:
    start = time.perf_counter()
    try:
        built = _build_provider(
            ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)
        )
        caps = _model_capabilities(provider, model, base_url)
        chunks: list[str] = []
        done: DoneEvent | None = None
        async for event in built.chat(
            [Message(role="user", content=f"Reply exactly with: {expected}")],
            # Match the production default.  A forced temperature is rejected
            # by some fixed-sampling/reasoning models (notably Responses), and
            # is unrelated to the stream contract this smoke validates.
            config=ChatConfig(
                max_tokens=max_tokens,
                temperature=None,
                timeout=_DIRECT_TIMEOUT_SECONDS,
                model_capabilities=caps,
            ),
        ):
            if isinstance(event, TextDeltaEvent):
                chunks.append(event.text)
            elif isinstance(event, DoneEvent):
                done = event
            elif isinstance(event, ErrorEvent):
                latency = int((time.perf_counter() - start) * 1000)
                return "failed", redact_text(event.message or event.code, (api_key,)), {}, latency
        latency = int((time.perf_counter() - start) * 1000)
        content = "".join(chunks)
        if done is None:
            return "failed", "missing DoneEvent", {}, latency
        usage = {
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "cached_tokens": done.cached_tokens,
            "cache_write_tokens": done.cache_write_tokens,
            "reasoning_tokens": done.reasoning_tokens,
            "model": done.model,
            "billed_cost": done.billed_cost,
            "cost_source": done.cost_source,
        }
        status = "passed" if expected in content else "content_mismatch"
        return status, content, usage, latency
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostic
        latency = int((time.perf_counter() - start) * 1000)
        return "failed", redact_text(f"{type(exc).__name__}: {exc}", (api_key,)), {}, latency


def _usage_summary(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {key: usage[key] for key in keys if key in usage}


def _cost_estimate(provider: str, model: str, usage: dict[str, Any]) -> dict[str, Any]:
    direct_value = usage.get("direct")
    stream_value = usage.get("stream")
    direct_usage: dict[str, Any] = direct_value if isinstance(direct_value, dict) else {}
    stream_usage: dict[str, Any] = stream_value if isinstance(stream_value, dict) else {}
    prompt_tokens = direct_usage.get("prompt_tokens") or stream_usage.get("input_tokens") or 0
    completion_tokens = (
        direct_usage.get("completion_tokens") or stream_usage.get("output_tokens") or 0
    )
    cache_read_tokens = int(
        stream_usage.get("cached_tokens")
        or direct_usage.get("cache_read_input_tokens")
        or 0
    )
    cache_write_tokens = int(
        stream_usage.get("cache_write_tokens")
        or direct_usage.get("cache_creation_input_tokens")
        or 0
    )
    resolved = resolve_model_price(model, provider)
    estimate_result = estimate_cost(
        input_tokens=int(prompt_tokens),
        output_tokens=int(completion_tokens),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        price=resolved.entry,
    )
    estimate = estimate_result.cost_usd
    # The stream DoneEvent carries the provider-billed cost when the upstream
    # reports one (OpenRouter usage.cost); surface it instead of pretending
    # only static estimates exist.
    billed = stream_usage.get("billed_cost") or 0.0
    billed_source = str(stream_usage.get("cost_source") or "")
    provider_billed = billed if billed_source == "provider_billed" else None
    cost_source = billed_source if provider_billed is not None else "opensquilla_static_estimate"
    return {
        "provider_billed_cost_usd": provider_billed,
        "opensquilla_estimated_cost_usd": estimate,
        "cost_source": cost_source,
        "billing_scope": "provider_billed" if provider_billed is not None else "static_estimate",
        "provider_billed": provider_billed,
        "opensquilla_estimate": estimate,
        "input_per_m": resolved.entry.input_per_m,
        "output_per_m": resolved.entry.output_per_m,
        "cache_read_per_m": resolved.entry.cache_read_per_m,
        "cache_write_per_m": resolved.entry.cache_write_per_m,
        "price_source": resolved.source,
        "estimate_basis": estimate_result.basis,
        "source": cost_source,
    }


def _error_summary(resp: httpx.Response, *, secrets: tuple[str, ...] = ()) -> str:
    try:
        body = json.dumps(resp.json(), ensure_ascii=False)
    except ValueError:
        body = resp.text
    return redact_text(f"HTTP {resp.status_code}: {body[:300]}", secrets)


async def smoke_provider(
    provider: str,
    *,
    include_stream: bool = True,
    model_override: str | None = None,
    base_url_override: str | None = None,
    max_tokens: int = 64,
    apply_token_floor: bool = True,
) -> SmokeResult:
    spec = get_provider_spec(provider)
    env_key = spec.env_key
    api_key = os.environ.get(env_key, "").strip()
    if apply_token_floor:
        max_tokens = max(max_tokens, _MIN_MAX_TOKENS.get(provider, 0))
    model = (
        model_override
        or os.environ.get(_MODEL_ENV.get(provider, ""), "").strip()
        or _DEFAULT_MODELS.get(provider, "")
    )
    if not model:
        raise SystemExit(
            f"no model configured for provider {provider!r}: pass --model or set "
            f"{_MODEL_ENV.get(provider) or 'a model env override'}"
        )
    requested_base_url = (
        base_url_override or os.environ.get(_BASE_ENV.get(provider, ""), "").strip()
    )
    base_url = registry_endpoint(provider, requested_base_url or None)
    expected = f"opensquilla {provider} smoke ok"

    # Local providers (ollama, lm_studio, ovms) declare their key optional in
    # the registry; only skip when the spec actually requires one.
    if not api_key and spec.requires_api_key():
        return SmokeResult(
            provider=provider,
            model=model,
            base_url=base_url,
            env_key=env_key,
            key_present=False,
            direct_status="skipped",
            stream_status="skipped",
            response_model="",
            content_match="not_run",
            usage={},
            cost={
                "provider_billed_cost_usd": None,
                "opensquilla_estimated_cost_usd": None,
                "cost_source": "unavailable",
                "billing_scope": "none",
                "provider_billed": None,
                "opensquilla_estimate": None,
                "source": "unavailable",
            },
            error=f"{env_key} is empty",
            latency_ms=0,
        )

    if spec.backend == "anthropic":
        (
            direct_status,
            response_model,
            direct_content,
            usage,
            direct_latency,
        ) = await _direct_anthropic(
            model,
            api_key,
            base_url,
            expected,
            max_tokens,
            spec.auth_header_style,
        )
    elif spec.backend == "openai_responses":
        (
            direct_status,
            response_model,
            direct_content,
            usage,
            direct_latency,
        ) = await _direct_openai_responses(model, api_key, base_url, expected, max_tokens)
    else:
        direct_status, response_model, direct_content, usage, direct_latency = await _direct_openai(
            provider, model, api_key, base_url, expected, max_tokens
        )
    if include_stream:
        stream_status, stream_content, stream_usage, stream_latency = await _stream_opensquilla(
            provider, model, api_key, base_url, expected, max_tokens
        )
    else:
        stream_status = "skipped"
        stream_content = ""
        stream_usage = {}
        stream_latency = 0

    errors = []
    if direct_status == "failed":
        errors.append(f"direct={direct_content}")
    if stream_status == "failed":
        errors.append(f"stream={stream_content}")
    content_match = (
        "exact" if direct_status == "passed" and stream_status == "passed" else "not_validated"
    )
    if direct_status == "passed" and stream_status == "skipped":
        content_match = "direct_exact"
    merged_usage = {"direct": usage, "stream": stream_usage}

    return SmokeResult(
        provider=provider,
        model=model,
        base_url=base_url,
        env_key=env_key,
        key_present=bool(api_key),
        direct_status=direct_status,
        stream_status=stream_status,
        response_model=response_model,
        content_match=content_match,
        usage=merged_usage,
        cost=_cost_estimate(provider, response_model or model, merged_usage),
        error="; ".join(errors),
        latency_ms=direct_latency + stream_latency,
    )


def _project_public_result(result: SmokeResult) -> dict[str, Any]:
    """Project detailed smoke evidence onto the persisted report contract."""

    skipped = result.direct_status == "skipped" and result.stream_status == "skipped"
    passed = result.direct_status == "passed" and result.stream_status in {
        "passed",
        "skipped",
    }
    status = "skipped" if skipped else ("passed" if passed else "failed")
    failure_class = None
    if skipped:
        failure_class = "missing-credential"
    elif not passed:
        failure_class = classify_failure(
            result.error or f"{result.direct_status} {result.stream_status}"
        )
    return {
        "provider": result.provider,
        "model": result.response_model or result.model,
        "status": status,
        "failure_class": failure_class,
        "usage": dict(result.usage),
        "cost": dict(result.cost),
        "latency_ms": int(result.latency_ms),
    }


def _assert_public_report_schema(report: Any) -> None:
    """Require an array of exact public rows before and after sanitizing."""

    if not isinstance(report, list):
        raise RuntimeError("public live report must be a JSON array")
    for index, row in enumerate(report):
        if not isinstance(row, dict) or set(row) != _PUBLIC_RESULT_KEYS:
            raise RuntimeError(f"public live report row {index} has an invalid field set")
        if not all(isinstance(row[field], str) for field in ("provider", "model", "status")):
            raise RuntimeError(f"public live report row {index} has an invalid identity")
        if row["failure_class"] is not None and not isinstance(row["failure_class"], str):
            raise RuntimeError(f"public live report row {index} has an invalid failure class")
        if not isinstance(row["usage"], dict) or not isinstance(row["cost"], dict):
            raise RuntimeError(f"public live report row {index} has invalid accounting fields")
        if isinstance(row["latency_ms"], bool) or not isinstance(row["latency_ms"], int | float):
            raise RuntimeError(f"public live report row {index} has an invalid latency")


def _public_report_rows(results: list[SmokeResult]) -> list[dict[str, Any]]:
    rows = [_project_public_result(result) for result in results]
    _assert_public_report_schema(rows)
    return rows


def _emit_main_diagnostics(rows: list[dict[str, Any]], secrets: dict[str, str]) -> None:
    coverage = {
        "requested": len(rows),
        "passed": sum(row["status"] == "passed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
    }
    message = "live provider smoke coverage: " + json.dumps(coverage, sort_keys=True)
    print(redact_text(message, secrets), file=sys.stderr)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["dashscope", "deepseek", "gemini", "volcengine", "byteplus"],
    )
    parser.add_argument("--models")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--exact-max-tokens", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-stream", action="store_true")
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--child-report", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not is_temporary_report_path(output):
        parser.error("--output must be inside the system temporary directory")

    if not args.no_env_file and os.environ.get("OPENSQUILLA_LIVE_DISABLE_DOTENV") != "1":
        _load_env_quietly()
    providers = [args.provider] if args.provider else list(args.providers)
    models = _csv_values(args.models)
    if args.model and models:
        parser.error("--model and --models are mutually exclusive")
    if models and len(providers) != 1:
        parser.error("--models requires exactly one provider")

    jobs: list[tuple[str, str | None]] = []
    if models:
        jobs = [(providers[0], model) for model in models]
    else:
        jobs = [(provider, args.model) for provider in providers]

    results = [
        await smoke_provider(
            provider,
            include_stream=not args.skip_stream,
            model_override=model,
            base_url_override=args.base_url,
            max_tokens=args.max_tokens,
            apply_token_floor=not args.exact_max_tokens,
        )
        for provider, model in jobs
    ]
    all_ok = all(
        result.direct_status == "passed" and result.stream_status in {"passed", "skipped"}
        for result in results
    )
    detailed_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": all_ok,
        "results": [asdict(result) for result in results],
    }
    secrets = {
        name: os.environ.get(name, "") for name in provider_secret_names() if os.environ.get(name)
    }
    try:
        if args.child_report:
            output_payload: Any = detailed_payload
        else:
            output_payload = _public_report_rows(results)
            _emit_main_diagnostics(output_payload, secrets)
        output_payload = sanitize_report(output_payload, secrets)
        if report_contains_secret(output_payload, secrets):
            raise RuntimeError("refusing to write a report containing provider credentials")
        output_payload = write_safe_report(output, output_payload, secrets)
        if not args.child_report:
            _assert_public_report_schema(output_payload)
    except (OSError, RuntimeError, ValueError) as exc:
        output.unlink(missing_ok=True)
        print(redact_text(f"unable to write live report: {exc}", secrets), file=sys.stderr)
        return 2
    print(json.dumps(output_payload, indent=2, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
