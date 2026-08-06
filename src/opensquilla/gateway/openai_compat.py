"""OpenAI-compatible relay surface for third-party clients.

Registers ``POST /v1/chat/completions`` and ``GET /v1/models`` on the
gateway so OpenAI-protocol clients (astrbot, one-api, chatbox, ...) can
drive the configured LLM deployments instead of getting a 404 on every
OpenAI-shaped request (issues #978 / #979).

Design (agreed with maintainers):

- **Auth** — the relay implements no authentication of its own.  ``/v1/*``
  is part of the gateway control plane, so the global ``AuthMiddleware``
  applies: ``auth.mode = token|password|trusted-proxy`` protects the
  relay exactly like the rest of the control plane.  In the default
  ``auth.mode = "none"`` the middleware passes everything through, so the
  relay keeps a loopback-only guard: remote peers get 403, keeping the
  gateway's default bind scope as the safety boundary.  Browser
  cross-origin requests are rejected in both cases.
- **Routing** — the request ``model`` is resolved against the runtime LLM
  (``llm``) and the Squilla Router tier ladder (``squilla_router.tiers``).
  Unknown models get an OpenAI-style 404.  An omitted model uses the
  runtime default model.
- **Streaming** — ``stream: true`` replies in OpenAI SSE chunk format
  ending with ``data: [DONE]``; ``stream: false`` (default) returns a
  single JSON completion.  Request ``tools`` are forwarded and tool calls
  surface as OpenAI ``tool_calls`` (the client executes them — standard
  OpenAI semantics; the relay never runs tools or approvals).
- **Accounting** — v1 does not write the usage ledger: existing ledger
  sinks are session-scoped and a raw relay has no session.  Out of scope
  unless a non-session global sink is added (see design proposal).
- **Exposure** — disabled by default (``gateway.openai_compat.enabled =
  false``); operators opt in explicitly.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

log = structlog.get_logger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_OPENAI_SCHEMA_KEYS = ("type", "properties", "required", "additionalProperties")
_INVALID_REQUEST_ERROR = "invalid_request_error"
_SERVER_ERROR_TYPE = "server_error"


def _openai_error(
    message: str,
    error_type: str = _INVALID_REQUEST_ERROR,
    code: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message, "type": error_type}
    if code:
        payload["code"] = code
    return {"error": payload}


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    return host in _LOOPBACK_HOSTS


def _guard_default_auth(config: Any, request: Request) -> tuple[int, str] | None:
    """Return ``(status_code, error_json)`` when the relay must refuse, else None.

    In ``auth.mode = "none"`` (the default) the global ``AuthMiddleware``
    passes every request through, so the relay enforces the loopback-only
    safety boundary itself.  In every other auth mode the middleware has
    already authenticated the request before it reaches the relay.
    """
    if getattr(config.auth, "mode", "none") == "none" and not _is_loopback(request):
        return (
            403,
            json.dumps(
                _openai_error(
                    "This OpenAI-compatible endpoint is loopback-only unless "
                    "gateway authentication is configured (auth.mode = token "
                    "or trusted-proxy)",
                    "authentication_error",
                    "api_key_required",
                )
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def resolve_relay_target(config: Any, model: str | None) -> tuple[str, str, str, str, str] | None:
    """Resolve ``(provider, model, api_key, base_url, proxy)`` for one request.

    ``model`` is matched against the runtime LLM and the Squilla Router
    tier ladder.  ``None``/empty model uses the runtime default.  Returns
    ``None`` when the model is not configured anywhere (caller replies 404).
    """
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    runtime = resolve_llm_runtime_config(config)
    if not model or model == runtime.model:
        return runtime.provider, runtime.model, runtime.api_key, runtime.base_url, runtime.proxy

    from opensquilla.provider.deployment import resolve_provider_deployment

    tiers = getattr(config.squilla_router, "tiers", None) or {}
    for tier in tiers.values():
        if not isinstance(tier, dict):
            continue
        if tier.get("model") != model:
            continue
        provider_id = str(tier.get("provider") or runtime.provider)
        profiles = getattr(config, "llm_profiles", None) or {}
        if provider_id == runtime.provider and provider_id not in profiles:
            # The runtime LLM's own credentials are authoritative for its
            # provider (llm.api_key / api_key_env may not exist as a profile).
            return runtime.provider, model, runtime.api_key, runtime.base_url, runtime.proxy
        resolution = resolve_provider_deployment(config, provider_id, model)
        if resolution.ready and resolution.provider_config is not None:
            pc = resolution.provider_config
            return provider_id, model, pc.api_key, pc.base_url, pc.proxy
        return None
    return None


def _relay_model_ids(config: Any) -> list[str]:
    """Models the relay can serve: runtime default + tier ladder (dedup)."""
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    ids: list[str] = []
    try:
        runtime = resolve_llm_runtime_config(config)
        if runtime.model:
            ids.append(runtime.model)
    except Exception:
        pass
    tiers = getattr(config.squilla_router, "tiers", None) or {}
    for tier in tiers.values():
        if isinstance(tier, dict) and tier.get("model"):
            model_id = str(tier["model"])
            if model_id not in ids:
                ids.append(model_id)
    return ids


# ---------------------------------------------------------------------------
# Request conversion (OpenAI wire format → internal types)
# ---------------------------------------------------------------------------


def _user_content(content: Any) -> str | list[Any]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    blocks: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            blocks.append(ContentBlockText(text=str(part.get("text") or "")))
        elif part_type == "image_url":
            image_url = part.get("image_url") or {}
            url = str(image_url.get("url") or "")
            if url.startswith("data:"):
                media_type = url.split(";", 1)[0].removeprefix("data:")
                blocks.append(
                    ContentBlockImage(
                        source_type="base64",
                        media_type=media_type or "image/png",
                        data=url.split(",", 1)[-1],
                    )
                )
            else:
                blocks.append(
                    ContentBlockImage(source_type="url", media_type="image/png", data=url)
                )
    return blocks


def _assistant_content(message: dict[str, Any]) -> str | list[Any]:
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if isinstance(content, str) and not tool_calls:
        return content
    blocks: list[Any] = []
    if isinstance(content, str) and content:
        blocks.append(ContentBlockText(text=content))
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            blocks.append(
                ContentBlockToolUse(
                    id=str(call.get("id") or f"call_{secrets.token_hex(8)}"),
                    name=str(fn.get("name") or ""),
                    input=arguments if isinstance(arguments, dict) else {},
                )
            )
    return blocks


def _messages_from_openai(openai_messages: list[Any]) -> tuple[str, list[Message]]:
    """Convert OpenAI ``messages`` into ``(system_prompt, internal messages)``."""
    system_parts: list[str] = []
    internal: list[Message] = []
    tool_results: list[ContentBlockToolResult] = []

    def flush_tool_results() -> None:
        nonlocal tool_results
        if tool_results:
            internal.append(Message(role="user", content=list(tool_results)))
            tool_results = []

    for raw in openai_messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "")
        content = raw.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(str(part.get("text") or ""))
            continue
        if role == "tool":
            tool_results.append(
                ContentBlockToolResult(
                    tool_use_id=str(raw.get("tool_call_id") or ""),
                    content=_tool_result_content(raw.get("content")),
                    is_error=False,
                )
            )
            continue
        # user / assistant — flush any pending tool results first so they
        # stay in a dedicated message (provider payload builders require
        # tool results to never coexist with text/image blocks).
        flush_tool_results()
        if role == "user":
            internal.append(Message(role="user", content=_user_content(content)))
        elif role == "assistant":
            internal.append(Message(role="assistant", content=_assistant_content(raw)))
    flush_tool_results()
    return "\n\n".join(part for part in system_parts if part), internal


def _tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        if text_parts:
            return "\n".join(text_parts)
        return json.dumps(content, ensure_ascii=False)
    return str(content or "")


def _tools_from_openai(tools: Any) -> list[ToolDefinition]:
    if not isinstance(tools, list):
        return []
    definitions: list[ToolDefinition] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = {}
        definitions.append(
            ToolDefinition(
                name=str(fn.get("name") or ""),
                description=str(fn.get("description") or ""),
                input_schema=ToolInputSchema(
                    **{key: schema[key] for key in _OPENAI_SCHEMA_KEYS if key in schema}
                ),
            )
        )
    return [definition for definition in definitions if definition.name]


# ---------------------------------------------------------------------------
# Response conversion (internal events → OpenAI wire format)
# ---------------------------------------------------------------------------


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _map_stop_reason(stop_reason: str) -> str:
    if stop_reason in ("tool_use", "tool_calls"):
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    return "stop"


async def _relay_completion(
    target: tuple[str, str, str, str, str],
    *,
    model: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    chat_config: ChatConfig,
) -> dict[str, Any]:
    provider = build_provider(*target[:4], proxy=target[4])
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    done: DoneEvent | None = None
    async for event in provider.chat(messages, tools=tools or None, config=chat_config):
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.text)
        elif isinstance(event, ToolUseEndEvent):
            tool_calls.append(
                {
                    "id": event.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": event.tool_name,
                        "arguments": json.dumps(event.arguments, ensure_ascii=False),
                    },
                }
            )
        elif isinstance(event, DoneEvent):
            done = event
        elif isinstance(event, ErrorEvent):
            raise _RelayProviderError(event)
    if done is None:
        raise _RelayProviderError(
            ErrorEvent(
                message="Provider stream ended without a terminal event",
                code="incomplete_stream",
            )
        )
    content = "".join(text_parts) or None
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": _map_stop_reason(done.stop_reason),
            }
        ],
        "usage": {
            "prompt_tokens": done.input_tokens,
            "completion_tokens": done.output_tokens,
            "total_tokens": done.input_tokens + done.output_tokens,
        },
    }


class _RelayProviderError(Exception):
    """A provider ErrorEvent observed while relaying a completion."""

    def __init__(self, event: ErrorEvent) -> None:
        super().__init__(event.message)
        self.event = event


async def _relay_stream(
    target: tuple[str, str, str, str, str],
    *,
    model: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    chat_config: ChatConfig,
):
    """Yield OpenAI SSE ``data:`` lines for one streamed completion."""
    provider = build_provider(*target[:4], proxy=target[4])
    completion_id = _completion_id()
    created = int(time.time())

    def chunk(choices: list[dict[str, Any]], *, usage: dict[str, int] | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices,
            "usage": usage,
        }
        return f"data: {json.dumps(payload)}\n\n"

    yield chunk([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}])
    tool_call_index = 0
    text_parts: list[str] = []
    done: DoneEvent | None = None
    async for event in provider.chat(messages, tools=tools or None, config=chat_config):
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.text)
            yield chunk([{"index": 0, "delta": {"content": event.text}, "finish_reason": None}])
        elif isinstance(event, ToolUseStartEvent):
            tool_call = {
                "index": tool_call_index,
                "id": event.tool_use_id,
                "type": "function",
                "function": {"name": event.tool_name, "arguments": ""},
            }
            yield chunk([{"index": 0, "delta": {"tool_calls": [tool_call]}, "finish_reason": None}])
        elif isinstance(event, ToolUseDeltaEvent):
            tool_call = {"index": tool_call_index, "function": {"arguments": event.json_fragment}}
            yield chunk([{"index": 0, "delta": {"tool_calls": [tool_call]}, "finish_reason": None}])
        elif isinstance(event, ToolUseEndEvent):
            tool_call_index += 1
        elif isinstance(event, DoneEvent):
            done = event
        elif isinstance(event, ErrorEvent):
            error_payload = _openai_error(event.message, _SERVER_ERROR_TYPE, event.code)
            yield f"data: {json.dumps(error_payload)}\n\n"
            yield "data: [DONE]\n\n"
            return
    finish_reason = _map_stop_reason(done.stop_reason) if done is not None else "stop"
    usage = (
        {
            "prompt_tokens": done.input_tokens,
            "completion_tokens": done.output_tokens,
            "total_tokens": done.input_tokens + done.output_tokens,
        }
        if done is not None
        else None
    )
    yield chunk([{"index": 0, "delta": {}, "finish_reason": finish_reason}], usage=usage)
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


async def _handle_chat_completions(request: Request, config: Any) -> Response:
    started = time.monotonic()

    def finish(response: Response, *, model: str = "") -> Response:
        log.info(
            "openai_compat.request",
            method=request.method,
            path=request.url.path,
            model=model or None,
            status=response.status_code,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return response

    guard = _guard_default_auth(config, request)
    if guard is not None:
        status, payload = guard
        return finish(Response(payload, status_code=status, media_type="application/json"))
    if request.headers.get("origin"):
        # Browser-mediated cross-origin calls cannot authenticate as an
        # OpenAI client; keep CSRF off the relay in both auth modes.
        return finish(
            JSONResponse(_openai_error("Cross-origin requests are not allowed"), status_code=403)
        )
    try:
        body = await request.json()
    except Exception:
        return finish(JSONResponse(_openai_error("Invalid JSON body"), status_code=400))
    if not isinstance(body, dict):
        return finish(
            JSONResponse(_openai_error("Request body must be a JSON object"), status_code=400)
        )

    model = body.get("model")
    target = resolve_relay_target(config, model)
    if target is None:
        return finish(
            JSONResponse(
                _openai_error(
                    f"The model `{model}` does not exist or is not configured",
                    code="model_not_found",
                ),
                status_code=404,
            ),
            model=str(model or ""),
        )
    model = target[1]

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        return finish(
            JSONResponse(_openai_error("messages must be a non-empty array"), status_code=400),
            model=model,
        )
    system, messages = _messages_from_openai(raw_messages)
    if not messages:
        return finish(
            JSONResponse(_openai_error("messages must be a non-empty array"), status_code=400),
            model=model,
        )
    tools = _tools_from_openai(body.get("tools"))

    try:
        max_tokens = int(body.get("max_tokens") or 16384)
        timeout = float(body.get("timeout") or 120.0)
    except (TypeError, ValueError):
        return finish(
            JSONResponse(
                _openai_error("max_tokens and timeout must be numbers"),
                status_code=400,
            ),
            model=model,
        )

    chat_config = ChatConfig(
        system=system or None,
        temperature=body.get("temperature"),
        max_tokens=max_tokens,
        timeout=timeout,
    )

    try:
        if body.get("stream") is True:
            streaming_started = time.monotonic()

            async def _streaming_wrapper():
                try:
                    async for line in _relay_stream(
                        target,
                        model=model,
                        messages=messages,
                        tools=tools,
                        chat_config=chat_config,
                    ):
                        yield line
                finally:
                    log.info(
                        "openai_compat.request",
                        method=request.method,
                        path=request.url.path,
                        model=model,
                        status=200,
                        streaming=True,
                        duration_ms=round((time.monotonic() - streaming_started) * 1000, 1),
                    )

            return StreamingResponse(
                _streaming_wrapper(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result = await _relay_completion(
            target,
            model=model,
            messages=messages,
            tools=tools,
            chat_config=chat_config,
        )
        return finish(JSONResponse(result), model=model)
    except _RelayProviderError as exc:
        return finish(
            JSONResponse(
                _openai_error(exc.event.message, _SERVER_ERROR_TYPE, exc.event.code),
                status_code=502,
            ),
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 - relay must answer with an OpenAI envelope
        return finish(
            JSONResponse(
                _openai_error(f"Provider request failed: {exc}", _SERVER_ERROR_TYPE),
                status_code=502,
            ),
            model=model,
        )


async def _handle_models(request: Request, config: Any) -> Response:
    started = time.monotonic()

    def finish(response: Response, *, model: str = "") -> Response:
        log.info(
            "openai_compat.request",
            method=request.method,
            path=request.url.path,
            model=model or None,
            status=response.status_code,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return response

    guard = _guard_default_auth(config, request)
    if guard is not None:
        status, payload = guard
        return finish(Response(payload, status_code=status, media_type="application/json"))
    if request.headers.get("origin"):
        return finish(
            JSONResponse(_openai_error("Cross-origin requests are not allowed"), status_code=403)
        )
    rows = [
        {"id": model_id, "object": "model", "created": 0, "owned_by": "opensquilla"}
        for model_id in _relay_model_ids(config)
    ]
    return finish(JSONResponse({"object": "list", "data": rows}))


def openai_compat_routes(config: Any) -> list[Any]:
    """Return the OpenAI-compatible relay routes for the gateway app.

    Empty when ``gateway.openai_compat.enabled`` is false.
    """
    if not getattr(config.openai_compat, "enabled", False):
        return []
    from starlette.routing import Route

    async def chat_completions(request: Request) -> Response:
        return await _handle_chat_completions(request, config)

    async def models(request: Request) -> Response:
        return await _handle_models(request, config)

    return [
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
    ]
