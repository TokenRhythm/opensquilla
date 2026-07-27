"""OpenAI Responses API provider path.

This provider intentionally stays separate from the OpenAI-compatible Chat
Completions provider because Responses uses item-shaped input/output and native
state protocols that should evolve independently.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

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
from .failures import retry_after_from_headers
from .openai import _http_error_body_text, _resolve_llm_proxy, _versioned_api_url
from .protocol import ProviderConnectionConfig, ProviderMetadata
from .stream_assembly import ToolStreamAccumulator, ToolStreamProtocolError
from .trace_recorder import LLMTraceRecorder
from .types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
)

_OPENAI_RESPONSES_BASE = "https://api.openai.com/v1"

log = structlog.get_logger(__name__)


def _responses_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": {
            "type": tool.input_schema.type,
            "properties": tool.input_schema.properties,
            "required": tool.input_schema.required,
        },
    }


def _responses_tool_output(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _responses_message_item(role: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": content}


def _responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message.content, str):
            items.append({"role": message.role, "content": message.content})
            continue

        pending_content: list[dict[str, Any]] = []

        def flush_pending_message() -> None:
            if pending_content:
                items.append(_responses_message_item(message.role, list(pending_content)))
                pending_content.clear()

        for block in message.content:
            if isinstance(block, ContentBlockText):
                content_type = "output_text" if message.role == "assistant" else "input_text"
                pending_content.append({"type": content_type, "text": block.text})
            elif isinstance(block, ContentBlockImage):
                # input_image is only valid on user/system input, not assistant
                # output. Drop (with a warning) on assistant turns rather than
                # emit an invalid part.
                if message.role == "assistant":
                    log.warning(
                        "openai_responses.dropped_assistant_image",
                        note="input_image is not valid on assistant output",
                    )
                    continue
                if block.source_type == "url":
                    image_url = block.data
                else:
                    image_url = f"data:{block.media_type};base64,{block.data}"
                pending_content.append({"type": "input_image", "image_url": image_url})
            elif isinstance(block, ContentBlockToolUse):
                flush_pending_message()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    }
                )
            elif isinstance(block, ContentBlockToolResult):
                flush_pending_message()
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": _responses_tool_output(block.content),
                    }
                )
        flush_pending_message()
    return items


def _usage_fields(usage: Any) -> tuple[int, int, int, int]:
    if not isinstance(usage, dict):
        return 0, 0, 0, 0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    input_details = usage.get("input_tokens_details")
    cached_tokens = (
        int(input_details.get("cached_tokens") or 0) if isinstance(input_details, dict) else 0
    )
    output_details = usage.get("output_tokens_details")
    reasoning_tokens = (
        int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, dict) else 0
    )
    return input_tokens, output_tokens, reasoning_tokens, cached_tokens


def _candidate_field_has_content(value: object | None) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict | list | tuple):
        return bool(value)
    return True


def _candidate_malformed_function_call(
    item: dict[str, Any],
) -> dict[str, object] | None:
    """Retain malformed function-call data only when non-structural content remains."""

    sanitized = strip_candidate_tool_identity(item)
    if not isinstance(sanitized, dict):
        return None
    residual = dict(sanitized)
    for field in ("type", "status", "name", "arguments"):
        residual.pop(field, None)
    if not residual:
        return None
    return {"malformed_function_call": sanitized}


class OpenAIResponsesProvider:
    """OpenAI native Responses API provider.

    The initial implementation supports text and function-call event mapping
    with stateless requests (`store: false`). Provider-native compaction/item
    replay is added in later continuity work.
    """

    provider_name = "openai_responses"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.4",
        base_url: str = _OPENAI_RESPONSES_BASE,
        org_id: str | None = None,
        proxy: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        self._api_key = clean_header_secret(api_key, label="LLM API key")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._org_id = org_id
        self._proxy = _resolve_llm_proxy(proxy)
        self.provider_id = (provider_id or self.provider_name).strip()

    @property
    def model(self) -> str:
        return self._model

    def disable_provider_state_replay(self) -> None:
        """Keep the cross-provider replay contract explicit for this adapter.

        Responses requests are already stateless (``store: false``) and do not
        replay provider-native response ids, so no mutable state is required.
        """

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name=self.provider_name,
            provider_kind="openai_responses",
            model=self._model,
            base_url=self._base_url,
            provider_id=self.provider_id,
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        return ProviderConnectionConfig(
            provider_kind="openai_responses",
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def _api_url(self, path: str) -> str:
        return _versioned_api_url(self._base_url, path)

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self.chat_items(
            _responses_input(messages),
            tools=tools,
            config=config or ChatConfig(),
        )

    def chat_items(
        self,
        input_items: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a Responses request from canonical Responses input items."""

        return self._complete_items(input_items, tools=tools, config=config or ChatConfig())

    async def _complete_items(
        self,
        input_items: list[dict[str, Any]],
        *,
        tools: list[ToolDefinition] | None,
        config: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id

        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": config.max_tokens,
            "store": False,
        }
        if config.output_json_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "structured_output",
                    "strict": config.output_json_schema_strict,
                    "schema": config.output_json_schema,
                }
            }
        if config.system:
            payload["instructions"] = config.system
        if config.temperature is not None:
            payload["temperature"] = config.temperature
        if config.stop_sequences:
            payload["stop"] = config.stop_sequences
        if tools:
            payload["tools"] = [_responses_tool(tool) for tool in tools]
            payload["tool_choice"] = config.tool_choice or "auto"
        endpoint = self._api_url("/v1/responses")
        trace = LLMTraceRecorder(
            provider="openai_responses",
            model=self._model,
            base_url=self._base_url,
            endpoint=endpoint,
            stream=False,
        )
        trace.record_request(
            payload=payload,
            headers=headers,
            metadata={"timeout_seconds": config.timeout, "tools_count": len(tools or [])},
        )

        try:
            async with httpx.AsyncClient(
                timeout=config.timeout,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            message = redact_upstream_error_text(
                f"Request timed out: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(code="timeout", message=message)
            yield ErrorEvent(message=message, code="timeout")
            return
        except httpx.RequestError as exc:
            message = redact_upstream_error_text(
                f"Request error: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(code="request_error", message=message)
            yield ErrorEvent(message=message, code="request_error")
            return

        if response.status_code != 200:
            detail = _http_error_body_text(response.text)
            message = f"OpenAI Responses API error {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            message = redact_upstream_error_text(
                message,
                api_key=self._api_key,
                max_len=2000,
            )
            response_body = redact_upstream_error_text(
                response.text,
                api_key=self._api_key,
                max_len=4000,
            )
            trace.record_error(
                code=str(response.status_code),
                message=message,
                status_code=response.status_code,
                response_body=response_body,
            )
            yield ErrorEvent(
                message=message,
                code=str(response.status_code),
                retry_after_s=retry_after_from_headers(
                    response.status_code,
                    getattr(response, "headers", None),
                ),
            )
            return

        try:
            data = response.json()
        except json.JSONDecodeError:
            response_body = redact_upstream_error_text(
                response.text,
                api_key=self._api_key,
                max_len=4000,
            )
            trace.record_error(
                code="invalid_json",
                message="Invalid JSON response from OpenAI Responses API",
                response_body=response_body,
            )
            yield ErrorEvent(
                message="Invalid JSON response from OpenAI Responses API",
                code="invalid_json",
            )
            return

        if not isinstance(data, dict):
            message = "Invalid response object from OpenAI Responses API"
            trace.record_error(
                code="invalid_response",
                message=message,
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
            )
            yield ErrorEvent(message=message, code="invalid_response")
            return

        response_status = data.get("status")
        incomplete_details = data.get("incomplete_details")
        truncated_by_length = (
            response_status == "incomplete"
            and isinstance(incomplete_details, dict)
            and incomplete_details.get("reason") == "max_output_tokens"
        )
        response_completed = response_status == "completed"

        raw_output_items = data.get("output")
        if response_completed and not isinstance(raw_output_items, list):
            message = "OpenAI Responses API completed response has invalid output"
            trace.record_error(
                code="invalid_response",
                message=message,
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
                metadata={"response_status": response_status},
            )
            yield ErrorEvent(message=message, code="invalid_response")
            return
        output_items = raw_output_items if isinstance(raw_output_items, list) else []
        candidate_artifact = (
            CandidateArtifactBuilder()
            if config.candidate_output_mode == "inert_artifact"
            else None
        )
        parsed_tool_arguments: dict[
            int,
            tuple[str, str, str, str, dict[str, Any]],
        ] = {}
        validated_message_text: dict[int, list[str]] = {}
        invalid_tool_call_count = 0
        invalid_output_shape = False
        for item_index, item in enumerate(output_items):
            if not isinstance(item, dict):
                invalid_output_shape = True
                continue
            item_type = item.get("type")
            if item_type == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    invalid_output_shape = True
                    continue
                rendered_parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        invalid_output_shape = True
                        continue
                    part_type = part.get("type")
                    if part_type == "output_text":
                        text = part.get("text")
                        if not isinstance(text, str):
                            invalid_output_shape = True
                        elif text:
                            rendered_parts.append(text)
                    elif part_type == "refusal":
                        refusal = part.get("refusal")
                        if not isinstance(refusal, str):
                            invalid_output_shape = True
                        elif refusal:
                            # Refusal is terminal assistant content, not a tool.
                            rendered_parts.append(refusal)
                    else:
                        invalid_output_shape = True
                validated_message_text[item_index] = rendered_parts
                continue
            if item_type != "function_call":
                # Unknown but well-shaped output item types are provider state
                # that this adapter does not need to surface.
                continue
            if candidate_artifact is not None and (
                response_completed or truncated_by_length
            ):
                candidate_name = item.get("name")
                candidate_arguments = item.get("arguments")
                if (
                    not _candidate_field_has_content(candidate_name)
                    and not _candidate_field_has_content(candidate_arguments)
                ):
                    candidate_arguments = _candidate_malformed_function_call(item)
                try:
                    if truncated_by_length:
                        candidate_artifact.append_or_start(
                            item_index,
                            name_fragment=candidate_name,
                            arguments_fragment=candidate_arguments,
                        )
                    else:
                        candidate_artifact.observe_call(
                            item_index,
                            name_text=candidate_name,
                            arguments=candidate_arguments,
                        )
                except CandidateArtifactLimitError as exc:
                    message = "OpenAI Responses candidate artifact exceeded safety limits"
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
                    yield ErrorEvent(
                        message=message,
                        code="candidate_artifact_limit_exceeded",
                    )
                    return
                continue
            if not response_completed:
                continue
            raw_call_id = item.get("call_id")
            raw_item_id = item.get("id")
            if (
                raw_call_id is not None
                and (not isinstance(raw_call_id, str) or not raw_call_id.strip())
            ) or (
                raw_item_id is not None
                and (not isinstance(raw_item_id, str) or not raw_item_id.strip())
            ):
                invalid_tool_call_count += 1
                continue
            tool_name = item.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                invalid_tool_call_count += 1
                continue
            raw_arguments = item.get("arguments")
            if raw_arguments is None:
                raw_arguments = ""
            if not isinstance(raw_arguments, str):
                invalid_tool_call_count += 1
                continue
            try:
                arguments = (
                    json.loads(
                        raw_arguments,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(value)
                        ),
                    )
                    if raw_arguments.strip()
                    else {}
                )
            except (
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                invalid_tool_call_count += 1
                continue
            if not isinstance(arguments, dict):
                invalid_tool_call_count += 1
                continue
            try:
                json.dumps(arguments, allow_nan=False)
            except (RecursionError, TypeError, ValueError):
                invalid_tool_call_count += 1
                continue
            call_id = raw_call_id or raw_item_id or f"call_{uuid4().hex[:12]}"
            key = raw_item_id or call_id
            parsed_tool_arguments[item_index] = (
                call_id,
                key,
                tool_name,
                raw_arguments,
                arguments,
            )

        if invalid_output_shape:
            message = "OpenAI Responses API response has malformed output items"
            trace.record_error(
                code="invalid_response",
                message=message,
                metadata={"response_status": response_status},
            )
            yield ErrorEvent(message=message, code="invalid_response")
            return
        if response_completed and "error" in data and data["error"] is not None:
            message = "OpenAI Responses API completed response contains an error"
            trace.record_error(code="invalid_response", message=message)
            yield ErrorEvent(message=message, code="invalid_response")
            return
        if response_completed and invalid_tool_call_count:
            message = "OpenAI Responses API response ended with an incomplete tool call"
            trace.record_error(
                code="incomplete_tool_call",
                message=message,
                metadata={"invalid_tool_calls": invalid_tool_call_count},
            )
            for item_index in range(len(output_items)):
                for text in validated_message_text.get(item_index, []):
                    yield TextDeltaEvent(text=text)
            yield ErrorEvent(message=message, code="incomplete_tool_call")
            return

        tools_acc = ToolStreamAccumulator()
        prepared_tool_events: dict[int, list[StreamEvent]] = {}
        assistant_text_parts: list[str] = []
        trace_tool_calls: list[dict[str, Any]] = []
        if response_completed:
            try:
                for item_index, (
                    call_id,
                    key,
                    tool_name,
                    arguments_text,
                    arguments,
                ) in parsed_tool_arguments.items():
                    events = tools_acc.start(
                        key,
                        tool_use_id=call_id,
                        tool_name=tool_name,
                    )
                    if arguments_text:
                        events.extend(tools_acc.append(key, arguments_text))
                    events.extend(tools_acc.finish_with_arguments(key, arguments))
                    prepared_tool_events[item_index] = events
                    trace_tool_calls.append(
                        {
                            "id": call_id,
                            "name": tool_name,
                            "arguments_raw": arguments_text,
                            "arguments_json_valid": True,
                            "arguments": arguments,
                        }
                    )
            except ToolStreamProtocolError as exc:
                message = "OpenAI Responses API returned an invalid tool lifecycle"
                trace.record_error(
                    code="incomplete_tool_call",
                    message=message,
                    metadata={"reason": exc.reason},
                )
                for item_index in range(len(output_items)):
                    for text in validated_message_text.get(item_index, []):
                        yield TextDeltaEvent(text=text)
                yield ErrorEvent(message=message, code="incomplete_tool_call")
                return

        emitted_tool = bool(prepared_tool_events)
        for item_index, item in enumerate(output_items):
            for text in validated_message_text.get(item_index, []):
                assistant_text_parts.append(text)
                yield TextDeltaEvent(text=text)
            for tool_event in prepared_tool_events.get(item_index, []):
                yield tool_event

        input_tokens, output_tokens, reasoning_tokens, cached_tokens = _usage_fields(
            data.get("usage")
        )
        actual_model = data.get("model") or self._model

        # A token-capped response is deliberately non-executable: keep partial
        # text and the length stop reason so the turn loop can request a
        # continuation, but never expose a partial function call as a tool.
        if truncated_by_length:
            stop_reason = "length"
        elif not response_completed:
            error = data.get("error")
            if response_status == "failed":
                code = (
                    str(error.get("code") or "response_failed")
                    if isinstance(error, dict)
                    else "response_failed"
                )
                message = (
                    str(error.get("message") or "OpenAI Responses API response failed")
                    if isinstance(error, dict)
                    else "OpenAI Responses API response failed"
                )
                message = redact_upstream_error_text(
                    message,
                    api_key=self._api_key,
                    max_len=2000,
                )
            elif response_status == "cancelled":
                code = "response_cancelled"
                message = "OpenAI Responses API response was cancelled"
            elif response_status == "incomplete":
                code = "response_incomplete"
                reason = (
                    incomplete_details.get("reason")
                    if isinstance(incomplete_details, dict)
                    else None
                )
                message = f"OpenAI Responses API response was incomplete: {reason or 'unknown'}"
            else:
                code = "invalid_response_status"
                status = response_status if isinstance(response_status, str) else "missing"
                message = f"OpenAI Responses API returned invalid status: {status}"
            code = redact_upstream_error_code(
                code,
                api_key=self._api_key,
            )
            message = redact_upstream_error_text(
                message,
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code=code,
                message=message,
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
                metadata={"response_status": response_status},
            )
            yield ErrorEvent(message=message, code=code)
            return
        elif invalid_tool_call_count:
            message = "OpenAI Responses API response ended with an incomplete tool call"
            trace.record_error(
                code="incomplete_tool_call",
                message=message,
                metadata={"invalid_tool_calls": invalid_tool_call_count},
            )
            yield ErrorEvent(message=message, code="incomplete_tool_call")
            return
        elif emitted_tool or (
            candidate_artifact is not None and candidate_artifact.has_calls
        ):
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"
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
            response=data,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
            },
            stop_reason=stop_reason,
            actual_model=actual_model,
            assistant_text="".join(assistant_text_parts),
            tool_calls=trace_tool_calls,
            response_ids=[str(data["id"])] if data.get("id") else [],
        )
        yield DoneEvent(
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            model=actual_model,
            provider=self.provider_id,
        )

    async def list_models(self, *, raise_on_error: bool = False) -> list[ModelInfo]:
        """List available models.

        By default any auth/transport failure degrades to an empty list (the
        historical contract every runtime caller relies on). Pass
        ``raise_on_error=True`` to surface the underlying exception instead,
        so callers that must distinguish a wrong key from an empty catalog
        (e.g. onboarding discovery) can classify it.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                response = await client.get(self._api_url("/v1/models"), headers=headers)
        except httpx.HTTPError as exc:
            if raise_on_error:
                raise redacted_httpx_error(exc, api_key=self._api_key) from None
            return []

        if response.status_code != 200:
            if raise_on_error:
                # 4xx/5xx raise a classifiable HTTPStatusError; an unexpected
                # non-200 success shape still degrades to the empty list.
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise redacted_httpx_error(exc, api_key=self._api_key) from None
            return []
        try:
            data = response.json()
        except json.JSONDecodeError:
            if raise_on_error:
                raise
            return []

        models: list[ModelInfo] = []
        for raw in data.get("data", []):
            model_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(model_id, str):
                models.append(
                    ModelInfo(
                        provider=self.provider_name,
                        model_id=model_id,
                        display_name=raw.get("name") or model_id,
                    )
                )
        return models

    async def compact_window(
        self,
        input_items: list[dict[str, Any]],
        *,
        config: ChatConfig | None = None,
    ) -> dict[str, Any]:
        """Call `/responses/compact` and return the raw compact response.

        The returned `output` is an opaque canonical context window. Callers
        must store and later replay it without pruning or inspecting internals.
        """

        cfg = config or ChatConfig()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id
        endpoint = self._api_url("/v1/responses/compact")
        payload = {"model": self._model, "input": input_items}
        trace = LLMTraceRecorder(
            provider="openai_responses",
            model=self._model,
            base_url=self._base_url,
            endpoint=endpoint,
            stream=False,
        )
        trace.record_request(
            payload=payload,
            headers=headers,
            metadata={"timeout_seconds": cfg.timeout, "operation": "compact_window"},
        )

        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            message = redact_upstream_error_text(
                f"Request timed out: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code="timeout",
                message=message,
                metadata={"operation": "compact_window"},
            )
            raise redacted_httpx_error(exc, api_key=self._api_key) from None
        except httpx.RequestError as exc:
            message = redact_upstream_error_text(
                f"Request error: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code="request_error",
                message=message,
                metadata={"operation": "compact_window"},
            )
            raise redacted_httpx_error(exc, api_key=self._api_key) from None

        if response.status_code != 200:
            detail = _http_error_body_text(response.text)
            message = f"OpenAI Responses compact API error {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            message = redact_upstream_error_text(
                message,
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code=str(response.status_code),
                message=message,
                status_code=response.status_code,
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
                metadata={"operation": "compact_window"},
            )
            raise RuntimeError(message)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            trace.record_error(
                code="invalid_json",
                message="Invalid JSON response from OpenAI Responses compact API",
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
                metadata={"operation": "compact_window"},
            )
            raise RuntimeError("Invalid JSON response from OpenAI Responses compact API") from exc
        if not isinstance(data, dict):
            trace.record_error(
                code="invalid_shape",
                message="Invalid response shape from OpenAI Responses compact API",
                metadata={"operation": "compact_window"},
            )
            raise RuntimeError("Invalid response shape from OpenAI Responses compact API")
        trace.record_response(
            response=data,
            actual_model=data.get("model") or self._model,
            response_ids=[str(data["id"])] if data.get("id") else [],
            metadata={"operation": "compact_window"},
        )
        return data
