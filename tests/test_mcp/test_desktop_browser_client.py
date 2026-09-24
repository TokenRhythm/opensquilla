from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from opensquilla.browser import DesktopBrowserClient
from opensquilla.mcp.desktop_browser import (
    DesktopBrowserMCPClient,
    browser_tool_policy,
)
from opensquilla.mcp.discovery import close_active_clients, register_client_tools
from opensquilla.mcp.types import MCPCallContext, MCPToolDef, current_mcp_call_context
from opensquilla.observability.log_privacy import log_metadata
from opensquilla.sandbox.integration import sandbox_policy_scope
from opensquilla.sandbox.policy_models import NetworkPolicySettings, SandboxPolicy
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.browser_policy import (
    BROWSER_MCP_OPTIONAL_TOOLS,
    BROWSER_MCP_REQUIRED_TOOLS,
    BROWSER_MCP_TOOL_NAMES,
    BROWSER_MCP_TOOLS,
)
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.policy_runtime import resolve_runtime_tool_surface
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    PlanAccess,
    ToolContext,
    ToolSpec,
    current_tool_context,
)


@pytest.fixture
def browser():
    return DesktopBrowserClient("http://127.0.0.1:43123/v1/browser", "s" * 48)


def context(browser):
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        session_key="conversation-a",
        desktop_browser=browser,
        usage_root_turn_id="turn-a",
    )


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    async def handle(request):
        body = json.loads(request.content)
        calls.append((request, body))
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "experimental": {
                    "opensquilla/browser": {"coordinateAuthority": "browser-state"},
                }},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": name,
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                    for name in sorted(BROWSER_MCP_TOOLS)
                ]
            }
        else:
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


async def test_managed_discovery_dispatch_and_trusted_call_context(browser, mock_http):
    registry = ToolRegistry()
    client = DesktopBrowserMCPClient(browser)
    try:
        names = await register_client_tools(client, registry, spec_transform=browser_tool_policy)
        assert set(names) == BROWSER_MCP_TOOL_NAMES
        ctx = context(browser)
        handler = build_tool_handler(registry, ctx)
        first = await handler(ToolCall("call-a", "mcp__desktop-browser__browser_tabs", {}))
        assert not first.is_error
        request, payload = mock_http[-1]
        assert request.url.path == "/v1/browser/mcp"
        assert request.headers["authorization"] == f"Bearer {browser.token}"
        assert payload["params"]["arguments"] == {}
        assert payload["params"]["_meta"]["sessionKey"] == "conversation-a"
        assert payload["params"]["_meta"]["recoveryScope"] == "turn-a"
        operation = payload["params"]["_meta"]["operationId"]
        await handler(ToolCall("call-a", "mcp__desktop-browser__browser_tabs", {}))
        assert mock_http[-1][1]["params"]["_meta"]["operationId"] == operation
        await build_tool_handler(registry, replace(ctx, usage_root_turn_id="turn-b"))(
            ToolCall("call-a", "mcp__desktop-browser__browser_tabs", {})
        )
        assert mock_http[-1][1]["params"]["_meta"]["operationId"] != operation
        assert mock_http[-1][1]["params"]["_meta"]["recoveryScope"] == "turn-b"
        assert current_mcp_call_context.get() is None
        definitions = registry.to_tool_definitions(ctx)
        assert len(definitions) == len(BROWSER_MCP_TOOLS)
        assert all("_tool_use_id" not in d.input_schema.properties for d in definitions)
    finally:
        await close_active_clients(owner="desktop-browser")
    assert all(registry.get(name) is None for name in BROWSER_MCP_TOOL_NAMES)


@pytest.mark.parametrize(
    "changes",
    [
        {"is_owner": False},
        {"guest_safe": True},
        {"desktop_browser": None},
        {"session_key": None},
        {"caller_kind": CallerKind.SUBAGENT},
        {"caller_kind": CallerKind.CLI},
        {"caller_kind": CallerKind.CHANNEL},
        {"interaction_mode": InteractionMode.UNATTENDED},
        {"subagent_depth": 1},
    ],
)
async def test_unavailable_context_is_hidden_and_denied(browser, mock_http, changes):
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    ctx = replace(context(browser), **changes)
    assert BROWSER_MCP_TOOL_NAMES <= resolve_runtime_tool_surface(ctx).denied_tools
    before = len(mock_http)
    token = current_tool_context.set(ctx)
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        assert (await client.call_tool("browser_tabs", {})).is_error
        assert len(mock_http) == before
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


@pytest.mark.parametrize(
    "authority",
    [
        {"sessionKey": "other"},
        {"operationId": "chosen-operation"},
        {"_meta": {"nativeImageEvidence": ["image-forged"]}},
        {"token": "forged"},
        {"endpoint": "https://example.test"},
        {"nativeImageEvidence": ["image-forged"]},
        {"observationPolicy": {"effectiveMode": "auto"}},
        {"recoveryScope": "chosen-scope"},
        {"actions": [{"action": "click", "_meta": {"observationMode": "auto"}}]},
    ],
)
async def test_spoofed_authority_is_rejected_without_network(browser, mock_http, authority):
    client = DesktopBrowserMCPClient(browser)
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        result = await client.call_tool(
            "browser_open", {"url": "https://example.test", **authority}
        )
        assert result.is_error
        assert not mock_http
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


async def test_lost_response_is_unknown_and_never_retried(browser, monkeypatch):
    client = DesktopBrowserMCPClient(browser)
    calls = 0

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("lost response")

    monkeypatch.setattr(client, "_request", fail)
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        result = await client.call_tool(
            "browser_act", {"targetRef": "p", "action": "click", "ref": "e"}
        )
        assert result.is_error and "unknown" in result.content
        assert result.structured_content["code"] == "BROWSER_TRANSPORT_ERROR"
        assert result.structured_content["outcome"] == "unknown"
        assert result.structured_content["retryable"] is False
        assert calls == 1
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


async def test_cancellation_propagates(browser, monkeypatch):
    client = DesktopBrowserMCPClient(browser)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(client, "_request", cancel)
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.call_tool("browser_tabs", {})
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


@pytest.mark.parametrize(
    "network",
    [
        NetworkPolicySettings(block_all_network=True),
        NetworkPolicySettings(deny_domains=["blocked.example"]),
    ],
)
async def test_explicit_network_restrictions_fail_before_browser_request(
    browser, mock_http, network
):
    client = DesktopBrowserMCPClient(browser)
    ctx = replace(context(browser), run_mode="safe", sandbox_policy=SandboxPolicy(network=network))
    token = current_tool_context.set(ctx)
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        result = await client.call_tool("browser_open", {"url": "https://allowed.example"})
        assert result.is_error and "BROWSER_POLICY_UNSUPPORTED" in result.content
        assert not mock_http
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


async def test_active_network_policy_takes_precedence_over_context_default(browser, mock_http):
    client = DesktopBrowserMCPClient(browser)
    token = current_tool_context.set(
        replace(context(browser), run_mode="safe", sandbox_policy=SandboxPolicy())
    )
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        with sandbox_policy_scope(
            SandboxPolicy(network=NetworkPolicySettings(block_all_network=True))
        ):
            result = await client.call_tool("browser_open", {"url": "https://example.test"})
        assert result.is_error and "BROWSER_POLICY_UNSUPPORTED" in result.content
        assert not mock_http
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


def catalog_entry(name):
    return {
        "name": name,
        "description": name,
        "inputSchema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize("optional", [set(), {"browser_observe"}, BROWSER_MCP_OPTIONAL_TOOLS])
async def test_catalog_negotiates_old_and_optional_tools(browser, monkeypatch, optional):
    client = DesktopBrowserMCPClient(browser)
    names = BROWSER_MCP_REQUIRED_TOOLS | optional

    async def respond(*args, **kwargs):
        return {"result": {"tools": [catalog_entry(name) for name in sorted(names)]}}

    monkeypatch.setattr(client, "_request", respond)
    assert {tool.name for tool in await client.list_tools()} == names


async def test_catalog_filters_unknown_tools_without_granting_authority(browser, monkeypatch):
    client = DesktopBrowserMCPClient(browser)

    async def respond(*args, **kwargs):
        return {
            "result": {
                "tools": [
                    *[catalog_entry(name) for name in sorted(BROWSER_MCP_REQUIRED_TOOLS)],
                    catalog_entry("browser_observe"),
                    catalog_entry("browser_delete_all_profiles"),
                    {"name": ["malformed-name"]},
                    None,
                ]
            }
        }

    monkeypatch.setattr(client, "_request", respond)
    definitions = await client.list_tools()
    assert {tool.name for tool in definitions} == BROWSER_MCP_REQUIRED_TOOLS | {"browser_observe"}


@pytest.mark.parametrize("name", ["browser_tabs", "browser_observe"])
async def test_duplicate_known_catalog_entry_is_rejected(browser, monkeypatch, name):
    client = DesktopBrowserMCPClient(browser)

    async def respond(*args, **kwargs):
        return {
            "result": {
                "tools": [
                    *[catalog_entry(tool) for tool in sorted(BROWSER_MCP_TOOLS)],
                    catalog_entry(name),
                ]
            }
        }

    monkeypatch.setattr(client, "_request", respond)
    with pytest.raises(RuntimeError, match="Duplicate"):
        await client.list_tools()


@pytest.mark.parametrize("mutation", ["missing-required", "invalid-optional"])
async def test_invalid_catalog_fails_without_partial_negotiation(browser, monkeypatch, mutation):
    client = DesktopBrowserMCPClient(browser)
    entries = [catalog_entry(name) for name in sorted(BROWSER_MCP_TOOLS)]
    if mutation == "missing-required":
        entries = [entry for entry in entries if entry["name"] != "browser_tabs"]
    else:
        next(entry for entry in entries if entry["name"] == "browser_observe")["inputSchema"] = []

    async def respond(*args, **kwargs):
        return {"result": {"tools": entries}}

    monkeypatch.setattr(client, "_request", respond)
    with pytest.raises(RuntimeError, match="Desktop browser tool catalog"):
        await client.list_tools()
    assert client._available_tools == BROWSER_MCP_REQUIRED_TOOLS


async def test_unadvertised_optional_tool_is_rejected_without_request(browser, mock_http):
    client = DesktopBrowserMCPClient(browser)
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        result = await client.call_tool("browser_observe", {"targetRef": "page-a"})
        assert result.is_error and "BROWSER_UNSUPPORTED" in result.content
        assert not mock_http
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


def failing_vision_resolver():
    raise RuntimeError("route unavailable")


@pytest.mark.parametrize("requested", [None, "auto", "dom", "invalid"])
async def test_capture_preference_needs_no_agent_policy(browser, mock_http, requested):
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    await client.list_tools()
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("capture-call"))
    arguments = {"targetRef": "page-a"}
    if requested is not None:
        arguments["observationMode"] = requested
    try:
        result = await client.call_tool("browser_observe", arguments)
        assert not result.is_error
        params = mock_http[-1][1]["params"]
        assert params["_meta"]["observationMode"] == (requested or "auto")
        assert params["arguments"] == arguments
        assert set(params["_meta"]) == {
            "sessionKey", "operationId", "recoveryScope", "observationMode",
        }
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


@pytest.mark.parametrize("requested", ["auto", "dom"])
async def test_capture_log_is_bounded_and_does_not_claim_delivery(
    browser, mock_http, monkeypatch, requested,
):
    from opensquilla.mcp import desktop_browser

    events = []

    class ProjectedLog:
        def info(self, event, **fields):
            events.append(log_metadata({"event": event, **fields}))

    monkeypatch.setattr(desktop_browser, "log", ProjectedLog())
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    await client.list_tools()
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("capture-log"))
    try:
        await client.call_tool("browser_observe", {
            "targetRef": "opaque-page", "observationMode": requested,
        })
        operation = mock_http[-1][1]["params"]["_meta"]["operationId"]
        assert events == [{
            "event": "desktop_browser.capture_requested", "tool": "browser_observe",
            "operation_id": operation, "browser_requested_mode": requested,
        }, {
            "event": "desktop_browser.tool_result", "tool": "browser_observe",
            "operation_id": operation, "image_block_count": 0,
        }]
        serialized = json.dumps(events)
        for private in (browser.token, browser.endpoint, "opaque-page", "conversation-a"):
            assert private not in serialized
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


@pytest.mark.parametrize("root_turn,task,expected", [
    ("turn-fixture", "task-fixture", "turn-fixture"),
    (None, "task-fixture", "task-fixture"),
    (None, None, "conversation-a"),
])
async def test_recovery_scope_is_trusted_and_stable_across_tool_calls(
    browser, mock_http, root_turn, task, expected,
):
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    token = current_tool_context.set(replace(
        context(browser), usage_root_turn_id=root_turn, task_id=task,
    ))
    try:
        operations = []
        for tool_id in ("call-one", "call-two"):
            call_token = current_mcp_call_context.set(MCPCallContext(tool_id))
            try:
                await client.call_tool("browser_tabs", {})
                meta = mock_http[-1][1]["params"]["_meta"]
                operations.append(meta["operationId"])
                assert meta["recoveryScope"] == expected
            finally:
                current_mcp_call_context.reset(call_token)
        assert operations[0] != operations[1]
    finally:
        current_tool_context.reset(token)
        await client.close()


@pytest.mark.parametrize(
    ("failure", "code", "extra"),
    [
        ("timeout", "BROWSER_TRANSPORT_TIMEOUT", {}),
        ("connection", "BROWSER_TRANSPORT_ERROR", {}),
        ("wire_protocol", "BROWSER_PROTOCOL_ERROR", {}),
        ("http", "BROWSER_HTTP_ERROR", {"httpStatus": 503}),
        ("json", "BROWSER_PROTOCOL_ERROR", {}),
        ("identity", "BROWSER_PROTOCOL_ERROR", {}),
        ("boolean_identity", "BROWSER_PROTOCOL_ERROR", {}),
        ("rpc", "BROWSER_PROTOCOL_ERROR", {"rpcCode": -32602}),
        ("result", "BROWSER_PROTOCOL_ERROR", {}),
        ("oversize", "BROWSER_RESPONSE_TOO_LARGE", {}),
    ],
)
async def test_transport_errors_preserve_classification_without_replay_or_private_body(
    browser, failure, code, extra,
):
    client = DesktopBrowserMCPClient(browser)
    requests = []
    private_marker = "synthetic-private-response"

    async def respond(request):
        requests.append(request)
        request_id = json.loads(request.content)["id"]
        if failure == "timeout":
            raise httpx.ReadTimeout(private_marker, request=request)
        if failure == "connection":
            raise httpx.ConnectError(private_marker, request=request)
        if failure == "wire_protocol":
            raise httpx.RemoteProtocolError(private_marker, request=request)
        if failure == "http":
            return httpx.Response(503, text=private_marker)
        if failure == "json":
            return httpx.Response(200, text=private_marker)
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * (12 * 1024 * 1024 + 1))
        payload = {"jsonrpc": "2.0", "id": request_id, "result": {"content": []}}
        if failure == "identity":
            payload["id"] = "wrong-request"
        elif failure == "boolean_identity":
            payload["id"] = True
        elif failure == "rpc":
            del payload["result"]
            payload["error"] = {"code": -32602, "message": private_marker}
        elif failure == "result":
            payload["result"] = {"content": private_marker}
        return httpx.Response(200, json=payload)

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("transport-fixture"))
    try:
        result = await client.call_tool(
            "browser_act", {"targetRef": "page-fixture", "action": "click", "ref": "ref-fixture"},
        )
        assert result.is_error
        assert result.structured_content == {
            **result.structured_content,
            "ok": False, "code": code, "outcome": "unknown", "retryable": False, **extra,
        }
        assert result.content == json.dumps(result.structured_content)
        assert private_marker not in result.content
        assert browser.token not in result.content
        assert len(requests) == 1
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


async def test_disconnected_bridge_is_classified(browser):
    client = DesktopBrowserMCPClient(browser)
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("disconnected-fixture"))
    try:
        result = await client.call_tool("browser_tabs", {})
        assert result.structured_content["code"] == "BROWSER_DISCONNECTED"
        assert result.structured_content["retryable"] is False
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


async def test_native_structured_failure_survives_transport(browser):
    expected = {
        "ok": False, "code": "NAVIGATION_FAILED", "targetRef": "page-fixture",
        "outcome": "unknown", "retryable": False,
        "navigation": {"code": "ERR_NAME_NOT_RESOLVED", "errorCode": -105},
        "recovery": {"allowedActions": ["observe", "close"]},
    }

    async def respond(request):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": json.loads(request.content)["id"],
            "result": {"isError": True, "structuredContent": expected,
                       "content": [{"type": "text", "text": json.dumps(expected)}]},
        })

    client = DesktopBrowserMCPClient(browser)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("native-error-fixture"))
    try:
        result = await client.call_tool("browser_open", {"url": "https://page.invalid"})
        assert result.is_error
        assert result.structured_content == expected
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


def coordinate_arguments():
    return {"targetRef": "page-a", "actions": [{
        "action": "click", "observationId": "observation-a", "imageId": "image-a",
        "x": 12, "y": 24,
    }]}


@pytest.mark.parametrize("resolver", [None, lambda: None, failing_vision_resolver])
async def test_coordinate_actions_require_current_vision_capability(browser, mock_http, resolver):
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    await client.list_tools()
    token = current_tool_context.set(replace(context(browser), image_analysis_target=resolver))
    call_token = current_mcp_call_context.set(MCPCallContext("coordinate-call"))
    before = len(mock_http)
    try:
        result = await client.call_tool("browser_batch", coordinate_arguments())
        assert result.is_error
        assert result.structured_content["code"] == "VISION_UNAVAILABLE"
        assert result.structured_content["outcome"] == "not_started"
        assert result.structured_content["retryable"] is False
        assert len(mock_http) == before
        # DOM targets do not require vision, including after model fallback.
        result = await client.call_tool("browser_batch", {
            "targetRef": "page-a", "actions": [{"action": "click", "ref": "element-a"}],
        })
        assert not result.is_error
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


async def test_capability_is_resolved_each_call_without_image_receipts(browser, mock_http):
    client = DesktopBrowserMCPClient(browser)
    await client.connect()
    await client.list_tools()
    active_target = (object(), object())
    ctx = replace(context(browser), image_analysis_target=lambda: active_target)
    token = current_tool_context.set(ctx)
    call_token = current_mcp_call_context.set(MCPCallContext("coordinate-call"))
    try:
        arguments = coordinate_arguments()
        result = await client.call_tool("browser_batch", arguments)
        assert not result.is_error
        params = mock_http[-1][1]["params"]
        assert params["arguments"] == arguments
        assert "nativeImageEvidence" not in params["_meta"]
        active_target = None
        before = len(mock_http)
        result = await client.call_tool("browser_batch", arguments)
        assert result.structured_content["code"] == "VISION_UNAVAILABLE"
        assert len(mock_http) == before
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


@pytest.mark.parametrize("capabilities", [{}, {"experimental": []}, {
    "experimental": {"opensquilla/browser": {"coordinateAuthority": "legacy"}},
}])
async def test_old_client_remains_usable_but_coordinates_explain_upgrade(
    browser, monkeypatch, capabilities,
):
    client = DesktopBrowserMCPClient(browser)
    calls = []

    async def respond(method, params, **kwargs):
        calls.append(method)
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": capabilities}}
        if method == "tools/list":
            return {"result": {"tools": [catalog_entry(name) for name in BROWSER_MCP_TOOLS]}}
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(client, "_request", respond)
    await client.connect()
    await client.list_tools()
    token = current_tool_context.set(replace(
        context(browser), image_analysis_target=lambda: (object(), object()),
    ))
    call_token = current_mcp_call_context.set(MCPCallContext("legacy-coordinate-call"))
    try:
        result = await client.call_tool("browser_batch", coordinate_arguments())
        assert result.structured_content["code"] == "BROWSER_CLIENT_UPDATE_REQUIRED"
        assert result.structured_content["outcome"] == "not_started"
        assert "tools/call" not in calls
        assert not (await client.call_tool("browser_observe", {"targetRef": "page-a"})).is_error
        assert not (await client.call_tool("browser_batch", {
            "targetRef": "page-a", "actions": [{"action": "click", "ref": "element-a"}],
        })).is_error
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)
        await client.close()


async def test_reconnect_drops_previous_coordinate_capability(browser, monkeypatch):
    client = DesktopBrowserMCPClient(browser)
    capabilities = {"experimental": {
        "opensquilla/browser": {"coordinateAuthority": "browser-state"},
    }}

    async def respond(method, params, **kwargs):
        return {"result": {"protocolVersion": "2025-06-18", "capabilities": capabilities}}

    monkeypatch.setattr(client, "_request", respond)
    try:
        await client.connect()
        assert client._coordinate_authority
        capabilities = {}
        await client.connect()
        assert not client._coordinate_authority
    finally:
        await client.close()


def observation_response():
    return {
        "result": {
            "content": [
                {"type": "text", "text": "page state"},
                {
                    "type": "image",
                    "mimeType": "image/png",
                    # Byte validation belongs to the shared MCP projection;
                    # this boundary preserves the standard MCP content.
                    "data": "cGl4ZWxz",
                    "_meta": {
                        "opensquilla/browserObservation": {
                            "targetRef": "page-a",
                            "observationId": "observation-a",
                            "imageId": "image-a",
                        }
                    },
                },
            ],
            "structuredContent": {
                "targetRef": "page-a",
                "observation": {
                    "targetRef": "page-a",
                    "observationId": "observation-a",
                    "consistency": "consistent",
                    "imageStatus": "available",
                    "image": {"imageId": "image-a", "width": 640, "height": 480},
                },
            },
        }
    }


@pytest.mark.parametrize(
    "tool_name", ["browser_observe", "browser_open", "browser_act", "browser_batch"],
)
@pytest.mark.parametrize("action_failed", [False, True])
async def test_observation_keeps_standard_images_even_when_action_failed(
    browser, monkeypatch, tool_name, action_failed,
):
    client = DesktopBrowserMCPClient(browser)

    async def respond(method, params, **kwargs):
        if method == "tools/list":
            return {"result": {"tools": [catalog_entry(name) for name in BROWSER_MCP_TOOLS]}}
        response = observation_response()
        response["result"]["isError"] = action_failed
        if action_failed:
            response["result"]["structuredContent"]["execution"] = {"state": "partial"}
        return response

    monkeypatch.setattr(client, "_request", respond)
    await client.list_tools()
    token = current_tool_context.set(context(browser))
    call_token = current_mcp_call_context.set(MCPCallContext("call"))
    try:
        args = (
            {"url": "https://example.test"}
            if tool_name == "browser_open"
            else {
                "targetRef": "page-a",
            }
        )
        result = await client.call_tool(tool_name, args)
        assert result.is_error == action_failed
        assert result.content_blocks == observation_response()["result"]["content"]
        assert result.structured_content["observation"]["image"]["imageId"] == "image-a"
    finally:
        current_tool_context.reset(token)
        current_mcp_call_context.reset(call_token)


@pytest.mark.parametrize("tool_name", sorted(BROWSER_MCP_OPTIONAL_TOOLS))
def test_optional_tools_keep_browser_policy_metadata(tool_name):
    spec = browser_tool_policy(
        MCPToolDef(tool_name, tool_name, {}),
        ToolSpec(name=f"mcp__desktop-browser__{tool_name}", description="", parameters={}),
    )
    assert spec.owner_only
    assert spec.plan_access is (
        PlanAccess.READ_ONLY if tool_name == "browser_observe" else PlanAccess.DENY
    )
    assert spec.sandbox.kind == "browser"


async def test_optional_tool_dispatch_honors_explicit_denial_in_plan_context(browser, mock_http):
    registry = ToolRegistry()
    client = DesktopBrowserMCPClient(browser)
    try:
        await register_client_tools(client, registry, spec_transform=browser_tool_policy)
        name = "mcp__desktop-browser__browser_batch"
        ctx = replace(context(browser), collaboration_mode="plan", denied_tools={name})
        before = len(mock_http)
        result = await build_tool_handler(registry, ctx)(ToolCall("denied-call", name, {}))
        assert result.is_error
        assert len(mock_http) == before
    finally:
        await close_active_clients(owner="desktop-browser")
