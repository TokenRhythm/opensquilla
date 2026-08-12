#!/usr/bin/env python3
"""Deterministic, synthetic OpenAI-compatible fault server for long-task tests.

The server never forwards a request and never retains request prompts, headers,
or response bodies.  Tests select a scenario with the
``X-OpenSquilla-Fault-Scenario`` header (or with the server-side sequence), so
provider retry and stream-recovery behavior can be exercised without network
access or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class FaultScenario(StrEnum):
    OK = "ok"
    RATE_LIMITED = "429_retry_after"
    OVERLOADED = "503"
    RESET_BEFORE_FIRST_TOKEN = "reset_before_first_token"
    PARTIAL_THEN_RESET = "partial_then_reset"
    REASONING_ONLY = "reasoning_only"
    LATE_TERMINAL = "late_terminal"


@dataclass(frozen=True)
class FaultRequestRecord:
    """Non-sensitive request metadata retained for deterministic assertions."""

    request_number: int
    scenario: str
    model: str
    stream: bool
    received_bytes: int
    received_monotonic_ns: int


class _FaultServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        scenarios: tuple[FaultScenario, ...],
        late_terminal_delay_seconds: float,
        max_request_bytes: int,
        completion_text: str,
        completion_bytes: int,
        completion_chunk_bytes: int,
        completion_chunk_delay_seconds: float,
        reasoning_before_completion: bool,
    ) -> None:
        super().__init__(server_address, _FaultHandler)
        self.scenarios = scenarios
        self.late_terminal_delay_seconds = late_terminal_delay_seconds
        self.max_request_bytes = max_request_bytes
        self.completion_text = _synthetic_completion(completion_text, completion_bytes)
        self.completion_chunk_bytes = completion_chunk_bytes
        self.completion_chunk_delay_seconds = completion_chunk_delay_seconds
        self.reasoning_before_completion = reasoning_before_completion
        self._records: list[FaultRequestRecord] = []
        self._lock = threading.Lock()

    def record_request(
        self,
        requested: str | None,
        *,
        model: str,
        stream: bool,
        received_bytes: int,
    ) -> tuple[int, FaultScenario]:
        with self._lock:
            request_number = len(self._records) + 1
            if requested:
                scenario = FaultScenario(requested)
            else:
                scenario = self.scenarios[min(request_number - 1, len(self.scenarios) - 1)]
            self._records.append(
                FaultRequestRecord(
                    request_number=request_number,
                    scenario=scenario.value,
                    model=model,
                    stream=stream,
                    received_bytes=received_bytes,
                    received_monotonic_ns=time.monotonic_ns(),
                )
            )
            return request_number, scenario

    def records_snapshot(self) -> tuple[FaultRequestRecord, ...]:
        with self._lock:
            return tuple(self._records)


class _FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenSquillaFaultProxy/1"
    sys_version = ""

    @property
    def fault_server(self) -> _FaultServer:
        server = self.server
        assert isinstance(server, _FaultServer)
        return server

    def log_message(self, _format: str, *args: Any) -> None:
        # Do not put request paths, headers, or accidental prompt material in logs.
        del args

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.rstrip("/") == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.split("?", 1)[0].rstrip("/") != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"type": "not_found"}})
            return

        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"type": "invalid_length"}})
            return
        if content_length < 0 or content_length > self.fault_server.max_request_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"type": "request_too_large"}},
            )
            return

        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"type": "invalid_json"}})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"type": "invalid_request"}})
            return

        model = str(payload.get("model") or "synthetic-model")[:200]
        stream = payload.get("stream") is True
        try:
            request_number, scenario = self.fault_server.record_request(
                self.headers.get("X-OpenSquilla-Fault-Scenario"),
                model=model,
                stream=stream,
                received_bytes=len(body),
            )
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"type": "unknown_scenario"}})
            return
        self._run_scenario(scenario, model=model, request_number=request_number)

    def _run_scenario(
        self,
        scenario: FaultScenario,
        *,
        model: str,
        request_number: int,
    ) -> None:
        if scenario is FaultScenario.RATE_LIMITED:
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": {"type": "rate_limit", "message": "synthetic rate limit"}},
                extra_headers={"Retry-After": "8"},
            )
            return
        if scenario is FaultScenario.OVERLOADED:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"type": "overloaded", "message": "synthetic overload"}},
            )
            return
        if scenario is FaultScenario.RESET_BEFORE_FIRST_TOKEN:
            self._begin_chunked_stream()
            self._abort_stream()
            return

        self._begin_chunked_stream()
        if scenario is FaultScenario.PARTIAL_THEN_RESET:
            self._write_sse_chunk(
                _stream_chunk(
                    model=model,
                    request_number=request_number,
                    delta={"content": "synthetic partial"},
                )
            )
            self._abort_stream()
            return
        if scenario is FaultScenario.REASONING_ONLY:
            self._write_sse_chunk(
                _stream_chunk(
                    model=model,
                    request_number=request_number,
                    delta={"reasoning_content": "synthetic reasoning pulse"},
                )
            )
            self._write_terminal(model=model, request_number=request_number)
            return
        if self.fault_server.reasoning_before_completion:
            self._write_sse_chunk(
                _stream_chunk(
                    model=model,
                    request_number=request_number,
                    delta={"reasoning_content": "synthetic reasoning pulse"},
                )
            )
        if scenario is FaultScenario.LATE_TERMINAL:
            self._write_completion(model=model, request_number=request_number)
            time.sleep(self.fault_server.late_terminal_delay_seconds)
            self._write_terminal(model=model, request_number=request_number)
            return

        self._write_completion(model=model, request_number=request_number)
        self._write_terminal(model=model, request_number=request_number)

    def _write_completion(self, *, model: str, request_number: int) -> None:
        text = self.fault_server.completion_text
        chunk_size = self.fault_server.completion_chunk_bytes or len(text)
        for offset in range(0, len(text), chunk_size):
            self._write_sse_chunk(
                _stream_chunk(
                    model=model,
                    request_number=request_number,
                    delta={"content": text[offset : offset + chunk_size]},
                )
            )
            if offset + chunk_size < len(text):
                time.sleep(self.fault_server.completion_chunk_delay_seconds)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _begin_chunked_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

    def _write_sse_chunk(self, payload: dict[str, Any] | str) -> None:
        value = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        body = f"data: {value}\n\n".encode()
        self.wfile.write(f"{len(body):X}\r\n".encode("ascii"))
        self.wfile.write(body)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _write_terminal(self, *, model: str, request_number: int) -> None:
        self._write_sse_chunk(
            _stream_chunk(
                model=model,
                request_number=request_number,
                delta={},
                finish_reason="stop",
            )
        )
        self._write_sse_chunk("[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    def _abort_stream(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            # The peer may already have reset the stream; close still follows.
            pass
        self.connection.close()


def _stream_chunk(
    *,
    model: str,
    request_number: int,
    delta: dict[str, str],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"fault-{request_number}",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason},
        ],
    }


def _synthetic_completion(marker: str, completion_bytes: int) -> str:
    if not completion_bytes:
        return marker
    prefix = f"{marker}\n\n# Synthetic Markdown Fixture\n\n"
    suffix = f"\n\n{marker}"
    filler = "- synthetic deterministic line 0000\n"
    available = completion_bytes - len(prefix.encode()) - len(suffix.encode())
    repeated = (filler * (available // len(filler.encode()) + 1)).encode()[:available].decode()
    result = prefix + repeated + suffix
    if len(result.encode()) != completion_bytes:
        raise ValueError("synthetic completion could not satisfy its byte size")
    return result


class DeterministicFaultProxy:
    """Thread-backed context manager used by offline integration tests."""

    def __init__(
        self,
        scenarios: Iterable[FaultScenario | str] = (FaultScenario.OK,),
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        late_terminal_delay_seconds: float = 0.05,
        max_request_bytes: int = 1024 * 1024,
        completion_text: str = "synthetic complete",
        completion_bytes: int = 0,
        completion_chunk_bytes: int = 0,
        completion_chunk_delay_seconds: float = 0.0,
        reasoning_before_completion: bool = False,
    ) -> None:
        parsed = tuple(FaultScenario(value) for value in scenarios)
        if not parsed:
            raise ValueError("at least one fault scenario is required")
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("fault proxy must bind to loopback")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not 0 <= late_terminal_delay_seconds <= 30:
            raise ValueError("late terminal delay must be between 0 and 30 seconds")
        if not 1 <= max_request_bytes <= 8 * 1024 * 1024:
            raise ValueError("max request size must be between 1 byte and 8 MiB")
        if completion_text != "synthetic complete" and not re.fullmatch(
            r"OSQ_[A-F0-9]{16}", completion_text
        ):
            raise ValueError("completion text must be the fixed synthetic value or a test marker")
        if completion_bytes and not 16 * 1024 <= completion_bytes <= 32 * 1024:
            raise ValueError("long synthetic completion must be between 16 and 32 KiB")
        if not 0 <= completion_chunk_bytes <= 64 * 1024:
            raise ValueError("completion chunk size must be between 0 and 64 KiB")
        if completion_chunk_bytes and completion_chunk_bytes > (
            completion_bytes or len(completion_text)
        ):
            raise ValueError("completion chunk size exceeds the completion")
        if not 0 <= completion_chunk_delay_seconds <= 1:
            raise ValueError("completion chunk delay must be between 0 and 1 second")
        if not isinstance(reasoning_before_completion, bool):
            raise ValueError("reasoning-before-completion must be boolean")
        self._server = _FaultServer(
            (host, port),
            scenarios=parsed,
            late_terminal_delay_seconds=late_terminal_delay_seconds,
            max_request_bytes=max_request_bytes,
            completion_text=completion_text,
            completion_bytes=completion_bytes,
            completion_chunk_bytes=completion_chunk_bytes,
            completion_chunk_delay_seconds=completion_chunk_delay_seconds,
            reasoning_before_completion=reasoning_before_completion,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="opensquilla-fault-proxy",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        host_text = str(host)
        bracketed = f"[{host_text}]" if ":" in host_text else host_text
        return f"http://{bracketed}:{port}/v1"

    @property
    def records(self) -> tuple[FaultRequestRecord, ...]:
        return self._server.records_snapshot()

    def start(self) -> DeterministicFaultProxy:
        if not self._thread.is_alive():
            self._thread.start()
        return self

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()

    def __enter__(self) -> DeterministicFaultProxy:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _write_ready_file(path: Path, proxy: DeterministicFaultProxy) -> None:
    if path.resolve().parent not in {
        Path(os.getenv("TMPDIR") or "/tmp").resolve(),
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
    }:
        raise ValueError("--ready-file must be directly inside a system temporary directory")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"base_url": proxy.base_url}, stream)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[scenario.value for scenario in FaultScenario],
        default=[],
    )
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--late-terminal-delay-seconds", type=float, default=0.05)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args(argv)
    scenarios = args.scenario or [FaultScenario.OK.value]
    try:
        with DeterministicFaultProxy(
            scenarios,
            host=args.host,
            port=args.port,
            late_terminal_delay_seconds=args.late_terminal_delay_seconds,
        ) as proxy:
            if args.ready_file is not None:
                _write_ready_file(args.ready_file, proxy)
            else:
                print(json.dumps({"base_url": proxy.base_url}))
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
