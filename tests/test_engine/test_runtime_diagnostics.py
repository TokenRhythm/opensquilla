from __future__ import annotations

import json
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolCall, ToolResult
from opensquilla.engine.runtime_diagnostics import (
    RuntimeDiagnosticsObserver,
    classify_path,
    normalize_command_family,
)
from opensquilla.engine.runtime_recovery import source_loop_recovery_decision
from opensquilla.git_runtime import (
    GitCapability,
    GitCapabilityState,
    GitRunResult,
    GitRunState,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent
from opensquilla.tools.types import ToolContext


def _call(
    tool_use_id: str,
    tool_name: str = "exec_command",
    arguments: dict[str, Any] | None = None,
) -> ToolCall:
    return ToolCall(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        arguments=arguments or {"command": "cargo test -p crate"},
    )


def _result(
    tool_use_id: str,
    tool_name: str = "exec_command",
    *,
    content: str = "error[E0592]: duplicate definitions with name `fallback_service`",
    is_error: bool = True,
) -> ToolResult:
    return ToolResult(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        content=content,
        is_error=is_error,
    )


def _write(path: str) -> dict[str, Any]:
    return {"relative_path": path, "operation": "edit", "created": False}


def _read(path: str) -> dict[str, Any]:
    return {"relative_path": path, "operation": "read"}


def _source_loop_event(
    *,
    reason: str = "repeated_failure_anchor",
    path: str = "src/lib.rs",
    path_class: str = "source",
) -> dict[str, Any]:
    return {
        "feature": "runtime_observer",
        "reason": reason,
        "trigger_count": 3,
        "command_family": "cargo:test",
        "failure_anchor_hash": "abc123",
        "failure_anchor_excerpt": "error[E0592]: duplicate definitions",
        "changed_files": [path],
        "diff_paths": [path],
        "diff_fingerprint_before": "diff-a",
        "diff_fingerprint_after": "diff-a",
        "path_classes": {
            "changed_files": {path: path_class},
            "diff_paths": {path: path_class},
        },
    }


def _message_text(messages: list[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def test_runtime_diagnostics_classifies_paths_and_commands() -> None:
    assert classify_path("src/app.py") == "source"
    assert classify_path("tests/test_app.py") == "test"
    assert classify_path("docs/usage.md") == "docs"
    assert classify_path("target/generated.rs") == "generated"
    assert classify_path("tmp/repro.py") == "debug"
    assert classify_path("crates/regex/src/test_word_bug.rs") == "debug"
    assert classify_path("crates/regex/src/word.rs.test_fix") == "debug"
    assert classify_path("src/test_utils.rs") == "source"

    assert normalize_command_family("cd /testbed && cargo test -p axum 2>&1 | tail -20") == (
        "cargo:test"
    )
    assert normalize_command_family("cd /testbed && npm run build 2>&1 | tail -10") == (
        "npm run:build"
    )
    assert normalize_command_family("git diff HEAD") == "git:diff"


def test_runtime_diff_paths_preserve_non_repository_as_unknown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = GitCapability(
        state=GitCapabilityState.AVAILABLE,
        executable=workspace / "git",
        source="test",
    )
    result = GitRunResult(
        state=GitRunState.NOT_REPOSITORY,
        returncode=128,
        stdout=b"",
        stderr=b"not a git repository",
        capability=capability,
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: result,
    )
    agent = Agent(
        provider=None,  # type: ignore[arg-type]
        tool_context=ToolContext(workspace_dir=str(workspace)),
    )

    assert agent._workspace_diff_paths_for_runtime_event() is None
    assert agent._runtime_git_state is GitRunState.NOT_REPOSITORY


def test_source_loop_recovery_log_mode_observes_only() -> None:
    decision = source_loop_recovery_decision(
        global_mode="log",
        diagnostic_events=[_source_loop_event()],
        attempted=False,
    )

    assert decision is not None
    assert decision.mechanism == "source_loop_recovery"
    assert decision.action == "observe"
    assert decision.injected_to_model is False
    assert decision.message is None
    assert decision.details["diff_paths"] == ["src/lib.rs"]
    assert decision.details["failure_anchor_hash"] == "abc123"


def test_source_loop_recovery_warn_model_nudges_once() -> None:
    decision = source_loop_recovery_decision(
        global_mode="warn_model",
        diagnostic_events=[_source_loop_event()],
        attempted=False,
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert decision.injected_to_model is True
    assert decision.message
    assert decision.message.startswith("[Runtime recovery]")
    assert decision.details["trigger_reason"] == "repeated_failure_anchor"

    assert (
        source_loop_recovery_decision(
            global_mode="warn_model",
            diagnostic_events=[_source_loop_event()],
            attempted=True,
        )
        is None
    )


def test_source_loop_recovery_can_nudge_for_new_event_key_with_budget() -> None:
    first = source_loop_recovery_decision(
        global_mode="warn_model",
        diagnostic_events=[_source_loop_event()],
        attempted=False,
        attempted_event_keys=set(),
        max_nudges=2,
    )
    assert first is not None
    first_key = first.details["recovery_event_key"]

    second = source_loop_recovery_decision(
        global_mode="warn_model",
        diagnostic_events=[
            _source_loop_event(),
            _source_loop_event(reason="edit_churn_after_failure"),
        ],
        attempted=True,
        attempted_event_keys={first_key},
        max_nudges=2,
    )

    assert second is not None
    assert second.details["trigger_reason"] == "edit_churn_after_failure"
    assert second.details["source_loop_recovery_count"] == 2

    assert (
        source_loop_recovery_decision(
            global_mode="warn_model",
            diagnostic_events=[_source_loop_event(reason="repeated_source_read_after_write")],
            attempted=True,
            attempted_event_keys={
                first_key,
                second.details["recovery_event_key"],
            },
            max_nudges=2,
        )
        is None
    )


def test_source_loop_recovery_ignores_test_only_diff() -> None:
    decision = source_loop_recovery_decision(
        global_mode="warn_model",
        diagnostic_events=[
            _source_loop_event(path="tests/test_lib.py", path_class="test"),
        ],
        attempted=False,
    )

    assert decision is None


def test_source_loop_recovery_ignores_scratch_like_source_dir_diff() -> None:
    decision = source_loop_recovery_decision(
        global_mode="warn_model",
        diagnostic_events=[
            _source_loop_event(
                path="crates/regex/src/test_word_bug.rs",
                path_class=classify_path("crates/regex/src/test_word_bug.rs"),
            ),
            _source_loop_event(
                path="crates/regex/src/word.rs.test_fix",
                path_class=classify_path("crates/regex/src/word.rs.test_fix"),
            ),
        ],
        attempted=False,
    )

    assert decision is None


def test_runtime_diagnostics_emits_repeated_failure_anchor_at_threshold() -> None:
    observer = RuntimeDiagnosticsObserver(session_key="s", agent_id="a")
    events: list[dict[str, Any]] = []

    for iteration in range(1, 4):
        events.extend(
            observer.observe_tool_results(
                iteration=iteration,
                provider_call_count=iteration,
                tool_calls=[_call(f"tool-{iteration}")],
                results=[_result(f"tool-{iteration}")],
                read_records=[],
                write_records=[_write("src/lib.rs")],
                scratch_records=[],
                diff_paths=["src/lib.rs"],
                diff_fingerprint="diff-a",
                failure_anchor_summary=(
                    "exec_command: error[E0592]: duplicate definitions with name "
                    "`fallback_service`"
                ),
            )
        )

    repeated = [event for event in events if event["reason"] == "repeated_failure_anchor"]
    assert len(repeated) == 1
    assert repeated[0]["trigger_count"] == 3
    assert repeated[0]["failure_anchor_hash"]
    assert repeated[0]["injected_to_model"] is False
    assert repeated[0]["path_classes"]["diff_paths"] == {"src/lib.rs": "source"}


def test_runtime_diagnostics_emits_repeated_source_read_after_write() -> None:
    observer = RuntimeDiagnosticsObserver(session_key="s", agent_id="a")
    read_records: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for iteration in range(1, 7):
        read_records.append(_read("src/lib.rs"))
        events.extend(
            observer.observe_tool_results(
                iteration=iteration,
                provider_call_count=iteration,
                tool_calls=[_call(f"tool-{iteration}", "read_file", {"path": "src/lib.rs"})],
                results=[
                    _result(
                        f"tool-{iteration}",
                        "read_file",
                        content="source",
                        is_error=False,
                    )
                ],
                read_records=list(read_records),
                write_records=[_write("src/lib.rs")],
                scratch_records=[],
                diff_paths=["src/lib.rs"],
                diff_fingerprint="diff-a",
                failure_anchor_summary="",
            )
        )

    reads = [
        event for event in events if event["reason"] == "repeated_source_read_after_write"
    ]
    assert len(reads) == 1
    assert reads[0]["normalized_path"] == "src/lib.rs"
    assert reads[0]["path_hash"]
    assert reads[0]["trigger_count"] == 6


def test_runtime_diagnostics_emits_verification_without_diff_change() -> None:
    observer = RuntimeDiagnosticsObserver(session_key="s", agent_id="a")
    events: list[dict[str, Any]] = []

    for iteration in range(1, 4):
        events.extend(
            observer.observe_tool_results(
                iteration=iteration,
                provider_call_count=iteration,
                tool_calls=[
                    _call(
                        f"tool-{iteration}",
                        "exec_command",
                        {"command": "cd /testbed && cargo test -p axum 2>&1 | tail -20"},
                    )
                ],
                results=[_result(f"tool-{iteration}", is_error=False, content="test failed")],
                read_records=[],
                write_records=[_write("src/lib.rs")],
                scratch_records=[],
                diff_paths=["src/lib.rs"],
                diff_fingerprint="same-diff",
                failure_anchor_summary="",
            )
        )

    verification = [
        event
        for event in events
        if event["reason"] == "repeated_verification_without_diff_change"
    ]
    assert len(verification) == 1
    assert verification[0]["command_family"] == "cargo:test"
    assert verification[0]["diff_fingerprint_after"] == "same-diff"


def test_runtime_diagnostics_emits_finish_error_with_non_empty_diff() -> None:
    observer = RuntimeDiagnosticsObserver(session_key="s", agent_id="a")

    events = observer.observe_finish_error(
        iteration=9,
        provider_call_count=12,
        error_code="agent_runtime_timeout",
        changed_files=["src/lib.rs"],
        diff_paths=["src/lib.rs"],
        diff_fingerprint="diff-final",
    )

    assert len(events) == 1
    event = events[0]
    assert event["reason"] == "finish_error_with_non_empty_diff"
    assert event["diff_paths"] == ["src/lib.rs"]
    assert event["evidence"]["error_code"] == "agent_runtime_timeout"


class _ThreeToolProvider:
    provider_name = "fake"

    def __init__(self, *, tool_turns: int = 3) -> None:
        self.calls = 0
        self.tool_turns = tool_turns
        self.messages_by_call: list[list[Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls += 1
        self.messages_by_call.append(list(messages))
        return self._stream(self.calls)

    async def _stream(self, call_number: int):
        if call_number <= self.tool_turns:
            tool_use_id = f"tool-{call_number}"
            yield ProviderToolUseStartEvent(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
            )
            yield ProviderToolUseEndEvent(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "cargo test -p crate"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderTextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock tool {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


@pytest.mark.asyncio
async def test_agent_skips_git_diff_diagnostics_when_no_observer_needs_them(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_git_runtime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool_context = ToolContext(workspace_dir=str(workspace))

    def unexpected_diff_probe(_self) -> Any:
        raise AssertionError("Git diff diagnostics should stay lazy")

    monkeypatch.setattr(
        Agent,
        "_workspace_diff_paths_for_runtime_event",
        unexpected_diff_probe,
    )
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_fingerprint_for_runtime_event",
        unexpected_diff_probe,
    )

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=_ThreeToolProvider(tool_turns=1),
        config=AgentConfig(max_iterations=2, flush_enabled=False),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("run one command")]

    assert events
    assert unavailable_git_runtime.resolution_calls


@pytest.mark.asyncio
async def test_plain_chat_finishes_when_git_is_unavailable(
    tmp_path,
    unavailable_git_runtime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _ThreeToolProvider(tool_turns=0)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, flush_enabled=False),
        tool_context=ToolContext(workspace_dir=str(workspace)),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert events
    assert provider.calls == 1
    assert unavailable_git_runtime.resolution_calls


@pytest.mark.asyncio
async def test_agent_runtime_diagnostics_write_jsonl_without_model_hint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_paths_for_runtime_event",
        lambda self: ["src/lib.rs"],
    )
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_fingerprint_for_runtime_event",
        lambda self: "same-diff",
    )
    tool_context = ToolContext(workspace_dir=str(workspace), agent_id="agent-1")

    async def handler(call: ToolCall) -> ToolResult:
        tool_context.workspace_file_writes.append(_write("src/lib.rs"))
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="error[E0592]: duplicate definitions with name `fallback_service`",
            is_error=True,
        )

    agent = Agent(
        provider=_ThreeToolProvider(),
        config=AgentConfig(
            max_iterations=4,
            runtime_events_path=str(runtime_events_path),
            tool_result_projection_max_inline_chars=10_000,
            tool_failure_loop_block_threshold=0,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
        tool_context=tool_context,
        session_key="session-1",
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert events
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    diagnostic = next(
        event for event in logged if event.get("reason") == "repeated_failure_anchor"
    )
    assert diagnostic["feature"] == "runtime_observer"
    assert diagnostic["mechanism"] == "trace_cache_diagnostics"
    assert diagnostic["mode"] == "log"
    assert diagnostic["injected_to_model"] is False
    assert diagnostic["changed_files"] == ["src/lib.rs"]


@pytest.mark.asyncio
async def test_agent_source_loop_recovery_warns_model_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_paths_for_runtime_event",
        lambda self: ["src/lib.rs"],
    )
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_fingerprint_for_runtime_event",
        lambda self: "same-diff",
    )
    tool_context = ToolContext(workspace_dir=str(workspace), agent_id="agent-1")

    async def handler(call: ToolCall) -> ToolResult:
        tool_context.workspace_file_writes.append(_write("src/lib.rs"))
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="error[E0592]: duplicate definitions with name `fallback_service`",
            is_error=True,
        )

    provider = _ThreeToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5,
            runtime_events_path=str(runtime_events_path),
            runtime_recovery_mode="warn_model",
            progress_watchdog_mode="log",
            tool_result_projection_max_inline_chars=10_000,
            tool_failure_loop_block_threshold=0,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
        tool_context=tool_context,
        session_key="session-1",
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert events
    assert provider.calls == 4
    assert "[Runtime recovery]" in _message_text(provider.messages_by_call[3])
    assert "[Runtime recovery]" not in _message_text(agent._history)

    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    recovery = next(
        event for event in logged if event.get("mechanism") == "source_loop_recovery"
    )
    assert recovery["feature"] == "runtime_recovery"
    assert recovery["action"] == "nudge"
    assert recovery["mode"] == "warn_model"
    assert recovery["injected_to_model"] is True
    assert recovery["evidence"]["diff_paths"] == ["src/lib.rs"]


@pytest.mark.asyncio
async def test_git_unavailable_skips_runtime_recovery_without_extra_model_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    unavailable_capability = GitCapability(
        state=GitCapabilityState.UNAVAILABLE,
        reason="git_not_found",
    )
    unavailable_result = GitRunResult(
        state=GitRunState.UNAVAILABLE,
        returncode=None,
        stdout=b"",
        stderr=b"git_not_found",
        capability=unavailable_capability,
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: unavailable_result,
    )
    tool_context = ToolContext(workspace_dir=str(workspace), agent_id="agent-1")

    async def handler(call: ToolCall) -> ToolResult:
        tool_context.workspace_file_writes.append(_write("src/lib.rs"))
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="error[E0592]: duplicate definitions with name `fallback_service`",
            is_error=True,
        )

    provider = _ThreeToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5,
            runtime_events_path=str(runtime_events_path),
            runtime_recovery_mode="warn_model",
            progress_watchdog_mode="log",
            tool_result_projection_max_inline_chars=10_000,
            tool_failure_loop_block_threshold=0,
            post_write_convergence_enabled=True,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
        tool_context=tool_context,
        session_key="session-1",
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert events
    assert provider.calls == 4
    assert "[Runtime recovery]" not in _message_text(provider.messages_by_call[-1])
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    skipped = [
        event
        for event in logged
        if event.get("name") == "runtime_git_observation.skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["git_state"] == "unavailable"
    assert not any(
        event.get("mechanism") == "source_loop_recovery" for event in logged
    )


@pytest.mark.asyncio
async def test_agent_source_loop_recovery_can_warn_for_second_source_loop_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_paths_for_runtime_event",
        lambda self: ["src/lib.rs"],
    )
    monkeypatch.setattr(
        Agent,
        "_workspace_diff_fingerprint_for_runtime_event",
        lambda self: "same-diff",
    )
    tool_context = ToolContext(workspace_dir=str(workspace), agent_id="agent-1")
    tool_results_seen = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal tool_results_seen
        tool_results_seen += 1
        tool_context.workspace_file_writes.append(_write("src/lib.rs"))
        error_name = "fallback_service" if tool_results_seen <= 3 else "router_service"
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=f"error[E0592]: duplicate definitions with name `{error_name}`",
            is_error=True,
        )

    provider = _ThreeToolProvider(tool_turns=6)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=8,
            runtime_events_path=str(runtime_events_path),
            runtime_recovery_mode="warn_model",
            runtime_recovery_source_loop_max_nudges=2,
            progress_watchdog_mode="log",
            tool_result_projection_max_inline_chars=10_000,
            tool_failure_loop_block_threshold=0,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
        tool_context=tool_context,
        session_key="session-1",
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert events
    assert provider.calls == 7
    assert "[Runtime recovery]" in _message_text(provider.messages_by_call[3])
    assert "[Runtime recovery]" in _message_text(provider.messages_by_call[6])

    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    recoveries = [
        event for event in logged if event.get("mechanism") == "source_loop_recovery"
    ]
    assert [event["action"] for event in recoveries] == ["nudge", "nudge"]
    assert [
        event["evidence"]["source_loop_recovery_count"] for event in recoveries
    ] == [1, 2]
