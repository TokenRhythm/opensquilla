"""Managed MCP connection to the Desktop's existing browser process."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx
import structlog

from opensquilla.browser import DesktopBrowserClient
from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import (
    MCPServerConfig,
    MCPToolDef,
    MCPToolResult,
    current_mcp_call_context,
)
from opensquilla.tools.browser_policy import (
    BROWSER_MCP_REQUIRED_TOOLS,
    BROWSER_MCP_TOOLS,
    browser_context_available,
    browser_network_policy_supported,
    browser_tool_spec,
)
from opensquilla.tools.types import ToolContext, ToolSpec, current_tool_context

_AUTHORITY_ARGUMENTS = frozenset(
    {
        "sessionKey",
        "operationId",
        "_meta",
        "token",
        "endpoint",
        "nativeImageEvidence",
        "observationPolicy",
        "recoveryScope",
        "uploadFile",
    }
)
log = structlog.get_logger(__name__)


class _DesktopBrowserTransportError(RuntimeError):
    """A bounded local transport failure, without response bodies or credentials."""

    def __init__(self, code: str, message: str, **details: int) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _contains_authority_argument(arguments: dict[str, Any]) -> bool:
    pending: list[Any] = [arguments]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if _AUTHORITY_ARGUMENTS.intersection(value):
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _coordinate_batch(name: str, arguments: dict[str, Any]) -> bool:
    actions = arguments.get("actions")
    return name == "browser_batch" and isinstance(actions, list) and any(
        isinstance(action, dict) and ("x" in action or "y" in action) for action in actions
    )


def _vision_available(context: ToolContext) -> bool:
    """Read existing active-model capability; this is not an image delivery receipt."""
    if context.image_analysis_target is None:
        return False
    try:
        return context.image_analysis_target() is not None
    except Exception:  # Capability lookup failure cannot authorize coordinate actions.
        return False


def _coordinate_unavailable(code: str, message: str, recovery: str) -> MCPToolResult:
    result = {
        "ok": False, "code": code, "message": message,
        "outcome": "not_started", "retryable": False, "recovery": recovery,
    }
    text = json.dumps(result)
    return MCPToolResult(
        text, True, content_blocks=[{"type": "text", "text": text}], structured_content=result,
    )


def _transport_failure_result(error: RuntimeError) -> MCPToolResult:
    if isinstance(error, _DesktopBrowserTransportError):
        code, explanation, details = error.code, str(error), error.details
    else:
        code, explanation, details = (
            "BROWSER_TRANSPORT_ERROR", "The Desktop browser connection ended.", {},
        )
    result = {
        "ok": False, "code": code,
        "message": f"{explanation} The operation outcome is unknown; inspect before retrying.",
        "outcome": "unknown", "retryable": False,
        **details,
    }
    text = json.dumps(result)
    return MCPToolResult(
        text, True, content_blocks=[{"type": "text", "text": text}], structured_content=result,
    )


def browser_tool_policy(definition: MCPToolDef, spec: ToolSpec) -> ToolSpec:
    return browser_tool_spec(definition.name, spec)


class DesktopBrowserMCPClient(MCPClient):
    """JSON-response Streamable HTTP client with trusted per-call session metadata.

    The endpoint and bearer token never appear in model-facing configuration.
    Reuses the Desktop's in-memory capability rather than spawning a browser.
    """

    def __init__(self, browser: DesktopBrowserClient) -> None:
        super().__init__(
            MCPServerConfig(
                name="desktop-browser",
                transport="streamable-http",
                tool_timeout_seconds=35,
                description="Use the conversation's built-in browser with Playwright.",
            )
        )
        self._browser = browser
        self._http: httpx.AsyncClient | None = None
        self._next_id = 0
        self._available_tools = BROWSER_MCP_REQUIRED_TOOLS
        self._coordinate_authority = False
        self._attachment_uploads = False

    async def connect(self) -> None:
        await self.close()
        self._http = httpx.AsyncClient(timeout=32, trust_env=False, follow_redirects=False)
        response = await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "opensquilla-gateway", "version": "1.0.0"},
            },
        )
        result = response.get("result", {})
        if not isinstance(result, dict) or result.get("protocolVersion") != "2025-06-18":
            raise RuntimeError("Desktop browser MCP is unavailable or incompatible")
        capabilities = result.get("capabilities")
        experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
        browser = (
            experimental.get("opensquilla/browser") if isinstance(experimental, dict) else None
        )
        self._coordinate_authority = (
            isinstance(browser, dict) and browser.get("coordinateAuthority") == "browser-state"
        )
        self._attachment_uploads = (
            isinstance(browser, dict) and browser.get("attachmentUploads") is True
        )
        await self._request("notifications/initialized", {}, notification=True)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._available_tools = BROWSER_MCP_REQUIRED_TOOLS
        self._coordinate_authority = False
        self._attachment_uploads = False

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        notification: bool = False,
    ) -> dict[str, Any]:
        if self._http is None:
            raise _DesktopBrowserTransportError(
                "BROWSER_DISCONNECTED", "The Desktop browser MCP is disconnected.",
            )
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        try:
            async with self._http.stream(
                "POST",
                self._browser.endpoint + "/mcp",
                json=message,
                headers={
                    "Authorization": f"Bearer {self._browser.token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                    "x-opensquilla-deadline-at-ms": str(int(time.time() * 1000) + 30_000),
                },
            ) as response:
                if notification and response.status_code == 202:
                    return {}
                if response.status_code != 200:
                    raise _DesktopBrowserTransportError(
                        "BROWSER_HTTP_ERROR", "The Desktop browser HTTP request failed.",
                        httpStatus=response.status_code,
                    )
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 12 * 1024 * 1024:
                        raise _DesktopBrowserTransportError(
                            "BROWSER_RESPONSE_TOO_LARGE",
                            "The Desktop browser response exceeded the size limit.",
                        )
            parsed = json.loads(payload)
            if (
                not isinstance(parsed, dict)
                or parsed.get("jsonrpc") != "2.0"
                or isinstance(parsed.get("id"), bool)
                or parsed.get("id") != request_id
                or ("result" in parsed) == ("error" in parsed)
            ):
                raise _DesktopBrowserTransportError(
                    "BROWSER_PROTOCOL_ERROR",
                    "The Desktop browser returned an invalid MCP response.",
                )
            if "error" in parsed:
                error = parsed["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise _DesktopBrowserTransportError(
                    "BROWSER_PROTOCOL_ERROR", "The Desktop browser rejected the MCP request.",
                    **({"rpcCode": code} if type(code) is int else {}),
                )
            if method == "tools/call" and (
                not isinstance(parsed["result"], dict)
                or not isinstance(parsed["result"].get("content"), list)
                or not isinstance(parsed["result"].get("isError", False), bool)
            ):
                raise _DesktopBrowserTransportError(
                    "BROWSER_PROTOCOL_ERROR",
                    "The Desktop browser returned an invalid tool result.",
                )
            return parsed
        except httpx.TimeoutException:
            raise _DesktopBrowserTransportError(
                "BROWSER_TRANSPORT_TIMEOUT", "The Desktop browser request timed out.",
            ) from None
        except httpx.ProtocolError:
            raise _DesktopBrowserTransportError(
                "BROWSER_PROTOCOL_ERROR", "The Desktop browser returned an invalid HTTP response.",
            ) from None
        except httpx.HTTPError:
            raise _DesktopBrowserTransportError(
                "BROWSER_TRANSPORT_ERROR", "The Desktop browser connection ended.",
            ) from None
        except ValueError:
            raise _DesktopBrowserTransportError(
                "BROWSER_PROTOCOL_ERROR", "The Desktop browser returned invalid JSON.",
            ) from None

    async def list_tools(self) -> list[MCPToolDef]:
        response = await self._request("tools/list", {})
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise RuntimeError("Invalid Desktop browser tool catalog")
        definitions: list[MCPToolDef] = []
        names: set[str] = set()
        for entry in result["tools"]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or name not in BROWSER_MCP_TOOLS:
                continue
            if name in names:
                raise RuntimeError("Duplicate Desktop browser tool catalog entry")
            if not isinstance(entry.get("description"), str) or not isinstance(
                entry.get("inputSchema"), dict
            ):
                raise RuntimeError("Invalid Desktop browser tool catalog entry")
            names.add(name)
            definitions.append(MCPToolDef(name, entry["description"], entry["inputSchema"]))
        if not BROWSER_MCP_REQUIRED_TOOLS <= names:
            raise RuntimeError("Incomplete Desktop browser tool catalog")
        self._available_tools = frozenset(names)
        return definitions

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        context = current_tool_context.get()
        call = current_mcp_call_context.get()
        if (
            not browser_context_available(context)
            or context is None
            or context.desktop_browser is not self._browser
        ):
            return MCPToolResult(
                "BROWSER_UNAVAILABLE: This conversation has no built-in browser connection.", True
            )
        if name not in BROWSER_MCP_TOOLS or not call or not call.tool_use_id:
            return MCPToolResult("INVALID_REQUEST: Missing trusted browser call identity.", True)
        if name not in self._available_tools:
            return MCPToolResult(
                "BROWSER_UNSUPPORTED: This Desktop version does not advertise this operation. "
                "Use its available browser tools.",
                True,
            )
        if not browser_network_policy_supported(context):
            return MCPToolResult(
                "BROWSER_POLICY_UNSUPPORTED: Built-in browser automation cannot enforce this "
                "conversation's network restrictions. Use managed web tools instead.",
                True,
            )
        if _contains_authority_argument(arguments):
            return MCPToolResult(
                "INVALID_REQUEST: Browser authority cannot be supplied as tool arguments.", True
            )
        upload_file = None
        if name == "browser_act" and arguments.get("action") == "upload":
            if not self._attachment_uploads:
                return _coordinate_unavailable(
                    "BROWSER_UNSUPPORTED",
                    "This Desktop does not support user attachment uploads. Update the client.",
                    "update_client",
                )
            from opensquilla.tools.browser_attachments import (
                BrowserAttachmentError,
                resolve_browser_upload,
            )

            try:
                upload_file = await resolve_browser_upload(context, arguments.get("fileId"))
            except BrowserAttachmentError as error:
                return _coordinate_unavailable(
                    "BROWSER_ATTACHMENT_UNAVAILABLE", str(error), "choose_attachment",
                )
        if _coordinate_batch(name, arguments):
            if not self._coordinate_authority:
                return _coordinate_unavailable(
                    "BROWSER_CLIENT_UPDATE_REQUIRED",
                    "This Desktop client requires an older image delivery protocol. "
                    "Update the client for coordinate actions; DOM actions remain available.",
                    "update_client_or_use_dom",
                )
            if not _vision_available(context):
                return _coordinate_unavailable(
                    "VISION_UNAVAILABLE",
                    "The active model cannot use visual coordinates. Use DOM refs or "
                    "switch to a model with image input and observe again.",
                    "use_dom",
                )
        operation_id = hashlib.sha256(
            json.dumps(
                [
                    context.session_key,
                    context.usage_root_turn_id or context.task_id,
                    call.tool_use_id,
                ]
            ).encode()
        ).hexdigest()
        # Capture must precede generic image routing: a text-first route may
        # switch to a vision-capable continuation when it receives this result.
        requested_mode = arguments.get("observationMode", "auto")
        log.info(
            "desktop_browser.capture_requested", tool=name, operation_id=operation_id,
            browser_requested_mode=(
                requested_mode if requested_mode in ("auto", "dom") else "invalid"
            ),
        )
        try:
            response = await self._request(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                    "_meta": {
                        "sessionKey": context.session_key,
                        "operationId": operation_id,
                        "recoveryScope": (
                            context.usage_root_turn_id or context.task_id or context.session_key
                        ),
                        "observationMode": requested_mode,
                        **({"uploadFile": upload_file} if upload_file is not None else {}),
                    },
                },
            )
        except RuntimeError as error:
            # A lost response does not prove that a click or form submission failed.
            result = _transport_failure_result(error)
            log.warning(
                "desktop_browser.transport_failure", tool=name, operation_id=operation_id,
                **(result.structured_content or {}),
            )
            return result
        result = MCPToolResult.from_response(response)
        if self._attachment_uploads and name in {
            "browser_tabs", "browser_open", "browser_navigate", "browser_reload",
            "browser_inspect", "browser_observe", "browser_batch", "browser_tab",
        }:
            from opensquilla.tools.browser_attachments import browser_upload_descriptors

            uploads = await browser_upload_descriptors(context)
            if uploads:
                descriptor_text = json.dumps({"availableUploads": uploads}, ensure_ascii=False)
                result.content += "\n" + descriptor_text
                result.content_blocks.append({"type": "text", "text": descriptor_text})
                if result.structured_content is not None:
                    result.structured_content["availableUploads"] = uploads
        log.info(
            "desktop_browser.tool_result", tool=name, operation_id=operation_id,
            image_block_count=sum(block.get("type") == "image" for block in result.content_blocks),
        )
        return result
