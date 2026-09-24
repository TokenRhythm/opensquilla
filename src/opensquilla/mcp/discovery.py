"""MCP tool discovery and registration into OpenSquilla ToolRegistry."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from anyascii import anyascii

from opensquilla.contracts.attachments import (
    IMAGE_ATTACHMENT_BYTES,
    IMAGE_ATTACHMENT_MIMES,
    MAX_ATTACHMENTS,
    normalize_attachment_mime,
)
from opensquilla.contracts.image_validation import validate_image_bytes
from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import (
    MCPCallContext,
    MCPServerConfig,
    MCPToolDef,
    MCPToolResult,
    current_mcp_call_context,
)
from opensquilla.safety.secret_redaction import redact_secret_text, redact_secret_value
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import SafeToolError, ToolSpec, current_tool_context

MCPToolSpecTransform = Callable[[MCPToolDef, ToolSpec], ToolSpec]


@dataclass(frozen=True)
class ActiveMCPClient:
    """Tracked MCP client with the owner that controls its lifecycle."""

    owner: str
    server_name: str
    transport: str
    client: MCPClient
    # Appended defaults preserve construction compatibility for integrations
    # that used the previously exported four-field lifecycle record.
    registry: ToolRegistry | None = field(default=None, repr=False)
    namespace: str = ""
    registered_tools: tuple[str, ...] = ()

    async def close(self) -> None:
        try:
            await self.client.close()
        finally:
            if self.registry is not None:
                for tool_name in self.registered_tools:
                    self.registry.unregister(tool_name)
                if self.namespace:
                    self.registry.unregister_mcp_namespace(self.namespace)


# Module-level registry to keep clients alive for tool handlers.
_active_clients: list[ActiveMCPClient] = []

_PROVIDER_TOOL_NAME_MAX_LENGTH = 64
_MCP_NAMESPACE_MAX_LENGTH = 32
# Leave room beneath the standard curated tool-error envelope's 2,000-character cap.
_MCP_ERROR_SUMMARY_MAX_CHARS = 1800
_JSON_STRING_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"')


def _bounded_name(value: str, *, max_length: int, separator: str) -> str:
    """Bound an identifier while preserving deterministic collision resistance."""

    if len(value) <= max_length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[: max_length - len(digest) - 1]}{separator}{digest}"


def mcp_namespace(server_name: str) -> str:
    """Return a stable provider-safe namespace for one MCP server."""

    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", anyascii(server_name.strip()).lower()).strip("-_")
    normalized = re.sub(r"-+", "-", normalized) or "server"
    return _bounded_name(
        f"mcp__{normalized}",
        max_length=_MCP_NAMESPACE_MAX_LENGTH,
        separator="-",
    )


def mcp_tool_name(namespace: str, tool_name: str) -> str:
    """Return the exact provider-safe callable name for one MCP tool.

    The advertised namespace remains ``mcp__<server>``. A second ``__``
    separates it from the tool component because dots are rejected by common
    function-calling APIs. Tool components that need normalization receive a
    short source-name hash so two distinct MCP names cannot silently alias.
    """

    source_name = tool_name.strip()
    ascii_name = anyascii(source_name)
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", ascii_name)
    normalized = re.sub(r"_+", "_", normalized).strip("-_") or "tool"
    changed = normalized != source_name
    available = _PROVIDER_TOOL_NAME_MAX_LENGTH - len(namespace) - 2
    if available < 10:  # Defensive: mcp_namespace currently guarantees >= 30.
        raise ValueError(f"MCP namespace is too long for a callable tool name: {namespace}")
    if changed:
        digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized}_{digest}"
    normalized = _bounded_name(normalized, max_length=available, separator="_")
    return f"{namespace}__{normalized}"


def active_clients_snapshot() -> tuple[ActiveMCPClient, ...]:
    """Return active MCP clients without exposing mutable runtime state."""
    return tuple(_active_clients)


async def close_active_clients(owner: str | None = None) -> int:
    """Close active MCP clients, optionally scoped to one owner/server name."""
    remaining: list[ActiveMCPClient] = []
    closing: list[ActiveMCPClient] = []
    for entry in _active_clients:
        if owner is None or entry.owner == owner or entry.server_name == owner:
            closing.append(entry)
        else:
            remaining.append(entry)
    _active_clients[:] = remaining

    closed = 0
    for entry in closing:
        try:
            await entry.close()
            closed += 1
        except Exception:
            pass
    return closed


def create_client(config: MCPServerConfig) -> MCPClient:
    """Factory: create the appropriate MCPClient for the given transport."""
    if config.transport == "stdio":
        from opensquilla.mcp.stdio import MCPStdioClient

        return MCPStdioClient(config)
    elif config.transport == "sse":
        from opensquilla.mcp.sse import MCPSSEClient

        return MCPSSEClient(config)
    else:
        raise ValueError(f"Unknown MCP transport: {config.transport!r}")


def _project_tool_result(result: MCPToolResult, tool_use_id: str) -> str:
    """Project text and validated images without leaking binary data into text."""
    parts = [result.content] if result.content else []
    if result.structured_content is not None:
        structured = json.dumps(result.structured_content, ensure_ascii=False)
        # Modern servers may also include a serialized copy for old clients.
        try:
            duplicated = json.loads(result.content) == result.structured_content
        except (ValueError, TypeError):
            duplicated = False
        if not duplicated:
            parts.append(structured)

    context = current_tool_context.get()
    images: list[dict[str, Any]] = []
    total_bytes = 0
    image_count = 0
    for block in result.content_blocks:
        if block.get("type") != "image":
            continue
        image_count += 1
        if image_count > MAX_ATTACHMENTS:
            parts.append("[Additional MCP images omitted: attachment count limit reached.]")
            break
        encoded = block.get("data")
        mime = normalize_attachment_mime(block.get("mimeType"))
        try:
            if mime not in IMAGE_ATTACHMENT_MIMES:
                raise ValueError("unsupported image MIME")
            if not isinstance(encoded, str) or not (
                1 <= len(encoded) <= ((IMAGE_ATTACHMENT_BYTES + 2) // 3) * 4
            ):
                raise ValueError("invalid image size")
            payload = base64.b64decode(encoded, validate=True)
            total_bytes += len(payload)
            if total_bytes > IMAGE_ATTACHMENT_BYTES:
                raise ValueError("image result byte limit exceeded")
            assert mime is not None
            validate_image_bytes(payload, mime)
        except (ValueError, binascii.Error):
            parts.append(
                f"[MCP image {image_count} was not loaded: invalid, unsupported, "
                "or oversized image data. This image was not analyzed.]"
            )
            continue
        if context is None or not tool_use_id:
            parts.append(
                f"[MCP image {image_count} was not loaded: no active model tool call.]"
            )
            continue
        image = {"mime": mime, "data": encoded}
        images.append(image)
    if images and context is not None:
        context.tool_result_media[tool_use_id] = images
        parts.append(
            f"[{len(images)} MCP image(s) loaded for this tool call. "
            "Image delivery depends on the current model's vision capability.]"
        )
    return "\n".join(parts)


def _error_json(value: Any) -> str:
    # Redact values before serialization. Escape assignment separators inside
    # JSON strings so subsequent generic text redaction cannot swallow closing
    # quotes/braces (for example an Authorization header used as a page label).
    encoded = json.dumps(redact_secret_value(value), ensure_ascii=False, separators=(",", ":"))
    return _JSON_STRING_TOKEN.sub(
        lambda match: match.group().replace(":", "\\u003a").replace("=", "\\u003d"), encoded,
    )


def _browser_error_summary(summary: dict[str, Any], structured: dict[str, Any]) -> None:
    """Keep executable recovery identities ahead of large page text and ref lists."""
    observation = structured.get("observation")
    refs = observation.get("refs") if isinstance(observation, dict) else None
    current: dict[str, Any] = {"refsOmitted": len(refs)} if isinstance(refs, list) else {}
    details: dict[str, Any] = {"observation": current} if isinstance(observation, dict) else {}
    summary["result"] = details
    summary["guidance"] = (
        "If an observation is returned, it replaces previous refs. Do not reuse earlier refs. "
        "If fresh refs or metadata are omitted, retrieve the full result or call browser_observe "
        "before acting. Do not repeat an action with unknown outcome."
    )

    def put(target: dict[str, Any], key: str, value: Any) -> bool:
        target[key] = value
        if len(_error_json(summary)) <= _MCP_ERROR_SUMMARY_MAX_CHARS:
            return True
        del target[key]
        return False

    for key in ("code", "outcome", "retryable", "recovery", "targetRef", "causeCode"):
        if key in structured:
            put(details, key, structured[key])
    execution = structured.get("execution")
    if isinstance(execution, dict):
        compact = {key: execution[key] for key in ("state",) if key in execution}
        actions = execution.get("actions")
        if isinstance(actions, list):
            compact["actions"] = [{
                key: action[key]
                for key in ("index", "state", "code", "outcome", "performed") if key in action
            } for action in actions[:3] if isinstance(action, dict)]
        put(details, "execution", compact)
    if not isinstance(observation, dict):
        return
    for key in ("observationId", "consistency", "imageStatus", "image", "viewport", "browserState"):
        if key in observation:
            put(current, key, observation[key])
    if isinstance(refs, list):
        retained: list[Any] = []
        for ref in refs:
            # Refs remain exact records: never truncate an identifier, label, or URL.
            if not put(current, "refs", [*retained, ref]):
                if retained:
                    current["refs"] = retained
                break
            retained.append(ref)
            current["refsOmitted"] = len(refs) - len(retained)


async def _recoverable_error_content(
    result: MCPToolResult, content: str, tool_name: str, tool_use_id: str,
) -> str:
    """Use the existing result store instead of silently clipping MCP error recovery data."""
    if len(content) <= _MCP_ERROR_SUMMARY_MAX_CHARS:
        return content
    try:
        duplicated = json.loads(result.content) == result.structured_content
    except (ValueError, TypeError):
        duplicated = False
    if result.structured_content is not None:
        original_prefix = result.content
        safe_prefix = _error_json(result.structured_content)
        if not duplicated:
            original_parts = [result.content] if result.content else []
            original_parts.append(json.dumps(result.structured_content, ensure_ascii=False))
            original_prefix = "\n".join(original_parts)
            if result.content:
                safe_prefix = redact_secret_text(result.content) + "\n" + safe_prefix
        content = safe_prefix + redact_secret_text(content[len(original_prefix):])
    else:
        content = redact_secret_text(content)
    recovery: dict[str, Any] = {"available": False}
    context = current_tool_context.get()
    writer = context.tool_result_snapshot_writer if context is not None else None
    if (
        writer is not None and context is not None and context.tool_result_retrieval_available
        and tool_use_id.strip()
    ):
        try:
            reference = await writer(content, tool_name, tool_use_id)
        except Exception:  # Best-effort persistence must not obscure the original tool failure.
            reference = None
        if (
            isinstance(reference, dict)
            and isinstance(reference.get("handle"), str)
            and re.fullmatch(r"tr-[0-9a-f]{32}", reference["handle"])
            and reference.get("sha256") == hashlib.sha256(content.encode("utf-8")).hexdigest()
        ):
            recovery = {
                "available": True, "handle": reference["handle"],
                "next_call": {"name": "retrieve_tool_result", "arguments": {
                    "handle": reference["handle"], "mode": "raw_slice", "offset": 0,
                    "limit": 12000,
                }},
            }
    summary: dict[str, Any] = {
        "isError": True, "truncated": True, "originalChars": len(content),
        "content_recovery": recovery,
    }
    if tool_name.startswith("mcp__desktop-browser__") and result.structured_content is not None:
        _browser_error_summary(summary, result.structured_content)
    else:
        summary["preview"] = content[:600]
        summary["guidance"] = (
            "Retrieve the complete error before using omitted result data."
            if recovery["available"] else
            "Full error recovery is unavailable. Request a smaller read-only inspection; "
            "do not blindly repeat a state-changing action."
        )
        while len(_error_json(summary)) > _MCP_ERROR_SUMMARY_MAX_CHARS:
            summary["preview"] = summary["preview"][:len(summary["preview"]) // 2]
    return _error_json(summary)


def _make_tool_handler(
    client: MCPClient,
    namespace: str,
    tool_name: str,
    tool_def: MCPToolDef,
    registry: ToolRegistry,
    timeout_seconds: float,
    spec_transform: MCPToolSpecTransform | None = None,
) -> None:
    """Register a single MCP tool in its server-scoped namespace."""
    # Extract properties and required from input_schema
    schema = tool_def.input_schema
    raw_properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    properties: dict[str, Any] = dict(raw_properties) if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required", []) if isinstance(schema, Mapping) else []
    required = (
        [item for item in raw_required if isinstance(item, str)]
        if isinstance(raw_required, list | tuple)
        else []
    )
    if "_tool_use_id" in properties:
        raise ValueError("MCP input schema uses reserved runtime argument: _tool_use_id")

    spec = ToolSpec(
        name=mcp_tool_name(namespace, tool_name),
        description=tool_def.description,
        parameters=properties,
        required=required,
        execution_timeout_seconds=timeout_seconds + 5.0,
        runtime_only_arguments=frozenset({"_tool_use_id"}),
    )
    if spec_transform is not None:
        spec = spec_transform(tool_def, spec)
        if spec.name != mcp_tool_name(namespace, tool_name):
            raise ValueError("MCP tool policy must preserve the registered tool name")
        if spec.runtime_only_arguments != frozenset({"_tool_use_id"}):
            raise ValueError("MCP tool policy must preserve runtime call identity")

    async def handler(_tool_use_id: str = "", **kwargs: Any) -> str:
        call_token = current_mcp_call_context.set(MCPCallContext(tool_use_id=_tool_use_id))
        try:
            result = await asyncio.wait_for(
                client.call_tool(tool_name, kwargs),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise SafeToolError(
                f"MCP tool '{tool_name}' timed out after {timeout_seconds}s"
            ) from None
        finally:
            current_mcp_call_context.reset(call_token)
        # Keep MCP failures on the standard error path. Large structured results
        # need a bounded recovery summary before the generic exception envelope.
        content = _project_tool_result(result, _tool_use_id)
        if result.is_error:
            content = await _recoverable_error_content(
                result, content, spec.name, _tool_use_id,
            )
            raise SafeToolError(content or f"MCP tool '{tool_name}' failed")
        return content

    registry.register(spec, handler)


async def discover_and_register(
    config: MCPServerConfig,
    registry: ToolRegistry,
    *,
    owner: str | None = None,
) -> list[str]:
    """Connect to MCP server, list tools, register each as a OpenSquilla tool.

    Returns list of registered tool names.
    The client is kept alive in module-level _active_clients so tool handlers can use it.
    """
    client = create_client(config)
    return await register_client_tools(client, registry, owner=owner)


async def register_client_tools(
    client: MCPClient,
    registry: ToolRegistry,
    *,
    owner: str | None = None,
    spec_transform: MCPToolSpecTransform | None = None,
) -> list[str]:
    """Connect and register a runtime-provided MCP client with managed cleanup.

    The optional trusted transform supplies tool policy without placing service
    specific branches in MCP discovery or exposing runtime context as arguments.
    """
    config = client.config
    registered: list[str] = []
    namespace = mcp_namespace(config.name)
    namespace_registered = False
    try:
        await client.connect()
        tools = await client.list_tools()
        registry.register_mcp_namespace(
            namespace,
            config.description or f"Tools provided by the {config.name} MCP server.",
        )
        namespace_registered = True
        for t in tools:
            registered_name = mcp_tool_name(namespace, t.name)
            if registry.get(registered_name) is not None:
                raise ValueError(f"MCP tool is already registered: {registered_name}")
            # Track before registration so a partially failing custom registry
            # implementation is still given an idempotent cleanup attempt.
            registered.append(registered_name)
            _make_tool_handler(
                client,
                namespace,
                t.name,
                t,
                registry,
                timeout_seconds=config.tool_timeout_seconds,
                spec_transform=spec_transform,
            )
        _active_clients.append(
            ActiveMCPClient(
                owner=owner or config.name,
                server_name=config.name,
                transport=config.transport,
                client=client,
                registry=registry,
                namespace=namespace,
                registered_tools=tuple(registered),
            )
        )
    except BaseException:
        for registered_name in registered:
            registry.unregister(registered_name)
        if namespace_registered:
            registry.unregister_mcp_namespace(namespace)
        try:
            await asyncio.shield(client.close())
        except BaseException:
            # Preserve the discovery/cancellation failure. Lifecycle cleanup is
            # best effort, and no registry entry remains callable at this point.
            pass
        raise
    return registered
