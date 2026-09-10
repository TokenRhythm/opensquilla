from __future__ import annotations

import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from opensquilla.tools.builtin import media
from opensquilla.tools.ssrf import environment_proxy_url
from opensquilla.tools.types import SafeToolError, ToolContext, ToolError, current_tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_target", [False, True])
async def test_image_tool_does_not_select_a_separate_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_target: bool,
) -> None:
    from PIL import Image

    Image.new("RGB", (2, 2)).save(tmp_path / "sample.png")
    monkeypatch.setenv("OPENSQUILLA_VISION_MODEL", "openai/unselected-vision")

    def forbidden_resolver(**kwargs):
        pytest.fail("image analysis must not read legacy model selection")

    monkeypatch.setattr(media, "_resolve_vision_provider_config", forbidden_resolver)
    context = ToolContext(
        workspace_dir=str(tmp_path),
        image_analysis_target=(lambda: None) if bound_target else None,
    )
    token = current_tool_context.set(context)
    try:
        result = json.loads(await media.image("sample.png", "Describe the colors"))
    finally:
        current_tool_context.reset(token)
    assert result["status"] == "not_analyzed"
    assert "description" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_image_tool_calls_only_the_bound_physical_provider(
    tmp_path: Path, fail: bool,
) -> None:
    from PIL import Image

    from opensquilla.provider.correlation_context import bind_provider_request_correlation
    from opensquilla.provider.types import (
        ChatConfig,
        ContentBlockImage,
        ProviderRequestCorrelation,
        TextDeltaEvent,
    )

    Image.new("RGB", (2, 2)).save(tmp_path / "sample.png")
    calls = []

    class PhysicalProvider:
        provider_name = "openai"
        model = "configured-vision"

        async def chat(self, messages, config):
            calls.append((messages, config))
            if fail:
                raise RuntimeError("temporary failure")
            yield TextDeltaEvent(text="Two colors")

    provider = PhysicalProvider()
    token = current_tool_context.set(ToolContext(
        workspace_dir=str(tmp_path),
        image_analysis_target=lambda: (provider, ChatConfig(model_vision_support="supported")),
    ))
    try:
        with bind_provider_request_correlation(ProviderRequestCorrelation(
            session_id="test-session", turn_id="test-turn", execution_id="test-call",
            call_kind="primary",
        )):
            result = json.loads(await media.image("sample.png", "Describe the colors"))
    finally:
        current_tool_context.reset(token)
    assert len(calls) == 1
    assert isinstance(calls[0][0][0].content[0], ContentBlockImage)
    assert calls[0][1].provider_request_correlation.call_kind == "auxiliary.media"
    assert calls[0][1].provider_request_max_chars > 0
    if fail:
        assert result["status"] == "analysis_failed"
    else:
        assert result["description"] == "Two colors"


def _write_pdf(path: Path) -> None:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(240, 160))
    pdf.drawString(32, 120, "Accuracy")
    pdf.rect(40, 30, 40, 70, fill=1)
    pdf.rect(100, 30, 40, 95, fill=1)
    pdf.save()
    path.write_bytes(buffer.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("support", "ensemble", "use_fallback"),
    [("supported", False, False), ("unsupported", False, False),
     ("unknown", False, False), ("supported", True, False),
     ("supported", False, True), ("unsupported", False, True)],
)
async def test_agent_binds_image_tool_to_actual_selected_deployment(
    tmp_path: Path, support: str, ensemble: bool, use_fallback: bool,
    budget: dict | None = None, expected_error: str | None = None,
    expected_auxiliary_calls: int | None = None, tool_count: int = 1,
    with_tracker: bool = False, duplicate_done: bool = False,
    reject_initial_image: bool = False,
) -> None:
    from types import SimpleNamespace

    from PIL import Image

    from opensquilla.engine import ToolResult
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.engine.types import AgentConfig
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.provider.protocol import count_provider_image_blocks
    from opensquilla.provider.types import (
        ContentBlockImage,
        DoneEvent,
        ErrorEvent,
        Message,
        ModelCapabilities,
        ProviderRequestCorrelation,
        TextDeltaEvent,
        ToolDefinition,
        ToolInputSchema,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )

    Image.new("RGB", (2, 2)).save(tmp_path / "sample.png")
    auxiliary_calls = []
    main_calls = []
    tool_results = []

    class SelectedProvider:
        provider_name = "ensemble" if ensemble else "openai"
        model = "selected-deployment"

        async def chat(self, messages, tools=None, config=None):
            if config.provider_request_correlation.call_kind == "auxiliary.media":
                auxiliary_calls.append((messages, tools, config))
                yield TextDeltaEvent(text="Image description")
                receipt = DoneEvent(
                    model=self.model, input_tokens=77, output_tokens=8,
                    billed_cost=1.0, cost_source="provider_billed",
                )
                yield receipt
                if duplicate_done:
                    yield receipt
                return
            if reject_initial_image and count_provider_image_blocks(messages):
                yield ErrorEvent(code="image_input_unsupported", message="Images not supported")
                return
            main_calls.append(messages)
            if len(main_calls) == 1:
                for index in range(tool_count):
                    yield ToolUseStartEvent(tool_use_id=f"analyze-{index}", tool_name="image")
                    yield ToolUseEndEvent(
                        tool_use_id=f"analyze-{index}", tool_name="image", arguments={
                            "path": "sample.png", "prompt": "Describe the colors",
                        },
                    )
                yield DoneEvent(
                    stop_reason="tool_use", model=self.model, input_tokens=10, output_tokens=2,
                )
            else:
                yield TextDeltaEvent(text="Finished")
                yield DoneEvent(model=self.model, input_tokens=10, output_tokens=2)

    selected = SelectedProvider()
    provider = selected
    if use_fallback:
        class Primary:
            provider_name = "openai"

            async def chat(self, messages, tools=None, config=None):
                yield ErrorEvent(code="503", message="Unavailable")

        fallback_config = SimpleNamespace(provider="openai", model=selected.model)

        class Selector:
            current_config = SimpleNamespace(provider="openai", model="primary")

            def next_fallback_after_failure(self, error):
                self.current_config = fallback_config
                return selected

        provider = _SelectorFallbackProvider(Primary(), Selector())
        provider.configure_fallback_deployment_vision_support([(fallback_config, support)])
        provider.configure_fallback_deployment_limits([
            (fallback_config, 0, 0, ModelCapabilities(supports_vision=support == "supported")),
        ])

    context = ToolContext(workspace_dir=str(tmp_path))

    async def handle_tool(call):
        # Ingress handlers can retain their original context after Agent has
        # replaced its own context to attach a configured result budget.
        token = current_tool_context.set(context)
        try:
            result = await media.image(**call.arguments)
        finally:
            current_tool_context.reset(token)
        tool_results.append(json.loads(result))
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=result)

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="primary" if use_fallback else selected.model,
            model_vision_support="unsupported" if use_fallback else support,
            max_provider_retries=0,
            tool_result_dispatch_max_chars=5000,
            **(budget or {}),
        ),
        tool_context=context,
        usage_tracker=UsageTracker() if with_tracker else None,
        session_key="agent:main:synthetic-image-analysis",
        tool_handler=handle_tool,
        tool_definitions=[ToolDefinition(
            name="image", description="Analyze a workspace image",
            input_schema=ToolInputSchema(properties={
                "path": {"type": "string"}, "prompt": {"type": "string"},
            }, required=["path", "prompt"]),
        )],
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="test-session", turn_id="test-turn", execution_id="test-execution",
            call_kind="primary",
        ),
    )
    extra_messages = [Message(role="user", content=[
        ContentBlockImage(media_type="image/png", data="c3ludGhldGlj"),
    ])] if reject_initial_image else None
    events = [event async for event in agent.run_turn(
        "Analyze the workspace file", extra_messages=extra_messages,
    )]
    errors = [event for event in events if event.kind == "error"]
    assert [event.code for event in errors] == ([expected_error] if expected_error else [])
    assert len(main_calls) == (1 if expected_error else 2)
    allowed = support == "supported" and not ensemble
    call_count = int(allowed) if expected_auxiliary_calls is None else expected_auxiliary_calls
    assert len(auxiliary_calls) == call_count
    assert len([result for result in tool_results if "description" in result]) == call_count
    if auxiliary_calls:
        assert auxiliary_calls[0][1] is None
        assert auxiliary_calls[0][2].physical_attempt_limit == 1
    elif not allowed:
        assert tool_results[0]["status"] == "not_analyzed"
    completed = [event for event in events if event.kind == "done"]
    if completed:
        assert completed[-1].input_tokens == len(main_calls) * 10 + call_count * 77
        assert completed[-1].output_tokens == len(main_calls) * 2 + call_count * 8
        assert completed[-1].billed_cost == call_count * 1.0
        assert sum(row["input_tokens"] for row in completed[-1].model_usage_breakdown) == (
            completed[-1].input_tokens
        )
    assert context.image_analysis_target is None
    assert agent._tool_context.image_analysis_target is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tracker", [False, True])
async def test_image_tool_receipt_is_counted_once(
    tmp_path: Path, with_tracker: bool,
) -> None:
    await test_agent_binds_image_tool_to_actual_selected_deployment(
        tmp_path, "supported", False, False,
        with_tracker=with_tracker, duplicate_done=True,
    )


@pytest.mark.asyncio
async def test_image_tool_does_not_reprobe_a_rejected_deployment(tmp_path: Path) -> None:
    await test_agent_binds_image_tool_to_actual_selected_deployment(
        tmp_path, "supported", False, False,
        reject_initial_image=True, expected_auxiliary_calls=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "error", "calls", "tool_count"),
    [
        ({"max_turn_llm_calls": 1}, "turn_llm_call_budget_exceeded", 0, 1),
        ({"max_turn_llm_calls": 2}, "turn_llm_call_budget_exceeded", 1, 2),
        ({"max_turn_billed_cost_usd": 0.1}, "turn_billed_cost_budget_exceeded", 1, 1),
        ({"max_turn_cost_usd": 0.1}, "turn_cost_budget_exceeded", 1, 1),
        ({"max_turn_input_tokens": 50}, "turn_input_token_budget_exceeded", 1, 1),
        ({"max_turn_output_tokens": 5}, "turn_output_token_budget_exceeded", 1, 1),
    ],
)
async def test_image_tool_shares_the_turn_hard_budget(
    tmp_path: Path, budget: dict, error: str, calls: int, tool_count: int,
) -> None:
    await test_agent_binds_image_tool_to_actual_selected_deployment(
        tmp_path, "supported", False, False, budget=budget,
        expected_error=error, expected_auxiliary_calls=calls, tool_count=tool_count,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("use_fallback", [False, True])
@pytest.mark.parametrize("explicit_cap", [0, 17])
async def test_image_tool_keeps_operator_request_limits(
    use_fallback: bool, explicit_cap: int,
) -> None:
    from types import SimpleNamespace

    from opensquilla.engine.agent import Agent
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.engine.types import AgentConfig
    from opensquilla.provider.auxiliary_budget import AuxiliaryRequestTooLargeError
    from opensquilla.provider.types import DoneEvent

    calls = []

    class Provider:
        provider_name = "openai"
        model = "selected-deployment"

        async def chat(self, messages, config):
            calls.append(messages)
            yield DoneEvent()

    provider = Provider()
    if use_fallback:
        deployment = SimpleNamespace(provider="openai", model=provider.model)
        provider = _SelectorFallbackProvider(provider, SimpleNamespace(current_config=deployment))
        provider.configure_fallback_deployment_vision_support([(deployment, "supported")])
        provider._note_fallback_hop()
    agent = Agent(provider=provider, config=AgentConfig(
        model_vision_support="supported", max_tokens=64,
        context_window_tokens_global_override=1000,
        provider_request_proof_max_chars=explicit_cap,
        provider_request_proof_max_chars_explicit=bool(explicit_cap),
    ))
    target = agent._image_analysis_target()
    assert target is not None
    assert target[1].context_window_tokens_global_override == 1000
    if explicit_cap:
        assert target[1].provider_request_max_chars <= explicit_cap
        assert target[1].provider_request_max_chars_explicit_cap == explicit_cap
    token = current_tool_context.set(
        ToolContext(image_analysis_target=agent._image_analysis_target)
    )
    try:
        with pytest.raises(AuxiliaryRequestTooLargeError):
            await media._call_vision_provider("c3ludGhldGlj", "image/png", "detail " * 1000)
    finally:
        current_tool_context.reset(token)
    assert calls == []


@pytest.mark.asyncio
async def test_image_tool_renders_workspace_pdf_before_vision_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "figure.pdf"
    _write_pdf(pdf_path)
    seen: dict[str, str] = {}

    async def fake_vision(b64_data: str, media_type: str, prompt: str) -> str:
        seen["media_type"] = media_type
        seen["prompt"] = prompt
        seen["payload_prefix"] = b64_data[:16]
        return "rendered chart"

    monkeypatch.setattr(media, "_call_vision_provider", fake_vision)

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        result = json.loads(await media.image("/workspace/figure.pdf", "describe the chart"))
    finally:
        current_tool_context.reset(token)

    assert result["description"] == "rendered chart"
    assert result["path"] == "/workspace/figure.pdf"
    assert seen == {
        "media_type": "image/png",
        "prompt": "describe the chart",
        "payload_prefix": seen["payload_prefix"],
    }
    assert seen["payload_prefix"]


@pytest.mark.asyncio
async def test_image_tool_reports_attachment_display_name_as_safe_path_error(
    tmp_path: Path,
) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        with pytest.raises(SafeToolError) as exc_info:
            await media.image(
                "ab367eca88278bd6905ff705e3fee0b2907b86fbda389d9ed3f9c9d86f4603f5.png",
                "describe this image",
            )
    finally:
        current_tool_context.reset(token)

    message = exc_info.value.user_message
    assert "not accessible by the image tool" in message
    assert "local file path or HTTP(S) URL" in message
    assert "chat attachment" in message


@pytest.mark.asyncio
async def test_image_tool_reports_unsupported_format_as_safe_error(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not an image", encoding="utf-8")

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        with pytest.raises(SafeToolError) as exc_info:
            await media.image("notes.txt", "describe this image")
    finally:
        current_tool_context.reset(token)

    assert "Unsupported image format" in exc_info.value.user_message


@pytest.mark.asyncio
async def test_image_tool_reports_corrupt_image_as_safe_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.png"
    source.write_bytes(b"not a png")

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        with pytest.raises(SafeToolError) as exc_info:
            await media.image("broken.png", "describe this image")
    finally:
        current_tool_context.reset(token)

    assert "corrupt or unreadable" in exc_info.value.user_message


_PUBLIC_IP = "93.184.216.34"


@pytest.mark.asyncio
async def test_fetch_image_url_pins_vetted_ip_against_dns_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    counter = {"n": 0}
    attempted_hosts: list[str] = []
    real = socket.getaddrinfo

    def rebinding_getaddrinfo(host, req_port, *args, **kwargs):
        host_str = host.decode("ascii") if isinstance(host, bytes) else host
        if host_str != "rebind.test":
            return real(host, req_port, *args, **kwargs)
        counter["n"] += 1
        # First resolution (the guard) sees a public IP; every later resolution
        # rebinds to loopback — the connection must never follow it.
        if counter["n"] == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, req_port or 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", req_port or 0))]

    async def fail_connection_immediately(
        _transport: httpx.AsyncHTTPTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        # The production pinned transport rewrites the logical hostname before
        # delegating here. Recording that boundary proves the vetted address is
        # used without waiting for a real public-network timeout.
        attempted_hosts.append(request.url.host)
        raise httpx.ConnectError("deterministic test connection failure", request=request)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        fail_connection_immediately,
    )

    with pytest.raises(ToolError, match="deterministic test connection failure"):
        await media._fetch_image_url("http://rebind.test:8080/metadata.png")

    assert attempted_hosts == [_PUBLIC_IP]
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_fetch_image_url_resolves_relative_redirect_against_logical_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    requested: list[str] = []

    class RedirectingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> RedirectingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            requested.append(url)
            if len(requested) == 1:
                return httpx.Response(
                    302,
                    headers={"location": "/image.png"},
                    request=httpx.Request("GET", "https://93.184.216.34/start"),
                )
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"png-bytes",
                request=httpx.Request("GET", "https://93.184.216.34/image.png"),
            )

    monkeypatch.setattr(media, "validate_http_url_for_fetch", lambda url: ["93.184.216.34"])
    monkeypatch.setattr(httpx, "AsyncClient", RedirectingClient)
    monkeypatch.setattr(
        "opensquilla.tools.ssrf.pinned_transport", lambda *args, **kwargs: object()
    )

    image_bytes, media_type = await media._fetch_image_url(
        "https://images.example.test/start"
    )

    assert requested == [
        "https://images.example.test/start",
        "https://images.example.test/image.png",
    ]
    assert image_bytes == b"png-bytes"
    assert media_type == "image/png"


@pytest.mark.asyncio
async def test_fetch_image_url_uses_opted_in_environment_proxy_with_pinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API name
            seen["path"] = self.path
            seen["host"] = self.headers.get("Host", "")
            if self.path.startswith("http://127.0.0.1:"):
                payload = b"proxied-png"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(502)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    proxy = HTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    port = int(proxy.server_address[1])
    try:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("REQUEST_METHOD", raising=False)
        monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
        proxy_url = f"http://127.0.0.1:{port}"
        target_url = f"http://proxy-target.test:{port}/image.png"
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        assert environment_proxy_url(target_url) == proxy_url
        monkeypatch.setattr(media, "validate_http_url_for_fetch", lambda url: ["127.0.0.1"])

        image_bytes, media_type = await media._fetch_image_url(target_url)
    finally:
        proxy.shutdown()
        proxy.server_close()

    assert image_bytes == b"proxied-png"
    assert media_type == "image/png"
    assert seen["path"].startswith(f"http://127.0.0.1:{port}/")
    assert seen["host"] == f"proxy-target.test:{port}"


@pytest.mark.asyncio
async def test_pdf_applies_filesystem_read_authorization_before_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import filesystem

    blocked = {
        "status": "path_access_required",
        "reason": "attachment_read_grant_required",
    }
    monkeypatch.setattr(
        filesystem,
        "_sandbox_path_access_envelope",
        lambda _path, *, write: blocked if not write else None,
    )
    result = json.loads(await media.pdf(str(tmp_path / "unopened.pdf")))

    assert result == blocked
