"""Managed MCP connection to the Desktop's existing browser process."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from copy import deepcopy
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
        "exportPdf",
    }
)
_VALIDATION_FIELD = re.compile(
    r"(?:\$|[A-Za-z_][A-Za-z0-9_$]*(?:\.(?:[A-Za-z_][A-Za-z0-9_$]*|[0-9]+))*)"
)
_VALIDATION_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_VALIDATION_EXPECTATIONS = {
    "required": frozenset({"present"}),
    "type": frozenset({"object", "array", "string", "boolean", "finite_number", "integer"}),
    "range": frozenset({"within_bounds"}),
    "enum": frozenset({"supported_value"}),
    "unknown_field": frozenset({"known_fields"}),
    "conflict": frozenset({"exclusive_fields"}),
    "dependency": frozenset({"related_fields"}),
    "order": frozenset({"field_update_before_final"}),
}
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


def _safe_validation_field(field: Any) -> bool:
    if not isinstance(field, str) or len(field) > 128 or not _VALIDATION_FIELD.fullmatch(field):
        return False
    parts = field.split(".")
    if any(part in _AUTHORITY_ARGUMENTS for part in parts):
        return False
    # Keep array paths canonical so a server cannot smuggle arbitrary syntax
    # into the model-facing diagnostic while still accepting future indexes.
    return not any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in parts)


def _safe_validation_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_VALIDATION_TOKEN.fullmatch(value))


def _field_in_schema(field: str, schema: dict[str, Any] | None) -> bool:
    if field == "$":
        return True
    node: Any = schema
    for part in field.split("."):
        if not isinstance(node, dict):
            return False
        if part.isdigit():
            maximum = node.get("maxItems")
            if node.get("type") != "array" or (
                type(maximum) is int and int(part) >= maximum
            ):
                return False
            node = node.get("items")
        else:
            properties = node.get("properties")
            if not isinstance(properties, dict) or part not in properties:
                return False
            node = properties[part]
    return True


def _argument_validation_result(
    error: Any, schema: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Decode only the bounded, value-free contract validation error format."""
    if (
        not isinstance(error, dict) or type(error.get("code")) is not int
        or error["code"] != -32602 or not isinstance(error.get("message"), str)
    ):
        return None
    data = error.get("data")
    if not isinstance(data, dict) or set(data) != {
        "code", "phase", "contractVersion", "outcome", "retryable", "issues",
    }:
        return None
    if (
        data["code"] != "INVALID_REQUEST" or data["phase"] != "argument_validation"
        or type(data["contractVersion"]) is not int or data["contractVersion"] != 1
        or data["outcome"] != "not_started" or data["retryable"] is not False
        or not isinstance(data["issues"], list) or not 1 <= len(data["issues"]) <= 16
    ):
        return None
    issues = []
    descriptions = []
    for issue in data["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"field", "rule", "expected"}:
            return None
        field, rule, expected = issue["field"], issue["rule"], issue["expected"]
        if not _safe_validation_field(field) or not _safe_validation_token(rule):
            return None
        if not _safe_validation_token(expected):
            return None
        if not _field_in_schema(field, schema):
            field = "$"
        if expected not in _VALIDATION_EXPECTATIONS.get(rule, ()):
            rule, expected = "unrecognized", "see_tool_schema"
        issues.append({"field": field, "rule": rule, "expected": expected})
        descriptions.append(f"{field}: {rule}; expected {expected}")
    result = {
        "ok": False, "code": "INVALID_REQUEST", "phase": "argument_validation",
        "contractVersion": 1, "outcome": "not_started", "retryable": False,
        "issues": issues,
        "message": "Browser arguments were rejected before execution. "
        "Correct the listed fields using the tool schema: " + "; ".join(descriptions) + ".",
    }
    return {
        "isError": True, "structuredContent": result,
        "content": [{"type": "text", "text": json.dumps(result)}],
    }


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


def _project_browser_error_summary(
    summary: dict[str, Any],
    structured: dict[str, Any],
    *,
    serialize: Callable[[Any], str],
    max_chars: int,
) -> bool:
    observation = structured.get("observation")
    refs = observation.get("refs") if isinstance(observation, dict) else None
    current: dict[str, Any] = {"refsOmitted": len(refs)} if isinstance(refs, list) else {}
    details: dict[str, Any] = {"observation": current} if isinstance(observation, dict) else {}
    summary["result"] = details
    summary["guidance"] = (
        "Observation replaces previous refs. Retrieve omitted refs or call browser_observe "
        "before acting. Inspect unknown outcomes before retrying."
    )

    def put(target: dict[str, Any], key: str, value: Any) -> bool:
        target[key] = value
        if len(serialize(summary)) <= max_chars:
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
    if isinstance(observation, dict):
        for key in (
            "observationId", "consistency", "imageStatus", "image", "viewport", "browserState",
        ):
            if key in observation:
                put(current, key, observation[key])
        if isinstance(refs, list):
            retained: list[Any] = []
            for ref in refs:
                # Keep each retained ref intact; an abbreviated identity cannot be acted on.
                if not put(current, "refs", [*retained, ref]):
                    if retained:
                        current["refs"] = retained
                    break
                retained.append(ref)
                current["refsOmitted"] = len(refs) - len(retained)
    return True


def browser_tool_policy(definition: MCPToolDef, spec: ToolSpec) -> ToolSpec:
    return browser_tool_spec(definition.name, spec)


class DesktopBrowserMCPClient(MCPClient):
    """JSON-response Streamable HTTP client with trusted per-call session metadata.

    The endpoint and bearer token never appear in model-facing configuration.
    Reuses the Desktop's in-memory capability rather than spawning a browser.
    """

    def project_error_summary(
        self,
        summary: dict[str, Any],
        structured: dict[str, Any],
        *,
        serialize: Callable[[Any], str],
        max_chars: int,
    ) -> bool:
        return _project_browser_error_summary(
            summary, structured, serialize=serialize, max_chars=max_chars,
        )

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
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._coordinate_authority = False
        self._attachment_uploads = False
        self._pdf_download_export = False

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
        self._pdf_download_export = (
            isinstance(browser, dict) and browser.get("pdfDownloadExport") is True
        )
        await self._request("notifications/initialized", {}, notification=True)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._available_tools = BROWSER_MCP_REQUIRED_TOOLS
        self._tool_schemas = {}
        self._coordinate_authority = False
        self._attachment_uploads = False
        self._pdf_download_export = False

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
                name = params.get("name") if method == "tools/call" else None
                schema = self._tool_schemas.get(name) if isinstance(name, str) else None
                validation = _argument_validation_result(error, schema) if name else None
                if validation is not None:
                    return {"jsonrpc": "2.0", "id": request_id, "result": validation}
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
        self._tool_schemas = {tool.name: deepcopy(tool.input_schema) for tool in definitions}
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
        export_pdf = bool(
            self._pdf_download_export
            and name == "browser_inspect"
            and isinstance(arguments.get("downloadId"), str)
            and context.workspace_dir
            and context.artifact_session_id
            and context.artifact_media_root
        )
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
                        **({"exportPdf": True} if export_pdf else {}),
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
        raw_result = response.get("result")
        private_meta = raw_result.pop("_meta", None) if isinstance(raw_result, dict) else None
        pdf_export = (
            private_meta.get("opensquilla/pdfExport")
            if isinstance(private_meta, dict) else None
        )
        if pdf_export is not None:
            if not export_pdf or not isinstance(raw_result, dict) or raw_result.get("isError"):
                return _coordinate_unavailable(
                    "BROWSER_PROTOCOL_ERROR", "The Desktop returned an unexpected PDF download.",
                    "inspect",
                )
            structured = raw_result.get("structuredContent")
            download = structured.get("download") if isinstance(structured, dict) else None
            if (
                not isinstance(download, dict)
                or download.get("downloadId") != arguments["downloadId"]
                or structured.get("targetRef") != arguments.get("targetRef")
            ):
                return _coordinate_unavailable(
                    "BROWSER_PROTOCOL_ERROR", "The PDF download receipt did not match the page.",
                    "inspect",
                )
            from opensquilla.tools.browser_pdf_downloads import (
                BrowserPdfDownloadError,
                materialize_browser_pdf_export,
            )

            try:
                pdf_path = await materialize_browser_pdf_export(
                    context, arguments["downloadId"], pdf_export,
                )
            except BrowserPdfDownloadError as error:
                return _coordinate_unavailable(
                    "BROWSER_PDF_EXPORT_FAILED", str(error), "inspect_or_retry",
                )
        else:
            pdf_path = None
        result = MCPToolResult.from_response(response)
        if pdf_path is not None:
            assert result.structured_content is not None
            download = result.structured_content["download"]
            download["workspacePath"] = pdf_path
            download["pdfReadable"] = True
            note = json.dumps({"downloadId": arguments["downloadId"],
                               "workspacePath": pdf_path, "pdfReadable": True})
            result.content += "\n" + note
            result.content_blocks.append({"type": "text", "text": note})
        if self._attachment_uploads and name in {
            "browser_tabs", "browser_open", "browser_navigate", "browser_reload",
            "browser_inspect", "browser_observe", "browser_batch", "browser_tab",
        }:
            from opensquilla.tools.browser_attachments import browser_upload_descriptors

            uploads = await browser_upload_descriptors(context)
            if uploads:
                if result.structured_content is not None:
                    result.structured_content["availableUploads"] = uploads
                else:
                    descriptor_text = json.dumps({"availableUploads": uploads}, ensure_ascii=False)
                    result.content += "\n" + descriptor_text
                    result.content_blocks.append({"type": "text", "text": descriptor_text})
        log.info(
            "desktop_browser.tool_result", tool=name, operation_id=operation_id,
            image_block_count=sum(block.get("type") == "image" for block in result.content_blocks),
        )
        return result
