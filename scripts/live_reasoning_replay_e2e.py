#!/usr/bin/env python3
"""Opt-in reasoning replay and suffix compaction checks through reopened SQLite.

Only synthetic prompts and pure tools are used. Native response/request data
stays in memory; the public report contains counts and boolean assertions only.
Run one provider per process with credentials supplied in its registry env var.
Compaction variants have fixed physical generation limits and disable automatic retries.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("scripts.live_reasoning_replay_e2e", sys.modules[__name__])

import httpx  # noqa: E402

from opensquilla.engine.cache_break_monitor import add_compaction_listener  # noqa: E402
from opensquilla.engine.runtime import TurnRunner  # noqa: E402
from opensquilla.gateway.config import GatewayConfig  # noqa: E402
from opensquilla.provider import ModelCapabilities  # noqa: E402
from opensquilla.provider.model_catalog import ModelCatalog  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from opensquilla.provider.selector import (  # noqa: E402
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)
from opensquilla.session.manager import SessionManager  # noqa: E402
from opensquilla.session.storage import SessionStorage  # noqa: E402
from opensquilla.session.terminal_reply import safe_provider_failure_code  # noqa: E402
from opensquilla.token_estimation import estimate_tokens_with_source  # noqa: E402
from opensquilla.tools.registry import ToolRegistry  # noqa: E402
from opensquilla.tools.types import ToolContext, ToolSpec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    minimal_child_environment,
    registry_endpoint,
    require_temporary_report_path,
    sanitize_report,
    scan_and_remove_temporary_tree,
    write_safe_report,
)

DEFAULT_MODELS = {
    "deepseek": "deepseek-v4-flash",
    "openrouter": "deepseek/deepseek-v4-flash",
    "tokenrhythm": "deepseek-v4-pro-0813",
}
FIRST_PROMPT = (
    "This is a synthetic tool protocol test. Call replay_step with value=7 first. "
    "Wait for its result, then call replay_step again with value equal to the returned "
    "next_value. After the second result, finish with only REPLAY_FIRST_OK. "
    "Do not combine the two calls or invent the result."
)
SECOND_PROMPT = (
    "Continue the synthetic test. Call replay_step once with value=23, wait for "
    "the result, and finish with only REPLAY_SECOND_OK."
)
CHAT_PROMPTS = (
    "This is a synthetic conversation test. Remember the label amber-17 and reply CHAT_FIRST_OK.",
    "Return the label I asked you to remember, followed by CHAT_SECOND_OK.",
)
# This independent test expectation deliberately names the gateway's exact
# documented routes; other TokenRhythm families do not inherit V4's contract.
TOKENRHYTHM_TOOL_REASONING_MODELS = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-flash-0731", "deepseek-v4-pro", "deepseek-v4-pro-0813"}
)
THINKING_CHOICES = ("default", "off", "minimal", "low", "medium", "high", "xhigh", "adaptive")
COMPACTION_VARIANTS = (
    "basic", "chunked", "tools", "replay_off", "model_switch", "repeated", "truncated",
    "long_reasoning",
)
COMPACTION_CALL_LIMITS = {
    "basic": 3, "chunked": 4, "tools": 4, "replay_off": 4, "model_switch": 3,
    "repeated": 7, "truncated": 3, "long_reasoning": 3,
}
COMPACTION_FIRST_PROMPT = (
    "This is a synthetic memory test. Invent a new label of exactly eight lowercase letters. "
    "Reply only COMPACTION_LABEL=<your label>. Keep that label as the durable fact for the "
    "next turn. Do not use tools."
)
COMPACTION_TAIL_MARKER = "SYNTHETIC_COMPACTION_CURRENT_TAIL"
COMPACTION_SOURCE_MARKER = "SYNTHETIC_COMPACTION_OLD_HISTORY"
COMPACTION_ENTRY_PATTERN = r"SYNTHETIC_COMPACTION_ENTRY_[0-9]+_(?:USER|ASSISTANT)"
COMPACTION_PADDING = (
    "This is disposable synthetic background prose about arranging colored paper on a table. "
    "It contains no task requirements and can be summarized in a single short sentence. "
)
COMPACTION_TASKS = ("coding", "research", "chinese_mix")


def compaction_task_facts(profile: str) -> dict[str, str]:
    """Public synthetic task state, including completed and pending work."""
    _require(profile in COMPACTION_TASKS, "invalid_task_profile")
    return {
        "coding": {
            "project": "amber-parser", "owner": "Nora", "region": "west-lab",
            "release": "r17", "artifact": "src/parser/scan.py",
            "verification": "python -m pytest tests/test_scan.py -q",
            "deadline": "2030-06-14", "limit": "742",
            "completed": "unicode-boundary-fix", "pending": "empty-stream-regression",
            "rejected": "global-regex-rewrite", "next": "read tests/test_scan.py",
        },
        "research": {
            "project": "cobalt-replication", "owner": "Iris", "region": "north-lab",
            "release": "dataset-v23", "artifact": "data/synthetic-cohort-23.csv",
            "verification": "python analysis/bootstrap.py --seed 619",
            "deadline": "2030-07-19", "limit": "358",
            "completed": "stratified-split-audit", "pending": "confidence-interval-review",
            "rejected": "test-set-hyperparameter-tuning", "next": "read analysis/protocol.md",
        },
        "chinese_mix": {
            "project": "青禾-release-check", "owner": "林青", "region": "华东-test-zone",
            "release": "版本-r29", "artifact": "docs/验收清单.md",
            "verification": "python tools/verify_release.py --locale zh-CN",
            "deadline": "2030-08-21", "limit": "926",
            "completed": "离线资源校验完成", "pending": "重连状态验收待办",
            "rejected": "启动时全量网络扫描", "next": "读取 docs/回滚步骤.md",
        },
    }[profile].copy()


def compaction_task_instructions(profile: str) -> str:
    return ("This is a synthetic memory exercise only. Record the described task; "
            "do not perform its actions, read files, browse, or use tools. Task description: ") + {
        "coding": "Resume the parser repair from its source and test file. Preserve the verified "
                  "fix, pending regression, rejected rewrite, and exact next file to read.",
        "research": "Resume a synthetic cohort replication. Preserve the dataset, seeded "
                    "verification command, completed audit, pending review, and rejected method.",
        "chinese_mix": "继续中英混合的发布验收任务。保留文件路径、验证命令、已完成事项、待办、"
                       "明确拒绝的方案和下一步操作，不要交换它们的状态。",
    }[profile]


def compaction_fact_coverage(text: str, facts: Mapping[str, str]) -> dict[str, bool]:
    """Substring diagnostics only; continuation acceptance also checks field associations."""
    def normalize(value: str) -> str:
        return " ".join(value.replace("`", "").casefold().split())

    normalized = normalize(text)
    return {key: normalize(value) in normalized for key, value in facts.items()}


def compaction_answer_fact_checks(text: str, facts: Mapping[str, str]) -> dict[str, bool]:
    """Require one JSON object with the twelve exact key/value associations."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_fact_field")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_pairs)
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text[match.start():])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and set(facts) <= candidate.keys():
            return {key: candidate[key] == value for key, value in facts.items()}
    return dict.fromkeys(facts, False)


def deployment_window_evidence(
    provider: str, model: str, api_key: str, endpoint: str, configured_window: int,
) -> dict[str, Any]:
    from opensquilla.provider.model_catalog import shared_catalog

    limits = shared_catalog().resolve_deployment_limits(
        model, provider=provider, api_key=api_key, base_url=endpoint,
    )
    known = getattr(limits, "context_window_known", False) is True
    return {
        "physical_window_known": known,
        "physical_context_window_tokens": limits.context_window if known else None,
        "configured_window_matches_physical": known and limits.context_window == configured_window,
    }


@dataclass(frozen=True)
class CompactionCaseOptions:
    layout: str = "suffix"
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    preflight_ratio: float | None = None
    task_profile: str | None = None
    native_pressure: bool = False

    def __post_init__(self) -> None:
        _require(self.layout in {"prefix", "suffix"}, "invalid_compaction_layout")
        for value in (self.context_window_tokens, self.max_output_tokens):
            _require(value is None or value > 0, "invalid_compaction_budget")
        _require(self.preflight_ratio is None or 0 < self.preflight_ratio <= 1,
                 "invalid_compaction_ratio")
        _require(self.task_profile is None or self.task_profile in COMPACTION_TASKS,
                 "invalid_task_profile")
        _require(not self.native_pressure or self.context_window_tokens is not None,
                 "native_pressure_requires_resolved_window")
        _require(not self.native_pressure or self.preflight_ratio in (None, 0.85),
                 "native_pressure_requires_production_threshold")


class ReplayCheckError(RuntimeError):
    """A fixed diagnostic code, never provider-controlled content."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReplayCheckError(code)


def _assert_turn_answer(content: object, *, scenario: str, turn: int) -> None:
    _require(isinstance(content, str), "final_reply_marker_missing")
    normalized = re.sub(r"[\W_]+", "", str(content)).casefold()
    stage = "first" if turn == 0 else "second"
    prefix = "replay" if scenario == "tools" else "chat"
    _require(f"{prefix}{stage}ok" in normalized, "final_reply_marker_missing")
    if scenario == "chat" and turn == 1:
        _require("amber17" in normalized, "chat_memory_recall_mismatch")


@dataclass
class WireCall:
    request: dict[str, Any]
    provider: str = "unknown"
    injected_fault: str | None = None
    started_at: float = field(default_factory=time.monotonic, repr=False)
    elapsed_ms: int | None = None
    response: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    status_code: int = 0
    retry_after_seconds: float | None = None
    limit_category: str | None = None
    raw_reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    native_reasoning_content: str | None = field(default=None, repr=False)
    response_fields: set[str] = field(default_factory=set)
    finish_reason: str | None = None
    body_format: str = "unknown"
    body_error_code: str | None = None
    error_field_mentions: set[str] = field(default_factory=set)
    error_kind_mentions: set[str] = field(default_factory=set)
    malformed_frames: int = 0
    content_encoding: str = "identity"
    encoded_response_bytes: int = 0
    decoded_response_bytes: int = 0

    def observe_error(self, error: dict[str, Any]) -> None:
        code = str(error.get("code", ""))
        self.body_error_code = (
            "litellm_error"
            if code.strip().upper() == "LITELLM_ERROR"
            else safe_provider_failure_code(code, None)
        )
        message = error.get("message")
        if not isinstance(message, str):
            return
        normalized = message[:16_000].lower()
        if any(word in normalized for word in ("quota", "insufficient credit", "余额", "配额")):
            self.limit_category = "quota"
        elif any(word in normalized for word in (
            "rate limit", "too many requests", "速率", "限流",
        )):
            self.limit_category = "rate_limit"
        for name in (
            "reasoning_content", "reasoning_details", "thinking", "enable_thinking",
            "tool_choice", "max_tokens", "tool_call_id",
        ):
            if re.search(rf"(?<![a-z_]){name}(?![a-z_])", normalized):
                self.error_field_mentions.add(name)
        for name in ("missing", "required", "unsupported", "invalid", "unavailable", "limit"):
            if re.search(rf"\b{name}\b", normalized):
                self.error_kind_mentions.add(name)
        for name, translated in (("permission", "权限"), ("model", "模型"), ("access", "访问")):
            if re.search(rf"\b{name}\b", normalized) or translated in normalized:
                self.error_kind_mentions.add(name)

    def consume(self, body: bytes) -> None:
        """Assemble independent wire evidence, without production replay helpers."""
        _require(len(body) <= 8_000_000, "decoded_response_observation_limit")
        self.decoded_response_bytes = len(body)
        self.native_reasoning_content = None
        details: list[dict[str, Any]] = []
        content = ""
        reasoning = ""
        reasoning_present = False
        details_present = False
        tool_calls: dict[int, dict[str, Any]] = {}
        decoded = body.decode("utf-8", errors="replace")
        if decoded.lstrip().startswith("{"):
            self.body_format = "json"
            try:
                payload = json.loads(decoded)
                error = payload.get("error")
                if isinstance(error, dict):
                    self.observe_error(error)
                elif (
                    isinstance(payload.get("message"), str)
                    and "code" in payload
                    and (
                        self.status_code >= 400
                        or payload["code"] == "LITELLM_ERROR"
                    )
                ):
                    # Some gateways return the error directly rather than
                    # inside error. Only fixed codes/mentions cross the report
                    # boundary; message and traceId are never retained.
                    self.observe_error(payload)
            except (ValueError, AttributeError):
                self.malformed_frames += 1
        for line in decoded.splitlines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            self.body_format = "sse"
            try:
                frame = json.loads(line[5:].strip())
            except ValueError:
                self.malformed_frames += 1
                continue
            error = frame.get("error")
            if isinstance(error, dict):
                self.observe_error(error)
            if isinstance(frame.get("usage"), dict):
                self.usage.update(frame["usage"])
            for choice in frame.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                self.response_fields.update(delta)
                reasoning_present |= any(
                    isinstance(delta.get(key), str) for key in ("reasoning_content", "reasoning")
                )
                details_present |= isinstance(delta.get("reasoning_details"), list)
                # Keep the exact field separate from aliases and readable
                # details. Only this value proves raw reasoning_content replay.
                raw_reasoning = delta.get("reasoning_content")
                if isinstance(raw_reasoning, str):
                    self.native_reasoning_content = (
                        self.native_reasoning_content or ""
                    ) + raw_reasoning
                content += delta.get("content") or ""
                reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
                details.extend(copy.deepcopy(delta.get("reasoning_details") or []))
                for tool in delta.get("tool_calls") or []:
                    target = tool_calls.setdefault(
                        tool.get("index", 0),
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if tool.get("id"):
                        target["id"] = tool["id"]
                    function = tool.get("function") or {}
                    for key in ("name", "arguments"):
                        target["function"][key] += function.get(key) or ""
                if choice.get("finish_reason") is not None:
                    self.completed = True
                    reason = choice["finish_reason"]
                    self.finish_reason = (
                        reason
                        if reason
                        in {
                            "stop",
                            "end_turn",
                            "length",
                            "tool_calls",
                            "function_call",
                            "content_filter",
                        }
                        else "other"
                    )
        self.response = {"role": "assistant", "content": content}
        if reasoning_present:
            self.response["reasoning_content"] = reasoning
        if details_present:
            self.raw_reasoning_details = details
            self.response["reasoning_details"] = _logical_details(details)
        if tool_calls:
            self.response["tool_calls"] = list(tool_calls.values())


def _logical_details(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct consecutive native text/summary deltas independently.

    OpenRouter SDK PR #520 documents why index=0 may repeat across types.
    Encrypted blocks are indivisible; original chunks stay in WireCall.
    """
    result: list[dict[str, Any]] = []
    for chunk in chunks:
        previous = result[-1] if result else None
        text_key = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(
            chunk.get("type", "")
        )
        compatible = previous is not None and previous.get("type") == chunk.get("type")
        if compatible:
            compatible = all(
                previous.get(key) in (None, "")
                or chunk.get(key) in (None, "")
                or previous[key] == chunk[key]
                for key in set(previous).intersection(chunk) - {"text", "summary"}
            )
        if text_key and compatible:
            assert previous is not None
            for key, value in chunk.items():
                if key == text_key:
                    previous[key] = previous.get(key, "") + value
                elif previous.get(key) in (None, ""):
                    previous[key] = copy.deepcopy(value)
        else:
            result.append(copy.deepcopy(chunk))
    return result


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, call: WireCall, encoding: str = ""):
        self.inner, self.call = inner, call
        self.encoding = encoding
        self.body_chunks: list[bytes] = []
        self.body_bytes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            self.body_bytes += len(chunk)
            _require(self.body_bytes <= 8_000_000, "response_observation_limit")
            self.body_chunks.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        # Providers may stop reading immediately at [DONE]. Their context
        # manager still closes the response, which is the observation commit.
        try:
            self.call.encoded_response_bytes = self.body_bytes
            # The wrapped stream precedes HTTPX's content decoder. Decode an
            # observation copy with the same public HTTPX response API; the
            # real response is still consumed unmodified by the provider.
            # Preserve the original chunk boundaries: after [DONE], a Brotli
            # stream can close before its compression trailer arrives.
            # Replaying that partial stream as one chunk can retain decoded
            # bytes even though incremental reads emitted them.
            observed = (
                httpx.Response(
                    200,
                    headers={"content-encoding": self.encoding},
                    content=self.body_chunks,
                ).read()
                if self.encoding
                else b"".join(self.body_chunks)
            )
            self.call.consume(observed)
        finally:
            self.call.elapsed_ms = round((time.monotonic() - self.call.started_at) * 1000)
            await self.inner.aclose()


class WireObserver:
    def __init__(
        self,
        endpoint: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_calls: int | None = None,
        endpoints: Mapping[str, str] | None = None,
        summary_fault: Callable[[], str | None] | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        # Keys are registry provider ids, never arbitrary request headers or credentials.
        self.endpoints = {
            str(provider): url.rstrip("/") for provider, url in (endpoints or {}).items()
        }
        self.endpoints.setdefault("unknown", self.endpoint)
        self.summary_fault = summary_fault
        self.transport = transport
        self.max_calls = max_calls
        self.calls: list[WireCall] = []
        self.engine_error_codes: list[str] = []
        self.replay_checks: dict[str, Any] = {}
        self.blocked_unobserved_generation_requests = 0
        self.blocked_http_requests: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def observe(self) -> Iterator[None]:
        original_send = httpx.AsyncClient.send

        async def send(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any):
            matched_provider = next((
                provider for provider, endpoint in self.endpoints.items()
                if request.url.copy_with(query=None) in {
                    httpx.URL(endpoint + "/chat/completions"),
                    httpx.URL(endpoint.rstrip("/") + "/v1/chat/completions")
                    if not httpx.URL(endpoint).path.rstrip("/") else None,
                }
            ), None)
            if matched_provider is not None and request.method == "POST":
                _require(
                    self.max_calls is None or len(self.calls) < self.max_calls,
                    "physical_model_call_limit",
                )
                call = WireCall(
                    request=json.loads(request.content), provider=matched_provider,
                )
                self.calls.append(call)
                fault = (
                    self.summary_fault()
                    if self.summary_fault is not None and _is_compaction_wire_call(call)
                    else None
                )
                if fault:
                    _require(fault in {"error", "empty", "length", "timeout"},
                             "invalid_summary_fault")
                    call.injected_fault = fault
                    if fault == "timeout":
                        call.elapsed_ms = round((time.monotonic() - call.started_at) * 1000)
                        raise httpx.ReadTimeout("Synthetic summary-only timeout", request=request)
                    if fault == "error":
                        response = httpx.Response(503, request=request, json={
                            "error": {"code": "503", "message": "Synthetic summary-only outage"},
                        })
                    else:
                        frame = {"choices": [{"index": 0, "delta": {"content": ""},
                                             "finish_reason": (
                                                 "length" if fault == "length" else "stop"
                                             )}]}
                        response = httpx.Response(
                            200, request=request,
                            headers={"content-type": "text/event-stream"},
                            content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode(),
                        )
                elif self.transport is None:
                    response = await original_send(
                        client, request, **{**kwargs, "follow_redirects": False}
                    )
                else:
                    response = await self.transport.handle_async_request(request)
                    response.request = request
                call.status_code = response.status_code
                retry_after = response.headers.get("retry-after", "")
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", retry_after):
                    call.retry_after_seconds = min(float(retry_after), 86400.0)
                if response.status_code == 429:
                    call.limit_category = "rate_or_quota_unspecified"
                encoding = response.headers.get("content-encoding", "")
                encodings = [part.strip().lower() for part in encoding.split(",") if part.strip()]
                call.content_encoding = (
                    ",".join(encodings)
                    if encodings
                    and all(
                        item in {"identity", "gzip", "deflate", "br", "zstd"} for item in encodings
                    )
                    else "other"
                    if encodings
                    else "identity"
                )
                if response.is_stream_consumed:
                    # Error responses and custom transports may already have
                    # read and decoded their body before send() returns.
                    call.consume(response.content)
                    call.elapsed_ms = round((time.monotonic() - call.started_at) * 1000)
                else:
                    response.stream = _ObservedStream(response.stream, call, encoding)
                return response
            if self.transport is not None:
                raise ReplayCheckError("unexpected_offline_http_request")
            catalog_urls = {endpoint + "/models" for endpoint in self.endpoints.values()}
            catalog_urls.update(endpoint + "/v1/models" for endpoint in self.endpoints.values()
                                if not httpx.URL(endpoint).path.rstrip("/"))
            if "tokenrhythm" in self.endpoints:
                catalog_urls.add("https://tokenrhythm.studio/api/models")
            if request.method == "GET" and str(request.url) in catalog_urls:
                return await original_send(
                    client, request, **{**kwargs, "follow_redirects": False}
                )
            if len(self.blocked_http_requests) < 30:
                self.blocked_http_requests.append({
                    "method": request.method, "scheme": request.url.scheme,
                    "host": request.url.host, "path": request.url.path[:200],
                    "generation_path": request.url.path.endswith(
                        ("/chat/completions", "/responses", "/messages", ":generateContent")
                    ),
                })
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                self.blocked_unobserved_generation_requests += 1
                raise ReplayCheckError("unobserved_generation_request_blocked")
            raise ReplayCheckError("http_endpoint_not_allowlisted")

        with patch.object(httpx.AsyncClient, "send", send):
            yield


class _Catalog:
    """Fixed capacity isolates this test from unrelated catalog network refresh."""

    def __init__(self) -> None:
        self._catalog = ModelCatalog()

    def resolve_max_tokens(self, model_id: str, *, user_override: int = 0, **_: Any) -> int:
        return user_override or 4096

    def resolve_context_window(self, model_id: str, **_: Any) -> int:
        return 128_000

    def get_capabilities(self, model_id: str, **kwargs: Any) -> ModelCapabilities:
        return self._catalog.get_capabilities(model_id, **kwargs)

    def resolve_vision_support(self, model_id: str, **_: Any) -> str:
        return "unsupported"

    def resolve_deployment_vision_support(self, model_id: str, **_: Any) -> str:
        return "unsupported"


def _config(
    root: Path, provider: str, model: str, endpoint: str, *, thinking: str = "low"
) -> GatewayConfig:
    config = GatewayConfig()
    config.state_dir = str(root / "state")
    config.workspace_dir = str(root / "workspace")
    config.attachments.media_root = str(root / "media")
    Path(config.workspace_dir).mkdir(parents=True, exist_ok=True)
    config.llm.provider, config.llm.model, config.llm.base_url = provider, model, endpoint
    config.llm.max_tokens = 4096
    config.llm.context_window_tokens = 128_000
    config.llm.thinking = None if thinking == "default" else thinking
    config.tools.profile = "full"
    config.tools.allow = ["replay_step"]
    config.squilla_router.enabled = False
    config.naming.enabled = False
    config.compaction.enabled = False
    config.memory.retrieval_mode = "fts_only"
    config.memory.auto_capture_enabled = False
    config.memory.capture_mode = "off"
    config.memory.dream.enabled = False
    config.meta_skill.enabled = False
    config.heartbeat.enabled = False
    config.agent_max_iterations = 5
    config.agent_max_provider_retries = 0
    config.agent_runtime_timeout_seconds = 180
    config.agent_request_timeout_seconds = 90
    return config


def _native_fields(message: dict[str, Any]) -> list[str]:
    return [
        key
        for key in ("reasoning_content", "reasoning_details")
        if key in message and not (key == "reasoning_content" and message.get("reasoning_details"))
    ]


def _assert_tool_results(calls: list[WireCall]) -> int:
    """Check synthetic results independently of production message converters."""
    comparisons = 0
    expected: list[tuple[str, int]] = []
    for call in calls:
        results = [message for message in call.request["messages"] if message.get("role") == "tool"]
        _require(len(results) == len(expected), "tool_result_count_mismatch")
        for result, (tool_id, next_value) in zip(results, expected, strict=True):
            _require(result.get("tool_call_id") == tool_id, "tool_result_id_or_order_mismatch")
            try:
                body = json.loads(result.get("content", ""))
            except (TypeError, ValueError):
                raise ReplayCheckError("tool_result_content_mismatch") from None
            _require(body == {"next_value": next_value}, "tool_result_content_mismatch")
            comparisons += 1
        for tool in call.response.get("tool_calls", []):
            function = tool.get("function") or {}
            _require(function.get("name") == "replay_step", "unexpected_tool_name")
            try:
                arguments = json.loads(function.get("arguments", ""))
                value = arguments["value"]
            except (TypeError, ValueError, KeyError):
                raise ReplayCheckError("unexpected_tool_arguments") from None
            _require(type(value) is int, "unexpected_tool_arguments")
            expected.append((tool["id"], value + 11))
    return comparisons


def _assert_wire_replay(
    calls: list[WireCall],
    restart_index: int,
    provider: str,
    *,
    model: str | None = None,
    scenario: str = "tools",
    require_native_replay: bool | None = None,
) -> dict[str, Any]:
    minimum_calls, minimum_restart = (5, 3) if scenario == "tools" else (2, 1)
    _require(
        len(calls) >= minimum_calls and restart_index >= minimum_restart,
        "insufficient_model_calls",
    )
    model = model or DEFAULT_MODELS[provider]
    v4_tool_scope = (
        provider == "tokenrhythm"
        and model.removeprefix("tokenrhythm/") in TOKENRHYTHM_TOOL_REASONING_MODELS
    )
    require_all = (
        provider != "tokenrhythm" if require_native_replay is None else require_native_replay
    )
    comparisons = 0
    post_restart = 0
    final_replayed = False
    opportunities = 0
    omissions: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        _require(call.completed, "incomplete_provider_response")
        _require(bool(call.request.get("tools")) is (scenario == "tools"), "tools_request_mismatch")
        assistants = [msg for msg in call.request["messages"] if msg.get("role") == "assistant"]
        _require(len(assistants) == index, "assistant_call_boundary_missing_or_duplicated")
        for previous_index, (previous, replayed) in enumerate(
            zip(calls[:index], assistants, strict=True)
        ):
            native = previous.response
            ids = [tool["id"] for tool in native.get("tool_calls", [])]
            if ids:
                _require(
                    [tool["id"] for tool in replayed.get("tool_calls", [])] == ids,
                    "assistant_call_boundary_missing_or_duplicated",
                )
            else:
                _require(
                    replayed.get("content") == native.get("content"),
                    "assistant_call_boundary_missing_or_duplicated",
                )
            for key in _native_fields(native):
                opportunities += 1
                expected_native = (
                    previous.native_reasoning_content
                    if key == "reasoning_content"
                    and previous.native_reasoning_content is not None
                    else native[key]
                )
                # The documented V4 route withholds ordinary assistant
                # reasoning and any tool reasoning beyond 50k UTF-16 units.
                # Its explicit empty placeholder is an omission, not replay.
                scoped_omission = bool(
                    v4_tool_scope
                    and key == "reasoning_content"
                    and (
                        not ids
                        or len(expected_native.encode("utf-16-le")) // 2 > 50_000
                    )
                )
                exact = key in replayed and replayed[key] == expected_native
                if exact:
                    comparisons += 1
                    if index >= restart_index:
                        post_restart += 1
                        final_replayed |= not ids and previous_index < restart_index
                    continue
                present = key in replayed
                empty_placeholder = scoped_omission and replayed.get(key) == ""
                _require(not present or empty_placeholder, "native_state_wire_mismatch")
                required = bool(
                    not scoped_omission
                    and (require_all or (v4_tool_scope and key == "reasoning_content" and ids))
                )
                omissions.append(
                    {
                        "request_index": index,
                        "response_index": previous_index,
                        "field": key,
                        "required": required,
                        "documented_projection": scoped_omission,
                        "empty_placeholder": empty_placeholder,
                    }
                )
                _require(not required, "native_state_wire_mismatch")
    old_final_returned_reasoning = any(
        not call.response.get("tool_calls") and bool(_native_fields(call.response))
        for call in calls[:restart_index]
    )
    returned = sum(len(_native_fields(call.response)) for call in calls)
    if not returned:
        coverage = "not_returned"
    elif not opportunities:
        coverage = "no_replay_opportunity"
    else:
        coverage = "partial" if omissions else "complete"
    return {
        "native_state_comparisons": comparisons,
        "post_restart_comparisons": post_restart,
        "completed_old_round_reasoning_replayed": final_replayed,
        "completed_old_round_reasoning_returned": old_final_returned_reasoning,
        "native_wire_coverage": coverage,
        "native_state": {
            "returned": returned,
            "replay_opportunities": opportunities,
            "wire_present": comparisons,
            "omitted": len(omissions),
            "omissions": omissions,
        },
    }


def _assert_persisted_replay(saved: list[dict[str, Any]], calls: list[WireCall]) -> int:
    assistants = [
        message
        for entry in saved
        for message in entry["messages"]
        if message["role"] == "assistant"
    ]
    _require(len(assistants) == len(calls), "persisted_assistant_call_count_mismatch")
    persisted = 0
    for message, call in zip(assistants, calls, strict=True):
        state = message.get("provider_replay") or {}
        _require(
            bool(state.get("source")) and bool(state.get("protocol")),
            "persisted_native_origin_missing",
        )
        native = call.response
        _require(
            state.get("native_reasoning_content") == call.native_reasoning_content,
            "persisted_native_reasoning_content_mismatch",
        )
        if "reasoning_content" in native and not native.get("reasoning_details"):
            _require(
                message.get("reasoning_content") == native["reasoning_content"],
                "persisted_reasoning_mismatch",
            )
        if "reasoning_details" in native:
            _require(
                state.get("reasoning_details") == native["reasoning_details"],
                "persisted_native_details_mismatch",
            )
        persisted += len(_native_fields(native))
    return persisted


def _usage_report(calls: list[WireCall]) -> dict[str, Any]:
    def numeric(value: Any) -> float:
        return float(value) if isinstance(value, (float, int)) else 0.0

    costs = [numeric(call.usage["cost"]) for call in calls if "cost" in call.usage]

    def usage_detail(call: WireCall, detail: str, name: str, legacy: str = "") -> int | None:
        details = call.usage.get(detail)
        value = details.get(name) if isinstance(details, dict) else None
        if value is None and legacy:
            value = call.usage.get(legacy)
        return int(value) if type(value) in (int, float) else None

    cached = [
        usage_detail(call, "prompt_tokens_details", "cached_tokens", "prompt_cache_hit_tokens")
        for call in calls
    ]
    reasoning = [
        usage_detail(call, "completion_tokens_details", "reasoning_tokens") for call in calls
    ]
    return {
        "model_calls": len(calls),
        "input_tokens": sum(int(numeric(call.usage.get("prompt_tokens"))) for call in calls),
        "output_tokens": sum(int(numeric(call.usage.get("completion_tokens"))) for call in calls),
        "provider_reported_cost_usd": sum(costs) if costs else None,
        "cost_reported": bool(costs),
        "cached_input_tokens_by_call": cached,
        "reasoning_tokens_by_call": reasoning,
        "cached_input_tokens": sum(value for value in cached if value is not None)
        if any(value is not None for value in cached) else None,
        "reasoning_tokens": sum(value for value in reasoning if value is not None)
        if any(value is not None for value in reasoning) else None,
        "last_http_status": calls[-1].status_code if calls else None,
    }


def _wire_diagnostics(observer: WireObserver) -> dict[str, Any]:
    thinking_keys = (
        "enable_thinking", "preserve_thinking", "thinking_budget", "thinking_budget_tokens",
        "reasoning_effort", "thinking", "reasoning",
    )
    public_fields = {
        "role",
        "content",
        "tool_calls",
        "function_call",
        "refusal",
        "reasoning_content",
        "reasoning",
        "reasoning_details",
    }

    def shape(message: dict[str, Any]) -> dict[str, Any]:
        details = message.get("reasoning_details")
        return {
            "fields": sorted(public_fields.intersection(message)),
            "reasoning_present": "reasoning_content" in message,
            "reasoning_chars": len(message.get("reasoning_content") or ""),
            "details_present": "reasoning_details" in message,
            "detail_blocks": len(details) if isinstance(details, list) else 0,
            "detail_chars": len(json.dumps(details, ensure_ascii=False)) if details else 0,
            "content_chars": len(json.dumps(message.get("content"), ensure_ascii=False)),
            "tool_calls": len(message.get("tool_calls") or []),
        }

    def thinking_controls(request: dict[str, Any]) -> dict[str, Any]:
        # Only fixed request-control keys and validated scalar values are public.
        result: dict[str, Any] = {}
        for key in ("enable_thinking", "preserve_thinking"):
            if isinstance(request.get(key), bool):
                result[key] = request[key]
        for key in ("thinking_budget", "thinking_budget_tokens"):
            if type(request.get(key)) is int:
                result[key] = request[key]
        allowed = {
            "enabled", "disabled", "none", "off", "minimal", "low", "medium", "high",
            "xhigh", "adaptive",
        }
        if (
            isinstance(request.get("reasoning_effort"), str)
            and request["reasoning_effort"] in allowed
        ):
            result["reasoning_effort"] = request["reasoning_effort"]
        for key in ("thinking", "reasoning"):
            raw = request.get(key)
            if not isinstance(raw, dict):
                continue
            controls = {}
            for name in ("type", "effort"):
                if isinstance(raw.get(name), str) and raw[name] in allowed:
                    controls[name] = raw[name]
            for name in ("budget_tokens", "max_tokens"):
                if type(raw.get(name)) is int:
                    controls[name] = raw[name]
            for name in ("enabled", "exclude"):
                if isinstance(raw.get(name), bool):
                    controls[name] = raw[name]
            result[key] = controls
        return result

    return {
        "engine_error_codes": list(observer.engine_error_codes),
        "transport_kind": "mock" if observer.transport is not None else "real",
        "blocked_unobserved_generation_requests": observer.blocked_unobserved_generation_requests,
        "blocked_http_requests": list(observer.blocked_http_requests),
        "request_models_by_call": [
            model if isinstance(model := call.request.get("model"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model)
            else "invalid"
            for call in observer.calls
        ],
        **observer.replay_checks,
        "calls": [
            {
                **wire_pressure_evidence(call),
                "http_status": call.status_code,
                "retry_after_seconds": call.retry_after_seconds,
                "limit_category": call.limit_category,
                "content_encoding": call.content_encoding,
                "encoded_response_bytes": call.encoded_response_bytes,
                "decoded_response_bytes": call.decoded_response_bytes,
                "body_format": call.body_format,
                "body_error_code": call.body_error_code,
                "error_field_mentions": sorted(call.error_field_mentions),
                "error_kind_mentions": sorted(call.error_kind_mentions),
                "malformed_frames": call.malformed_frames,
                "finish_reason": call.finish_reason,
                "completed": call.completed,
                "request_thinking_controls": thinking_controls(call.request),
                "request_thinking_control_fields": [
                    key for key in thinking_keys if key in call.request
                ],
                "request_thinking_controls_omitted": not any(
                    key in call.request for key in thinking_keys
                ),
                "raw_reasoning_content_present": call.native_reasoning_content is not None,
                "raw_reasoning_content_chars": len(call.native_reasoning_content or ""),
                "response_fields": sorted(public_fields.intersection(call.response_fields)),
                "response_unknown_field_count": len(call.response_fields - public_fields),
                "response": shape(call.response),
                "request_assistants": [
                    shape(message)
                    for message in call.request.get("messages", [])
                    if message.get("role") == "assistant"
                ],
            }
            for call in observer.calls
        ],
    }


def _compaction_generated_label(content: str) -> str | None:
    # The memory check needs a new, unambiguous fact, not exact compliance
    # with the fixture's preferred label length or formatting.
    match = re.search(r"\bCOMPACTION_LABEL\s*[:=]\s*[`\"']?([A-Za-z0-9_-]{4,64})\b", content)
    return match.group(1) if match else None


def _canonical_message_digests(entries: list[Any]) -> dict[str, str]:
    """Compare archived bodies, not only identities; never publish synthetic content."""
    fields = (
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "reasoning_content",
        "assistant_replay",
    )
    return {
        entry.message_id: hashlib.sha256(
            json.dumps(
                {name: getattr(entry, name, None) for name in fields},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for entry in entries
    }


def _is_compaction_wire_call(call: WireCall) -> bool:
    messages = call.request.get("messages", [])
    suffix = bool(
        messages and messages[-1].get("role") == "user"
        and "Summarize the preceding conversation into a portable checkpoint."
        in str(messages[-1].get("content", ""))
    )
    prefix = any(
        message.get("role") in {"system", "developer"}
        and "You are a conversation compactor." in str(message.get("content", ""))
        for message in messages
    )
    return suffix or prefix


def wire_pressure_evidence(call: WireCall) -> dict[str, Any]:
    """Report a physical call, never confuse ensemble billing totals with pressure."""
    from opensquilla.provider.request_proof import project_provider_payload

    payload = json.dumps(call.request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Use the same media-aware estimate as final request admission. Base64
    # transport bytes are neither text context nor provider-reported usage.
    proof = project_provider_payload(
        call.request, projection_adapter="live_wire_observer", proof_budget=0,
    )
    return {
        "provider": call.provider if re.fullmatch(r"[a-z0-9_-]{1,50}", call.provider)
        else "unknown",
        "summary_request": _is_compaction_wire_call(call),
        "injected_fault": call.injected_fault,
        "request_payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "request_chars": len(payload),
        "request_estimated_tokens": proof["estimated_tokens"],
        "request_estimate_source": str(proof["token_estimate_source"]),
        "physical_prompt_tokens": call.usage.get("prompt_tokens")
        if type(call.usage.get("prompt_tokens")) is int else None,
        "physical_completion_tokens": call.usage.get("completion_tokens")
        if type(call.usage.get("completion_tokens")) is int else None,
        "generation_budget": next((call.request[key] for key in
                                   ("max_completion_tokens", "max_tokens", "max_output_tokens")
                                   if type(call.request.get(key)) is int), None),
        "wire_message_count": len(call.request.get("messages") or []),
        "tool_count": len(call.request.get("tools") or []),
        "tools_sha256": hashlib.sha256(json.dumps(
            call.request.get("tools"), ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest(),
        "elapsed_ms": call.elapsed_ms,
    }


def _compaction_tool_history_representation(call: WireCall) -> str:
    """Validate native or quoted pairs without conflating their wire representations."""
    pending: dict[str, int] = {}
    matched = 0
    recorded_tools = False
    messages = []
    for message in call.request.get("messages", []):
        content = message.get("content")
        if isinstance(content, str) and content.startswith("Recorded conversation context:"):
            try:
                records = json.loads(content.split("\n", 1)[1])
            except (IndexError, TypeError, ValueError):
                return "invalid"
            for record in records:
                blocks = record.get("content")
                for block in blocks if isinstance(blocks, list) else []:
                    if block.get("type") == "tool_use":
                        recorded_tools = True
                        messages.append(
                            {
                                "tool_calls": [
                                    {
                                        "id": block.get("id"),
                                        "function": {
                                            "name": block.get("name"),
                                            "arguments": json.dumps(block.get("input")),
                                        },
                                    }
                                ]
                            }
                        )
                    elif block.get("type") == "tool_result":
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
                                "content": block.get("content"),
                            }
                        )
        else:
            messages.append(message)
    for message in messages:
        for tool in message.get("tool_calls") or []:
            function = tool.get("function") or {}
            if function.get("name") != "replay_step":
                return "invalid"
            try:
                value = json.loads(function.get("arguments", ""))["value"]
                pending[tool["id"]] = value + 11
            except (KeyError, TypeError, ValueError):
                return "invalid"
        if message.get("role") == "tool":
            expected = pending.pop(message.get("tool_call_id"), None)
            try:
                actual = json.loads(message.get("content", ""))
            except (TypeError, ValueError):
                return "invalid"
            if expected is None or actual != {"next_value": expected}:
                return "invalid"
            matched += 1
    if matched != 1 or pending:
        return "invalid"
    return "recorded" if recorded_tools else "native"


def _compaction_coverage(variant: str) -> dict[str, Any]:
    expected = [
        "source_range",
        "current_tail",
        "archive_integrity",
        "summary_replayed",
        "memory_recall",
        "current_configuration",
    ]
    if variant in {"tools", "replay_off"}:
        expected.extend(("tool_roundtrip", "tool_schema"))
    if variant == "replay_off":
        expected.extend(("native_parent_state", "replay_disabled"))
    if variant == "model_switch":
        expected.append("model_switch")
    if variant == "chunked":
        expected.extend(("chunked_summary", "single_preflight"))
    if variant == "repeated":
        expected.extend(("repeated_compaction", "cumulative_memory"))
    if variant == "long_reasoning":
        expected.append("reasoning_over_1024")
    if variant == "truncated":
        expected = ["length_observed", "source_preserved", "no_summary_committed"]
    return {
        "expected": expected,
        "observed": dict.fromkeys(expected, False),
        "status": "not_covered",
    }


async def _run_compaction_case(
    root: Path,
    *,
    provider: str,
    model: str,
    api_key: str,
    observer: WireObserver | None,
    thinking: str,
    variant: str = "basic",
    next_model: str | None = None,
    options: CompactionCaseOptions | None = None,
    comparison_snapshot_out: Path | None = None,
) -> dict[str, Any]:
    """Exercise real automatic preflight, bounded wire calls and reopened SQLite."""
    _require(variant in COMPACTION_VARIANTS, "invalid_compaction_variant")
    options = options or CompactionCaseOptions()
    if variant == "model_switch":
        _require(bool(next_model) and next_model != model, "distinct_next_model_required")
    endpoint = registry_endpoint(provider)
    limit = COMPACTION_CALL_LIMITS[variant] + int(options.native_pressure)
    observer = observer or WireObserver(endpoint, max_calls=limit)
    observer.max_calls = min(observer.max_calls or limit, limit)
    coverage = _compaction_coverage(variant)
    observed = coverage["observed"]
    if options.native_pressure:
        coverage["expected"].append("native_pressure")
        observed["native_pressure"] = False
    observer.replay_checks["coverage"] = coverage
    config = _config(root, provider, model, endpoint, thinking=thinking)
    if options.context_window_tokens is not None:
        config.llm.context_window_tokens = options.context_window_tokens
    if options.max_output_tokens is not None:
        config.llm.max_tokens = options.max_output_tokens
    if options.preflight_ratio is not None:
        config.preflight_compact_ratio = options.preflight_ratio
    facts = compaction_task_facts(options.task_profile) if options.task_profile else {}
    physical_window = deployment_window_evidence(
        provider, model, api_key, endpoint, config.llm.context_window_tokens,
    )
    fact_checks: list[dict[str, Any]] = []
    recall_checks: list[dict[str, Any]] = []
    pressure_checks: list[dict[str, Any]] = []
    observer.replay_checks["critical_fact_checks"] = fact_checks
    observer.replay_checks["memory_recall_checks"] = recall_checks
    observer.replay_checks["pressure_checks"] = pressure_checks
    catalog = _Catalog()
    catalog_loaded = False
    if provider == "openrouter" and observer.transport is None:
        try:
            await catalog._catalog.fetch_openrouter(api_key, endpoint.removesuffix("/v1"))
        except Exception:
            raise ReplayCheckError("catalog_setup_failed") from None
        catalog_loaded = True
    tools_enabled = variant in {"tools", "replay_off"}
    config.agent_max_iterations = 2 if tools_enabled else 1
    registry = ToolRegistry()
    tool_values: list[int] = []

    async def step(value: int) -> str:
        tool_values.append(value)
        return json.dumps({"next_value": value + 11})

    if tools_enabled:
        registry.register(
            ToolSpec(
                name="replay_step",
                description="Pure synthetic test: return value plus eleven.",
                parameters={"value": {"type": "integer"}},
                required=["value"],
            ),
            step,
        )
    key = "agent:main:synthetic-suffix-compaction"
    db = root / "sessions.sqlite"
    labels: list[str] = []
    original: dict[str, str] = {}
    previous_canonical: dict[str, str] = {}
    summary_indexes: list[int] = []
    prefix_counts: list[int] = []
    message_json_prefix_chars: list[int] = []
    source_entry_marker_counts: list[int] = []
    _, token_estimate_source = estimate_tokens_with_source(COMPACTION_PADDING)
    conservative_estimate = token_estimate_source == "utf8_unicode_conservative"
    # Match the ordinary fixture's estimated source pressure when the optional
    # tokenizer is unavailable (this prose estimates about 86 rather than 30
    # tokens per repetition). Physical request limits remain unchanged.
    seed_repetitions = 16 if conservative_estimate else 45
    seed_padding = " log 17;" if options.native_pressure else COMPACTION_PADDING
    if variant == "chunked":
        # Exceed the normal preflight threshold and one summary chunk, without
        # letting long prose exhaust the independent character budget first.
        seed_padding = " log 17;"
        seed_repetitions = max(1, 14_000 // (5 * estimate_tokens_with_source(seed_padding)[0]))
    if options.native_pressure:
        from opensquilla.context_budget import ContextBudgetGovernor
        from opensquilla.token_estimation import estimate_tokens

        capacity = ContextBudgetGovernor.from_values(
            context_window_tokens=config.llm.context_window_tokens,
            max_output_tokens=config.llm.max_tokens,
            thinking_budget_tokens=0,
            context_overflow_threshold=config.preflight_compact_ratio,
        ).snapshot().usable_tokens
        seed_repetitions = max(1, int(capacity * 0.89 / (5 * estimate_tokens(seed_padding))))
    first_prompt = COMPACTION_FIRST_PROMPT
    if facts:
        first_prompt = (
            "Continue the same synthetic task. All twelve previously recorded task facts and "
            "their completed/pending/rejected/next states remain active and must be preserved. "
            "Invent eight random lowercase ASCII letters, not a real word or any substring "
            "already present in the input. Add this label as an additional identifier; "
            "it does not replace, supersede, or cancel any earlier fact. For this reply only, "
            "return COMPACTION_LABEL=<your label>. Keep both the task state and the new label "
            "for subsequent turns. Do not execute the described task or use tools."
        )
    if tools_enabled:
        first_prompt = (
            "This is a synthetic memory and tool test. Call replay_step exactly once with "
            "value=7. Wait for its result, then invent a new label of exactly eight lowercase "
            "letters and reply only COMPACTION_LABEL=<your label>. Do not invent tool results."
        )
    if variant == "long_reasoning":
        # A real workload, never fabricated response reasoning: the summary must
        # reconcile a sequence of ledger edits while keeping its visible answer short.
        ledger = "\n".join(
            f"Ledger event {index}: add {index * 17 + 3}, subtract {index * 11 + 5}; "
            f"record remainder modulo {index + 41} before the next event."
            for index in range(1, 41)
        )
        first_prompt += (
            "\nFor the eventual compact checkpoint, carefully reconcile every ledger event "
            "from balance 271, checking the running balance and remainder after each event. "
            "Do not calculate it in this initial answer. When later asked to summarize, "
            "perform those checks internally; the visible checkpoint must retain the generated "
            "label and report only the final balance and remainder in under 120 words. "
            "Do not copy the ledger, intermediate steps, or background prose into the summary.\n"
            + ledger
        )
    first_prompt = "SYNTHETIC_COMPACTION_ENTRY_5_USER\n" + first_prompt
    # Keep one whole current turn large enough to occupy the recent-tail target;
    # the preceding generated fact/tool round must genuinely enter the summary.
    # Conservative estimation also charges more for the ledger and fixed
    # instructions. Keep this protocol fixture's protected request sendable;
    # native-pressure fixtures retain their original workload.
    long_tail = variant == "long_reasoning" and (
        not conservative_estimate or options.native_pressure
    )
    tail_padding = " log 17;" * (1800 if long_tail else 1000)
    tail_prompt = (
        "SYNTHETIC_COMPACTION_ENTRY_6_USER\n"
        f"{COMPACTION_TAIL_MARKER}\n{tail_padding}\n"
        "The background prose above is disposable. Return the exact label you invented "
        "in your previous answer, followed by COMPACTION_RECALL_OK. Do not invent a new "
        "label and do not use tools."
    )
    fact_recall_instruction = ""
    if facts:
        fact_recall_instruction = (
            " After the recalled label and marker, return a JSON object with these exact keys: "
            + ", ".join(facts)
            + ". Return every value as a JSON string, including numeric-looking values. "
            "Recover each original value from the recorded task. "
            "Keep completed, pending, rejected and next-action states distinct."
        )
        tail_prompt += fact_recall_instruction
    prompts = [first_prompt, tail_prompt]
    if variant == "repeated":
        prompts[1] = tail_prompt.replace("Do not invent a new label and", "Do not") + (
            " After recalling that first label, invent a distinct second label and append "
            "COMPACTION_LABEL_2=<new eight lowercase letters>. Retain both labels."
        )
        prompts.append(
            "SYNTHETIC_COMPACTION_ENTRY_7_USER\n"
            f"{COMPACTION_TAIL_MARKER}_SECOND\n{tail_padding}\n"
            "Return both labels you invented, in order, followed by COMPACTION_RECALL_OK. "
            "Then invent a distinct third label and append "
            "COMPACTION_LABEL_3=<new eight lowercase letters>. Retain all three labels. "
            "Do not use tools."
            + fact_recall_instruction
        )
        prompts.append(
            "SYNTHETIC_COMPACTION_ENTRY_8_USER\n"
            f"{COMPACTION_TAIL_MARKER}_THIRD\n{tail_padding}\n"
            "Return all three labels you invented, in order, followed by COMPACTION_RECALL_OK. "
            "Do not invent new labels and do not use tools."
            + fact_recall_instruction
        )
    compaction_events: list[dict[str, Any]] = []
    attempted_by_turn: list[bool] = []
    observer.replay_checks["compaction_events"] = compaction_events
    observer.replay_checks["compaction_attempted_by_turn"] = attempted_by_turn

    def observe_compaction_event(session_key: str, payload: dict[str, Any]) -> None:
        if session_key != key:
            return
        event: dict[str, Any] = {}
        for name in ("status", "source", "phase", "reason", "effect_status", "skip_reason"):
            value = payload.get(name)
            if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,80}", value):
                event[name] = value
        for name in ("removed_count", "kept_count", "tokens_before", "tokens_after"):
            if type(payload.get(name)) is int:
                event[name] = payload[name]
        compaction_events.append(event)

    with contextlib.ExitStack() as stack:
        stack.enter_context(observer.observe())
        stack.enter_context(patch.dict(
            os.environ, {"OPENSQUILLA_COMPACTION_PROMPT_LAYOUT": options.layout}
        ))
        stack.callback(add_compaction_listener(observe_compaction_event))
        for turn, prompt in enumerate(prompts):
            storage = SessionStorage(str(db))
            await storage.connect()
            try:
                manager = SessionManager(
                    storage,
                    inject_time_prefix=False,
                    checkpoint_workspace_dir=config.workspace_dir,
                )
                if turn == 0:
                    await manager.create(session_key=key, agent_id="main")
                    for index in range(5):
                        await manager.append_message(
                            key,
                            "user",
                            f"SYNTHETIC_COMPACTION_ENTRY_{index}_USER\n"
                            f"{COMPACTION_SOURCE_MARKER if index == 0 else 'Background'}\n"
                            f"{seed_padding * seed_repetitions}"
                            + (
                                "\n" + compaction_task_instructions(
                                    options.task_profile or "coding"
                                )
                                + "\nTask facts (preserve exact field/value associations):\n"
                                + json.dumps(facts, ensure_ascii=False)
                                if index == 0 and facts else ""
                            ),
                        )
                        await manager.append_message(
                            key, "assistant",
                            f"SYNTHETIC_COMPACTION_ENTRY_{index}_ASSISTANT "
                            "Background acknowledged.",
                        )
                else:
                    restored = _canonical_message_digests(
                        await manager.get_canonical_transcript(key)
                    )
                    _require(restored == previous_canonical, "database_reopen_state_mismatch")
                    config.compaction.enabled = True
                    config.llm.context_window_tokens = options.context_window_tokens or 20_000
                    if (
                        options.preflight_ratio is None
                        and not options.native_pressure
                        and variant != "chunked"
                    ):
                        # Small protocol cases exercise a deliberate early trigger.
                        # Native-pressure cases retain the production threshold.
                        config.preflight_compact_ratio = 0.2 if conservative_estimate else 0.1
                    if variant == "model_switch":
                        config.llm.model = next_model
                    if variant == "truncated":
                        config.llm.max_tokens = 1
                    if variant == "long_reasoning":
                        config.llm.max_tokens = options.max_output_tokens or 8192
                        config.llm.thinking = "high"
                        config.llm.context_window_tokens = options.context_window_tokens or 32_000
                        if not options.native_pressure:
                            config.preflight_compact_ratio = 0.1
                    if variant in {"repeated", "tools", "replay_off"} and (
                        options.preflight_ratio is None and not options.native_pressure
                    ):
                        config.preflight_compact_ratio = 0.2
                selector_config = SelectorConfig(
                    primary=ProviderConfig(
                        provider=provider,
                        model=config.llm.model,
                        api_key=api_key,
                        base_url=endpoint,
                        replay_provider_state=not (variant == "replay_off" and turn > 0),
                    )
                )
                runner = TurnRunner(
                    provider_selector=ModelSelector(selector_config),
                    tool_registry=registry,
                    session_manager=manager,
                    config=config,
                    model_catalog=catalog,
                )
                before_entries = await manager.get_transcript(key)
                before_active = _canonical_message_digests(before_entries)
                source_entry_markers = {
                    marker for entry in before_entries
                    for marker in re.findall(COMPACTION_ENTRY_PATTERN, entry.content or "")
                }
                call_start = len(observer.calls)
                user = await manager.append_message(key, "user", prompt)
                compaction_event_start = len(compaction_events)
                events = [
                    event
                    async for event in runner.run(
                        prompt,
                        session_key=key,
                        bound_user_message_id=user.message_id,
                        tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
                    )
                ]
                # run() clears its per-turn flags in finally. Retain the actual
                # lifecycle evidence instead of sampling those cleared flags.
                started_phases = [
                    event.get("phase") for event in compaction_events[compaction_event_start:]
                    if event.get("status") == "started"
                ]
                attempted_by_turn.append(bool(started_phases))
                observer.engine_error_codes.extend(
                    safe_provider_failure_code(
                        getattr(event, "code", None), getattr(event, "failure_kind", None)
                    )
                    for event in events
                    if event.kind == "error"
                )
                if variant != "truncated" or turn == 0:
                    _require(not observer.engine_error_codes, "turn_failed")
                canonical = _canonical_message_digests(await manager.get_canonical_transcript(key))
                _require(
                    all(canonical.get(mid) == digest for mid, digest in previous_canonical.items()),
                    "compaction_archive_changed_history",
                )
                previous_canonical = canonical
                if turn == 0:
                    _require(
                        len(observer.calls) == (2 if tools_enabled else 1),
                        "unexpected_parent_call_count",
                    )
                    _require(
                        all(call.request.get("model") == model for call in observer.calls),
                        "parent_request_model_mismatch",
                    )
                    parent = observer.calls[-1]
                    generated_label = _compaction_generated_label(
                        parent.response.get("content") or ""
                    )
                    _require(generated_label is not None, "parent_generated_label_missing")
                    assert generated_label is not None
                    labels.append(generated_label)
                    first_turn_entries = await manager.get_transcript(key)
                    _require(
                        any(
                            entry.role == "assistant"
                            and generated_label in str(entry.content or "")
                            for entry in first_turn_entries
                        ),
                        "parent_generated_label_not_persisted",
                    )
                    _require(
                        all(
                            generated_label not in json.dumps(call.request)
                            for call in observer.calls
                        ),
                        "parent_label_already_in_input",
                    )
                    original = canonical
                    if comparison_snapshot_out is not None:
                        from scripts.live_compaction_comparison import export_snapshot

                        _require(variant == "basic" and bool(options.task_profile),
                                 "comparison_requires_basic_task_profile")
                        _require(not await manager.get_summaries(key),
                                 "comparison_source_has_summary")
                        fingerprints = export_snapshot(
                            comparison_snapshot_out, db,
                            settings={
                                "provider": provider, "model": model, "thinking": thinking,
                                "layout": options.layout, "task_profile": options.task_profile,
                                "context_window_tokens": options.context_window_tokens or 20_000,
                                "max_output_tokens": options.max_output_tokens or 4096,
                                "preflight_ratio": options.preflight_ratio or 0.85,
                            },
                            prompt=tail_prompt, source_digests=canonical,
                            label=generated_label, secrets=(api_key,),
                        )
                        return {"ok": True, "status": "comparison_snapshot_exported",
                                "comparison_snapshot": fingerprints,
                                "model_calls": len(observer.calls)}
                    continue
                stage_calls = observer.calls[call_start:]
                _require(
                    all(call.request.get("model") == config.llm.model for call in stage_calls),
                    "compaction_request_model_mismatch",
                )
                summary_calls = [call for call in stage_calls if _is_compaction_wire_call(call)]
                compact = summary_calls[0] if summary_calls else None
                summaries = await manager.get_summaries(key)
                active_entries = await manager.get_transcript(key)
                active = _canonical_message_digests(active_entries)
                if variant == "truncated":
                    observed["length_observed"] = bool(
                        compact and compact.finish_reason == "length"
                    )
                    observed["source_preserved"] = all(
                        active.get(mid) == digest for mid, digest in before_active.items()
                    )
                    observed["no_summary_committed"] = not summaries
                    if observed["length_observed"]:
                        _require(observed["source_preserved"], "truncated_summary_deleted_source")
                        _require(observed["no_summary_committed"], "truncated_summary_committed")
                    break
                expected_summary_calls = (
                    {2} if variant == "chunked" else {1, 2} if options.native_pressure else {1}
                )
                _require(
                    compact is not None
                    and len(summary_calls) in expected_summary_calls
                    and len(stage_calls) == len(summary_calls) + 1
                    and not _is_compaction_wire_call(stage_calls[-1]),
                    "compaction_live_not_covered",
                )
                assert compact is not None
                _require(len(summaries) == turn, "single_compaction_not_persisted")
                latest = summaries[-1]
                _require(latest.summary_source == "llm", "compaction_used_fallback")
                _require(
                    latest.coverage_status in {"pass", "pass_with_backfill", "unknown"},
                    "compaction_coverage_failed",
                )
                summary_text = latest.summary_text
                _require(
                    bool(before_active.keys() - active.keys()),
                    "compaction_old_history_still_active",
                )
                _require(
                    any(
                        entry.message_id == user.message_id and entry.content == prompt
                        for entry in active_entries
                    ),
                    "compaction_current_tail_changed",
                )
                _require(
                    all(canonical.get(mid) == digest for mid, digest in original.items()),
                    "compaction_archive_lost_history",
                )
                compact_messages = compact.request.get("messages", [])
                compact_history = json.dumps([
                    (call.request.get("messages", [])[:-1] if options.layout == "suffix"
                     else call.request.get("messages", [])) for call in summary_calls
                ])
                removed_ids = before_active.keys() - active.keys()
                source_entry_markers = {
                    marker for entry in before_entries if entry.message_id in removed_ids
                    for marker in re.findall(COMPACTION_ENTRY_PATTERN, entry.content or "")
                }
                _require(
                    all(marker in compact_history for marker in source_entry_markers),
                    "compaction_source_entry_missing",
                )
                source_entry_marker_counts.append(len(source_entry_markers))
                # A generated label that remains in the protected active suffix
                # is intentionally absent from the compaction source. Only
                # labels belonging to removed history need source coverage.
                removed_text = "\n".join(
                    str(entry.content or "") for entry in before_entries
                    if entry.message_id in removed_ids
                )
                _require(
                    all(label not in removed_text or label in compact_history for label in labels),
                    "compaction_source_not_covered",
                )
                _require(
                    all(label not in removed_text or label in summary_text for label in labels),
                    "summary_generated_fact_missing",
                )
                if turn == 1:
                    _require(
                        COMPACTION_SOURCE_MARKER in compact_history, "compaction_source_not_covered"
                    )
                    _require(
                        COMPACTION_TAIL_MARKER not in compact_history,
                        "compaction_included_current_tail",
                    )
                else:
                    _require(
                        COMPACTION_TAIL_MARKER + ("_SECOND" if turn == 2 else "_THIRD")
                        not in compact_history,
                        "compaction_included_current_tail",
                    )
                resumed = stage_calls[-1]
                resumed_input = json.dumps(resumed.request, ensure_ascii=False)
                _require(
                    summary_text
                    in "\n".join(
                        message.get("content") or ""
                        for message in resumed.request.get("messages", [])
                        if isinstance(message.get("content"), str)
                    ),
                    "persisted_summary_not_in_next_request",
                )
                _require(sum(
                    message["content"].count(summary_text)
                    for message in resumed.request.get("messages", [])
                    if isinstance(message.get("content"), str)
                ) == 1, "persisted_summary_replayed_more_than_once")
                _require(
                    COMPACTION_TAIL_MARKER in resumed_input, "current_tail_not_in_next_request"
                )
                answer = resumed.response.get("content") or ""
                if facts:
                    summary_facts = compaction_fact_coverage(summary_text, facts)
                    answer_facts = compaction_answer_fact_checks(answer, facts)
                    fact_checks.append({"summary": summary_facts, "answer": answer_facts})
                    _require(all(summary_facts.values()), "summary_critical_fact_missing")
                    _require(all(answer_facts.values()), "answer_critical_fact_missing")
                before_pressure = wire_pressure_evidence(parent)
                after_pressure = wire_pressure_evidence(resumed)
                if options.native_pressure:
                    from opensquilla.context_budget import ContextBudgetGovernor

                    input_capacity = ContextBudgetGovernor.from_values(
                        context_window_tokens=config.llm.context_window_tokens,
                        max_output_tokens=before_pressure["generation_budget"]
                        or config.llm.max_tokens,
                        thinking_budget_tokens=0,
                        context_overflow_threshold=config.preflight_compact_ratio,
                    ).snapshot().usable_tokens
                    before_pressure["physical_input_capacity_tokens"] = input_capacity
                    before_pressure["estimated_pressure_ratio"] = (
                        before_pressure["request_estimated_tokens"] / input_capacity
                    )
                    before_pressure["reported_pressure_ratio"] = (
                        before_pressure["physical_prompt_tokens"] / input_capacity
                        if before_pressure["physical_prompt_tokens"] is not None else None
                    )
                    observed["native_pressure"] = (
                        physical_window["configured_window_matches_physical"]
                        and before_pressure["estimated_pressure_ratio"] >= 0.85
                    )
                pressure_checks.append({
                    "before_parent": before_pressure,
                    "after_continuation": after_pressure,
                    "summary_chars": len(summary_text),
                    "removed_messages": len(removed_ids),
                    "kept_messages": len(active_entries),
                    "same_payload_scope": False,
                    "note": "parent_and_continuation_have_different_current_turns",
                })
                recall_check = {
                    "labels_in_summary": [label in summary_text for label in labels],
                    "labels_in_answer": [label in answer for label in labels],
                    "completion_marker_in_answer": "COMPACTION_RECALL_OK" in answer,
                }
                recall_checks.append(recall_check)
                _require(
                    all(recall_check["labels_in_answer"])
                    and recall_check["completion_marker_in_answer"],
                    "compaction_memory_recall_mismatch",
                )
                _require(
                    all(call.completed and call.finish_reason == "stop" for call in summary_calls)
                    and resumed.completed
                    and resumed.finish_reason == "stop",
                    "incomplete_provider_response",
                )
                for name in (
                    "model",
                    "tools",
                    "thinking",
                    "reasoning",
                    "reasoning_effort",
                    "enable_thinking",
                    "preserve_thinking",
                    "max_tokens",
                    "max_completion_tokens",
                ):
                    if options.layout == "prefix" and name != "model":
                        continue
                    _require(
                        all(call.request.get(name) == resumed.request.get(name)
                            for call in summary_calls),
                        "compaction_request_configuration_changed",
                    )
                    if variant not in {"model_switch", "long_reasoning", "replay_off"}:
                        _require(
                            parent.request.get(name) == compact.request.get(name),
                            "compaction_request_configuration_changed",
                        )
                common = 0
                for left, right in zip(
                    parent.request.get("messages", []), compact_messages, strict=False
                ):
                    if left != right:
                        break
                    common += 1
                if options.layout == "suffix" and turn == 1 and variant not in {
                    "model_switch", "replay_off",
                }:
                    _require(common > 1, "parent_history_prefix_not_reused")
                # Rebased recorded-history JSON grows within one message. Count
                # characters separately; whole-message equality is not token equality.
                parent_json = json.dumps(parent.request.get("messages", []), ensure_ascii=False)
                compact_json = json.dumps(compact_messages, ensure_ascii=False)
                common_chars = 0
                for left_char, right_char in zip(parent_json, compact_json, strict=False):
                    if left_char != right_char:
                        break
                    common_chars += 1
                message_json_prefix_chars.append(common_chars)
                prefix_counts.append(common)
                summary_indexes.extend(observer.calls.index(call) for call in summary_calls)
                observed.update(
                    dict.fromkeys(
                        (
                            "source_range",
                            "current_tail",
                            "archive_integrity",
                            "summary_replayed",
                            "memory_recall",
                            "current_configuration",
                        ),
                        True,
                    )
                )
                if tools_enabled and options.layout == "suffix":
                    observed["tool_roundtrip"] = (
                        tool_values == [7]
                        and _compaction_tool_history_representation(compact) != "invalid"
                    )
                    observer.replay_checks["tool_history_representation"] = (
                        _compaction_tool_history_representation(compact)
                    )
                    observed["tool_schema"] = bool(compact.request.get("tools"))
                    _require(observed["tool_roundtrip"], "compaction_tool_pair_missing")
                    _require(observed["tool_schema"], "compaction_tool_schema_missing")
                if variant == "replay_off":
                    observed["native_parent_state"] = any(
                        bool(
                            call.native_reasoning_content or call.response.get("reasoning_details")
                        )
                        for call in observer.calls[:call_start]
                    )
                    # TokenRhythm tool routes may require an empty reasoning_content
                    # placeholder. Empty schema fields do not replay native state.
                    observed["replay_disabled"] = all(
                        not message.get("reasoning_content")
                        and not message.get("reasoning_details")
                        for call in observer.calls[call_start:]
                        for message in call.request.get("messages", [])
                        if message.get("role") == "assistant"
                    )
                    _require(observed["replay_disabled"], "disabled_replay_native_state_leaked")
                if variant == "model_switch":
                    observed["model_switch"] = parent.request.get("model") != compact.request.get(
                        "model"
                    ) and compact.request.get("model") == resumed.request.get("model")
                    _require(observed["model_switch"], "compaction_used_previous_model")
                if variant == "chunked":
                    observed["chunked_summary"] = len(summary_calls) == latest.chunk_count == 2
                    observed["single_preflight"] = started_phases == ["preflight"]
                if variant == "long_reasoning":
                    reasoning = _usage_report([compact])["reasoning_tokens_by_call"][0]
                    observed["reasoning_over_1024"] = reasoning is not None and reasoning > 1024
                if variant == "repeated" and turn in {1, 2}:
                    label_number = turn + 1
                    ordinal = "second" if turn == 1 else "third"
                    match = re.search(
                        rf"COMPACTION_LABEL_{label_number}\s*[:=]\s*[`\"']?"
                        r"([A-Za-z0-9_-]{4,64})", answer
                    )
                    _require(bool(match), f"{ordinal}_generated_label_missing")
                    assert match is not None
                    new_label = match.group(1)
                    _require(
                        new_label not in labels and new_label not in resumed_input,
                        f"{ordinal}_label_already_in_input",
                    )
                    labels.append(new_label)
                if variant == "repeated" and turn == 3:
                    observed["repeated_compaction"] = len(summaries) == 3
                    observed["cumulative_memory"] = all(
                        label in summary_text and label in answer for label in labels
                    )
                parent = resumed
            finally:
                await storage.close()
    covered = all(observed.get(code) is True for code in coverage["expected"])
    coverage["status"] = "covered" if covered else "not_covered"
    return {
        "ok": covered,
        "status": "passed" if covered else "compaction_variant_not_covered",
        "provider": provider,
        "model": model,
        "scenario": "compaction",
        "compaction_variant": variant,
        "layout": options.layout,
        "task_profile": options.task_profile,
        "context_window_tokens": config.llm.context_window_tokens,
        "max_output_tokens": config.llm.max_tokens,
        "preflight_ratio": config.preflight_compact_ratio,
        "native_pressure_requested": options.native_pressure,
        "acceptance_scope": (
            "native_window_pressure" if options.native_pressure
            and physical_window["configured_window_matches_physical"]
            else "configured_window_pressure" if options.native_pressure
            else "small_window_protocol_integration"
        ),
        **physical_window,
        "critical_fact_checks": fact_checks,
        "pressure_checks": pressure_checks,
        "source_fixture_sha256": hashlib.sha256(
            json.dumps(sorted(original.values())).encode()
        ).hexdigest(),
        "compaction_next_model": next_model,
        "thinking": thinking,
        **_usage_report(observer.calls),
        "coverage": coverage,
        "storage_reopened": True,
        "new_agent_after_restart": True,
        "compaction_source_verified": observed.get("source_range", False),
        "compaction_current_tail_verified": observed.get("current_tail", False),
        "compaction_archive_verified": observed.get("archive_integrity", False),
        "compaction_memory_recall_verified": observed.get("memory_recall", False),
        "compaction_configuration_verified": observed.get("current_configuration", False),
        "compaction_summary_replayed": observed.get("summary_replayed", False),
        "common_parent_prefix_messages": prefix_counts[0] if prefix_counts else 0,
        "common_parent_prefix_messages_by_summary": prefix_counts,
        "common_message_json_prefix_chars_by_summary": message_json_prefix_chars,
        "summary_call_indexes": summary_indexes,
        "tool_calls": len(tool_values),
        "tool_history_representation": observer.replay_checks.get("tool_history_representation"),
        "source_digest_fields_verified": True,
        "source_entry_markers_verified": source_entry_marker_counts,
        "request_models_by_call": [call.request.get("model") for call in observer.calls],
        "replay_transition": {"parent": True, "current": variant != "replay_off"},
        "compaction_events": compaction_events,
        "compaction_attempted_by_turn": attempted_by_turn,
        "catalog_loaded": catalog_loaded,
    }


async def run_case(
    root: Path,
    *,
    provider: str,
    model: str,
    api_key: str,
    observer: WireObserver | None = None,
    scenario: str = "tools",
    thinking: str = "low",
    require_native_replay: bool | None = None,
    compaction_variant: str = "basic",
    compaction_next_model: str | None = None,
    compaction_options: CompactionCaseOptions | None = None,
    comparison_snapshot_out: Path | None = None,
) -> dict[str, Any]:
    _require(scenario in {"tools", "chat", "compaction"}, "invalid_scenario")
    _require(thinking in THINKING_CHOICES, "invalid_thinking_level")
    if scenario == "compaction":
        return await _run_compaction_case(
            root, provider=provider, model=model, api_key=api_key,
            observer=observer, thinking=thinking,
            variant=compaction_variant, next_model=compaction_next_model,
            options=compaction_options,
            comparison_snapshot_out=comparison_snapshot_out,
        )
    endpoint = registry_endpoint(provider)
    observer = observer or WireObserver(endpoint)
    config = _config(root, provider, model, endpoint, thinking=thinking)
    tool_values: list[int] = []
    registry = ToolRegistry()

    async def step(value: int) -> str:
        tool_values.append(value)
        return json.dumps({"next_value": value + 11})

    if scenario == "tools":
        registry.register(
            ToolSpec(
                name="replay_step",
                description=(
                    "Pure test function returning next_value. Wait for this result before "
                    "using its next_value as the argument of a subsequent call."
                ),
                parameters={"value": {"type": "integer"}},
                required=["value"],
            ),
            step,
        )
    selector_config = SelectorConfig(
        primary=ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=endpoint,
        )
    )
    key = "agent:main:synthetic-reasoning-replay"
    db = root / "sessions.sqlite"
    persisted_before: list[dict[str, Any]] = []
    restart_index = 0
    persisted_fields = 0
    with observer.observe():
        prompts = (FIRST_PROMPT, SECOND_PROMPT) if scenario == "tools" else CHAT_PROMPTS
        for turn, prompt in enumerate(prompts):
            storage = SessionStorage(str(db))
            await storage.connect()
            try:
                manager = SessionManager(storage, inject_time_prefix=False)
                if turn == 0:
                    await manager.create(session_key=key, agent_id="main")
                else:
                    entries = await manager.get_canonical_transcript(key)
                    restored = [
                        entry.assistant_replay for entry in entries if entry.role == "assistant"
                    ]
                    _require(restored == persisted_before, "database_reopen_state_mismatch")
                # A new runner, selector and Agent for every user turn; no
                # transient provider/Agent history can bridge this boundary.
                runner = TurnRunner(
                    provider_selector=ModelSelector(selector_config),
                    tool_registry=registry,
                    session_manager=manager,
                    config=config,
                    model_catalog=_Catalog(),
                )
                user = await manager.append_message(key, "user", prompt)
                events = [
                    event
                    async for event in runner.run(
                        prompt,
                        session_key=key,
                        bound_user_message_id=user.message_id,
                        tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
                    )
                ]
                for event in events:
                    if event.kind == "error":
                        observer.engine_error_codes.append(
                            safe_provider_failure_code(
                                getattr(event, "code", None),
                                getattr(event, "failure_kind", None),
                            )
                        )
                _require(not observer.engine_error_codes, "turn_failed")
                _require(bool(observer.calls), "missing_provider_response")
                _assert_turn_answer(
                    observer.calls[-1].response.get("content"), scenario=scenario, turn=turn
                )
                entries = await manager.get_canonical_transcript(key)
                saved = [entry.assistant_replay for entry in entries if entry.role == "assistant"]
                _require(len(saved) == turn + 1, "assistant_row_count_mismatch")
                _require(
                    all(item and item.get("version") == 1 for item in saved),
                    "accepted_replay_state_not_persisted",
                )
                persisted_fields = _assert_persisted_replay(saved, observer.calls)
                if turn == 0:
                    persisted_before = copy.deepcopy(saved)
                    restart_index = len(observer.calls)
            finally:
                await storage.close()
    _require(tool_values == ([7, 18, 23] if scenario == "tools" else []), "tool_sequence_mismatch")
    tool_result_comparisons = _assert_tool_results(observer.calls)
    checks = _assert_wire_replay(
        observer.calls,
        restart_index,
        provider,
        model=model,
        scenario=scenario,
        require_native_replay=require_native_replay,
    )
    checks["native_state"]["persisted"] = persisted_fields
    observer.replay_checks = checks
    return {
        "provider": provider,
        "model": model,
        "scenario": scenario,
        "thinking": thinking,
        "require_native_replay": require_native_replay,
        "ok": True,
        **_usage_report(observer.calls),
        "tool_calls": len(tool_values),
        "tool_result_comparisons": tool_result_comparisons,
        "final_answers_verified": 2,
        "chat_memory_recall_verified": True if scenario == "chat" else None,
        "storage_reopened": True,
        "new_agent_after_restart": True,
        **checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable paid provider calls")
    parser.add_argument("--provider", choices=sorted(DEFAULT_MODELS), default="deepseek")
    parser.add_argument("--model")
    parser.add_argument("--scenario", choices=("tools", "chat", "compaction"), default="tools")
    parser.add_argument("--thinking", choices=THINKING_CHOICES, default="low")
    parser.add_argument("--compaction-variant", choices=COMPACTION_VARIANTS, default="basic")
    parser.add_argument("--compaction-next-model")
    parser.add_argument("--layout", choices=("prefix", "suffix"), default="suffix")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-output", type=int)
    parser.add_argument("--preflight-ratio", type=float)
    parser.add_argument("--task-profile", choices=COMPACTION_TASKS)
    parser.add_argument("--native-pressure", action="store_true")
    comparison_paths = parser.add_mutually_exclusive_group()
    comparison_paths.add_argument("--comparison-snapshot-out", type=Path)
    comparison_paths.add_argument("--comparison-snapshot-in", type=Path)
    parser.add_argument("--comparison-history-tokens", type=int)
    parser.add_argument("--comparison-history-chars", type=int)
    parser.add_argument("--serve-gateway", action="store_true")
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--relay-ready", type=Path)
    parser.add_argument("--gateway-read-files", action="store_true")
    parser.add_argument("--gateway-root", type=Path)
    parser.add_argument("--gateway-port", type=int, default=18799)
    parser.add_argument("--ui-dist", type=Path)
    parser.add_argument("--execution-config", type=Path)
    parser.add_argument("--observe-provider", choices=sorted(DEFAULT_MODELS), action="append")
    parser.add_argument(
        "--require-native-replay", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "status": "live_opt_in_required"}))
        return 2
    if not 1 <= args.max_calls <= 60:
        print(json.dumps({"ok": False, "status": "invalid_physical_call_limit"}))
        return 2
    relay_environment: dict[str, str] = {}
    if args.relay_ready:
        from scripts.live_tokenrhythm_transport import RelayTarget

        try:
            ready = json.loads(require_temporary_report_path(args.relay_ready).read_text())
            _require(args.provider == "tokenrhythm", "relay_requires_tokenrhythm")
            _require(ready.get("enabled") is True and ready.get("mode") == "functional",
                     "functional_relay_required")
            target = RelayTarget(str(ready["base_url"]), str(ready["client_key"]))
            relay_environment = {
                "TOKENRHYTHM_API_KEY": target.client_key,
                "OPENSQUILLA_LIVE_TRANSPORT": "1",
                "OPENSQUILLA_LIVE_RELAY_URL": str(target.url),
                "OPENSQUILLA_LIVE_RELAY_CLIENT_KEY": target.client_key,
            }
        except Exception:
            print(json.dumps({"ok": False, "status": "invalid_functional_relay"}))
            return 2
    if args.report:
        require_temporary_report_path(args.report)
    if args.comparison_snapshot_in:
        from scripts.live_compaction_comparison import read_snapshot

        try:
            settings = read_snapshot(args.comparison_snapshot_in)["settings"]
            explicit = set(argv if argv is not None else sys.argv[1:])
            for flag, attribute, key in (
                ("--provider", "provider", "provider"), ("--model", "model", "model"),
                ("--thinking", "thinking", "thinking"), ("--layout", "layout", "layout"),
                ("--context-window", "context_window", "context_window_tokens"),
                ("--max-output", "max_output", "max_output_tokens"),
                ("--preflight-ratio", "preflight_ratio", "preflight_ratio"),
                ("--task-profile", "task_profile", "task_profile"),
            ):
                _require(flag not in explicit or getattr(args, attribute) == settings[key],
                         "comparison_controls_changed")
                setattr(args, attribute, settings[key])
            args.scenario = "compaction"
        except Exception:
            print(json.dumps({"ok": False, "status": "invalid_comparison_snapshot_or_controls"}))
            return 2
    if args.comparison_snapshot_in or args.comparison_snapshot_out:
        if args.serve_gateway or args.compaction_variant != "basic" or not args.task_profile:
            print(json.dumps({"ok": False, "status": "comparison_requires_basic_task_profile"}))
            return 2
    if args.comparison_snapshot_out:
        try:
            snapshot_root = require_temporary_report_path(
                args.comparison_snapshot_out / "manifest.json"
            ).parent
            _require(not snapshot_root.exists(), "comparison_snapshot_already_exists")
        except Exception:
            print(json.dumps({"ok": False, "status": "invalid_or_existing_comparison_snapshot"}))
            return 2
    if args.comparison_history_tokens is not None or args.comparison_history_chars is not None:
        if not args.comparison_snapshot_in or not all(
            value is not None and value > 0
            for value in (args.comparison_history_tokens, args.comparison_history_chars)
        ):
            print(json.dumps({
                "ok": False, "status": "comparison_requires_both_positive_capacities",
            }))
            return 2
    try:
        compaction_options = CompactionCaseOptions(
            layout=args.layout, context_window_tokens=args.context_window,
            max_output_tokens=args.max_output, preflight_ratio=args.preflight_ratio,
            task_profile=args.task_profile, native_pressure=args.native_pressure,
        )
    except ReplayCheckError as exc:
        print(json.dumps({"ok": False, "status": str(exc)}))
        return 2
    spec = get_provider_spec(args.provider)
    api_key = relay_environment.get(spec.env_key) or os.environ.get(spec.env_key, "")
    model = (
        args.model
        or os.environ.get(f"{args.provider.upper()}_MODEL")
        or DEFAULT_MODELS[args.provider]
    )
    if not api_key or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model):
        print(json.dumps({"ok": False, "status": "missing_credential_or_invalid_model"}))
        return 2
    if args.compaction_next_model and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", args.compaction_next_model
    ):
        print(json.dumps({"ok": False, "status": "invalid_next_model"}))
        return 2
    if args.serve_gateway:
        if args.gateway_root is None or args.ui_dist is None:
            print(json.dumps({"ok": False, "status": "gateway_root_and_ui_dist_required"}))
            return 2
        root = args.gateway_root.resolve()
        require_temporary_report_path(root / "wire-summary.json")
        if not (args.ui_dist / "index.html").is_file():
            print(json.dumps({"ok": False, "status": "built_ui_required"}))
            return 2
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        root = Path(tempfile.mkdtemp(prefix="opensquilla-reasoning-replay-"))
    root.chmod(0o700)
    # Prevent unrelated diagnostic modes from writing wire payloads to the
    # user's state directory. All test state lives in the owned temp tree.
    env = {
        **minimal_child_environment(),
        spec.env_key: api_key,
        "OPENSQUILLA_STATE_DIR": str(root / "state"),
        "OPENSQUILLA_USER_STATE_DIR": str(root / "user-state"),
        "OPENSQUILLA_LIVE_DISABLE_DOTENV": "1",
        "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
        "OPENSQUILLA_COMPACTION_PROMPT_LAYOUT": args.layout,
        **relay_environment,
    }
    if os.name == "nt":
        # Path.home() is used by log redaction during Gateway boot. Keep it
        # resolvable without inheriting the operator's actual Windows profile.
        env["USERPROFILE"] = str(root / "user-state")
    observed_providers = set(args.observe_provider or ()) | {args.provider}
    if relay_environment and observed_providers != {"tokenrhythm"}:
        print(json.dumps({"ok": False, "status": "relay_provider_mismatch"}))
        return 2
    endpoints = {provider: registry_endpoint(provider) for provider in observed_providers}
    keys = {get_provider_spec(provider).env_key:
            relay_environment.get(get_provider_spec(provider).env_key)
            or os.environ.get(get_provider_spec(provider).env_key, "")
            for provider in observed_providers}
    env.update({key: value for key, value in keys.items() if value})
    if args.serve_gateway:
        env["OPENSQUILLA_CONTROL_UI_DIST"] = str(args.ui_dist.resolve())
    secrets = tuple(value for value in keys.values() if value)
    report: dict[str, Any]

    def summary_fault() -> str | None:
        fault_path = root / "summary-fault-mode"
        return fault_path.read_text().strip() or None if fault_path.is_file() else None

    observer = WireObserver(
        registry_endpoint(args.provider),
        endpoints=endpoints,
        max_calls=min(args.max_calls, COMPACTION_CALL_LIMITS[args.compaction_variant]
                      + int(args.native_pressure))
        if args.scenario == "compaction" and not args.serve_gateway else args.max_calls,
        summary_fault=summary_fault if args.serve_gateway else None,
    )
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.ExitStack() as live_stack,
        ):
            from opensquilla.application import approval_queue

            # Tool turns open the process-local approval SQLite connection.
            # An embedded caller may already own the singleton. Never reset
            # it: reset also unlinks its database, which can be outside root.
            previous_queue = approval_queue._queue

            def close_test_queue() -> None:
                queue = approval_queue._queue
                if queue is None or queue is previous_queue:
                    return
                if not queue._db_path.resolve().is_relative_to(root.resolve()):
                    return
                queue.close()
                if approval_queue._queue is queue:
                    approval_queue._queue = None

            # Close only a new, owned handle before Windows tree cleanup;
            # deletion remains the responsibility of the existing root scanner.
            live_stack.callback(close_test_queue)
            if relay_environment:
                from scripts.live_tokenrhythm_transport import install_from_env

                live_stack.callback(install_from_env())
            logging.disable(logging.CRITICAL)
            if args.serve_gateway:
                from opensquilla.gateway import control_ui
                from scripts.live_compaction_gateway import (
                    public_execution_overlay,
                    serve_compaction_gateway,
                )

                # TurnRunner imports this module before the isolated env is
                # installed; override its cached artifact for this serve only.
                live_stack.enter_context(patch.object(
                    control_ui, "_DIST_DIR", args.ui_dist.resolve(),
                ))
                overlay = (
                    public_execution_overlay(json.loads(args.execution_config.read_text()))
                    if args.execution_config else None
                )
                report = asyncio.run(serve_compaction_gateway(
                    root, provider=args.provider, model=model,
                    endpoint=registry_endpoint(args.provider), provider_env=spec.env_key,
                    observer=observer, options=compaction_options, port=args.gateway_port,
                    report_path=args.report or root / "wire-summary.json", secrets=secrets,
                    thinking=args.thinking, execution_overlay=overlay,
                    allow_read_files=args.gateway_read_files,
                ))
            elif args.comparison_snapshot_in:
                from scripts.live_compaction_comparison import run_comparison

                report = asyncio.run(run_comparison(
                    root, args.comparison_snapshot_in, api_key=api_key, observer=observer,
                    history_tokens=args.comparison_history_tokens,
                    history_chars=args.comparison_history_chars,
                ))
            else:
                report = asyncio.run(
                    run_case(
                        root,
                        provider=args.provider,
                        model=model,
                        api_key=api_key,
                        observer=observer,
                        scenario=args.scenario,
                        thinking=args.thinking,
                        require_native_replay=args.require_native_replay,
                        compaction_variant=args.compaction_variant,
                        compaction_next_model=args.compaction_next_model,
                        compaction_options=compaction_options,
                        comparison_snapshot_out=args.comparison_snapshot_out,
                    )
                )
    except ReplayCheckError as exc:
        report = {"ok": False, "provider": args.provider, "status": str(exc)}
    except Exception:
        report = {"ok": False, "provider": args.provider, "status": "harness_failed"}
    finally:
        logging.disable(logging.NOTSET)
        try:
            if not args.serve_gateway:
                scan_and_remove_temporary_tree(root, secrets)
        except Exception:
            report = {"ok": False, "provider": args.provider, "status": "cleanup_failed"}
    report.update(
        model=model,
        scenario=args.scenario,
        thinking=args.thinking,
        require_native_replay=args.require_native_replay,
        compaction_variant=args.compaction_variant,
        compaction_next_model=args.compaction_next_model,
    )
    report.update(_usage_report(observer.calls))
    report.update(_wire_diagnostics(observer))
    if observer.blocked_unobserved_generation_requests:
        report["ok"] = False
        report["status"] = "unobserved_generation_request_blocked"
    report = sanitize_report(report, secrets)
    if args.report and not args.serve_gateway:
        write_safe_report(args.report, report, secrets)
    print(json.dumps(report, sort_keys=True))
    if args.serve_gateway:
        return 0 if (
            report.get("lifecycle_status") == "stopped"
            and report.get("artifact_scan_status") == "passed"
            and not observer.blocked_unobserved_generation_requests
        ) else 1
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
