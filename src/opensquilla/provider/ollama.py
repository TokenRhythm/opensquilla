"""OllamaProvider — streams via Ollama local/cloud API using httpx."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.secrets import clean_header_secret

from .candidate_artifact import (
    CandidateArtifactBuilder,
    CandidateArtifactLimitError,
    strip_candidate_tool_identity,
)
from .error_redaction import (
    redact_upstream_error_code,
    redact_upstream_error_text,
    redacted_httpx_error,
)
from .stream_assembly import ToolStreamAccumulator, ToolStreamProtocolError
from .trace_recorder import LLMTraceRecorder
from .types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
)

log = structlog.get_logger(__name__)

_OLLAMA_DEFAULT_BASE = "http://localhost:11434"
# Ollama's server default num_ctx is 2048, which silently truncates the front of
# an agent prompt (system prompt + tool schemas) and makes tool use look broken.
# Default to a context window large enough for real agent turns; callers can
# override via the ``num_ctx`` constructor argument.
_OLLAMA_DEFAULT_NUM_CTX = 8192


def _build_ollama_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema.model_dump(exclude_none=True),
        },
    }


def _tool_result_content(block: Any) -> str:
    content = block.content
    return content if isinstance(content, str) else json.dumps(content)


def _candidate_wrapper_has_substantive_content(value: object) -> bool:
    """Return whether a sanitized malformed wrapper carries advisory content."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).casefold()
            if key_text == "index":
                continue
            # A bare native wrapper marker does not make an otherwise empty
            # action useful to the aggregator.
            if (
                key_text == "type"
                and isinstance(nested, str)
                and nested.strip().casefold() == "function"
            ):
                continue
            if _candidate_wrapper_has_substantive_content(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_candidate_wrapper_has_substantive_content(item) for item in value)
    # Numeric and boolean values are explicit provider-authored evidence.
    return True


def _candidate_field_has_content(value: object) -> bool:
    """Cheaply classify a native name/arguments field without walking it."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return bool(value)
    return True


def _build_ollama_messages(
    msg: Message,
    tool_names: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert one internal message into one or more Ollama chat messages.

    A single message may expand into several Ollama messages: assistant turns
    carry their ``tool_calls`` so the model keeps a record of what it invoked,
    and each ``tool_result`` block becomes its own ``tool`` role message (Ollama
    has no notion of bundled parallel results) tagged with ``tool_name`` so the
    model can correlate the result with the call.
    """
    if isinstance(msg.content, str):
        return [{"role": msg.role, "content": msg.content}]

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    images: list[str] = []
    tool_messages: list[dict[str, Any]] = []

    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"function": {"name": block.name, "arguments": block.input}})
        elif block.type == "image":
            # Ollama expects raw base64 strings in `images`; it does not fetch URLs.
            if block.source_type == "base64":
                images.append(block.data)
        elif block.type == "tool_result":
            tool_message: dict[str, Any] = {
                "role": "tool",
                "content": _tool_result_content(block),
            }
            name = tool_names.get(block.tool_use_id)
            if name:
                tool_message["tool_name"] = name
            tool_messages.append(tool_message)

    out: list[dict[str, Any]] = []
    if text_parts or tool_calls or images:
        main: dict[str, Any] = {"role": msg.role, "content": " ".join(text_parts)}
        if tool_calls:
            main["tool_calls"] = tool_calls
        if images:
            main["images"] = images
        out.append(main)
    out.extend(tool_messages)
    return out


def _convert_messages(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    # Map tool_use ids to their tool name so tool results can be correlated.
    tool_names: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if block.type == "tool_use":
                    tool_names[block.id] = block.name

    for msg in messages:
        out.extend(_build_ollama_messages(msg, tool_names))
    return out


class OllamaProvider:
    """Streams from an Ollama instance (local or cloud) using /api/chat."""

    provider_name = "ollama"

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = _OLLAMA_DEFAULT_BASE,
        proxy: str | None = None,
        api_key: str | None = None,
        num_ctx: int | None = None,
        provider_id: str | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy or None
        self._api_key = clean_header_secret(api_key, label="Ollama API key") if api_key else ""
        self._num_ctx = num_ctx or _OLLAMA_DEFAULT_NUM_CTX
        self.provider_id = (provider_id or self.provider_name).strip()

    @property
    def model(self) -> str:
        """Model id this provider was configured with.

        Public so callers (e.g. derived-cache key construction) can identify
        the underlying model without prying at private state.
        """
        return self._model

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cfg = config or ChatConfig()
        return self._stream(messages, tools, cfg)

    async def _stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        cfg: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        ollama_messages = _convert_messages(messages, cfg.system)

        options: dict[str, Any] = {
            "num_predict": cfg.max_tokens,
            "num_ctx": self._num_ctx,
        }
        if cfg.temperature is not None:
            options["temperature"] = cfg.temperature

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": True,
            "options": options,
        }
        if tools:
            payload["tools"] = [_build_ollama_tool(t) for t in tools]
        endpoint = f"{self._base_url}/api/chat"
        trace = LLMTraceRecorder(
            provider="ollama",
            model=self._model,
            base_url=self._base_url,
            endpoint=endpoint,
            stream=True,
        )
        trace.record_request(
            payload=payload,
            metadata={"timeout_seconds": cfg.timeout, "tools_count": len(tools or [])},
        )

        input_tokens = 0
        output_tokens = 0
        assistant_text_parts: list[str] = []
        # Ollama tool calls arrive whole inside stream frames. Validate and
        # assemble them as they arrive, but hold their public lifecycle events
        # until done=true supplies successful terminal evidence. This applies
        # response-local call/identity/argument limits before retaining an
        # attacker-controlled number of calls or bytes.
        candidate_artifact = (
            CandidateArtifactBuilder()
            if cfg.candidate_output_mode == "inert_artifact"
            else None
        )
        tools_acc = ToolStreamAccumulator()
        prepared_tool_events: list[StreamEvent] = []
        prepared_tool_calls: list[dict[str, Any]] = []
        candidate_call_key = 0
        saw_done = False

        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers=self._headers(),
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        body_text = body.decode("utf-8", errors="replace")
                        safe_body_text = redact_upstream_error_text(
                            body_text,
                            api_key=self._api_key,
                            max_len=4000,
                        )
                        message = redact_upstream_error_text(
                            f"HTTP {response.status_code}: {body_text}",
                            api_key=self._api_key,
                            max_len=2000,
                        )
                        trace.record_error(
                            code=str(response.status_code),
                            message=message,
                            status_code=response.status_code,
                            response_body=safe_body_text,
                        )
                        yield ErrorEvent(
                            message=message,
                            code=str(response.status_code),
                        )
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            message = "Ollama stream contained an invalid JSON frame"
                            trace.record_error(
                                code="invalid_stream_frame",
                                message=message,
                                metadata={"phase": "stream"},
                            )
                            yield ErrorEvent(message=message, code="invalid_stream_frame")
                            return

                        if not isinstance(chunk, dict):
                            message = "Ollama stream contained a non-object JSON frame"
                            trace.record_error(code="invalid_stream_frame", message=message)
                            yield ErrorEvent(message=message, code="invalid_stream_frame")
                            return
                        if "error" in chunk and chunk["error"] is not None:
                            top_level_error = chunk["error"]
                            error_message = (
                                str(top_level_error.get("message") or "Ollama stream error")
                                if isinstance(top_level_error, dict)
                                else str(top_level_error).strip() or "Ollama stream error"
                            )
                            error_message = redact_upstream_error_text(
                                error_message,
                                api_key=self._api_key,
                                max_len=2000,
                            )
                            error_code = (
                                str(top_level_error.get("code") or "stream_error")
                                if isinstance(top_level_error, dict)
                                else "stream_error"
                            )
                            error_code = redact_upstream_error_code(
                                error_code,
                                api_key=self._api_key,
                            )
                            trace.record_error(code=error_code, message=error_message)
                            yield ErrorEvent(message=error_message, code=error_code)
                            return
                        trace.record_chunk(chunk)
                        msg_chunk = chunk.get("message", {})
                        if not isinstance(msg_chunk, dict):
                            message = "Ollama stream contained a malformed message"
                            trace.record_error(code="invalid_stream_frame", message=message)
                            yield ErrorEvent(message=message, code="invalid_stream_frame")
                            return

                        # Text content
                        text = msg_chunk.get("content", "")
                        if text:
                            assistant_text_parts.append(text)
                            yield TextDeltaEvent(text=text)

                        # Ollama delivers tool_calls in a single chunk (non-streaming)
                        raw_tool_calls = msg_chunk.get("tool_calls", [])
                        if not isinstance(raw_tool_calls, list):
                            if candidate_artifact is not None:
                                sanitized_calls = strip_candidate_tool_identity(
                                    raw_tool_calls
                                )
                                if _candidate_wrapper_has_substantive_content(
                                    sanitized_calls
                                ):
                                    candidate_artifact.observe_call(
                                        candidate_call_key,
                                        arguments={
                                            "malformed_tool_calls": sanitized_calls
                                        },
                                    )
                                    candidate_call_key += 1
                                raw_tool_calls = []
                            else:
                                message = "Ollama stream contained malformed tool calls"
                                trace.record_error(code="incomplete_tool_call", message=message)
                                yield ErrorEvent(message=message, code="incomplete_tool_call")
                                return
                        for tc in raw_tool_calls:
                            if not isinstance(tc, dict):
                                if candidate_artifact is not None:
                                    sanitized_call = strip_candidate_tool_identity(tc)
                                    if _candidate_wrapper_has_substantive_content(
                                        sanitized_call
                                    ):
                                        candidate_artifact.observe_call(
                                            candidate_call_key,
                                            arguments={
                                                "malformed_tool_call": sanitized_call
                                            },
                                        )
                                        candidate_call_key += 1
                                    continue
                                message = "Ollama stream contained a malformed tool call"
                                trace.record_error(code="incomplete_tool_call", message=message)
                                yield ErrorEvent(message=message, code="incomplete_tool_call")
                                return
                            fn = tc.get("function", {})
                            if not isinstance(fn, dict):
                                if candidate_artifact is not None:
                                    sanitized_call = strip_candidate_tool_identity(tc)
                                    if _candidate_wrapper_has_substantive_content(
                                        sanitized_call
                                    ):
                                        candidate_artifact.observe_call(
                                            candidate_call_key,
                                            arguments={
                                                "malformed_tool_call": sanitized_call
                                            },
                                        )
                                        candidate_call_key += 1
                                    continue
                                message = "Ollama stream contained a malformed tool function"
                                trace.record_error(code="incomplete_tool_call", message=message)
                                yield ErrorEvent(message=message, code="incomplete_tool_call")
                                return
                            raw_tool_id = tc.get("id")
                            if candidate_artifact is not None:
                                candidate_name = fn.get("name")
                                candidate_arguments = fn.get("arguments")
                                if not _candidate_field_has_content(
                                    candidate_name
                                ) and not _candidate_field_has_content(
                                    candidate_arguments
                                ):
                                    sanitized_call = strip_candidate_tool_identity(tc)
                                    if not _candidate_wrapper_has_substantive_content(
                                        sanitized_call
                                    ):
                                        continue
                                    candidate_arguments = {
                                        "malformed_tool_call": sanitized_call,
                                    }
                                candidate_artifact.observe_call(
                                    candidate_call_key,
                                    name_text=candidate_name,
                                    arguments=candidate_arguments,
                                )
                                candidate_call_key += 1
                                continue
                            if "id" in tc and (
                                not isinstance(raw_tool_id, str) or not raw_tool_id.strip()
                            ):
                                message = "Ollama stream contained an invalid tool call id"
                                trace.record_error(code="incomplete_tool_call", message=message)
                                yield ErrorEvent(message=message, code="incomplete_tool_call")
                                return
                            key = tools_acc.next_int_key()
                            tool_use_id = (
                                raw_tool_id
                                if isinstance(raw_tool_id, str)
                                else f"call_{key}"
                            )
                            tool_name = fn.get("name", "")
                            arguments = fn.get("arguments", {})
                            try:
                                arguments_json = json.dumps(arguments, allow_nan=False)
                                call_events = [
                                    *tools_acc.start(
                                        key,
                                        tool_use_id=tool_use_id,
                                        tool_name=tool_name,
                                    ),
                                    *tools_acc.append(key, arguments_json),
                                    *tools_acc.finish_with_arguments(key, arguments),
                                ]
                            except (
                                OverflowError,
                                RecursionError,
                                TypeError,
                                ValueError,
                                ToolStreamProtocolError,
                            ) as exc:
                                message = (
                                    "Ollama response contained an invalid tool lifecycle"
                                )
                                reason = (
                                    exc.reason
                                    if isinstance(exc, ToolStreamProtocolError)
                                    else "invalid_tool_arguments"
                                )
                                trace.record_error(
                                    code="incomplete_tool_call",
                                    message=message,
                                    metadata={"reason": reason, "phase": "stream"},
                                )
                                yield ErrorEvent(
                                    message=message,
                                    code="incomplete_tool_call",
                                )
                                return
                            prepared_tool_events.extend(call_events)
                            prepared_tool_calls.append(
                                {
                                    "id": tool_use_id,
                                    "name": tool_name,
                                    "arguments": arguments,
                                }
                            )

                        # Final chunk carries usage stats
                        if chunk.get("done") is True:
                            saw_done = True
                            input_tokens = chunk.get("prompt_eval_count", 0)
                            output_tokens = chunk.get("eval_count", 0)
                            break

                    if not saw_done:
                        message = "Ollama stream ended before done=true"
                        trace.record_error(
                            code="incomplete_stream",
                            message=message,
                            metadata={"phase": "stream", "terminal_field": "done"},
                        )
                        yield ErrorEvent(message=message, code="incomplete_stream")
                        return

                    # Commit the already-validated lifecycle only after the
                    # response's explicit done=true terminal.
                    for tool_event in prepared_tool_events:
                        yield tool_event
                    if candidate_artifact is not None and candidate_artifact.has_calls:
                        artifact_text = candidate_artifact.render_text()
                        if artifact_text:
                            assistant_text_parts.append(artifact_text)
                            yield TextDeltaEvent(text=artifact_text)
                        log.info(
                            "provider.candidate_artifact",
                            provider=self.provider_name,
                            model=self._model,
                            call_count=candidate_artifact.call_count,
                            event_count=candidate_artifact.event_count,
                            char_count=candidate_artifact.char_count,
                            issue_codes=list(candidate_artifact.issue_codes),
                            truncated=False,
                        )

                    trace.record_response(
                        usage={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                        },
                        stop_reason="stop",
                        actual_model=self._model,
                        assistant_text="".join(assistant_text_parts),
                        tool_calls=[
                            {
                                "id": call["id"],
                                "name": call["name"],
                                "arguments": call["arguments"],
                                "arguments_json_valid": True,
                            }
                            for call in prepared_tool_calls
                        ],
                    )
                    yield DoneEvent(
                        stop_reason="stop",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model=self._model,
                        provider=self.provider_id,
                    )

        except CandidateArtifactLimitError as exc:
            message = "Ollama candidate artifact exceeded safety limits"
            trace.record_error(
                code="candidate_artifact_limit_exceeded",
                message=message,
                metadata={
                    "operation": exc.operation,
                    "reason": exc.reason,
                    "limit": exc.limit,
                    "observed": exc.observed,
                },
            )
            log.warning(
                "provider.candidate_artifact_limit",
                provider=self.provider_name,
                model=self._model,
                operation=exc.operation,
                reason=exc.reason,
                limit=exc.limit,
                observed=exc.observed,
            )
            yield ErrorEvent(message=message, code="candidate_artifact_limit_exceeded")
        except httpx.TimeoutException as exc:
            message = redact_upstream_error_text(
                f"Request timed out: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(code="timeout", message=message)
            yield ErrorEvent(message=message, code="timeout")
        except httpx.RequestError as exc:
            message = redact_upstream_error_text(
                f"Request error: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(code="request_error", message=message)
            yield ErrorEvent(message=message, code="request_error")
        except Exception as exc:  # noqa: BLE001 - chat() contract: ErrorEvent instead of raising
            message = redact_upstream_error_text(
                f"Provider response handling failed: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            log.error(
                "provider.stream_internal_error",
                provider=self.provider_name,
                model=self._model,
                error=message,
                exception_type=type(exc).__name__,
            )
            trace.record_error(
                code="provider_internal",
                message=message,
            )
            yield ErrorEvent(
                message=message,
                code="provider_internal",
            )

    async def list_models(self, *, raise_on_error: bool = False) -> list[ModelInfo]:
        """List available models.

        By default any auth/transport failure degrades to an empty list (the
        historical contract every runtime caller relies on). Pass
        ``raise_on_error=True`` to surface the underlying exception instead,
        so callers that must distinguish an unreachable/secured host from an
        empty catalog (e.g. onboarding discovery) can classify it.
        """
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/api/tags",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return [
                    ModelInfo(
                        provider=self.provider_name,
                        model_id=m["name"],
                        display_name=m.get("name", ""),
                        context_window=m.get("details", {}).get("context_length", 0),
                    )
                    for m in data.get("models", [])
                ]
        except httpx.HTTPError as exc:
            if raise_on_error:
                raise redacted_httpx_error(exc, api_key=self._api_key) from None
            return []
        except Exception:
            if raise_on_error:
                raise
            return []
