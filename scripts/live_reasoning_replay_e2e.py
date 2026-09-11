#!/usr/bin/env python3
"""Opt-in reasoning replay check through Agent, finalizer, and reopened SQLite.

Only synthetic prompts and pure tools are used. Native response/request data
stays in memory; the public report contains counts and boolean assertions only.
Run one provider per process with credentials supplied in its registry env var.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import io
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

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
    response: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    status_code: int = 0
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
        self.body = bytearray()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            self.body.extend(chunk)
            _require(len(self.body) <= 8_000_000, "response_observation_limit")
            yield chunk

    async def aclose(self) -> None:
        # Providers may stop reading immediately at [DONE]. Their context
        # manager still closes the response, which is the observation commit.
        try:
            self.call.encoded_response_bytes = len(self.body)
            # The wrapped stream precedes HTTPX's content decoder. Decode an
            # observation copy with the same public HTTPX response API; the
            # real response is still consumed unmodified by the provider.
            observed = (
                httpx.Response(
                    200,
                    headers={"content-encoding": self.encoding},
                    content=bytes(self.body),
                ).content
                if self.encoding
                else bytes(self.body)
            )
            self.call.consume(observed)
        finally:
            await self.inner.aclose()


class WireObserver:
    def __init__(self, endpoint: str, transport: httpx.AsyncBaseTransport | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.transport = transport
        self.calls: list[WireCall] = []
        self.engine_error_codes: list[str] = []
        self.replay_checks: dict[str, Any] = {}

    @contextlib.contextmanager
    def observe(self) -> Iterator[None]:
        original_send = httpx.AsyncClient.send

        async def send(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any):
            observed = str(request.url).startswith(self.endpoint + "/")
            if observed and request.url.path.endswith("/chat/completions"):
                call = WireCall(request=json.loads(request.content))
                self.calls.append(call)
                if self.transport is None:
                    response = await original_send(client, request, **kwargs)
                else:
                    response = await self.transport.handle_async_request(request)
                    response.request = request
                call.status_code = response.status_code
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
                else:
                    response.stream = _ObservedStream(response.stream, call, encoding)
                return response
            if self.transport is not None:
                raise ReplayCheckError("unexpected_offline_http_request")
            return await original_send(client, request, **kwargs)

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
    config.memory.repair_enabled = False
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
    return {
        "model_calls": len(calls),
        "input_tokens": sum(int(numeric(call.usage.get("prompt_tokens"))) for call in calls),
        "output_tokens": sum(int(numeric(call.usage.get("completion_tokens"))) for call in calls),
        "provider_reported_cost_usd": sum(costs) if costs else None,
        "cost_reported": bool(costs),
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
        **observer.replay_checks,
        "calls": [
            {
                "http_status": call.status_code,
                "content_encoding": call.content_encoding,
                "encoded_response_bytes": call.encoded_response_bytes,
                "decoded_response_bytes": call.decoded_response_bytes,
                "body_format": call.body_format,
                "body_error_code": call.body_error_code,
                "error_field_mentions": sorted(call.error_field_mentions),
                "error_kind_mentions": sorted(call.error_kind_mentions),
                "malformed_frames": call.malformed_frames,
                "finish_reason": call.finish_reason,
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
) -> dict[str, Any]:
    _require(scenario in {"tools", "chat"}, "invalid_scenario")
    _require(thinking in THINKING_CHOICES, "invalid_thinking_level")
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
    parser.add_argument("--scenario", choices=("tools", "chat"), default="tools")
    parser.add_argument("--thinking", choices=THINKING_CHOICES, default="low")
    parser.add_argument(
        "--require-native-replay", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "status": "live_opt_in_required"}))
        return 2
    if args.report:
        require_temporary_report_path(args.report)
    spec = get_provider_spec(args.provider)
    api_key = os.environ.get(spec.env_key, "")
    model = (
        args.model
        or os.environ.get(f"{args.provider.upper()}_MODEL")
        or DEFAULT_MODELS[args.provider]
    )
    if not api_key or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model):
        print(json.dumps({"ok": False, "status": "missing_credential_or_invalid_model"}))
        return 2
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
    }
    report: dict[str, Any]
    observer = WireObserver(registry_endpoint(args.provider))
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            logging.disable(logging.CRITICAL)
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
                )
            )
    except ReplayCheckError as exc:
        report = {"ok": False, "provider": args.provider, "status": str(exc)}
    except Exception:
        report = {"ok": False, "provider": args.provider, "status": "harness_failed"}
    finally:
        logging.disable(logging.NOTSET)
        try:
            scan_and_remove_temporary_tree(root, (api_key,))
        except Exception:
            report = {"ok": False, "provider": args.provider, "status": "cleanup_failed"}
    report.update(
        model=model,
        scenario=args.scenario,
        thinking=args.thinking,
        require_native_replay=args.require_native_replay,
    )
    report.update(_usage_report(observer.calls))
    report.update(_wire_diagnostics(observer))
    report = sanitize_report(report, (api_key,))
    if args.report:
        write_safe_report(args.report, report, (api_key,))
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
