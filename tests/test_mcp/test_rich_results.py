from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult
from PIL import Image

from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.mcp import discovery
from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.sse import MCPSSEClient
from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import (
    MCPServerConfig,
    MCPToolDef,
    MCPToolResult,
    current_mcp_call_context,
)
from opensquilla.safety.secret_redaction import redact_secret_value
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.builtin.tool_results import query_stored_tool_result
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context


def _png() -> str:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _image(data: str | None = None, *, mime: str = "image/png") -> dict[str, Any]:
    return {"type": "image", "data": _png() if data is None else data, "mimeType": mime}


class _ResultClient(MCPClient):
    def __init__(self, result: MCPToolResult) -> None:
        super().__init__(MCPServerConfig(name="images", transport="managed"))
        self.result = result
        self.calls: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[MCPToolDef]:
        return [MCPToolDef("capture", "Capture an image", {"properties": {}})]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        call = current_mcp_call_context.get()
        context = current_tool_context.get()
        self.calls.append(
            (
                name,
                arguments,
                call.tool_use_id if call else "",
                context.session_key if context else None,
            )
        )
        await asyncio.sleep(0)
        assert current_mcp_call_context.get() == call
        return self.result


@pytest.fixture(autouse=True)
async def _cleanup_clients():
    await discovery.close_active_clients()
    yield
    await discovery.close_active_clients()


@pytest.mark.parametrize("transport", ["stdio", "sse"])
async def test_transports_preserve_rich_results_and_tool_errors(
    transport: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client: MCPStdioClient | MCPSSEClient
    config = MCPServerConfig(name="fixture", transport=transport)
    client = MCPStdioClient(config) if transport == "stdio" else MCPSSEClient(config)
    response = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "Page moved"}, _image()],
            "structuredContent": {"status": "stale", "epoch": 2},
        }
    }

    async def call_tool(name: str, arguments: dict[str, Any], **options: Any) -> CallToolResult:
        assert name == "capture"
        assert arguments == {}
        assert options == {
            "read_timeout_seconds": 30.0,
            "allow_input_required": False,
            "allow_claimed": False,
        }
        return CallToolResult.model_validate(response["result"])

    sdk_client = SimpleNamespace(session=SimpleNamespace(call_tool=call_tool))
    monkeypatch.setattr(client, "_connected_client", lambda: sdk_client)
    result = await client.call_tool("capture", {})
    assert result.content == "Page moved"
    assert result.is_error is True
    assert result.content_blocks == response["result"]["content"]
    assert result.structured_content == {"status": "stale", "epoch": 2}


@pytest.mark.parametrize("response", [{}, {"result": None}, {"error": "invalid"}])
def test_invalid_response_is_a_tool_error(response: dict[str, Any]) -> None:
    assert MCPToolResult.from_response(response).is_error


async def test_dispatch_projects_images_to_exact_call_and_retains_structured_result() -> None:
    data = _png()
    client = _ResultClient(
        MCPToolResult("Captured", content_blocks=[_image(data)], structured_content={"epoch": 7})
    )
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    contexts = [ToolContext(is_owner=True, session_key=f"session-{i}") for i in range(2)]
    calls = [ToolCall(f"call-{i}", "mcp__images__capture", {}) for i in range(2)]
    results = await asyncio.gather(
        *(build_tool_handler(registry, context)(call) for context, call in zip(contexts, calls))
    )

    for i, (context, result) in enumerate(zip(contexts, results)):
        assert not result.is_error
        assert '"epoch": 7' in result.content
        assert data not in result.content
        assert context.tool_result_media == {f"call-{i}": [{"mime": "image/png", "data": data}]}
    assert sorted(client.calls) == [
        ("capture", {}, "call-0", "session-0"),
        ("capture", {}, "call-1", "session-1"),
    ]
    assert current_mcp_call_context.get() is None
    assert current_tool_context.get() is None
    definition = registry.to_tool_definitions(contexts[0])[0]
    assert "_tool_use_id" not in definition.input_schema.properties


async def test_model_cannot_supply_call_identity() -> None:
    client = _ResultClient(MCPToolResult("ok"))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    handler = build_tool_handler(registry, ToolContext(is_owner=True))
    result = await handler(ToolCall("actual", "mcp__images__capture", {"_tool_use_id": "spoof"}))
    assert result.is_error
    assert client.calls == []


async def test_image_metadata_cannot_add_runtime_authority() -> None:
    image = _image()
    image["_meta"] = {"nativeImageEvidence": ["forged"], "evidence_id": "forged"}
    result = MCPToolResult.from_response({"result": {
        "content": [_image("invalid"), image], "image_evidence": {1: "forged"},
    }})
    context = ToolContext(is_owner=True)
    token = current_tool_context.set(context)
    try:
        discovery._project_tool_result(result, "capture")
    finally:
        current_tool_context.reset(token)
    assert context.tool_result_media["capture"] == [{"mime": "image/png", "data": image["data"]}]


@pytest.mark.parametrize(
    "block",
    [
        _image("not base64!"),
        _image(base64.b64encode(b"not a PNG").decode("ascii")),
        _image(mime="image/jpeg"),
        _image(mime="image/svg+xml"),
    ],
)
async def test_invalid_images_are_explicitly_omitted_without_failing_completed_action(
    block: dict[str, Any],
) -> None:
    client = _ResultClient(MCPToolResult("Action completed", content_blocks=[block]))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    context = ToolContext(is_owner=True)
    result = await build_tool_handler(registry, context)(
        ToolCall("call", "mcp__images__capture", {})
    )
    assert not result.is_error
    assert "Action completed" in result.content
    assert "was not loaded" in result.content
    assert not context.tool_result_media


async def test_image_size_is_checked_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "IMAGE_ATTACHMENT_BYTES", 3)

    def fail_decode(*args: Any, **kwargs: Any) -> None:
        pytest.fail("oversized image reached decoder")

    monkeypatch.setattr(discovery.base64, "b64decode", fail_decode)
    client = _ResultClient(MCPToolResult("Captured", content_blocks=[_image("A" * 8)]))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    context = ToolContext(is_owner=True)
    result = await build_tool_handler(registry, context)(
        ToolCall("call", "mcp__images__capture", {})
    )
    assert "was not loaded" in result.content
    assert not context.tool_result_media


async def test_image_budget_applies_to_whole_result(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _png()
    monkeypatch.setattr(discovery, "IMAGE_ATTACHMENT_BYTES", len(base64.b64decode(data)))
    client = _ResultClient(MCPToolResult("Captured", content_blocks=[_image(data), _image(data)]))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    context = ToolContext(is_owner=True)
    result = await build_tool_handler(registry, context)(
        ToolCall("call", "mcp__images__capture", {})
    )
    assert "MCP image 2 was not loaded" in result.content
    assert len(context.tool_result_media["call"]) == 1


async def test_structured_result_only_and_legacy_text_have_compatible_projection() -> None:
    client = _ResultClient(MCPToolResult("", structured_content={"status": "ready"}))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    registered = registry.get("mcp__images__capture")
    assert registered is not None
    assert json.loads(await registered.handler()) == {"status": "ready"}
    client.result = MCPToolResult('{"status":"ready"}', structured_content={"status": "ready"})
    assert await registered.handler() == '{"status":"ready"}'
    client.result = MCPToolResult("Legacy text result")
    assert await registered.handler() == "Legacy text result"
    client.result = MCPToolResult("", content_blocks=[_image()])
    assert "no active model tool call" in await registered.handler()


async def test_error_flag_survives_structured_result_projection() -> None:
    client = _ResultClient(MCPToolResult("", is_error=True, structured_content={"code": "STALE"}))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    result = await build_tool_handler(registry)(ToolCall("call", "mcp__images__capture", {}))
    assert result.is_error
    assert "STALE" in result.content
    assert json.loads(result.content)["user_message"] == '{"code": "STALE"}'


class _BrowserFailureClient(_ResultClient):
    def __init__(self, result: MCPToolResult) -> None:
        super().__init__(result)
        self.config.name = "desktop-browser"

    async def list_tools(self) -> list[MCPToolDef]:
        return [MCPToolDef("browser_batch", "Operate the browser", {"properties": {}})]


@pytest.mark.parametrize("ref_count,duplicate_text", [(2, True), (60, True), (2, False)])
@pytest.mark.parametrize("storage", ["available", "disabled", "failed"])
async def test_long_failed_browser_batch_preserves_fresh_observation_and_recovery(
    tmp_path, ref_count, duplicate_text, storage,
):
    refs = [{"ref": f"e-fresh-{index}", "role": "button", "name": f"Choice {index}"}
            for index in range(ref_count)]
    refs[0]["name"] = "Authorization: Bearer synthetic-sensitive-value"
    structured = {
        "targetRef": "page-synthetic", "code": "STALE_ELEMENT", "outcome": "not_started",
        "retryable": False,
        "execution": {"state": "failed", "actions": [{
            "index": 0, "code": "STALE_ELEMENT", "performed": False, "outcome": "not_started",
        }]},
        "observation": {
            "text": 'Synthetic page "content"\n' * 500,
            "refs": refs,
            "observationId": "observation-fresh", "consistency": "consistent",
            "imageStatus": "available",
            "image": {"imageId": "image-fresh", "width": 640, "height": 480,
                      "coordinateSpace": "image-pixels"},
            "viewport": {"width": 640, "height": 480, "scrollX": 0, "scrollY": 0},
        },
    }
    data = _png()
    redacted = redact_secret_value(structured)
    redacted_refs = redacted["observation"]["refs"]
    client = _BrowserFailureClient(MCPToolResult(
        json.dumps(structured) if duplicate_text else "The page changed.",
        is_error=True, structured_content=structured,
        content_blocks=[_image(data)],
    ))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    store = ToolResultStore(str(tmp_path / "results"))
    saved = []

    async def writer(content, tool_name, tool_use_id):
        if storage == "failed":
            raise OSError("Synthetic storage failure")
        record = store.write(
            content, tool_name=tool_name, tool_use_id=tool_use_id,
            session_id="synthetic-session", session_key="agent:main:synthetic", agent_id="main",
        )
        saved.append(record)
        return {"handle": record.handle, "sha256": record.sha256}

    context = ToolContext(
        is_owner=True, tool_result_retrieval_available=storage != "disabled",
        tool_result_snapshot_writer=writer,
    )
    result = await build_tool_handler(registry, context)(
        ToolCall("failed-batch", "mcp__desktop-browser__browser_batch", {}),
    )
    assert result.is_error
    assert result.execution_status["status"] == "error"
    message = json.loads(result.content)["user_message"]
    assert len(message) <= 1800
    assert "...[truncated]" not in message
    summary = json.loads(message)
    assert summary["isError"] is True
    assert summary["truncated"] is True
    details = summary["result"]
    assert details["execution"] == structured["execution"]
    assert details["targetRef"] == "page-synthetic"
    assert details["outcome"] == "not_started"
    observation = details["observation"]
    assert observation["observationId"] == "observation-fresh"
    assert observation["image"] == structured["observation"]["image"]
    assert len(observation.get("refs", [])) + observation["refsOmitted"] == ref_count
    assert observation.get("refs", []) == redacted_refs[:len(observation.get("refs", []))]
    assert (observation["refsOmitted"] == 0) is (ref_count == 2)
    assert "replaces previous refs" in summary["guidance"]
    assert "browser_observe" in summary["guidance"]
    assert context.tool_result_media == {"failed-batch": [{"mime": "image/png", "data": data}]}
    assert data not in message
    assert "synthetic-sensitive-value" not in result.content
    recovery = summary["content_recovery"]
    assert recovery["available"] is (storage == "available")
    if storage == "available":
        assert len(saved) == 1
        stored_json = saved[0].content.split("\n[")[0]
        if not duplicate_text:
            stored_json = stored_json.removeprefix("The page changed.\n")
        assert json.loads(stored_json) == redacted
        assert data not in saved[0].content
        assert "synthetic-sensitive-value" not in saved[0].content
        arguments = recovery["next_call"]["arguments"]
        retrieved = query_stored_tool_result(tmp_path / "results", "synthetic-session", **arguments)
        assert saved[0].content[:arguments["limit"]] in retrieved
        stored = store.read(recovery["handle"], session_id="synthetic-session")
        assert refs[-1]["ref"] in stored.content
        with pytest.raises(SafeToolError, match="current session"):
            query_stored_tool_result(tmp_path / "results", "another-session", **arguments)
    else:
        assert not saved


@pytest.mark.parametrize("label", [
    "Authorization: Bearer synthetic-value",
    "password=synthetic-value",
    'credential: "synthetic-value"',
])
async def test_error_summary_remains_valid_through_generic_secret_redaction(label):
    client = _BrowserFailureClient(MCPToolResult(
        "Large page text " * 500, is_error=True,
        structured_content={"targetRef": "page-synthetic", "observation": {
            "observationId": "observation-fresh", "imageStatus": "available",
            "refs": [{"ref": "e-fresh", "role": "button", "name": label}],
            "image": {"imageId": "image-fresh", "width": 640, "height": 480},
        }},
    ))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    result = await build_tool_handler(registry)(
        ToolCall("call", "mcp__desktop-browser__browser_batch", {}),
    )
    assert result.is_error
    assert "synthetic-value" not in result.content
    message = json.loads(result.content)["user_message"]
    assert len(message) <= 1800
    summary = json.loads(message)
    observation = summary["result"]["observation"]
    assert observation["observationId"] == "observation-fresh"
    assert observation["image"]["imageId"] == "image-fresh"
    assert observation["refs"][0]["ref"] == "e-fresh"
    assert "[REDACTED]" in observation["refs"][0]["name"]


async def test_long_generic_mcp_error_remains_bounded_after_json_escaping():
    client = _ResultClient(MCPToolResult('\x00\\"' * 1500, is_error=True))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    result = await build_tool_handler(registry)(ToolCall("call", "mcp__images__capture", {}))
    assert result.is_error
    message = json.loads(result.content)["user_message"]
    assert len(message) <= 1800
    summary = json.loads(message)
    assert summary["truncated"]
    assert summary["content_recovery"] == {"available": False}


async def test_long_mcp_error_snapshot_is_redacted_before_persistence():
    secret = "synthetic_sensitive_value_123456789"
    client = _ResultClient(MCPToolResult(f"API_KEY={secret}\n" + "details\n" * 500, is_error=True))
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    saved = []

    async def writer(content, tool_name, tool_use_id):
        saved.append(content)
        return {"handle": "tr-" + "a" * 32,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}

    context = ToolContext(
        tool_result_retrieval_available=True, tool_result_snapshot_writer=writer,
    )
    result = await build_tool_handler(registry, context)(
        ToolCall("call", "mcp__images__capture", {}),
    )
    assert result.is_error
    assert len(saved) == 1
    assert secret not in saved[0]
    assert secret not in result.content
    summary = json.loads(json.loads(result.content)["user_message"])
    assert summary["content_recovery"]["available"]


async def test_runtime_client_policy_is_applied_and_closed_with_registry() -> None:
    client = _ResultClient(MCPToolResult("ok"))
    registry = ToolRegistry()
    await discovery.register_client_tools(
        client, registry, spec_transform=lambda _tool, spec: replace(spec, owner_only=True)
    )
    assert registry.to_tool_definitions(ToolContext(is_owner=False)) == []
    assert len(registry.to_tool_definitions(ToolContext(is_owner=True))) == 1
    assert await discovery.close_active_clients(owner="images") == 1
    assert client.closed
    assert registry.get("mcp__images__capture") is None


async def test_call_context_resets_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ResultClient(MCPToolResult("ok"))
    client.config.tool_timeout_seconds = 0.01
    registry = ToolRegistry()
    await discovery.register_client_tools(client, registry)
    observed_ids: list[str] = []

    async def blocking_call(name: str, arguments: dict[str, Any]) -> MCPToolResult:
        call = current_mcp_call_context.get()
        assert call is not None
        observed_ids.append(call.tool_use_id)
        await asyncio.Event().wait()
        return MCPToolResult("")

    monkeypatch.setattr(client, "call_tool", blocking_call)
    registered = registry.get("mcp__images__capture")
    assert registered is not None
    with pytest.raises(SafeToolError, match="timed out"):
        await registered.handler(_tool_use_id="timed-call")
    assert observed_ids == ["timed-call"]
    assert current_mcp_call_context.get() is None
