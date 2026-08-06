from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.cli.agent_event_stream import (
    AGENT_EVENT_STREAM_SCHEMA_VERSION,
    StderrAgentEventSink,
    agent_event_to_jsonl,
    project_agent_event_v1,
)
from opensquilla.engine.types import (
    ArtifactEvent,
    DoneEvent,
    ErrorEvent,
    RouterDecisionEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseDeltaEvent,
    ToolUseStartEvent,
    WarningEvent,
)


def _strict_json_loads(line: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    payload = json.loads(line, parse_constant=reject_constant)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            RouterDecisionEvent(
                tier="c1",
                model="synthetic/model",
                source="router",
                confidence=float("nan"),
                probs=[float("inf")],
            ),
            {"tier": "c1", "model": "synthetic/model", "source": "router"},
        ),
        (ThinkingEvent(text="private reasoning"), {}),
        (
            TextDeltaEvent(text="private answer", presentation="intermediate"),
            {"presentation": "intermediate"},
        ),
        (
            RunHeartbeatEvent(
                phase="tool",
                elapsed_ms=1200,
                idle_ms=200,
                message="private status",
            ),
            {"phase": "tool", "elapsed_ms": 1200, "idle_ms": 200},
        ),
        (
            ToolUseStartEvent(
                tool_use_id="call-1",
                tool_name="read_file",
                synthetic_from_text=True,
                started_at=42,
            ),
            {"tool_use_id": "call-1", "tool_name": "read_file", "started_at": 42},
        ),
        (
            ToolResultEvent(
                tool_use_id="call-1",
                tool_name="read_file",
                result="private result",
                arguments={"path": "/private/path"},
                is_error=False,
            ),
            {"tool_use_id": "call-1", "tool_name": "read_file", "is_error": False},
        ),
        (
            ArtifactEvent(
                id="artifact-1",
                name="report.txt",
                mime="text/plain",
                size=12,
                session_id="private-session",
                download_url="/private/path",
            ),
            {"id": "artifact-1", "name": "report.txt", "mime": "text/plain", "size": 12},
        ),
        (DoneEvent(text="private final", cost_usd=float("inf")), {}),
    ],
)
def test_project_agent_event_v1_has_an_explicit_field_allowlist(
    event: object,
    expected: dict[str, Any],
) -> None:
    payload = project_agent_event_v1(event)

    assert payload == {
        "_event": True,
        "schema_version": AGENT_EVENT_STREAM_SCHEMA_VERSION,
        "kind": event.kind,
        **expected,
    }
    line = agent_event_to_jsonl(event)
    assert line is not None
    assert _strict_json_loads(line) == payload
    assert "NaN" not in line
    assert "Infinity" not in line


def test_project_agent_event_v1_redacts_and_bounds_user_visible_messages() -> None:
    secret = "sk-test-super-secret"
    event = ErrorEvent(
        code="provider_error",
        message=f"Bearer {secret} api_key={secret} " + ("x" * 5000),
    )

    payload = project_agent_event_v1(event)

    assert payload is not None
    assert payload["code"] == "provider_error"
    assert secret not in payload["message"]
    assert "Bearer ***" in payload["message"]
    assert len(payload["message"]) <= 4096

    warning = project_agent_event_v1(
        WarningEvent(code="credential_warning", message=f"token={secret}")
    )
    assert warning is not None
    assert warning["message"] == "token=***"


def test_project_agent_event_v1_skips_unversioned_internal_events() -> None:
    assert project_agent_event_v1(ToolUseDeltaEvent(json_fragment='{"private":')) is None

    @dataclass
    class FutureEngineEvent:
        kind: str = "future_event"
        sensitive_value: str = "private"

    assert project_agent_event_v1(FutureEngineEvent()) is None
    assert agent_event_to_jsonl(FutureEngineEvent()) is None


class _BrokenStream:
    def __init__(self) -> None:
        self.write_calls = 0

    def write(self, _value: str) -> int:
        self.write_calls += 1
        raise BrokenPipeError("synthetic closed stderr")

    def flush(self) -> None:
        raise AssertionError("flush must not follow a failed write")


def test_stderr_event_sink_disables_itself_after_first_write_failure() -> None:
    stream = _BrokenStream()
    sink = StderrAgentEventSink(stream=stream)  # type: ignore[arg-type]

    sink(ThinkingEvent(text="private"))
    sink(DoneEvent(text="private"))

    assert sink.active is False
    assert stream.write_calls == 2


def test_stderr_event_sink_disables_itself_after_strict_json_failure() -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    event = RunHeartbeatEvent(elapsed_ms=float("nan"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        agent_event_to_jsonl(event)

    stream = RecordingStream()
    sink = StderrAgentEventSink(stream=stream)  # type: ignore[arg-type]
    sink(event)
    sink(DoneEvent())

    assert sink.active is False
    assert stream.values == [
        "Warning: agent progress event stream disabled after an encoding or "
        "stderr write failure.\n"
    ]


def test_stderr_event_sink_flushes_each_supported_event() -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.values: list[str] = []
            self.flush_count = 0

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            self.flush_count += 1

    stream = RecordingStream()
    sink = StderrAgentEventSink(stream=stream)  # type: ignore[arg-type]

    sink(ThinkingEvent(text="private"))
    sink(ToolUseDeltaEvent(json_fragment="private"))
    sink(DoneEvent(text="private"))

    assert stream.flush_count == 2
    assert len(stream.values) == 2
    assert [_strict_json_loads(value)["kind"] for value in stream.values] == [
        "thinking",
        "done",
    ]


def test_stderr_event_stream_is_incremental_and_stdout_stays_final_only() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    pythonpath = [str(repo_root / "src"), env.get("PYTHONPATH", "")]
    env["PYTHONPATH"] = os.pathsep.join(value for value in pythonpath if value)
    script = """
import json
import sys

from opensquilla.cli.agent_event_stream import StderrAgentEventSink
from opensquilla.engine.types import DoneEvent, RouterDecisionEvent

sink = StderrAgentEventSink()
sink(RouterDecisionEvent(tier="c1", model="synthetic/model", source="test"))
sys.stdin.readline()
sink(DoneEvent(text="private final"))
print(json.dumps({"status": "ok", "text": "final"}), flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdin is not None

    stdout_queue: queue.Queue[str] = queue.Queue()
    stdout_reader = threading.Thread(
        target=lambda: stdout_queue.put(process.stdout.readline()),
        daemon=True,
    )
    stdout_reader.start()

    first_event_line = process.stderr.readline()
    assert _strict_json_loads(first_event_line)["kind"] == "router_decision"
    assert process.poll() is None
    assert stdout_queue.empty()

    process.stdin.write("continue\n")
    process.stdin.flush()
    process.stdin.close()
    final_stdout = stdout_queue.get(timeout=10)
    stdout_reader.join(timeout=10)
    process.wait(timeout=10)
    remaining_stderr = process.stderr.read()

    assert process.returncode == 0
    assert json.loads(final_stdout) == {"status": "ok", "text": "final"}
    assert process.stdout.read() == ""
    event_lines = [first_event_line, *remaining_stderr.splitlines()]
    event_lines = [line.strip() for line in event_lines if line.strip()]
    assert [_strict_json_loads(line)["kind"] for line in event_lines] == [
        "router_decision",
        "done",
    ]

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript JSON.parse compatibility check")
    completed = subprocess.run(
        [
            node,
            "-e",
            (
                'const fs=require("fs");'
                'for(const line of fs.readFileSync(0,"utf8").trim().split(/\\r?\\n/)) {'
                "const value=JSON.parse(line);"
                "if(value._event!==true||value.schema_version!==1)process.exit(2);"
                "}"
            ),
        ],
        input="\n".join(event_lines),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
