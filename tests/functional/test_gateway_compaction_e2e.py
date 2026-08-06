"""Opt-in gateway/WebSocket compaction regression tests."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.cli.gateway_client import GatewayClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, server: subprocess.Popen[str]) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 20.0
    last_error = ""
    while time.monotonic() < deadline:
        if server.poll() is not None:
            stdout = server.stdout.read() if server.stdout else ""
            stderr = server.stderr.read() if server.stderr else ""
            output_path = getattr(server, "_opensquilla_output_path", None)
            process_output = (
                Path(output_path).read_text(encoding="utf-8", errors="replace")
                if output_path and Path(output_path).exists()
                else ""
            )
            raise AssertionError(
                f"gateway exited early code={server.returncode}\n"
                f"stdout={stdout}\nstderr={stderr}\nprocess_output={process_output}"
            )
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
        except Exception as exc:  # noqa: BLE001 - surfaced on timeout.
            last_error = str(exc)
        time.sleep(0.1)
    output_path = getattr(server, "_opensquilla_output_path", None)
    process_output = (
        Path(output_path).read_text(encoding="utf-8", errors="replace")
        if output_path and Path(output_path).exists()
        else ""
    )
    raise AssertionError(
        f"gateway did not become healthy: {last_error}\nprocess_output={process_output}"
    )


def _stop_process(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def _kill_process(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    server.kill()
    server.wait(timeout=10)


def _write_slow_compaction_server(
    path: Path,
    *,
    port: int,
    db_path: Path,
    session_key: str,
) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import os

            from opensquilla.gateway.boot import start_gateway_server
            from opensquilla.gateway.config import AuthConfig, GatewayConfig
            from opensquilla.gateway.websocket import SubscriptionManager
            from opensquilla.session.compaction import CompactionConfig
            from opensquilla.session.manager import SessionManager
            from opensquilla.session.storage import SessionStorage

            SESSION_KEY = {session_key!r}


            class SlowCompactionSessionManager(SessionManager):
                async def compact_with_result(
                    self,
                    session_key,
                    context_window_tokens,
                    config=None,
                    custom_instructions=None,
                    **kwargs,
                ):
                    await asyncio.sleep(1.2)
                    # Exercise the real compactor deterministically. Gateway
                    # target discovery may reflect a developer's configured
                    # online provider, which would make this local E2E depend
                    # on credentials and network availability.
                    kwargs["consumer_admission"] = None
                    kwargs["context_window_chars"] = None
                    return await super().compact_with_result(
                        session_key,
                        context_window_tokens,
                        CompactionConfig(),
                        custom_instructions,
                        **kwargs,
                    )


            async def seed(manager):
                existing = await manager.get_session(SESSION_KEY)
                if existing is not None:
                    return
                node = await manager.create(
                    session_key=SESSION_KEY,
                    agent_id="main",
                    display_name="slow compaction e2e",
                )
                for idx in range(11):
                    await manager.append_message(
                        node.session_key,
                        role="user" if idx % 2 == 0 else "assistant",
                        content=("seed transcript block %02d " % idx) + ("alpha beta gamma " * 80),
                        token_count=320,
                    )


            async def main():
                os.makedirs(os.environ["OPENSQUILLA_STATE_DIR"], exist_ok=True)
                storage = SessionStorage({str(db_path)!r})
                await storage.connect()
                manager = SlowCompactionSessionManager(storage, inject_time_prefix=False)
                await seed(manager)
                config = GatewayConfig(
                    host="127.0.0.1",
                    port={port},
                    auth=AuthConfig(mode="none"),
                )
                config.state_dir = os.environ["OPENSQUILLA_STATE_DIR"]
                await start_gateway_server(
                    config=config,
                    session_manager=manager,
                    subscription_manager=SubscriptionManager(),
                    run=True,
                )
                await asyncio.Event().wait()


            asyncio.run(main())
            """
        ),
        encoding="utf-8",
    )


async def _manual_compact_over_websocket(port: int, session_key: str) -> dict[str, Any]:
    client = GatewayClient()
    await client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        await client.call("sessions.subscribe")
        await client.call("sessions.messages.subscribe", {"key": session_key})
        compact_task = asyncio.create_task(
            client.call(
                "sessions.contextCompact",
                {"key": session_key, "contextWindowTokens": 2000},
            )
        )

        statuses: list[str] = []
        all_statuses: list[str] = []
        while True:
            if compact_task.done() and "completed" in all_statuses:
                break
            frame = await asyncio.wait_for(client._recv_queue.get(), timeout=45.0)  # noqa: SLF001
            if frame.get("event") != "session.event.compaction":
                continue
            payload = frame.get("payload") or {}
            status = str(payload.get("status") or "")
            all_statuses.append(status)
            if status:
                statuses.append(status)
            if status == "started":
                assert compact_task.done() is False
            if status in {"completed", "failed", "skipped", "cancelled"}:
                break

        result = await asyncio.wait_for(compact_task, timeout=15.0)
        return {
            "statuses": statuses,
            "all_statuses": all_statuses,
            "result": result,
        }
    finally:
        await client.close()


async def _start_manual_compact_and_wait_for_started(
    port: int, session_key: str
) -> tuple[GatewayClient, asyncio.Task[Any], dict[str, Any]]:
    client = GatewayClient()
    await client.connect(f"ws://127.0.0.1:{port}/ws")
    await client.call("sessions.subscribe")
    await client.call("sessions.messages.subscribe", {"key": session_key})
    compact_task = asyncio.create_task(
        client.call(
            "sessions.contextCompact",
            {"key": session_key, "contextWindowTokens": 2000},
        )
    )

    while True:
        frame = await asyncio.wait_for(client._recv_queue.get(), timeout=15.0)  # noqa: SLF001
        if frame.get("event") != "session.event.compaction":
            continue
        payload = frame.get("payload") or {}
        if payload.get("status") == "started":
            assert compact_task.done() is False
            return client, compact_task, payload


def _read_compaction_db_state(db_path: Path) -> dict[str, int]:
    # Windows can keep the abruptly terminated process' WAL/SHM handles alive
    # for a short time after wait() reports exit. Open normally (so SQLite
    # replays the WAL) and retry only the transient recovery/locking failures.
    deadline = time.monotonic() + 10.0
    while True:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_path, timeout=1.0)
            conn.execute("select 1 from sessions limit 1").fetchone()
            break
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            message = str(exc).lower()
            if time.monotonic() >= deadline or not any(
                marker in message
                for marker in ("disk i/o error", "locked", "busy")
            ):
                raise
            time.sleep(0.1)

    assert conn is not None
    with conn:
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "select session_id, compaction_count from sessions limit 1"
        ).fetchone()
        assert session is not None
        session_id = session["session_id"]
        summary_count = conn.execute(
            "select count(*) from session_summaries where session_id = ?",
            (session_id,),
        ).fetchone()[0]
        entry_count = conn.execute(
            "select count(*) from transcript_entries where session_id = ?",
            (session_id,),
        ).fetchone()[0]
        running_tasks = conn.execute(
            "select count(*) from agent_tasks where status in ('queued', 'running')"
        ).fetchone()[0]
    return {
        "compaction_count": int(session["compaction_count"] or 0),
        "summary_count": int(summary_count),
        "entry_count": int(entry_count),
        "running_tasks": int(running_tasks),
    }


def _start_slow_compaction_gateway(
    tmp_path: Path,
    *,
    port: int,
    db_path: Path,
    session_key: str,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    server_script = tmp_path / "slow_compaction_gateway.py"
    _write_slow_compaction_server(
        server_script,
        port=port,
        db_path=db_path,
        session_key=session_key,
    )
    output_path = tmp_path / "gateway-process.log"
    with output_path.open("w", encoding="utf-8") as output_stream:
        server = subprocess.Popen(
            [sys.executable, str(server_script)],
            cwd=Path.cwd(),
            env=env,
            stdout=output_stream,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    setattr(server, "_opensquilla_output_path", output_path)
    return server


@pytest.mark.asyncio
async def test_gateway_websocket_slow_manual_compaction_rewrites_db(
    tmp_path: Path,
) -> None:
    if os.environ.get("OPENSQUILLA_GATEWAY_COMPACTION_E2E") != "1":
        pytest.skip("set OPENSQUILLA_GATEWAY_COMPACTION_E2E=1 to run compaction e2e")

    port = _free_port()
    session_key = "agent:main:webchat:slowcompact"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    # The injected SessionManager owns this database. Keep it separate from
    # the Gateway-owned state/sessions.db that boot migrates before opening.
    db_path = tmp_path / "injected-sessions.db"

    env = os.environ.copy()
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_LOG_DIR"] = str(log_dir)
    server = _start_slow_compaction_gateway(
        tmp_path,
        port=port,
        db_path=db_path,
        session_key=session_key,
        env=env,
    )
    try:
        _wait_for_health(port, server)
        observed = await asyncio.wait_for(
            _manual_compact_over_websocket(port, session_key),
            timeout=90.0,
        )
    finally:
        _stop_process(server)

    result = observed["result"]
    db_state = _read_compaction_db_state(db_path)
    assert observed["statuses"] == ["started", "observed", "observed", "completed"]
    assert result["compacted"] is True
    assert result["tokens_before"] == 3520
    assert result["tokens_after"] < result["tokens_before"]
    assert db_state["compaction_count"] == 1
    assert db_state["summary_count"] == 1
    assert db_state["entry_count"] == result["kept_count"]
    assert db_state["running_tasks"] == 0


@pytest.mark.asyncio
async def test_gateway_restart_during_slow_manual_compaction_leaves_db_recoverable(
    tmp_path: Path,
) -> None:
    if os.environ.get("OPENSQUILLA_GATEWAY_COMPACTION_E2E") != "1":
        pytest.skip("set OPENSQUILLA_GATEWAY_COMPACTION_E2E=1 to run compaction e2e")

    port = _free_port()
    session_key = "agent:main:webchat:slowcompact"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    # The injected SessionManager owns this database. Keep it separate from
    # the Gateway-owned state/sessions.db that boot migrates before opening.
    db_path = tmp_path / "injected-sessions.db"
    env = os.environ.copy()
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_LOG_DIR"] = str(log_dir)

    server = _start_slow_compaction_gateway(
        tmp_path,
        port=port,
        db_path=db_path,
        session_key=session_key,
        env=env,
    )
    client: GatewayClient | None = None
    compact_task: asyncio.Task[Any] | None = None
    try:
        _wait_for_health(port, server)
        client, compact_task, _payload = await _start_manual_compact_and_wait_for_started(
            port,
            session_key,
        )
        _kill_process(server)
        with pytest.raises(Exception):
            await asyncio.wait_for(compact_task, timeout=5.0)
    finally:
        if client is not None:
            await client.close()
        _stop_process(server)

    interrupted_state = _read_compaction_db_state(db_path)
    assert interrupted_state == {
        "compaction_count": 0,
        "summary_count": 0,
        "entry_count": 11,
        "running_tasks": 0,
    }

    restarted = _start_slow_compaction_gateway(
        tmp_path,
        port=port,
        db_path=db_path,
        session_key=session_key,
        env=env,
    )
    try:
        _wait_for_health(port, restarted)
        observed = await asyncio.wait_for(
            _manual_compact_over_websocket(port, session_key),
            timeout=90.0,
        )
    finally:
        _stop_process(restarted)

    assert observed["statuses"] == ["started", "observed", "observed", "completed"]
    assert observed["result"]["compacted"] is True
    recovered_state = _read_compaction_db_state(db_path)
    assert recovered_state["compaction_count"] == 1
    assert recovered_state["summary_count"] == 1
    assert recovered_state["running_tasks"] == 0
