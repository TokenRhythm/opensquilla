from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reportlab.pdfgen import canvas

from opensquilla.attachment_workspace import AttachmentWorkspaceMaterializer
from opensquilla.browser import DesktopBrowserClient
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import agent as agent_module
from opensquilla.gateway.agent_tasks import AgentTaskRegistry
from opensquilla.mcp.desktop_browser import DesktopBrowserMCPClient, browser_tool_policy
from opensquilla.mcp.discovery import close_active_clients, register_client_tools
from opensquilla.mcp.types import MCPCallContext, current_mcp_call_context
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.browser_pdf_downloads import (
    MAX_BROWSER_PDF_BYTES,
    BrowserPdfDownloadError,
    materialize_browser_pdf_export,
)
from opensquilla.tools.browser_policy import BROWSER_MCP_TOOLS
from opensquilla.tools.builtin.media import pdf
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    CallerKind,
    ToolContext,
    WorkspaceAccessError,
    current_tool_context,
)


def _pdf_bytes() -> bytes:
    stream = io.BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(50, 700, "Synthetic browser download body")
    document.save()
    return stream.getvalue()


def _export(payload: bytes, download_id: str = "download-synthetic") -> dict[str, object]:
    return {
        "downloadId": download_id,
        "name": "paper.pdf",
        "mimeType": "application/pdf",
        "byteLength": len(payload),
        "state": "completed",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "dataBase64": base64.b64encode(payload).decode("ascii"),
    }


@pytest.fixture
def browser_context(tmp_path: Path) -> ToolContext:
    session_id = "synthetic-pdf-session"
    session = SimpleNamespace(session_id=session_id, epoch=4)
    manager = SimpleNamespace(get_session=AsyncMock(return_value=session))
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        session_key="agent:main:webchat:synthetic-pdf-session",
        session_id=session_id,
        session_epoch=4,
        desktop_browser=DesktopBrowserClient("http://127.0.0.1:43123/v1/browser", "s" * 48),
        workspace_dir=str(tmp_path / "workspace"),
        workspace_strict=True,
        artifact_session_id=session_id,
        artifact_media_root=str(tmp_path / "media"),
        sandbox_session_manager=manager,
        sandbox_gateway_config=SimpleNamespace(attachments=SimpleNamespace(
            persist_transcripts=True, workspace_attachment_disk_budget_bytes=MAX_BROWSER_PDF_BYTES,
        )),
    )


@pytest.fixture
def delayed_pdf_write(monkeypatch: pytest.MonkeyPatch):
    gate = SimpleNamespace(
        entered=threading.Event(), release=threading.Event(), finished=threading.Event(),
        fail=False, contexts=[],
    )
    original = AttachmentWorkspaceMaterializer.materialize_bytes

    def write(materializer, *args, **kwargs):
        gate.contexts.append(current_tool_context.get())
        gate.entered.set()
        try:
            if not gate.release.wait(5):
                raise RuntimeError("Synthetic disk worker did not receive its release.")
            if gate.fail:
                raise RuntimeError("Synthetic private disk error.")
            return original(materializer, *args, **kwargs)
        finally:
            gate.finished.set()

    monkeypatch.setattr(AttachmentWorkspaceMaterializer, "materialize_bytes", write)
    try:
        yield gate
    finally:
        gate.release.set()


async def test_session_deletion_waits_for_cancelled_pdf_writer_before_removing_material(
    browser_context: ToolContext, delayed_pdf_write,
) -> None:
    registry = AgentTaskRegistry()
    token = current_tool_context.set(browser_context)
    writer = asyncio.create_task(materialize_browser_pdf_export(
        browser_context, "download-synthetic", _export(_pdf_bytes()),
    ))
    current_tool_context.reset(token)
    registry.register(browser_context.session_key, writer)
    cleaned = asyncio.Event()

    async def delete_material() -> None:
        async with registry.quiesce_sessions([browser_context.session_key]):
            browser_context.sandbox_session_manager.get_session.return_value = None
            shutil.rmtree(Path(browser_context.workspace_dir or "") / ".opensquilla",
                          ignore_errors=True)
            cleaned.set()

    deleting = None
    try:
        assert await asyncio.to_thread(delayed_pdf_write.entered.wait, 2)
        deleting = asyncio.create_task(delete_material())

        async def cancellation_started() -> None:
            while not writer.cancelling():
                await asyncio.sleep(0)

        await asyncio.wait_for(cancellation_started(), timeout=2)
        await asyncio.sleep(0)
        assert not writer.done()
        assert not cleaned.is_set()
        writer.cancel()
        await asyncio.sleep(0)
        assert not writer.done()

        delayed_pdf_write.release.set()
        await asyncio.wait_for(deleting, timeout=2)
        assert writer.cancelled()
        assert delayed_pdf_write.finished.is_set()
        assert delayed_pdf_write.contexts == [browser_context]
        assert cleaned.is_set()
        assert not list(Path(browser_context.workspace_dir or "").rglob("*.pdf"))
    finally:
        delayed_pdf_write.release.set()
        tasks = [task for task in (writer, deleting) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("disk_error", [False, True])
async def test_shutdown_cancellation_waits_for_pdf_disk_worker(
    browser_context: ToolContext, delayed_pdf_write, disk_error: bool,
) -> None:
    delayed_pdf_write.fail = disk_error
    existing_tasks = asyncio.all_tasks()
    writer = asyncio.create_task(materialize_browser_pdf_export(
        browser_context, "download-synthetic", _export(_pdf_bytes()),
    ))
    try:
        assert await asyncio.to_thread(delayed_pdf_write.entered.wait, 2)
        # Apply shutdown's all-Task cancellation to only this test's work.
        for task in asyncio.all_tasks() - existing_tasks:
            task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not writer.done()
        writer.cancel()
        await asyncio.sleep(0)
        assert not writer.done()

        delayed_pdf_write.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(writer, timeout=2)
        assert delayed_pdf_write.finished.is_set()
    finally:
        delayed_pdf_write.release.set()
        await asyncio.gather(writer, return_exceptions=True)


async def test_pdf_export_does_not_return_a_path_after_session_epoch_changes_during_write(
    browser_context: ToolContext, delayed_pdf_write,
) -> None:
    writer = asyncio.create_task(materialize_browser_pdf_export(
        browser_context, "download-synthetic", _export(_pdf_bytes()),
    ))
    try:
        assert await asyncio.to_thread(delayed_pdf_write.entered.wait, 2)
        browser_context.sandbox_session_manager.get_session.return_value.epoch = 5
        delayed_pdf_write.release.set()
        with pytest.raises(BrowserPdfDownloadError, match="session changed"):
            await asyncio.wait_for(writer, timeout=2)
        assert delayed_pdf_write.finished.is_set()
    finally:
        delayed_pdf_write.release.set()
        await asyncio.gather(writer, return_exceptions=True)


async def test_agent_turn_deletion_keeps_nested_pdf_writer_owned_past_stop_grace(
    browser_context: ToolContext, delayed_pdf_write, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    packet = _export(_pdf_bytes())
    name = "mcp__desktop-browser__browser_inspect"
    tool_cancelled = asyncio.Event()
    real_cancel_task = agent_module.cancel_task

    async def observe_cancellation(task, **kwargs):
        if kwargs["operation"] == f"tool:{name}":
            tool_cancelled.set()
        return await real_cancel_task(task, **kwargs)

    async def respond(method, params, *, notification=False):
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {
                "experimental": {"opensquilla/browser": {"pdfDownloadExport": True}},
            }}}
        if method == "tools/list":
            return {"result": {"tools": [{
                "name": tool, "description": "Synthetic browser tool", "inputSchema": {
                    "type": "object", "properties": {},
                },
            } for tool in sorted(BROWSER_MCP_TOOLS)]}}
        if method == "tools/call":
            return {"result": {
                "content": [], "isError": False,
                "structuredContent": {
                    "targetRef": "page-synthetic", "download": {
                        "downloadId": "download-synthetic",
                    },
                },
                "_meta": {"opensquilla/pdfExport": packet},
            }}
        return {}

    class InspectProvider:
        provider_name = "fake"

        async def chat(self, messages, **kwargs):
            yield ProviderToolUseStart(tool_use_id="inspect-synthetic", tool_name=name)
            yield ProviderToolUseEnd(
                tool_use_id="inspect-synthetic", tool_name=name,
                arguments={"targetRef": "page-synthetic", "downloadId": "download-synthetic"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(client, "_request", respond)
    monkeypatch.setattr(agent_module, "cancel_task", observe_cancellation)
    registry = ToolRegistry()
    await register_client_tools(client, registry, spec_transform=browser_tool_policy)
    agent = Agent(
        provider=InspectProvider(), config=AgentConfig(max_iterations=1),
        tool_definitions=registry.to_tool_definitions(browser_context),
        tool_handler=build_tool_handler(registry, browser_context), tool_context=browser_context,
    )

    async def run_turn() -> None:
        async for _event in agent.run_turn("Inspect the synthetic PDF download"):
            pass

    turns = AgentTaskRegistry()
    turn = asyncio.create_task(run_turn())
    turns.register(browser_context.session_key, turn)
    cleaned = asyncio.Event()

    async def delete_material() -> None:
        async with turns.quiesce_sessions([browser_context.session_key]):
            browser_context.sandbox_session_manager.get_session.return_value = None
            shutil.rmtree(Path(browser_context.workspace_dir or "") / ".opensquilla",
                          ignore_errors=True)
            cleaned.set()

    deleting = None
    try:
        assert await asyncio.to_thread(delayed_pdf_write.entered.wait, 2)
        deleting = asyncio.create_task(delete_material())
        await asyncio.wait_for(tool_cancelled.wait(), timeout=2)
        await asyncio.sleep(agent_module.STOP_CANCEL_GRACE_SECONDS + 0.1)
        assert not turn.done()
        assert not cleaned.is_set()
        assert not delayed_pdf_write.finished.is_set()
        for _ in range(2):
            turn.cancel()
            await asyncio.sleep(0)
            assert not turn.done()
            assert not cleaned.is_set()

        delayed_pdf_write.release.set()
        await asyncio.wait_for(deleting, timeout=2)
        assert turn.cancelled()
        assert delayed_pdf_write.finished.is_set()
        assert cleaned.is_set()
        assert not list(Path(browser_context.workspace_dir or "").rglob("*.pdf"))
    finally:
        delayed_pdf_write.release.set()
        tasks = [task for task in (turn, deleting) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(delayed_pdf_write.finished.wait, 2)
        await close_active_clients(owner="desktop-browser")


async def test_exported_pdf_is_session_scoped_and_readable_by_existing_pdf_tool(
    browser_context: ToolContext,
) -> None:
    payload = _pdf_bytes()
    path = await materialize_browser_pdf_export(
        browser_context, "download-synthetic", _export(payload),
    )
    assert path.startswith(".opensquilla/attachments/synthetic-pdf-session/")
    assert (Path(browser_context.workspace_dir or "") / path).read_bytes() == payload

    token = current_tool_context.set(browser_context)
    try:
        result = json.loads(await pdf(path, _tool_use_id="read-browser-pdf"))
    finally:
        current_tool_context.reset(token)
    assert "Synthetic browser download body" in result["text"]
    assert result["path"] == path

    foreign = replace(browser_context, artifact_session_id="another-synthetic-session")
    token = current_tool_context.set(foreign)
    try:
        with pytest.raises(WorkspaceAccessError, match="another session"):
            await pdf(path, _tool_use_id="foreign-browser-pdf")
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("change", [
    "download_id", "mime", "magic", "hash", "length", "base64", "too_large",
])
async def test_pdf_export_rejects_bad_receipts_without_writing(
    browser_context: ToolContext, change: str,
) -> None:
    payload = _pdf_bytes()
    packet = _export(payload)
    if change == "download_id":
        packet["downloadId"] = "download-other"
    elif change == "mime":
        packet["mimeType"] = "text/html"
    elif change == "magic":
        packet = _export(b"<html>Not a PDF</html>")
    elif change == "hash":
        packet["sha256"] = "0" * 64
    elif change == "length":
        packet["byteLength"] = len(payload) + 1
    elif change == "base64":
        packet["dataBase64"] = "not valid base64!"
    else:
        packet["byteLength"] = MAX_BROWSER_PDF_BYTES + 1
    with pytest.raises(BrowserPdfDownloadError):
        await materialize_browser_pdf_export(browser_context, "download-synthetic", packet)
    assert not (Path(browser_context.workspace_dir or "") / ".opensquilla").exists()


async def test_pdf_export_honors_session_epoch_and_attachment_budget(
    browser_context: ToolContext,
) -> None:
    payload = _pdf_bytes()
    packet = _export(payload)
    browser_context.sandbox_session_manager.get_session.return_value.epoch = 5
    with pytest.raises(BrowserPdfDownloadError, match="session"):
        await materialize_browser_pdf_export(browser_context, "download-synthetic", packet)
    browser_context.sandbox_session_manager.get_session.return_value.epoch = 4
    browser_context.sandbox_gateway_config.attachments.workspace_attachment_disk_budget_bytes = 8
    with pytest.raises(BrowserPdfDownloadError, match="could not be saved"):
        await materialize_browser_pdf_export(browser_context, "download-synthetic", packet)
    browser_context.sandbox_gateway_config.attachments.persist_transcripts = False
    with pytest.raises(BrowserPdfDownloadError, match="does not retain"):
        await materialize_browser_pdf_export(browser_context, "download-synthetic", packet)


async def test_gateway_private_pdf_handoff_then_pdf_reader(
    browser_context: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    payload = _pdf_bytes()
    packet = _export(payload)
    calls: list[tuple[str, dict[str, object]]] = []

    async def respond(method, params, *, notification=False):
        calls.append((method, params))
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {
                "experimental": {"opensquilla/browser": {"pdfDownloadExport": True}},
            }}}
        if method == "tools/call":
            return {"result": {
                "content": [{"type": "text", "text": json.dumps({
                    "targetRef": "page-synthetic", "download": {"downloadId": "download-synthetic"},
                })}],
                "structuredContent": {
                    "targetRef": "page-synthetic", "download": {"downloadId": "download-synthetic"},
                },
                "isError": False,
                "_meta": {"opensquilla/pdfExport": packet},
            }}
        return {}

    monkeypatch.setattr(client, "_request", respond)
    await client.connect()
    context_token = current_tool_context.set(browser_context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-pdf-download"))
    try:
        result = await client.call_tool("browser_inspect", {
            "targetRef": "page-synthetic", "downloadId": "download-synthetic",
        })
        assert not result.is_error
        path = result.structured_content["download"]["workspacePath"]
        assert result.structured_content["download"]["pdfReadable"] is True
        assert calls[-1][1]["_meta"]["exportPdf"] is True
        assert "exportPdf" not in calls[-1][1]["arguments"]
        assert packet["dataBase64"] not in result.content
        assert packet["dataBase64"] not in json.dumps(result.structured_content)
        assert str(browser_context.workspace_dir) not in result.content
        read = json.loads(await pdf(path, _tool_use_id="read-synthetic-browser-pdf"))
        assert "Synthetic browser download body" in read["text"]
    finally:
        current_mcp_call_context.reset(call_token)
        current_tool_context.reset(context_token)
        await client.close()


async def test_older_desktop_keeps_text_inspect_and_does_not_request_export(
    browser_context: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    request = AsyncMock(return_value={"result": {
        "content": [{"type": "text", "text": "ordinary download"}],
        "structuredContent": {"targetRef": "page-synthetic", "download": {
            "downloadId": "download-synthetic", "textAvailable": True,
        }},
        "isError": False,
    }})
    monkeypatch.setattr(client, "_request", request)
    context_token = current_tool_context.set(browser_context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-old-client"))
    try:
        result = await client.call_tool("browser_inspect", {
            "targetRef": "page-synthetic", "downloadId": "download-synthetic",
        })
    finally:
        current_mcp_call_context.reset(call_token)
        current_tool_context.reset(context_token)
    assert not result.is_error
    assert result.content == "ordinary download"
    assert "workspacePath" not in result.structured_content["download"]
    assert "exportPdf" not in request.await_args.args[1]["_meta"]


async def test_gateway_rejects_mismatched_private_receipt_without_exposing_bytes(
    browser_context: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    client._pdf_download_export = True
    packet = _export(_pdf_bytes(), "download-foreign")
    request = AsyncMock(return_value={"result": {
        "content": [], "isError": False,
        "structuredContent": {
            "targetRef": "page-synthetic", "download": {"downloadId": "download-synthetic"},
        },
        "_meta": {"opensquilla/pdfExport": packet},
    }})
    monkeypatch.setattr(client, "_request", request)
    context_token = current_tool_context.set(browser_context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-mismatched-pdf"))
    try:
        result = await client.call_tool("browser_inspect", {
            "targetRef": "page-synthetic", "downloadId": "download-synthetic",
        })
    finally:
        current_mcp_call_context.reset(call_token)
        current_tool_context.reset(context_token)
    assert result.is_error
    assert result.structured_content["code"] == "BROWSER_PDF_EXPORT_FAILED"
    assert packet["dataBase64"] not in result.content
    assert "download-foreign" not in result.content
    assert not (Path(browser_context.workspace_dir or "") / ".opensquilla").exists()


@pytest.mark.parametrize("structured", [
    None,
    [],
    {},
    {"targetRef": "page-synthetic", "download": None},
    {"targetRef": "page-synthetic", "download": {"downloadId": "download-foreign"}},
    {"targetRef": "page-foreign", "download": {"downloadId": "download-synthetic"}},
])
async def test_gateway_rejects_invalid_pdf_receipt_before_materializing(
    browser_context: ToolContext, monkeypatch: pytest.MonkeyPatch, structured: object,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    client._pdf_download_export = True
    packet = _export(_pdf_bytes())
    request = AsyncMock(return_value={"result": {
        "content": [], "isError": False, "structuredContent": structured,
        "_meta": {"opensquilla/pdfExport": packet},
    }})
    monkeypatch.setattr(client, "_request", request)
    context_token = current_tool_context.set(browser_context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-invalid-pdf-receipt"))
    try:
        result = await client.call_tool("browser_inspect", {
            "targetRef": "page-synthetic", "downloadId": "download-synthetic",
        })
    finally:
        current_mcp_call_context.reset(call_token)
        current_tool_context.reset(context_token)
    assert result.is_error
    assert result.structured_content["code"] == "BROWSER_PROTOCOL_ERROR"
    assert packet["dataBase64"] not in result.content
    assert not (Path(browser_context.workspace_dir or "") / ".opensquilla").exists()


async def test_text_download_on_new_desktop_still_returns_ordinary_inspect(
    browser_context: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DesktopBrowserMCPClient(browser_context.desktop_browser)
    client._pdf_download_export = True
    request = AsyncMock(return_value={"result": {
        "content": [{"type": "text", "text": "download text"}],
        "structuredContent": {"targetRef": "page-synthetic", "download": {
            "downloadId": "download-synthetic", "textAvailable": True, "text": "download text",
        }},
        "isError": False,
    }})
    monkeypatch.setattr(client, "_request", request)
    context_token = current_tool_context.set(browser_context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-text-download"))
    try:
        result = await client.call_tool("browser_inspect", {
            "targetRef": "page-synthetic", "downloadId": "download-synthetic",
        })
    finally:
        current_mcp_call_context.reset(call_token)
        current_tool_context.reset(context_token)
    assert result.content == "download text"
    assert "workspacePath" not in result.structured_content["download"]
    assert request.await_args.args[1]["_meta"]["exportPdf"] is True
