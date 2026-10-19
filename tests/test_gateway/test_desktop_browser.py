from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from opensquilla import browser as module
from opensquilla.browser import DesktopBrowserClient, DesktopBrowserError
from opensquilla.mcp.desktop_browser import DesktopBrowserMCPClient
from opensquilla.mcp.types import MCPCallContext, current_mcp_call_context
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools import browser_policy
from opensquilla.tools.builtin.browser import browser
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import CallerKind, SafeToolError, ToolContext, current_tool_context


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:9000/v1/browser",
        "http://localhost:9000/v1/browser",
        "http://127.0.0.1:9000/other",
        "http://192.168.1.1:9000/v1/browser",
        "http://127.0.0.1:9000/v1/browser?token=secret",
    ],
)
def test_browser_connection_rejects_non_host_endpoints(endpoint):
    with pytest.raises(ValueError):
        DesktopBrowserClient(endpoint, "x" * 48)


def test_host_credentials_are_consumed_once_and_not_exposed(monkeypatch):
    monkeypatch.setattr(module, "_initialized", False)
    monkeypatch.setattr(module, "_client", None)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_BROWSER_URL", "http://127.0.0.1:9000/v1/browser")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_BROWSER_TOKEN", "x" * 48)
    client = module.initialize_desktop_browser()
    assert client is not None
    assert "x" * 48 not in repr(client)
    assert "OPENSQUILLA_DESKTOP_BROWSER_TOKEN" not in module.os.environ
    assert "OPENSQUILLA_DESKTOP_BROWSER_URL" not in module.os.environ
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_BROWSER_TOKEN", "profile-token")
    assert module.get_desktop_browser() is client


async def test_request_authenticates_and_keeps_exact_target_identity(monkeypatch):
    requests = []
    async_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"targetRef": "target-one", "text": "first page"})

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    result = await client.request(
        session_key="session-one", operation="snapshot", target_ref="target-one"
    )
    assert result["targetRef"] == "target-one"
    assert requests[0].headers["authorization"] == "Bearer " + "x" * 48
    assert json.loads(requests[0].content) == {
        "sessionKey": "session-one",
        "operation": "snapshot",
        "targetRef": "target-one",
    }
    with pytest.raises(DesktopBrowserError, match="target changed") as caught:
        await client.request(
            session_key="session-one", operation="snapshot", target_ref="target-two"
        )
    assert caught.value.details["outcome"] == "unknown"


async def test_image_bytes_use_ephemeral_media():
    captured = base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode()

    async def request(**kwargs):
        assert kwargs["session_key"] == "session-one"
        return {
            "targetRef": "target-one",
            "mimeType": "image/png",
            "dataBase64": captured,
            "width": 20,
            "height": 10,
        }

    context = ToolContext(
        is_owner=True, session_key="session-one", desktop_browser=SimpleNamespace(request=request)
    )
    token = current_tool_context.set(context)
    try:
        result = json.loads(await browser("screenshot", "target-one", _tool_use_id="tool-one"))
        assert "dataBase64" not in result
        assert context.tool_result_media["tool-one"][0]["data"] == captured
    finally:
        current_tool_context.reset(token)


def _browser_dispatch(context):
    registry = ToolRegistry()
    registered = get_default_registry().get("browser")
    assert registered is not None
    registry.register(registered.spec, browser)
    return build_tool_handler(registry, context)


@pytest.mark.parametrize("mode", ["default", "plan"])
@pytest.mark.parametrize("operation,arguments", [
    ("open", {}),
    ("open", {"url": "https://example.invalid/research"}),
    ("act", {"targetRef": "target-one", "action": "click", "ref": "node-one"}),
    ("reload", {"targetRef": "target-one"}),
])
async def test_browser_investigation_uses_ordinary_dispatch(mode, operation, arguments):
    requests = []

    async def request(**kwargs):
        requests.append(kwargs)
        return {"targetRef": "target-one"}

    context = ToolContext(
        is_owner=True, session_key="session-one", collaboration_mode=mode,
        allowed_tools={"browser"}, desktop_browser=SimpleNamespace(request=request),
        run_mode="safe",
    )
    result = await _browser_dispatch(context)(ToolCall(
        tool_use_id="investigate", tool_name="browser",
        arguments={"operation": operation, **arguments},
    ))
    assert not result.is_error, result.content
    assert json.loads(result.content) == {"targetRef": "target-one"}
    expected = {"session_key": "session-one", "operation": operation,
                "target_ref": arguments.get("targetRef")}
    expected.update({k: v for k, v in arguments.items() if k != "targetRef"})
    identity = requests[0].pop("operation_id")
    scope = requests[0].pop("recovery_scope")
    assert len(identity) == 64 and scope == "session-one"
    assert requests == [expected]


@pytest.mark.parametrize("restriction,error_class", [
    ("non_owner", "OwnerOnly"), ("denied_tool", "PolicyDenied"),
])
async def test_plan_browser_preserves_ordinary_authorization(restriction, error_class):
    async def must_not_request(**kwargs):
        raise AssertionError("A denied tool must not reach the desktop browser")

    context = ToolContext(
        is_owner=restriction != "non_owner", session_key="session-one",
        collaboration_mode="plan", allowed_tools={"browser"},
        denied_tools={"browser"} if restriction == "denied_tool" else set(),
        desktop_browser=SimpleNamespace(request=must_not_request), run_mode="safe",
    )
    result = await _browser_dispatch(context)(ToolCall(
        tool_use_id="denied", tool_name="browser",
        arguments={"operation": "open", "url": "https://example.invalid/research"},
    ))
    assert result.is_error
    assert json.loads(result.content)["error_class"] == error_class


async def test_plan_browser_preserves_native_navigation_policy(monkeypatch):
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        403, json={"ok": False, "code": "NAVIGATION_BLOCKED"},
    ))
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: async_client(**kwargs, transport=transport),
    )
    context = ToolContext(
        is_owner=True, session_key="session-one", collaboration_mode="plan",
        allowed_tools={"browser"}, run_mode="safe",
        desktop_browser=DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48),
    )
    result = await _browser_dispatch(context)(ToolCall(
        tool_use_id="blocked-navigation", tool_name="browser",
        arguments={"operation": "open", "url": "file:///private/synthetic"},
    ))
    assert result.is_error and "BROWSER_NAVIGATION_BLOCKED" in result.content


@pytest.mark.parametrize(
    "result",
    [
        {"mimeType": "image/jpeg", "dataBase64": "invalid", "width": 1, "height": 1},
        {
            "mimeType": "image/png",
            "dataBase64": base64.b64encode(b"other").decode(),
            "width": 1,
            "height": 1,
        },
    ],
)
def test_invalid_screenshot_is_not_forwarded(result):
    with pytest.raises(DesktopBrowserError):
        module.validate_browser_screenshot(result)


@pytest.mark.parametrize(
    ("status", "native_code", "public_code"),
    [
        (400, "INVALID_REQUEST", "BROWSER_REQUEST_INVALID"),
        (404, "TARGET_NOT_FOUND", "BROWSER_TARGET_NOT_FOUND"),
        (504, "TIMEOUT", "BROWSER_TIMEOUT"),
        (409, "PAGE_CHANGED", "BROWSER_PAGE_CHANGED"),
        (409, "BROWSER_UNAVAILABLE", "BROWSER_UNAVAILABLE"),
        (409, "BROWSER_PRIVATE_DETAIL", "BROWSER_OPERATION_FAILED"),
        (409, "untrusted detail", "BROWSER_OPERATION_FAILED"),
        (409, {"private": "renderer detail"}, "BROWSER_OPERATION_FAILED"),
    ],
)
async def test_native_browser_failure_preserves_safe_code_without_image(
    monkeypatch, status, native_code, public_code
):
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        status,
        json={"ok": False, "code": native_code, "message": "private renderer diagnostic",
              "dataBase64": "untrusted image bytes"},
    ))
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=transport),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    context = ToolContext(is_owner=True, session_key="session-one", desktop_browser=client)
    token = current_tool_context.set(context)
    try:
        with pytest.raises(SafeToolError) as caught:
            await browser("screenshot", "target-one", _tool_use_id="failed-screenshot")
        assert str(caught.value).startswith(public_code + ":")
        assert "private renderer diagnostic" not in str(caught.value)
        assert "untrusted image bytes" not in str(caught.value)
        assert context.tool_result_media == {}
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize(
    ("failure", "public_code"),
    [(httpx.ReadTimeout, "BROWSER_TIMEOUT"), (httpx.ReadError, "BROWSER_UNAVAILABLE")],
)
async def test_browser_transport_failure_keeps_timeout_distinct(monkeypatch, failure, public_code):
    async_client = httpx.AsyncClient

    def respond(request):
        raise failure("private transport diagnostic", request=request)

    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    with pytest.raises(DesktopBrowserError) as caught:
        await client.request(session_key="session-one", operation="snapshot", target_ref="page")
    assert caught.value.code == public_code
    assert caught.value.details == {
        "outcome": "unknown", "retryable": False, "recovery": "inspect",
    }
    assert "private transport diagnostic" not in str(caught.value)


async def test_browser_caller_cancellation_is_not_reported_as_timeout(monkeypatch):
    async_client = httpx.AsyncClient

    def respond(_request):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    with pytest.raises(asyncio.CancelledError):
        await client.request(session_key="session-one", operation="snapshot", target_ref="page")


async def test_browser_identity_headers_preserve_the_old_body_and_response(monkeypatch):
    requests = []
    async_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        # Older clients ignore the new headers and still return their original result shape.
        return httpx.Response(200, json={"targetRef": "page", "refs": [{"ref": "old-ref"}]})

    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    result = await client.request(
        session_key="session-one", operation="snapshot", target_ref="page",
        operation_id="trusted-operation", recovery_scope="任务 / scope%一",
    )
    assert result["refs"] == [{"ref": "old-ref"}]
    assert json.loads(requests[0].content) == {
        "sessionKey": "session-one", "operation": "snapshot", "targetRef": "page",
    }
    assert requests[0].headers["x-opensquilla-browser-operation-id"] == "trusted-operation"
    assert requests[0].headers["x-opensquilla-browser-recovery-scope"] == quote(
        "任务 / scope%一", safe="",
    )


async def test_browser_tool_identity_is_stable_for_one_call_and_changes_for_another():
    requests = []

    async def request(**kwargs):
        requests.append(kwargs)
        return {"targetRef": "page"}

    context = ToolContext(
        is_owner=True, session_key="session-one", task_id="task-one",
        desktop_browser=SimpleNamespace(request=request),
    )
    token = current_tool_context.set(context)
    try:
        await browser("act", "page", action="press", key="Enter", _tool_use_id="submit")
        await browser("act", "page", action="press", key="Enter", _tool_use_id="submit")
        await browser("act", "page", action="press", key="Enter", _tool_use_id="other-submit")
    finally:
        current_tool_context.reset(token)
    assert requests[0]["operation_id"] == requests[1]["operation_id"]
    assert requests[0]["operation_id"] != requests[2]["operation_id"]
    assert len({item["recovery_scope"] for item in requests}) == 1


async def test_browser_failure_keeps_recovery_facts_and_redacts_other_details(monkeypatch):
    async_client = httpx.AsyncClient
    response = {
        "ok": False, "code": "TIMEOUT", "message": "private renderer diagnostic",
        "targetRef": "page", "operationId": "trusted-operation", "outcome": "unknown",
        "retryable": False, "recovery": "inspect", "dataBase64": "private pixels",
        "navigation": {"url": "https://private.invalid/path?token=secret"},
        "recoveryBudget": {"domain": "action", "attempts": 2, "limit": 2,
                           "exhausted": True, "private": "secret"},
    }
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response),
        )),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    with pytest.raises(DesktopBrowserError) as caught:
        await client.request(
            session_key="session-one", operation="act", target_ref="page", action="press",
            key="Enter", operation_id="trusted-operation",
        )
    assert caught.value.details == {
        "targetRef": "page", "operationId": "trusted-operation", "outcome": "unknown",
        "retryable": False, "recovery": "inspect",
        "recoveryBudget": {"domain": "action", "attempts": 2, "limit": 2, "exhausted": True},
    }
    assert "private" not in str(caught.value) + json.dumps(caught.value.details)


async def test_browser_rejects_an_unrelated_operation_receipt(monkeypatch):
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "targetRef": "page", "operationId": "foreign-operation",
            }),
        )),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    with pytest.raises(DesktopBrowserError) as caught:
        await client.request(
            session_key="session-one", operation="act", target_ref="page", action="press",
            key="Enter", operation_id="trusted-operation",
        )
    assert caught.value.code == "BROWSER_RESPONSE_INVALID"
    assert caught.value.details["outcome"] == "unknown"
    assert caught.value.details["operationId"] == "trusted-operation"


@pytest.mark.parametrize("task_id", [None, "任务一"])
async def test_old_tool_and_mcp_share_unicode_operation_and_recovery_identity(monkeypatch, task_id):
    old_calls, mcp_calls = [], []

    async def old_request(self, **kwargs):
        old_calls.append(kwargs)
        return {"targetRef": "page"}

    async def mcp_request(method, params, **kwargs):
        mcp_calls.append(params)
        return {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(DesktopBrowserClient, "request", old_request)
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    mcp = DesktopBrowserMCPClient(client)
    monkeypatch.setattr(mcp, "_request", mcp_request)
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, session_key="会话一", task_id=task_id,
        desktop_browser=client,
    )
    token = current_tool_context.set(context)
    mcp_token = current_mcp_call_context.set(MCPCallContext("same-call"))
    try:
        await browser("act", "page", action="press", key="Enter", _tool_use_id="same-call")
        result = await mcp.call_tool("browser_act", {
            "targetRef": "page", "action": "press", "key": "Enter",
        })
        assert not result.is_error
    finally:
        current_mcp_call_context.reset(mcp_token)
        current_tool_context.reset(token)
    assert old_calls[0]["operation_id"] == mcp_calls[0]["_meta"]["operationId"]
    assert old_calls[0]["recovery_scope"] == mcp_calls[0]["_meta"]["recoveryScope"]


@pytest.mark.parametrize("kind", ["url-preview", "unknown", None, {"malformed": True}])
async def test_restricted_network_blocks_unconfirmed_or_url_targets(monkeypatch, kind):
    requests = []

    async def request(**kwargs):
        requests.append(kwargs)
        assert kwargs["operation"] == "list", "A denied URL action must never execute"
        return {"targets": [{"targetRef": "page", "kind": kind}]}

    monkeypatch.setattr(browser_policy, "browser_network_policy_supported", lambda _ctx: False)
    token = current_tool_context.set(ToolContext(
        is_owner=True, session_key="session-one", desktop_browser=SimpleNamespace(request=request),
    ))
    try:
        with pytest.raises(SafeToolError, match="BROWSER_POLICY_UNSUPPORTED"):
            await browser("act", "page", action="press", key="Enter", _tool_use_id="blocked")
        with pytest.raises(SafeToolError, match="BROWSER_POLICY_UNSUPPORTED"):
            await browser("open", url="https://example.invalid", _tool_use_id="blocked-open")
    finally:
        current_tool_context.reset(token)
    assert len(requests) == 1


async def test_restricted_network_keeps_confirmed_artifact_and_list_access(monkeypatch):
    requests = []

    async def request(**kwargs):
        requests.append(kwargs)
        if kwargs["operation"] == "list":
            return {"targets": [{"targetRef": "artifact", "kind": "artifact-preview"}]}
        return {"targetRef": "artifact", "performed": True}

    monkeypatch.setattr(browser_policy, "browser_network_policy_supported", lambda _ctx: False)
    token = current_tool_context.set(ToolContext(
        is_owner=True, session_key="session-one", desktop_browser=SimpleNamespace(request=request),
    ))
    try:
        result = json.loads(await browser("act", "artifact", action="press", key="Enter"))
        assert result["performed"]
        assert json.loads(await browser("list"))["targets"][0]["targetRef"] == "artifact"
    finally:
        current_tool_context.reset(token)
    assert [item["operation"] for item in requests] == ["list", "act", "list"]


async def test_blocked_browser_screenshot_keeps_recovery_instead_of_decoding_pixels(monkeypatch):
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "ok": False, "code": "BROWSER_BLOCKED", "targetRef": "page",
                "outcome": "not_started", "retryable": False, "recovery": "resolve_blocker",
            }),
        )),
    )
    client = DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48)
    with pytest.raises(DesktopBrowserError) as caught:
        await client.request(session_key="session-one", operation="screenshot", target_ref="page")
    assert caught.value.code == "BROWSER_BLOCKED"
    assert caught.value.details["recovery"] == "resolve_blocker"
