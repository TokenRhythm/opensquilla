from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from opensquilla import browser as module
from opensquilla.browser import DesktopBrowserClient, DesktopBrowserError
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.builtin.browser import browser
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry, get_default_registry
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


_RETAINED_TARGET = "page-11111111-2222-4333-8444-555555555555"


def _native_browser_context(monkeypatch, respond):
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: async_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    return ToolContext(
        is_owner=True, session_key="session-one", execution_id="turn-one",
        desktop_browser=DesktopBrowserClient("http://127.0.0.1:9000/v1/browser", "x" * 48),
    )


async def _dispatch_browser(context, operation="open", **arguments):
    return await _browser_dispatch(context)(ToolCall(
        tool_use_id="browser-recovery", tool_name="browser",
        arguments={"operation": operation, **arguments},
    ))


@pytest.mark.parametrize("code", ["PAGE_CHANGED", "NAVIGATION_FAILED", "NAVIGATION_CANCELLED"])
async def test_native_browser_retained_tab_recovery_survives_fixed_envelope(monkeypatch, code):
    context = _native_browser_context(monkeypatch, lambda _: httpx.Response(409, json={
        "ok": False, "code": code, "targetRef": _RETAINED_TARGET,
        "outcome": "completed", "recovery": "change_url_or_network", "retryable": True,
        "message": "private renderer diagnostic", "navigation": {"url": "https://secret.invalid"},
    }))
    result = await _dispatch_browser(context, url="https://example.invalid")
    payload = json.loads(result.content)
    assert set(payload) == {"status", "tool", "error_class", "user_message", "retry_allowed"}
    assert payload["retry_allowed"] is False
    message = payload["user_message"]
    assert f"BROWSER_{code}" in message
    assert _RETAINED_TARGET in message
    assert "finished with a failure" in message
    assert "do not repeat open without targetRef" in message
    assert "snapshot" in message and "navigate that same targetRef" in message
    assert "secret.invalid" not in message and "private renderer" not in message
    assert len(message) < 600


@pytest.mark.parametrize("details", [
    {"targetRef": "page-private-token", "outcome": "secret outcome", "recovery": "secret advice"},
    {"targetRef": _RETAINED_TARGET + "\nsecret", "outcome": [], "recovery": {}},
    {"targetRef": {"secret": "value"}, "outcome": {}, "recovery": []},
    {"targetRef": "x" * 10000, "outcome": True, "recovery": "x" * 10000},
])
async def test_native_browser_untrusted_recovery_fields_are_not_forwarded(monkeypatch, details):
    context = _native_browser_context(monkeypatch, lambda _: httpx.Response(409, json={
        "ok": False, "code": "PAGE_CHANGED", "message": "secret diagnostic", **details,
    }))
    result = await _dispatch_browser(context, url="https://example.invalid")
    message = json.loads(result.content)["user_message"]
    assert "secret" not in message and "x" * 30 not in message
    assert "outcome is unknown" in message
    assert "Run list, then snapshot" in message
    assert "do not repeat open without targetRef" in message


async def test_native_browser_mismatched_error_target_is_not_used_for_recovery(monkeypatch):
    other_target = "page-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    context = _native_browser_context(monkeypatch, lambda _: httpx.Response(409, json={
        "ok": False, "code": "PAGE_CHANGED", "targetRef": other_target,
    }))
    result = await _dispatch_browser(context, "snapshot", targetRef=_RETAINED_TARGET)
    message = json.loads(result.content)["user_message"]
    assert other_target not in message and _RETAINED_TARGET not in message
    assert "Run list" in message


@pytest.mark.parametrize("failure", [
    "legacy", "timeout", "invalid_response", "malformed_json", "oversized_response",
])
async def test_browser_unknown_open_blocks_duplicate_until_next_turn(monkeypatch, failure):
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["operation"] != "open" or payload.get("targetRef"):
            return httpx.Response(200, json={"targetRef": payload.get("targetRef")})
        if failure == "timeout":
            raise httpx.ReadTimeout("secret diagnostic", request=request)
        if failure == "invalid_response":
            return httpx.Response(200, json=[])
        if failure == "malformed_json":
            return httpx.Response(200, content=b"not-json secret diagnostic")
        if failure == "oversized_response":
            return httpx.Response(200, content=b"x" * 1000)
        return httpx.Response(409, json={"ok": False, "code": "PAGE_CHANGED"})

    context = _native_browser_context(monkeypatch, respond)
    if failure == "oversized_response":
        monkeypatch.setattr(module, "_MAX_RESPONSE_BYTES", 200)
    first = await _dispatch_browser(context, url="HTTPS://EXAMPLE.invalid:443#before")
    assert first.is_error
    assert "do not repeat open without targetRef" in first.content
    for operation, arguments in [
        ("list", {}), ("snapshot", {"targetRef": _RETAINED_TARGET}),
        ("open", {"targetRef": _RETAINED_TARGET, "url": "https://example.invalid/"}),
    ]:
        assert not (await _dispatch_browser(context, operation, **arguments)).is_error
    second = await _dispatch_browser(context, url="https://example.invalid/#after")
    assert second.is_error and "BROWSER_OPEN_UNRESOLVED" in second.content
    assert len(calls) == 4
    # Shallow per-call copies share turn state, but a new accepted turn is independent.
    next_turn = replace(context, execution_id="turn-two")
    await _dispatch_browser(next_turn, url="https://example.invalid/")
    assert len(calls) == 5
    for callback in context.turn_cleanup_callbacks:
        callback()
    assert context.browser_open_attempts == {}


async def test_browser_retained_tab_blocks_retry_even_when_navigation_did_not_start(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(409, json={
            "ok": False, "code": "TIMEOUT", "targetRef": _RETAINED_TARGET,
            "outcome": "not_started", "recovery": "inspect",
        })

    context = _native_browser_context(monkeypatch, respond)
    await _dispatch_browser(context, url="https://example.invalid/")
    result = await _dispatch_browser(context, url="https://example.invalid/")
    assert "BROWSER_OPEN_UNRESOLVED" in result.content
    assert _RETAINED_TARGET in result.content
    assert len(calls) == 1


@pytest.mark.parametrize("first_url", [
    " https://example.invalid/login",
    "https://example.invalid/login ",
    "   https://example.invalid/login   ",
])
async def test_browser_unknown_open_cannot_repeat_by_trimming_url_spaces(monkeypatch, first_url):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(409, json={"ok": False, "code": "PAGE_CHANGED"})

    context = _native_browser_context(monkeypatch, respond)
    first = await _dispatch_browser(context, url=first_url)
    assert first.is_error and "BROWSER_PAGE_CHANGED" in first.content
    second = await _dispatch_browser(context, url="https://example.invalid/login")
    assert second.is_error and "BROWSER_OPEN_UNRESOLVED" in second.content
    assert len(calls) == 1


@pytest.mark.parametrize("suffix", ["%20", "\u00a0"])
async def test_browser_open_keeps_path_whitespace_distinct(monkeypatch, suffix):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(409, json={"ok": False, "code": "PAGE_CHANGED"})

    context = _native_browser_context(monkeypatch, respond)
    await _dispatch_browser(context, url="https://example.invalid/login" + suffix)
    second = await _dispatch_browser(context, url="https://example.invalid/login")
    assert second.is_error and "BROWSER_PAGE_CHANGED" in second.content
    assert "BROWSER_OPEN_UNRESOLVED" not in second.content
    assert len(calls) == 2


@pytest.mark.parametrize("response", [
    {}, {"ok": True}, {"targetRef": None}, {"targetRef": ""},
    {"targetRef": "   "}, {"targetRef": []}, {"targetRef": 1},
    {"ok": True, "outcome": "not_started", "recovery": "stop"},
])
async def test_browser_incomplete_open_receipt_retains_unknown_outcome_guard(monkeypatch, response):
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["operation"] == "list":
            return httpx.Response(200, json={"targets": [{"targetRef": _RETAINED_TARGET}]})
        return httpx.Response(200, json=response)

    context = _native_browser_context(monkeypatch, respond)
    first = await _dispatch_browser(context, url="https://example.invalid/login")
    message = json.loads(first.content)["user_message"]
    assert first.is_error and "BROWSER_RESPONSE_INVALID" in message
    assert "outcome is unknown" in message and "Run list, then snapshot" in message
    assert "do not repeat open without targetRef" in message
    assert not (await _dispatch_browser(context, "list")).is_error
    second = await _dispatch_browser(context, url="https://example.invalid/login")
    assert second.is_error and "BROWSER_OPEN_UNRESOLVED" in second.content
    assert len(calls) == 2


async def test_browser_unresolved_open_is_isolated_from_other_sessions(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(409, json={"ok": False, "code": "PAGE_CHANGED"})

    context = _native_browser_context(monkeypatch, respond)
    await _dispatch_browser(context, url="https://example.invalid/")
    await _dispatch_browser(replace(context, session_key="session-two"),
                            url="https://example.invalid/")
    assert len(calls) == 2


@pytest.mark.parametrize("response", [
    {"targetRef": _RETAINED_TARGET},
    {"ok": False, "code": "INVALID_REQUEST"},
    {"ok": False, "code": "RESOURCE_LIMIT", "outcome": "not_started"},
])
async def test_browser_success_and_known_unstarted_opens_do_not_block_repetition(
    monkeypatch, response,
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    context = _native_browser_context(monkeypatch, respond)
    for _ in range(2):
        result = await _dispatch_browser(context, url="https://example.invalid/")
        assert result.is_error is (response.get("ok") is False)
        assert "BROWSER_OPEN_UNRESOLVED" not in result.content
    assert len(calls) == 2
    assert context.browser_open_attempts == {}


async def test_browser_pending_open_blocks_concurrent_duplicate_and_cancelled_replay():
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        entered.set()
        await finish.wait()
        return {"targetRef": _RETAINED_TARGET}

    context = ToolContext(
        is_owner=True, session_key="session-one", execution_id="turn-one",
        desktop_browser=SimpleNamespace(request=request),
    )
    token = current_tool_context.set(context)
    try:
        task = asyncio.create_task(browser("open", url="https://example.invalid/"))
        await entered.wait()
        with pytest.raises(SafeToolError, match="BROWSER_OPEN_UNRESOLVED"):
            await browser("open", url="https://example.invalid/#fragment")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(SafeToolError, match="BROWSER_OPEN_UNRESOLVED"):
            await browser("open", url="https://example.invalid/")
        assert len(calls) == 1
    finally:
        current_tool_context.reset(token)
