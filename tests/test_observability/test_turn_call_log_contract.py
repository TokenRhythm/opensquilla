from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.diagnostics import DiagnosticsState
from opensquilla.observability.decision_log import write_decision_entry
from opensquilla.observability.trace_details import load_turn_call_records
from opensquilla.observability.turn_call_log import (
    TurnCallLogger,
    TurnCallProgress,
    is_turn_call_log_enabled,
    resolve_turn_call_log_dir_with_source,
)
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.tools import ToolContext
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolSpec


class _ToolLoopProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ToolUseStartEvent(tool_use_id="tool-1", tool_name="echo")
            yield ToolUseEndEvent(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "ok"},
            )
            yield DoneEvent(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield TextDeltaEvent(text="done")
        yield DoneEvent(stop_reason="end_turn", input_tokens=4, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _FakeSelector:
    def __init__(self, provider: _ToolLoopProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="fake-model")

    def clone(self) -> _FakeSelector:
        return self

    def resolve(self) -> _ToolLoopProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model


class _NoProviderSelector:
    current_config = SimpleNamespace(model="missing-model")

    def clone(self) -> _NoProviderSelector:
        return self

    def resolve(self) -> None:
        return None


def test_turn_call_log_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)

    assert is_turn_call_log_enabled() is False


def test_call_progress_bounds_snapshots_and_tracks_streaming_tool_arguments(tmp_path) -> None:
    logger = TurnCallLogger(
        trace_id="trace-progress", turn_id="turn-progress", session_key="session-progress",
        agent_id="test", provider="fake", model="fake", log_dir=tmp_path,
    )
    now = [0.0]
    progress = TurnCallProgress(
        logger, call_id="call-a", iteration=1, attempt=1, clock=lambda: now[0]
    )
    progress.append(text="x" * 40_000)
    progress.tool_delta("tool-a", "write", '{"content":"')
    now[0] = 1.0
    progress.tool_delta("tool-a", "write", "y" * 40_000)
    [path] = list(tmp_path.glob("turn-calls-*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2
    snapshot = records[-1]["payload"]
    assert snapshot["partial"] is True
    assert len(snapshot["text"]) == 32_000
    assert snapshot["text_chars"] == 40_000
    assert snapshot["text_offset"] == 8000
    assert snapshot["tool_calls"][0]["arguments_text"] == "y" * 32_000
    assert snapshot["tool_calls"][0]["arguments_truncated"] is True
    assert snapshot["tool_calls"][0]["name"] == "write"
    assert "messages" not in snapshot
    assert "duration_ms" not in snapshot
    progress.reset()
    reset = json.loads(path.read_text().splitlines()[-1])["payload"]
    assert reset["call_id"] == "call-a"
    assert reset["text"] == ""
    assert reset["tool_calls"] == []


def test_turn_call_log_enabled_values(monkeypatch) -> None:
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", value)
        assert is_turn_call_log_enabled() is True


def test_turn_call_log_can_be_enabled_by_runtime_diagnostics(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig())

    state.set_runtime(enabled=True, raw=True)

    assert is_turn_call_log_enabled(state) is True


def test_standard_diagnostics_do_not_enable_raw_turn_call_log(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig(diagnostics_enabled=True))

    assert is_turn_call_log_enabled(state) is False


def test_turn_call_log_directory_empty_specific_env_falls_back(monkeypatch, tmp_path) -> None:
    shared_log_dir = tmp_path / "logs"
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", "")
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(shared_log_dir))

    directory, source = resolve_turn_call_log_dir_with_source()

    assert directory == shared_log_dir
    assert source == "OPENSQUILLA_LOG_DIR"
    assert not shared_log_dir.exists()


def test_turn_call_log_writes_raw_trace_contract(tmp_path) -> None:
    logger = TurnCallLogger(
        trace_id="trace-1",
        turn_id="turn-1",
        session_key="agent:main:test",
        session_id="session-1",
        session_intent="chat",
        agent_id="main",
        provider="fake",
        model="fake-model",
        source={"kind": "test"},
        log_dir=tmp_path,
    )

    first_path = logger.write("turn_start", {"message": "raw user prompt"})
    second_path = logger.write("turn_end", {"final_text": "raw assistant text"})

    assert first_path == second_path
    assert first_path is not None
    records = [
        json.loads(line)
        for line in first_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["kind"] for record in records] == ["turn_start", "turn_end"]
    assert [record["seq"] for record in records] == [1, 2]
    assert {record["schema_version"] for record in records} == {1}
    assert {record["privacy"] for record in records} == {"raw"}
    assert {record["trace_id"] for record in records} == {"trace-1"}
    assert {record["turn_id"] for record in records} == {"turn-1"}
    assert {record["session_key"] for record in records} == {"agent:main:test"}
    assert records[0]["payload"]["message"] == "raw user prompt"
    assert records[1]["payload"]["final_text"] == "raw assistant text"
    assert records[0]["ts"].endswith("Z")
    assert len(records[0]["ts"].split(".", 1)[1][:-1]) == 3
    assert isinstance(records[0]["elapsed_ms"], int)
    assert isinstance(records[1]["elapsed_ms"], int)
    assert records[1]["elapsed_ms"] >= records[0]["elapsed_ms"]
    assert {record["clock_origin"] for record in records} == {"logger_start"}
    assert {record["agent_trace"] for record in records} == {False}


def test_turn_call_logger_stops_writing_after_capture_is_disabled(tmp_path) -> None:
    enabled = [True]
    logger = TurnCallLogger(
        trace_id="trace-switch",
        turn_id="turn-switch",
        session_key="session-switch",
        agent_id="main",
        provider="fake",
        model="fake-model",
        log_dir=tmp_path,
        capture_enabled=lambda: enabled[0],
    )

    path = logger.write("turn_start", {"message": "synthetic input"})
    enabled[0] = False
    assert logger.write("llm_request", {"message": "not retained"}) is None
    assert path is not None
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["turn_start"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capture_mode", ["off", "legacy_env", "legacy_runtime", "agent_trace"]
)
async def test_runtime_raw_turn_call_log_records_ordered_tool_turn(
    tmp_path, monkeypatch, capture_mode
) -> None:
    monkeypatch.setenv(
        "OPENSQUILLA_TURN_CALL_LOG", "1" if capture_mode == "legacy_env" else "0"
    )
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    config = GatewayConfig(privacy={"agent_trace_enabled": capture_mode == "agent_trace"})
    diagnostics_state = DiagnosticsState.from_config(config)
    if capture_mode == "legacy_runtime":
        diagnostics_state.set_runtime(enabled=True, raw=True)
    if capture_mode in {"legacy_env", "legacy_runtime"}:
        assert is_turn_call_log_enabled(diagnostics_state) is True
    registry = ToolRegistry()

    async def echo(value: str) -> str:
        return f"echo:{value}"

    registry.register(
        ToolSpec(
            name="echo",
            description="Echo a value.",
            parameters={"value": {"type": "string"}},
            required=["value"],
        ),
        echo,
    )
    provider = _ToolLoopProvider()
    runner = TurnRunner(
        provider_selector=_FakeSelector(provider),
        tool_registry=registry,
        config=config,
        diagnostics_state=diagnostics_state,
    )

    events = [
        event
        async for event in runner.run(
            "use echo",
            "agent:main:turn-call-sequence",
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
        )
    ]

    assert any(event.kind == "done" for event in events)
    trace_files = list(tmp_path.glob("traces-*.jsonl"))
    assert bool(trace_files) is (capture_mode == "agent_trace")
    if capture_mode == "off":
        assert list(tmp_path.glob("turn-calls-*.jsonl")) == []
        return
    [log_file] = list(tmp_path.glob("turn-calls-*.jsonl"))
    records = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected_kinds = {
        "prompt_report",
        "turn_start",
        "llm_request",
        "llm_response",
        "tool_request",
        "tool_response",
        "turn_end",
    }
    kinds = [record["kind"] for record in records if record["kind"] in expected_kinds]

    assert kinds == [
        "turn_start",
        "prompt_report",
        "llm_request",
        "llm_response",
        "tool_request",
        "tool_response",
        "llm_request",
        "llm_response",
        "turn_end",
    ]
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    assert {record["privacy"] for record in records} == {"raw"}
    assert len({record["trace_id"] for record in records}) == 1
    assert {record["agent_trace"] for record in records} == {
        capture_mode == "agent_trace"
    }
    trace_id = records[0]["trace_id"]
    assert bool(load_turn_call_records(trace_id, log_dir=tmp_path)) is (
        capture_mode == "agent_trace"
    )


def test_trace_details_only_reads_records_written_with_trace_enabled(tmp_path) -> None:
    trace_enabled = [False]
    logger = TurnCallLogger(
        trace_id="mixed-trace",
        turn_id="mixed-turn",
        session_key="mixed-session",
        agent_id="main",
        provider="fake",
        model="fake-model",
        log_dir=tmp_path,
        agent_trace_enabled=lambda: trace_enabled[0],
    )
    logger.write("llm_request", {"message": "legacy diagnostics"})
    trace_enabled[0] = True
    logger.write("llm_response", {"message": "visible trace"})
    trace_enabled[0] = False
    logger.write("tool_response", {"message": "legacy again"})

    records = load_turn_call_records("mixed-trace", log_dir=tmp_path)

    assert [record["kind"] for record in records] == ["llm_response"]


@pytest.mark.asyncio
async def test_runtime_raw_trace_starts_before_setup_and_includes_setup_time(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    clock = [100.0]
    synthetic_time = SimpleNamespace(**vars(time))
    synthetic_time.monotonic = lambda: clock[0]
    monkeypatch.setattr("opensquilla.engine.runtime.time", synthetic_time)
    monkeypatch.setattr("opensquilla.observability.turn_call_log.time", synthetic_time)
    runner = TurnRunner(
        provider_selector=_FakeSelector(_ToolLoopProvider()),
        config=GatewayConfig(privacy={"agent_trace_enabled": True}),
    )
    input_stage_run = runner._input_stage.run
    prompt_stage_run = runner._prompt_assembler_stage.run

    async def capture_input(inp):
        [log_file] = list(tmp_path.glob("turn-calls-*.jsonl"))
        records = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert [record["kind"] for record in records] == ["turn_start"]
        assert records[0]["payload"]["message"] == "original input"
        assert records[0]["elapsed_ms"] == 0
        assert records[0]["provider"] == records[0]["model"] == ""
        clock[0] += 0.125
        output = await input_stage_run(inp)
        return replace(output, runtime_message="prepared input")

    async def capture_prompt(inp):
        output = await prompt_stage_run(inp)
        clock[0] += 1.875
        return output

    monkeypatch.setattr(runner._input_stage, "run", capture_input)
    monkeypatch.setattr(runner._prompt_assembler_stage, "run", capture_prompt)

    events = [
        event
        async for event in runner.run(
            "original input",
            "agent:main:setup-timing",
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
        )
    ]

    assert any(event.kind == "done" for event in events)
    [log_file] = list(tmp_path.glob("turn-calls-*.jsonl"))
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [record["kind"] for record in records[:2]] == ["turn_start", "prompt_report"]
    assert {record["clock_origin"] for record in records} == {"turn_runner_start"}
    assert records[0]["payload"]["boundary"] == "turn_runner_entry"
    assert records[1]["elapsed_ms"] == 2000
    assert records[1]["payload"]["effective_runtime_message"] == "prepared input"
    assert "tool_names" in records[1]["payload"]
    assert records[1]["provider"] == "fake"
    assert records[1]["model"]
    assert records[-1]["elapsed_ms"] >= 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "terminal_kind"),
    [(ValueError, "turn_error"), (asyncio.CancelledError, "turn_cancelled")],
)
async def test_runtime_raw_trace_closes_when_input_setup_fails(
    tmp_path, monkeypatch, failure, terminal_kind
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    runner = TurnRunner(
        provider_selector=_FakeSelector(_ToolLoopProvider()),
        config=GatewayConfig(privacy={"agent_trace_enabled": True}),
    )

    async def failing_input(inp):
        raise failure("synthetic setup failure")

    monkeypatch.setattr(runner._input_stage, "run", failing_input)

    async def run_turn():
        return [
            event
            async for event in runner.run(
                "original input",
                "agent:main:setup-failure",
                ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
            )
        ]

    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await run_turn()
    else:
        events = await run_turn()
        assert any(event.kind == "error" for event in events)

    [raw_log] = list(tmp_path.glob("turn-calls-*.jsonl"))
    records = [json.loads(line) for line in raw_log.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["turn_start", terminal_kind]


@pytest.mark.asyncio
async def test_runtime_correlates_trace_decision_and_raw_logs(
    tmp_path, monkeypatch
) -> None:
    safe_log_dir = tmp_path / "logs"
    raw_log_dir = tmp_path / "raw"
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(safe_log_dir))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(raw_log_dir))
    captured: dict[str, Any] = {}

    def _capture_decision_entry(entry: Any) -> Any:
        captured["entry"] = entry
        return write_decision_entry(entry, log_dir=safe_log_dir)

    monkeypatch.setattr(
        "opensquilla.engine.runtime.write_decision_entry",
        _capture_decision_entry,
    )
    provider = _ToolLoopProvider()
    runner = TurnRunner(
        provider_selector=_FakeSelector(provider),
        config=GatewayConfig(privacy={"agent_trace_enabled": True}),
    )

    events = [
        event
        async for event in runner.run(
            "hello",
            "agent:main:trace-correlation",
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
        )
    ]

    assert any(event.kind == "done" for event in events)
    [raw_log] = list(raw_log_dir.glob("turn-calls-*.jsonl"))
    raw_records = [
        json.loads(line)
        for line in raw_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    trace_ids = {record["trace_id"] for record in raw_records}
    assert len(trace_ids) == 1
    trace_id = trace_ids.pop()

    entry = captured["entry"]
    assert entry.trace_id == trace_id
    [decision_log] = list(safe_log_dir.glob("decisions-*.jsonl"))
    decision_record = json.loads(decision_log.read_text(encoding="utf-8").splitlines()[0])
    assert decision_record["trace_id"] == trace_id

    [trace_log] = list(safe_log_dir.glob("traces-*.jsonl"))
    trace_records = [
        json.loads(line)
        for line in trace_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["kind"] for record in trace_records] == ["turn_start", "turn_end"]
    assert {record["trace_id"] for record in trace_records} == {trace_id}
    assert {record["turn_id"] for record in trace_records} == {entry.turn_id}


@pytest.mark.asyncio
async def test_runtime_writes_trace_when_provider_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    runner = TurnRunner(
        provider_selector=_NoProviderSelector(),
        config=GatewayConfig(privacy={"agent_trace_enabled": True}),
    )

    events = [
        event
        async for event in runner.run(
            "hello",
            "agent:main:no-provider",
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
        )
    ]

    assert [(event.kind, getattr(event, "code", None)) for event in events] == [
        ("error", "no_provider")
    ]
    [trace_log] = list(tmp_path.glob("traces-*.jsonl"))
    trace_records = [
        json.loads(line)
        for line in trace_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["kind"] for record in trace_records] == ["turn_start", "turn_error"]
    assert {record["trace_id"] for record in trace_records} == {
        trace_records[0]["trace_id"]
    }
    assert {record["turn_id"] for record in trace_records} == {
        trace_records[0]["turn_id"]
    }
    assert trace_records[0]["payload"] == {"message_chars": 5, "attachment_count": 0}
    assert trace_records[1]["payload"] == {
        "error_type": "ProviderResolutionError",
        "error_code": "no_provider",
        "error_chars": len("No provider available"),
    }
    [raw_log] = list(tmp_path.glob("turn-calls-*.jsonl"))
    raw_records = [json.loads(line) for line in raw_log.read_text().splitlines()]
    assert [record["kind"] for record in raw_records] == ["turn_start", "turn_error"]
    assert {record["trace_id"] for record in raw_records} == {trace_records[0]["trace_id"]}
    assert raw_records[1]["payload"]["error_code"] == "no_provider"
