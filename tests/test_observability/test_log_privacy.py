"""Synthetic content must not cross operational logging or bundle boundaries."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog

from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.channels.websocket import WebSocketChannel
from opensquilla.gateway.boot import _setup_file_logging
from opensquilla.gateway.config import GatewayConfig
from opensquilla.observability.bundle import collect_bundle
from opensquilla.observability.cli_logging import configure_cli_structlog
from opensquilla.observability.decision_log import DecisionEntry, write_decision_entry
from opensquilla.observability.log_privacy import (
    PrivateLogFormatter,
    log_metadata,
    scrub_log_artifact,
    uvicorn_log_config,
)
from opensquilla.observability.safety_log import SafetyEvent, SafetyEventType, write_safety_event
from opensquilla.observability.trace import TraceContext, TraceEvent, write_trace_event

PRIVATE = "synthetic private orchard forecast"
SYSTEM = "synthetic private system instructions"
FILE_BODY = "synthetic private notebook contents"


@pytest.fixture
def private_logging(tmp_path, monkeypatch):
    old_config = structlog.get_config()
    was_configured = structlog.is_configured()
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    app_logger = logging.getLogger("opensquilla")
    old_level = app_logger.level
    stream = io.StringIO()
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_LOG_LEVEL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LOG_FILE_ENABLED", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DEBUG_LOG", raising=False)
    try:
        structlog.reset_defaults()
        _setup_file_logging(GatewayConfig(log_file_max_bytes=2048, log_file_backup_count=8))
        for handler in root.handlers:
            if getattr(handler, "_opensquilla_console_log_handler", False):
                handler.setStream(stream)
        yield tmp_path, stream
    finally:
        for handler in list(root.handlers):
            if handler not in old_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = old_handlers
        app_logger.setLevel(old_level)
        if was_configured:
            structlog.configure(**old_config)
        else:
            structlog.reset_defaults()


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
async def test_default_gateway_files_console_and_rotation_omit_content(private_logging, level):
    directory, stream = private_logging
    logging.getLogger("opensquilla").setLevel(level)
    conn = SimpleNamespace(conn_id="synthetic-connection", send_event=AsyncMock())
    channel = WebSocketChannel(conn=conn)
    incoming = IncomingMessage(sender_id="synthetic-user", channel_id="synthetic", content=PRIVATE)
    channel.enqueue(incoming)
    assert await channel.receive() is incoming
    await channel.send(OutgoingMessage(content=PRIVATE))
    assert conn.send_event.call_args.args[1]["content"] == PRIVATE

    request = httpx.Request("POST", "https://provider.invalid/v1/chat", content=SYSTEM + PRIVATE)
    response = httpx.Response(401, request=request, json={"error": PRIVATE})
    log = structlog.get_logger("opensquilla.test_provider")
    for _ in range(20):
        try:
            raise httpx.HTTPStatusError(PRIVATE, request=request, response=response)
        except httpx.HTTPStatusError:
            log.exception(
                "provider.authentication_failed", request_id="synthetic-request", status_code=401,
                request_payload_head=SYSTEM + PRIVATE, output_preview=FILE_BODY,
                response_preview=PRIVATE, message_head=PRIVATE, trigger_scan_head=PRIVATE,
                query_preview=PRIVATE, error=PRIVATE, reason="oneword", exc_info=True,
            )
            logging.getLogger("httpx").warning("provider echoed %s", PRIVATE, exc_info=True)
    files = list(directory.glob("debug.log*"))
    assert len(files) > 1  # Exercise real RotatingFileHandler output too.
    outputs = [stream.getvalue(), *(path.read_text() for path in files)]
    for output in outputs:
        assert all(marker not in output for marker in (PRIVATE, SYSTEM, FILE_BODY, "oneword"))
        records = [json.loads(line.split(": ", 1)[1]) for line in output.splitlines()]
        auth = [row for row in records if row.get("event") == "provider.authentication_failed"]
        assert auth
        assert all(row["status_code"] == 401 for row in auth)
        assert all(row["exception_type"] == "HTTPStatusError" for row in auth)
        assert all(row["request_id"] == "synthetic-request" for row in auth)


@pytest.mark.parametrize("marker", [PRIVATE, "短的私密内容", "oneword", "123456", "a\nb\r\nc"])
def test_all_content_fields_are_removed_even_without_secret_shapes(marker):
    fields = {
        "event": "provider.failed", "request_id": "synthetic-request", "status_code": 429,
        "content": marker, "prompt": marker, "query_preview": marker, "message_head": marker,
        "trigger_scan_head": marker, "request_payload_head": marker, "response_preview": marker,
        "error": marker, "unexpected_description": marker,
        "reason": marker, "fallback_reason": marker, "image_route_reason": marker,
        "session_flush_fallback_reason": marker,
        "payload": {"response": marker, "messages": [{"content": marker}], "tokens_input": 12},
    }
    safe = log_metadata(fields)
    assert marker not in json.dumps(safe, ensure_ascii=False)
    assert safe["status_code"] == 429
    assert safe["payload"]["tokens_input"] == 12
    assert log_metadata(safe) == safe


def test_logging_never_stringifies_payload_objects_or_formats_exceptions():
    class Unrenderable:
        def __str__(self):
            raise AssertionError("must not stringify private objects")

        __repr__ = __str__

    record = logging.LogRecord("sdk", logging.ERROR, __file__, 1, Unrenderable(), (), None)
    formatted = PrivateLogFormatter().format(record)
    assert json.loads(formatted.split(": ", 1)[1])["event"] == "unstructured_log"
    assert log_metadata({"event": "safe.event", "error": Unrenderable()}) == {
        "event": "safe.event",
    }
    recursive = {}
    recursive["payload"] = recursive
    assert len(json.dumps(log_metadata(recursive))) < 500


@pytest.mark.parametrize("reason", ["current_turn", "history_context"])
def test_image_route_reason_preserves_only_producer_owned_codes(reason):
    fields = {"image_route_reason": reason}
    assert log_metadata(fields) == fields
    assert log_metadata({"image_route_reason": "arbitrary-private-word"}) == {}


def test_uvicorn_own_error_handler_cannot_bypass_gateway_privacy(capsys):
    import uvicorn

    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    original = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level,
               logging.getLogger(name).propagate)
        for name in names
    }
    try:
        uvicorn.Config(AsyncMock(), log_config=uvicorn_log_config(), access_log=False)
        try:
            raise ValueError(PRIVATE)
        except ValueError:
            logging.getLogger("uvicorn.error").exception("ASGI request failed: %s", SYSTEM)
        output = capsys.readouterr().err
        assert PRIVATE not in output and SYSTEM not in output
        assert "ValueError" in output
    finally:
        for name, (handlers, level, propagate) in original.items():
            logger = logging.getLogger(name)
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def test_cli_warnings_cannot_echo_prompts_or_chained_exceptions(private_logging, capsys):
    configure_cli_structlog()
    try:
        try:
            raise ValueError(SYSTEM)
        except ValueError as exc:
            raise RuntimeError(PRIVATE) from exc
    except RuntimeError:
        structlog.get_logger("opensquilla.cli").exception(
            "provider.failed", error=PRIVATE, response_preview=FILE_BODY,
        )
    output = capsys.readouterr().err
    assert all(marker not in output for marker in (PRIVATE, SYSTEM, FILE_BODY))
    assert "provider.failed" in output
    assert "RuntimeError" in output


def test_jsonl_sinks_omit_content_including_structured_debug_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_DEBUG_LOG", "1")
    entry = DecisionEntry(
        turn_id="synthetic-turn", session_key="agent:main:synthetic", prompt_hash="a" * 16,
        system_prompt_hash="b" * 16, tool_list_hash="c" * 16, tool_choice="auto",
        tokens_input=10, tokens_output=4, model="synthetic-model", provider="synthetic",
        latency_ms=12, ts="2026-01-01T00:00:00Z", intent_summary=PRIVATE,
        session_intent=PRIVATE, reasoning_hint_resolved=SYSTEM, image_route_reason=PRIVATE,
    )
    path = write_decision_entry(entry, tmp_path)
    assert json.loads(path.read_text())["intent_summary"] is None
    assert entry.intent_summary == PRIVATE  # No mutation of live application data.
    write_trace_event(TraceEvent(
        kind="provider_error", context=TraceContext(trace_id="synthetic-trace"),
        payload={"error": PRIVATE, "request_payload_head": SYSTEM, "status_code": 401},
    ), tmp_path)
    write_safety_event(SafetyEvent(
        event_type=SafetyEventType.REFUSED_TOOL, session_id="synthetic-session",
        reason=FILE_BODY, ts="2026-01-01T00:00:00Z",
    ), tmp_path)
    for file in tmp_path.rglob("*.jsonl"):
        text = file.read_text()
        assert all(marker not in text for marker in (PRIVATE, SYSTEM, FILE_BODY))


def test_bundle_reprojects_legacy_rotation_desktop_and_error_rows(tmp_path, monkeypatch):
    home = tmp_path / "user-data" / "opensquilla" / "state"
    logs = home / "logs"
    logs.mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text("# synthetic config\n")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setattr("opensquilla.observability.bundle._collect_doctor", lambda: "{}")
    day = datetime.now(UTC).strftime("%Y%m%d")
    legacy = f"2026-01-01 01:02:03 [ERROR] opensquilla.provider: failed error='{PRIVATE}'\n"
    legacy += SYSTEM + "\n" + json.dumps({"body": FILE_BODY}) + "\n"
    for name in ("debug.log", "debug.log.1", "debug.log.2", "gateway.log"):
        (logs / name).write_text(legacy)
    desktop = home.parent.parent / "logs"
    desktop.mkdir()
    for name in ("desktop.log", "desktop.log.1", "desktop.log.2", "gateway.log"):
        (desktop / name).write_text(legacy)
    (logs / f"decisions-{day}.jsonl").write_text(json.dumps({
        "turn_id": "synthetic-turn", "intent_summary": PRIVATE,
    }) + "\n")
    (logs / f"traces-{day}.jsonl").write_text(json.dumps({
        "privacy": "raw", "payload": {"messages": [{"content": PRIVATE}]},
    }) + "\n")
    with sqlite3.connect(home / "sessions.db") as connection:
        connection.execute("CREATE TABLE turn_errors(error_id, session_key, ts_ms, message)")
        connection.execute("INSERT INTO turn_errors VALUES (?, ?, ?, ?)", (
            "synthetic-error", "agent:main:synthetic", int(datetime.now(UTC).timestamp() * 1000),
            PRIVATE,
        ))
    sources = {path: path.read_bytes() for folder in (logs, desktop) for path in folder.iterdir()}
    result = collect_bundle(tmp_path / "bundle.zip", home_dir=home, log_dir=logs)
    with zipfile.ZipFile(result.path) as archive:
        assert "logs/debug.log.2" in archive.namelist()
        assert "desktop/gateway.log" in archive.namelist()
        assert "errors.jsonl" in archive.namelist()
        assert json.loads(archive.read("errors.jsonl"))["error_id"] == "synthetic-error"
        for name in archive.namelist():
            text = archive.read(name).decode()
            assert all(marker not in text for marker in (PRIVATE, SYSTEM, FILE_BODY)), name
    assert all(path.read_bytes() == data for path, data in sources.items())


def test_malformed_legacy_fragments_and_escape_sequences_fail_closed():
    raw = '\x1b[31m' + PRIVATE + '\x1b[0m\n' + '{"body":"' + SYSTEM + '\n' + FILE_BODY
    clean = scrub_log_artifact(raw)
    assert all(marker not in clean for marker in (PRIVATE, SYSTEM, FILE_BODY))
    assert json.loads(clean)["omitted_lines"] == 3


def test_explicit_raw_trace_capture_remains_separate_and_opt_in(tmp_path):
    event = TraceEvent(
        kind="request", context=TraceContext(trace_id="synthetic-trace"), privacy="raw",
        payload={"messages": [{"content": PRIVATE}]},
    )
    with pytest.raises(ValueError, match="raw trace"):
        write_trace_event(event, tmp_path)
    assert not list(tmp_path.iterdir())
    path = write_trace_event(event, tmp_path, allow_raw=True)
    assert PRIVATE in path.read_text()
