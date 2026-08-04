from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import AnyUrl

from opensquilla.mcp_server.bridge import OpenSquillaMCPBridge


def _src_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path.cwd() / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing else f"{src_path}{os.pathsep}{existing}"
    env["OPENSQUILLA_STATE_DIR"] = str(tmp_path / "state")
    env["OPENSQUILLA_LOG_DIR"] = str(tmp_path / "logs")
    env["OPENSQUILLA_TURN_CALL_LOG"] = "0"
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20.0
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout = process.stdout.read() if process.stdout else ""
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(
                f"gateway exited early code={process.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            with urllib.request.urlopen(  # noqa: S310 - localhost test probe.
                f"http://127.0.0.1:{port}/health",
                timeout=1.0,
            ) as response:
                if response.status == 200 and json.loads(response.read()).get("ok") is True:
                    return
        except Exception as exc:  # noqa: BLE001 - surfaced on timeout.
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"gateway did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def _payload_from_tool_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", [])
    text = getattr(content[0], "text", "") if content else ""
    return json.loads(text)


def _payload_from_resource_result(result: Any) -> dict[str, Any]:
    contents = getattr(result, "contents", [])
    text = getattr(contents[0], "text", "") if contents else ""
    return json.loads(text)


@pytest.mark.asyncio
async def test_stdio_mcp_protocol_lists_calls_tools_and_reads_resources(tmp_path: Path) -> None:
    session_mod = pytest.importorskip("mcp.client.session")
    stdio_mod = pytest.importorskip("mcp.client.stdio")

    server_script = tmp_path / "mcp_stdio_smoke_server.py"
    server_script.write_text(
        textwrap.dedent(
            """
            import anyio

            from opensquilla.mcp_server.server import create_mcp_server


            class FakeBridge:
                async def conversations_list(self, limit=50):
                    return {"sessions": [{"key": "demo", "displayName": "Demo"}], "limit": limit}

                async def session_resolve(self, key):
                    return {"key": key, "session_id": "demo"}

                async def messages_read(self, key, limit=1000):
                    return {"messages": [{"role": "user", "text": f"hello {key}"}], "limit": limit}

                async def message_detail_read(
                    self,
                    key,
                    cursor,
                    offset=0,
                    chunk_bytes=131072,
                    field="message",
                ):
                    return {
                        "key": key,
                        "cursor": cursor,
                        "offset": offset,
                        "chunk_bytes": chunk_bytes,
                        "field": field,
                    }

                async def messages_send(self, key, message, intent="continue"):
                    return {
                        "status": "accepted",
                        "key": key,
                        "message": message,
                        "intent": intent,
                        "current_stream_seq": 1,
                    }

                async def events_wait(
                    self,
                    key,
                    since_stream_seq=None,
                    timeout_ms=30000,
                    max_events=100,
                    terminal_only=False,
                ):
                    return {
                        "key": key,
                        "events": [{"event": "session.event.done", "payload": {"stream_seq": 2}}],
                        "current_stream_seq": 2,
                        "timed_out": False,
                    }

                async def transcript_jsonl(self, key, limit=1000):
                    return (
                        '{"type":"message","message":{"role":"user",'
                        '"content":[{"type":"text","text":"hello"}]}}'
                    )


            async def main():
                await create_mcp_server(FakeBridge()).run_stdio_async()


            anyio.run(main)
            """
        ),
        encoding="utf-8",
    )

    params = stdio_mod.StdioServerParameters(
        command=sys.executable,
        args=[str(server_script)],
        env=_src_env(tmp_path),
        cwd=str(Path.cwd()),
    )
    with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_mod.stdio_client(params, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with session_mod.ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=10),
            ) as session:
                init = await session.initialize()
                assert init.capabilities.tools is not None
                assert init.capabilities.resources is not None

                tools = await session.list_tools()
                assert {
                    "conversations_list",
                    "session_resolve",
                    "messages_read",
                    "message_detail_read",
                    "messages_send",
                    "events_wait",
                    "transcript_export",
                }.issubset({tool.name for tool in tools.tools})

                list_result = await session.call_tool("conversations_list", {"limit": 3})
                assert list_result.isError is False
                assert _payload_from_tool_result(list_result)["sessions"][0]["key"] == "demo"

                send_result = await session.call_tool(
                    "messages_send",
                    {"key": "demo", "message": "from mcp smoke"},
                )
                assert send_result.isError is False
                assert _payload_from_tool_result(send_result)["current_stream_seq"] == 1

                resources = await session.list_resources()
                assert "opensquilla://sessions" in {
                    str(resource.uri) for resource in resources.resources
                }

                templates = await session.list_resource_templates()
                assert "opensquilla://sessions/{key}/messages" in {
                    str(template.uriTemplate) for template in templates.resourceTemplates
                }

                read_result = await session.read_resource(
                    AnyUrl("opensquilla://sessions/demo/messages")
                )
                assert (
                    _payload_from_resource_result(read_result)["messages"][0]["text"]
                    == "hello demo"
                )


@pytest.mark.asyncio
async def test_bridge_runs_against_real_gateway_websocket_session_flow(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn")
    pytest.importorskip("websockets")

    port = _free_port()
    server_script = tmp_path / "gateway_mcp_smoke_server.py"
    server_script.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import time
            from dataclasses import dataclass
            from types import SimpleNamespace

            import uvicorn

            from opensquilla.engine.types import DoneEvent, TextDeltaEvent
            from opensquilla.gateway.app import create_gateway_app
            from opensquilla.gateway.config import AuthConfig, GatewayConfig
            from opensquilla.gateway.websocket import SubscriptionManager


            @dataclass
            class Session:
                session_key: str = "agent:main:mcp-smoke"
                session_id: str = "mcp-smoke"
                status: str = "running"
                agent_id: str = "main"
                created_at: int = 1000
                updated_at: int = 1000
                display_name: str | None = "MCP smoke"
                model: str | None = None
                channel: str | None = None
                chat_type: str = "unknown"
                group_id: str | None = None
                subject: str | None = None
                last_channel: str | None = None
                last_to: str | None = None
                last_account_id: str | None = None
                last_thread_id: str | None = None
                delivery_context: dict | None = None
                parent_session_key: str | None = None
                spawned_by: str | None = None
                origin: dict | None = None


            class Storage:
                def __init__(self):
                    self.sessions = {{"agent:main:mcp-smoke": Session()}}
                    self.transcripts = {{"mcp-smoke": []}}

                async def list_sessions(self, limit=None):
                    rows = list(self.sessions.values())
                    return rows[:limit] if limit else rows

                async def get_session(self, key):
                    return self.sessions.get(key)

                async def count_transcript_entries(self, session_id):
                    return len(self.transcripts.get(session_id, []))

                async def list_agent_tasks_for_sessions(self, session_keys, limit_per_session=100):
                    return {{key: [] for key in session_keys}}

                async def list_agent_tasks(
                    self,
                    session_key=None,
                    status=None,
                    limit=100,
                    offset=0,
                ):
                    return []


            class SessionManager:
                def __init__(self):
                    self._storage = Storage()
                    self._epoch_cache = {{}}

                async def create(self, session_key, agent_id="main", display_name=None, model=None):
                    session = Session(
                        session_key=session_key,
                        session_id=session_key.rsplit(":", 1)[-1],
                        agent_id=agent_id,
                        display_name=display_name,
                        model=model,
                    )
                    self._storage.sessions[session_key] = session
                    self._storage.transcripts.setdefault(session.session_id, [])
                    return session

                async def append_message(self, key, role="user", content=""):
                    session = self._storage.sessions[key]
                    transcript_id = len(self._storage.transcripts[session.session_id]) + 1
                    message_id = f"msg-{{transcript_id}}"
                    entry = SimpleNamespace(
                        id=transcript_id,
                        role=role,
                        content=content,
                        message_id=message_id,
                        created_at=int(time.time() * 1000),
                    )
                    self._storage.transcripts.setdefault(session.session_id, []).append(entry)
                    session.updated_at = entry.created_at
                    return entry

                async def get_transcript(self, key):
                    session = self._storage.sessions.get(key)
                    if session is None:
                        return []
                    return list(self._storage.transcripts.get(session.session_id, []))

                async def get_canonical_transcript_cursor_page(
                    self,
                    key,
                    *,
                    limit,
                    before=None,
                    after=None,
                ):
                    entries = await self.get_transcript(key)
                    if before is not None:
                        entries = [
                            entry
                            for entry in entries
                            if (entry.created_at, entry.id) < before
                        ]
                    elif after is not None:
                        entries = [
                            entry
                            for entry in entries
                            if (entry.created_at, entry.id) > after
                        ]
                    if after is not None and before is None:
                        selected = entries[: limit + 1]
                        has_more = len(selected) > limit
                        selected = selected[:limit]
                    else:
                        selected = entries[-(limit + 1) :]
                        has_more = len(selected) > limit
                        selected = selected[-limit:]
                    return (
                        [
                            SimpleNamespace(
                                cursor=(entry.created_at, entry.id),
                                stored_bytes=len(entry.content.encode("utf-8")),
                                content_prefix=entry.content[:2048],
                                message_id=entry.message_id,
                                role=entry.role,
                                provenance_kind=None,
                                provenance_origin_session_id=None,
                                provenance_source_session_key=None,
                                provenance_source_channel=None,
                                provenance_source_tool=None,
                                turn_id=None,
                            )
                            for entry in selected
                        ],
                        has_more,
                        True,
                    )

                async def get_canonical_transcript_entry_by_cursor(self, key, *, cursor):
                    return next(
                        (
                            entry
                            for entry in await self.get_transcript(key)
                            if (entry.created_at, entry.id) == cursor
                        ),
                        None,
                    )

                async def get_summary_metadata(self, key):
                    return [], False, 0

                async def apply_intent(self, key, intent, **kwargs):
                    return self._storage.sessions[key], False


            class TurnRunner:
                def __init__(self):
                    self._locks = {{}}

                def get_session_lock(self, key):
                    self._locks.setdefault(key, asyncio.Lock())
                    return self._locks[key]

                def run(self, message, key, **kwargs):
                    async def events():
                        yield TextDeltaEvent(text="gateway smoke delta")
                        yield DoneEvent(text="gateway smoke done")

                    return events()


            config = GatewayConfig(
                host="127.0.0.1",
                port={port},
                auth=AuthConfig(mode="none"),
                state_dir=r"{str(tmp_path / "state")}",
            )
            app = create_gateway_app(
                config,
                session_manager=SessionManager(),
                subscription_manager=SubscriptionManager(),
                turn_runner=TurnRunner(),
            )

            uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
            """
        ),
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=Path.cwd(),
        env=_src_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_health(port, process)
        bridge = OpenSquillaMCPBridge(gateway_url=f"ws://127.0.0.1:{port}/ws")
        try:
            listed = await bridge.conversations_list(limit=5)
            assert listed["sessions"][0]["key"] == "agent:main:mcp-smoke"

            history_before = await bridge.messages_read("agent:main:mcp-smoke")
            assert history_before["messages"] == []

            send_result = await bridge.messages_send(
                "agent:main:mcp-smoke",
                "hello through real gateway",
            )
            assert send_result["status"] == "accepted"
            assert isinstance(send_result["current_stream_seq"], int)

            events = await bridge.events_wait(
                "agent:main:mcp-smoke",
                since_stream_seq=send_result["current_stream_seq"],
                timeout_ms=5000,
                terminal_only=True,
            )
            assert events["timed_out"] is False
            assert events["events"][0]["event"] == "session.event.done"

            history_after = await bridge.messages_read("agent:main:mcp-smoke")
            assert history_after["messages"][0]["role"] == "user"
            assert history_after["messages"][0]["text"] == "hello through real gateway"
        finally:
            await bridge.close()
    finally:
        _stop_process(process)
