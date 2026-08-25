#!/usr/bin/env python3
"""Live smoke provider-native thinking controls without printing secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from opensquilla.provider.compat_policy import effective_reasoning_format
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderConfig, _build_provider
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent

_MODEL_ENV = {
    "volcengine": "VOLCENGINE_MODEL",
    "deepseek": "DEEPSEEK_MODEL",
    "dashscope": "DASHSCOPE_MODEL",
    "gemini": "GEMINI_MODEL",
    "moonshot": "MOONSHOT_MODEL",
    "zhipu": "ZAI_MODEL",
}

_BASE_ENV = {
    "volcengine": "VOLCENGINE_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "dashscope": "DASHSCOPE_BASE_URL",
    "gemini": "GEMINI_BASE_URL",
    "moonshot": "MOONSHOT_BASE_URL",
    "zhipu": "ZAI_BASE_URL",
}

_DEFAULT_MODELS = {
    "volcengine": "doubao-seed-1-6-thinking-250715",
    "deepseek": "deepseek-v4-pro",
    "dashscope": "qwen3.6-plus",
    "gemini": "gemini-2.5-flash",
    "moonshot": "kimi-k2.5",
    "zhipu": "glm-5.1",
}


@dataclass
class ThinkingCaseResult:
    mode: str
    direct_status: str
    direct_latency_ms: int
    direct_response_model: str
    direct_text: str
    direct_reasoning_content_present: bool
    direct_usage: dict[str, Any]
    direct_error: str
    stream_status: str
    stream_latency_ms: int
    stream_text: str
    stream_reasoning_content_present: bool
    stream_reasoning_tokens: int
    stream_usage: dict[str, Any]
    stream_error: str
    expected_marker_present_direct: bool
    expected_marker_present_stream: bool


def _load_env_quietly(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _provider_thinking_payload(
    provider: str,
    *,
    model: str,
    base_url: str,
    enabled: bool,
    budget: int,
) -> dict[str, Any]:
    if provider == "dashscope":
        payload: dict[str, Any] = {"enable_thinking": enabled}
        if enabled:
            payload["thinking_budget"] = budget
        return payload
    if provider == "gemini":
        return {"reasoning_effort": "medium" if enabled else "none"}
    if provider == "deepseek":
        payload = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if enabled:
            payload["reasoning_effort"] = "high"
        return payload
    if provider == "zhipu":
        caps = ModelCatalog().get_capabilities(
            model,
            provider_name=provider,
            base_url=base_url,
        )
        dialect = effective_reasoning_format(
            get_provider_spec(provider).compat,
            caps.reasoning_format,
            base_url,
        )
        if dialect != "zai":
            return {}
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if provider in {"moonshot", "volcengine"}:
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    return {}


def _usage_summary(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    summary = {key: usage[key] for key in keys if key in usage}
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and "reasoning_tokens" in details:
        summary["completion_tokens_details.reasoning_tokens"] = details["reasoning_tokens"]
    return summary


async def _direct_case(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    marker: str,
    enabled: bool,
    max_tokens: int,
    thinking_budget: int,
) -> tuple[str, int, str, str, bool, dict[str, Any], str, dict[str, Any]]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"Reply exactly with: {marker}",
            }
        ],
        "max_tokens": max_tokens,
        **_provider_thinking_payload(
            provider,
            model=model,
            base_url=base_url,
            enabled=enabled,
            budget=thinking_budget,
        ),
    }
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            resp = await client.post(
                _chat_url(base_url),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            return (
                "failed",
                latency_ms,
                "",
                "",
                False,
                {},
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                payload,
            )
        data = resp.json()
        message = data.get("choices", [{}])[0].get("message", {})
        text = str(message.get("content") or "")
        reasoning_content = str(message.get("reasoning_content") or "")
        status = "passed" if marker in text else "content_mismatch"
        return (
            status,
            latency_ms,
            str(data.get("model") or ""),
            text,
            bool(reasoning_content),
            _usage_summary(data.get("usage")),
            "",
            payload,
        )
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostics
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ("failed", latency_ms, "", "", False, {}, f"{type(exc).__name__}: {exc}", payload)


async def _stream_case(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    marker: str,
    enabled: bool,
    max_tokens: int,
    thinking_budget: int,
) -> tuple[str, int, str, bool, int, dict[str, Any], str, dict[str, Any]]:
    caps = ModelCatalog().get_capabilities(model, provider_name=provider, base_url=base_url)
    config = ChatConfig(
        max_tokens=max_tokens,
        temperature=None,
        thinking=enabled,
        thinking_budget_tokens=thinking_budget if enabled else 0,
        timeout=60.0,
        model_capabilities=caps,
    )
    provider_obj = _build_provider(
        ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)
    )
    start = time.perf_counter()
    chunks: list[str] = []
    done: DoneEvent | None = None
    error = ""
    try:
        async for event in provider_obj.chat(
            [Message(role="user", content=f"Reply exactly with: {marker}")],
            config=config,
        ):
            if isinstance(event, TextDeltaEvent):
                chunks.append(event.text)
            elif isinstance(event, DoneEvent):
                done = event
            elif isinstance(event, ErrorEvent):
                error = event.message or event.code
                break
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = "".join(chunks)
        if error:
            return (
                "failed",
                latency_ms,
                text,
                False,
                0,
                {},
                error,
                config.model_dump(mode="json"),
            )
        if done is None:
            return (
                "failed",
                latency_ms,
                text,
                False,
                0,
                {},
                "missing DoneEvent",
                config.model_dump(mode="json"),
            )
        status = "passed" if marker in text else "content_mismatch"
        return (
            status,
            latency_ms,
            text,
            bool(done.reasoning_content),
            done.reasoning_tokens,
            {
                "input_tokens": done.input_tokens,
                "output_tokens": done.output_tokens,
                "reasoning_tokens": done.reasoning_tokens,
                "cached_tokens": done.cached_tokens,
                "cache_write_tokens": done.cache_write_tokens,
                "billed_cost": done.billed_cost,
                "model": done.model,
            },
            "",
            config.model_dump(mode="json"),
        )
    except Exception as exc:  # noqa: BLE001 - smoke reports compact diagnostics
        latency_ms = int((time.perf_counter() - start) * 1000)
        return (
            "failed",
            latency_ms,
            "".join(chunks),
            False,
            0,
            {},
            f"{type(exc).__name__}: {exc}",
            config.model_dump(mode="json"),
        )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="volcengine")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--thinking-budget", type=int, default=4096)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    _load_env_quietly()
    spec = get_provider_spec(args.provider)
    model = (
        args.model
        or os.environ.get(_MODEL_ENV.get(args.provider, ""), "").strip()
        or _DEFAULT_MODELS[args.provider]
    )
    base_url = (
        args.base_url
        or os.environ.get(_BASE_ENV.get(args.provider, ""), "").strip()
        or spec.default_base_url
    )
    api_key = os.environ.get(spec.env_key, "").strip()

    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "provider": args.provider,
        "model": model,
        "base_url": base_url,
        "env_key": spec.env_key,
        "key_present": bool(api_key),
        "cases": [],
    }
    if not api_key:
        payload["error"] = f"{spec.env_key} is empty"
    else:
        for mode, enabled in (("thinking_enabled", True), ("thinking_disabled", False)):
            marker = f"THINKING_{args.provider.upper()}_{mode.upper()}_{int(time.time() * 1000)}"
            (
                direct_status,
                direct_latency_ms,
                direct_response_model,
                direct_text,
                direct_reasoning_present,
                direct_usage,
                direct_error,
                direct_payload,
            ) = await _direct_case(
                provider=args.provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                marker=marker,
                enabled=enabled,
                max_tokens=args.max_tokens,
                thinking_budget=args.thinking_budget,
            )
            (
                stream_status,
                stream_latency_ms,
                stream_text,
                stream_reasoning_present,
                stream_reasoning_tokens,
                stream_usage,
                stream_error,
                stream_config,
            ) = await _stream_case(
                provider=args.provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                marker=marker,
                enabled=enabled,
                max_tokens=args.max_tokens,
                thinking_budget=args.thinking_budget,
            )
            payload["cases"].append(
                {
                    **asdict(
                        ThinkingCaseResult(
                            mode=mode,
                            direct_status=direct_status,
                            direct_latency_ms=direct_latency_ms,
                            direct_response_model=direct_response_model,
                            direct_text=direct_text,
                            direct_reasoning_content_present=direct_reasoning_present,
                            direct_usage=direct_usage,
                            direct_error=direct_error,
                            stream_status=stream_status,
                            stream_latency_ms=stream_latency_ms,
                            stream_text=stream_text,
                            stream_reasoning_content_present=stream_reasoning_present,
                            stream_reasoning_tokens=stream_reasoning_tokens,
                            stream_usage=stream_usage,
                            stream_error=stream_error,
                            expected_marker_present_direct=marker in direct_text,
                            expected_marker_present_stream=marker in stream_text,
                        )
                    ),
                    "marker": marker,
                    "direct_payload_without_secret": direct_payload,
                    "stream_config_without_secret": stream_config,
                }
            )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
