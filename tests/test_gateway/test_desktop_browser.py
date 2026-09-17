from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla import browser as module
from opensquilla.browser import DesktopBrowserClient, DesktopBrowserError
from opensquilla.tools.builtin.browser import browser
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context


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
    with pytest.raises(DesktopBrowserError, match="target changed"):
        await client.request(
            session_key="session-one", operation="snapshot", target_ref="target-two"
        )


async def test_image_bytes_use_ephemeral_media_and_plan_blocks_mutation():
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
        context.collaboration_mode = "plan"
        with pytest.raises(SafeToolError, match="READ_ONLY"):
            await browser("act", "target-one", action="click", ref="node-one")
    finally:
        current_tool_context.reset(token)


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
