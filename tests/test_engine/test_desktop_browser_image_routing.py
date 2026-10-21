from __future__ import annotations

import base64
import json
from dataclasses import replace

import httpx
import pytest

from opensquilla.browser import DesktopBrowserClient
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.mcp.desktop_browser import DesktopBrowserMCPClient, browser_tool_policy
from opensquilla.mcp.discovery import close_active_clients, register_client_tools
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockToolResult,
    DoneEvent,
    ErrorEvent,
    ModelCapabilities,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.tools.browser_policy import BROWSER_MCP_TOOLS
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context
from tests.helpers.image_bytes import image_bytes


class _BrowserProvider:
    provider_name = "openai"

    def __init__(self, model="synthetic-vision", *, route=False, fail=False):
        self.model = model
        self.route = route
        self.fail = fail
        self.payloads = []
        self.adapter = OpenAIProvider(api_key="dummy", model=model)

    def project_final_request(self, *args, **kwargs):
        return self.adapter.project_final_request(*args, **kwargs)

    async def list_models(self):
        return []

    async def prepare_image_continuation(self, messages, config):
        if not self.route:
            return None
        assert any(
            isinstance(block, ContentBlockImage)
            for message in messages if isinstance(message.content, list)
            for block in message.content
        )
        self.model = "synthetic-routed-vision"
        return config.model_copy(update={
            "model_vision_support": "supported",
            "model_capabilities": ModelCapabilities(supports_vision=True, supports_tools=True),
        })

    async def chat(self, messages, tools=None, config=None):
        payload, *_ = self.adapter._build_payload(messages, tools, config or ChatConfig())
        self.payloads.append(payload)
        completed = sum(
            isinstance(block, ContentBlockToolResult)
            for message in messages if isinstance(message.content, list)
            for block in message.content
        )
        if completed == 1 and self.fail:
            yield ErrorEvent(code="503", message="Synthetic deployment unavailable")
            return
        if completed >= 2:
            yield TextDeltaEvent(text="Inspection complete")
            yield DoneEvent(stop_reason="end_turn")
            return
        name = "browser_observe" if completed == 0 else "browser_batch"
        arguments = {"targetRef": "page-fixture", "observationMode": "auto"}
        if name == "browser_batch":
            arguments["actions"] = [{
                "action": "click", "x": 1, "y": 1,
                "observationId": "observation-1", "imageId": "image-1",
            }]
        call_id = f"browser-call-{completed}"
        tool_name = f"mcp__desktop-browser__{name}"
        yield ToolUseStartEvent(tool_use_id=call_id, tool_name=tool_name)
        yield ToolUseEndEvent(
            tool_use_id=call_id, tool_name=tool_name, arguments=arguments,
        )
        yield DoneEvent(stop_reason="tool_use", input_tokens=3, output_tokens=2)


@pytest.mark.parametrize("ambient_context", [False, True])
@pytest.mark.parametrize(
    "mode", ["supported", "unsupported", "unknown", "ensemble", "routed", "fallback"],
)
async def test_browser_mcp_uses_existing_tool_image_route(monkeypatch, mode, ambient_context):
    """A standard MCP image reaches the active model through ordinary tool dispatch."""
    await _check_browser_image_route(monkeypatch, mode, ambient_context=ambient_context)


@pytest.mark.parametrize("mode", ["supported", "routed"])
async def test_unbound_ambient_context_cannot_authorize_coordinate_actions(monkeypatch, mode):
    """An external caller's premature context copy has no active model authority."""
    await _check_browser_image_route(monkeypatch, mode, unbound_context=True)


async def _check_browser_image_route(
    monkeypatch, mode, *, ambient_context=False, unbound_context=False,
):
    png = base64.b64encode(image_bytes()).decode()
    requests = []

    async def respond(request):
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {
                "tools": {}, "experimental": {
                    "opensquilla/browser": {"coordinateAuthority": "browser-state"},
                },
            }}
        elif method == "tools/list":
            result = {"tools": [{
                "name": name, "description": name,
                "inputSchema": {"type": "object", "properties": {
                    "targetRef": {"type": "string"},
                    "observationMode": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "object"}},
                }},
            } for name in sorted(BROWSER_MCP_TOOLS)]}
        else:
            assert method == "tools/call"
            requests.append(body["params"])
            index = len(requests)
            observation = {
                "targetRef": "page-fixture", "observationId": f"observation-{index}",
                "consistency": "consistent", "imageStatus": "available",
                "image": {"imageId": f"image-{index}", "width": 2, "height": 2},
            }
            structured = {"targetRef": "page-fixture", "observation": observation}
            result = {
                "structuredContent": structured,
                "content": [
                    {"type": "text", "text": json.dumps(structured)},
                    {"type": "image", "data": png, "mimeType": "image/png"},
                ],
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    primary = _BrowserProvider(route=mode == "routed", fail=mode == "fallback")
    secondary = _BrowserProvider("synthetic-text-fallback")
    support = "unsupported" if mode in {"unsupported", "routed"} else "supported"
    if mode == "unknown":
        support = "unknown"
    if mode == "ensemble":
        primary.provider_name = "ensemble"
    provider = primary
    if mode != "routed":
        configs = [ProviderConfig(provider="openai", model=leg.model, api_key="dummy")
                   for leg in (primary, secondary)]
        providers = {primary.model: primary, secondary.model: secondary}
        monkeypatch.setattr("opensquilla.provider.selector._build_provider",
                            lambda config: providers[config.model])
        selector = ModelSelector(SelectorConfig(
            primary=configs[0], fallbacks=[configs[1]] if mode == "fallback" else [],
        ))
        provider = _SelectorFallbackProvider(selector.resolve(), selector)
        provider.configure_fallback_deployment_vision_support([
            (configs[0], support), (configs[1], "unsupported"),
        ])
    browser = DesktopBrowserClient("http://127.0.0.1:43123/v1/browser", "s" * 48)
    registry = ToolRegistry()
    unbound_token = None
    try:
        await register_client_tools(
            DesktopBrowserMCPClient(browser), registry, spec_transform=browser_tool_policy,
        )
        context = ToolContext(
            is_owner=True, caller_kind=CallerKind.WEB,
            session_key="synthetic-session", usage_root_turn_id="synthetic-turn",
            desktop_browser=browser,
        )
        dispatched_targets = []
        results = []
        handler = build_tool_handler(registry, context)

        async def handle(call):
            target = context.image_analysis_target()
            dispatched_targets.append(None if target is None else target[0].model)
            token = None
            if ambient_context:
                # Dispatch middleware copies the context after turn callbacks are bound.
                bound = current_tool_context.get()
                assert bound is not None
                token = current_tool_context.set(replace(bound, on_runtime_event=lambda _: None))
            try:
                result = await handler(call)
            finally:
                if token is not None:
                    current_tool_context.reset(token)
            results.append(result)
            return result

        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=4, model_vision_support=support, max_provider_retries=0,
                preserve_historical_images=True,
            ),
            tool_context=context, tool_registry=registry, tool_handler=handle,
            tool_definitions=registry.to_tool_definitions(context),
        )
        if unbound_context:
            # This copy is never passed to Agent, so no turn callbacks are bound to it.
            unbound_token = current_tool_context.set(replace(
                context, on_runtime_event=lambda _: None,
            ))
        _ = [event async for event in agent.run_turn("Inspect the page, then click its control.")]
        active = secondary if mode == "fallback" else primary
        has_native_image = mode in {"supported", "routed"}
        continuation_wire = json.dumps(active.payloads[-1], ensure_ascii=False)
        assert (f"data:image/png;base64,{png}" in continuation_wire) is has_native_image
        assert ("图片未分析" in continuation_wire) is not has_native_image
        assert requests[0]["arguments"]["observationMode"] == "auto"
        coordinate_allowed = has_native_image and not unbound_context
        assert [request["name"] for request in requests] == (
            ["browser_observe", "browser_batch"] if coordinate_allowed else ["browser_observe"]
        )
        assert all("nativeImageEvidence" not in request.get("_meta", {}) for request in requests)
        expected_before = (
            None if mode in {"unsupported", "unknown", "ensemble", "routed"} else primary.model
        )
        expected_after = primary.model if has_native_image else None
        assert dispatched_targets == [expected_before, expected_after]
        if not coordinate_allowed:
            assert results[-1].is_error
            assert "VISION_UNAVAILABLE" in results[-1].content
        if mode == "fallback":
            assert f"data:image/png;base64,{png}" in json.dumps(primary.payloads[-1])
        if mode == "routed":
            assert agent.config.model_vision_support == "supported"
        assert context.image_analysis_target is None
        assert context.tool_result_media == {}
    finally:
        if unbound_token is not None:
            current_tool_context.reset(unbound_token)
        await close_active_clients(owner="desktop-browser")
