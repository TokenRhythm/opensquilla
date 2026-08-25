"""Agent-loop tests for the review-on-submit checkpoint.

Scripted-provider tests covering the loop-level contract: off by default, the
explicit ``submit`` tool returns the checklist as a tool result (empty diff
returns the no-op note without spending the review), the implicit finalize path
injects the review exactly once for an unreviewed green diff, and unresolved red
execution evidence suppresses the implicit review (the red-evidence gate owns
that case).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import (
    Agent,
    AgentConfig,
    DoneEvent,
    ToolResult,
    ToolResultEvent,
    WarningEvent,
)
from opensquilla.git_runtime import (
    GitCapability,
    GitCapabilityState,
    GitRunResult,
    GitRunState,
)
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.types import ToolContext

_RED_MARKER = "fail-run"


def _init_git_workspace(workspace) -> Any:
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    source = workspace / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=workspace,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return source


class _ScriptedProvider:
    """Replays a fixed per-call script of tool calls and final texts.

    Script entries are ``("exec", command)``, ``("edit", path)``,
    ``("submit",)``, or ``("final",)``. Any call past the end of the script
    yields a final text.
    """

    provider_name = "fake"

    def __init__(self, script: list[tuple[str, ...]]) -> None:
        self.calls: list[list[Message]] = []
        self._script = script

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        entry: tuple[str, ...] = ("final",)
        if call_number <= len(self._script):
            entry = self._script[call_number - 1]
        if entry[0] == "edit":
            tool_use_id = f"edit-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": entry[1], "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if entry[0] == "exec":
            tool_use_id = f"cmd-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": entry[1]},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if entry[0] == "submit":
            tool_use_id = f"submit-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="submit")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="submit",
                arguments={"summary": "done"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _make_tool_handler(workspace, tool_context: ToolContext):
    source = workspace / "src.py"

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "exec_command":
            command = str(call.arguments.get("command") or "")
            red = _RED_MARKER in command
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=(
                    "exit_code=1\nFAILED: assertion did not hold" if red else "exit_code=0\nok"
                ),
                is_error=red,
                execution_status={
                    "version": 1,
                    "status": "error" if red else "success",
                    "exit_code": 1 if red else 0,
                    "timed_out": False,
                    "truncated": False,
                    "reason": "nonzero_exit" if red else None,
                    "source": "adapter",
                    "preservation_class": "diagnostic" if red else "normal",
                },
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    return _tool


def _config(tmp_path, *, enabled: bool = True, **overrides: Any) -> AgentConfig:
    return AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        # "log" keeps the pre-existing empty-diff/failed-tool warn_model
        # recoveries out of the way so only the submit review injects here.
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
        submit_review_enabled=enabled,
        **overrides,
    )


def _review_warnings(events: list[Any]) -> list[WarningEvent]:
    return [
        event
        for event in events
        if isinstance(event, WarningEvent) and event.code == "submit_review_implicit"
    ]


def _submit_results(events: list[Any]) -> list[ToolResultEvent]:
    return [
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_name == "submit"
    ]


def _injected_reviews(messages: list[Message]) -> list[str]:
    return [
        message.content
        for message in messages
        if isinstance(message.content, str) and message.content.startswith("[Submit review]")
    ]


@pytest.mark.asyncio
async def test_off_by_default_no_review_injected(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    provider = _ScriptedProvider([("edit", "src.py"), ("final",)])
    tool_context = ToolContext(workspace_dir=str(ws))
    config = AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
    )
    assert config.submit_review_enabled is False
    agent = Agent(
        provider=provider,
        config=config,
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 2"


@pytest.mark.asyncio
async def test_implicit_review_injected_once_for_green_diff(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider([("edit", "src.py"), ("final",), ("final",)])
    tool_context = ToolContext(workspace_dir=str(ws))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # First finish injects the review; second finish passes through unchanged.
    assert len(provider.calls) == 3
    reviews = _injected_reviews(provider.calls[2])
    assert len(reviews) == 1
    assert "will be submitted as they are" in reviews[0]  # implicit closing line
    assert "src.py" in reviews[0]  # the per-file summary is present
    assert "minimal" not in reviews[0].lower()
    assert len(_review_warnings(events)) == 1
    assert agent.config.metadata["submit_review_implicit_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"

    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    implicit = [event for event in logged if event.get("name") == "submit_review.implicit"]
    assert len(implicit) == 1
    assert implicit[0]["feature"] == "submit_review"
    assert implicit[0]["injected_to_model"] is True


@pytest.mark.asyncio
async def test_implicit_review_not_fired_for_empty_diff(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    provider = _ScriptedProvider([("final",)])
    tool_context = ToolContext(workspace_dir=str(ws))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path),
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 1
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 1"


@pytest.mark.asyncio
async def test_explicit_submit_returns_checklist_then_confirms(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    # Premature submit (empty diff) -> note; edit; real submit -> checklist;
    # finish -> passes through because the review was already shown explicitly.
    provider = _ScriptedProvider(
        [("submit",), ("edit", "src.py"), ("submit",), ("final",)]
    )
    tool_context = ToolContext(workspace_dir=str(ws))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 4
    submit_results = _submit_results(events)
    assert len(submit_results) == 2
    # Premature submit: no workspace changes yet, review not spent.
    assert "no workspace changes yet" in submit_results[0].result
    assert not submit_results[0].is_error
    # Real submit: the general hygiene checklist, without the implicit warning.
    assert submit_results[1].result.startswith("[Submit review]")
    assert "will be submitted as they are" not in submit_results[1].result
    assert "src.py" in submit_results[1].result
    # The explicit path handled the review; the implicit gate never fired.
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 4"

    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    explicit = [event for event in logged if event.get("name") == "submit_review.explicit"]
    assert [event["reason"] for event in explicit] == ["empty_diff_note", "show_checklist"]


@pytest.mark.asyncio
async def test_confirming_submit_terminates_the_turn(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    # edit -> submit (checklist) -> edit (acts on the checklist) -> submit
    # (confirm). The confirming submit must end the turn on the current diff, so
    # the trailing scripted ("final",) call is never requested. A run that did
    # not terminate would fall through to it and make five provider calls.
    provider = _ScriptedProvider(
        [("edit", "src.py"), ("submit",), ("edit", "src.py"), ("submit",), ("final",)]
    )
    tool_context = ToolContext(workspace_dir=str(ws))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # Four provider calls, not five: the confirming submit terminated the turn
    # before the scripted final text could be reached.
    assert len(provider.calls) == 4
    submit_results = _submit_results(events)
    assert len(submit_results) == 2
    assert submit_results[0].result.startswith("[Submit review]")
    assert "Submission received" in submit_results[1].result
    # The implicit gate never fired; the explicit confirm owned the finish.
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata

    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    explicit = [event for event in logged if event.get("name") == "submit_review.explicit"]
    assert [event["reason"] for event in explicit] == ["show_checklist", "confirm"]
    # Only the confirming submit carries the terminate flag.
    assert explicit[0]["details"]["terminates_turn"] is False
    assert explicit[-1]["details"]["terminates_turn"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git_state", "expected_code"),
    [
        (GitRunState.UNAVAILABLE, "GIT_UNAVAILABLE"),
        (GitRunState.NOT_REPOSITORY, "GIT_NOT_REPOSITORY"),
    ],
)
async def test_explicit_submit_git_unknown_terminates_without_empty_diff_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    git_state: GitRunState,
    expected_code: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = GitCapability(
        state=(
            GitCapabilityState.UNAVAILABLE
            if git_state is GitRunState.UNAVAILABLE
            else GitCapabilityState.AVAILABLE
        ),
        executable=(
            None if git_state is GitRunState.UNAVAILABLE else workspace / "git"
        ),
        source="test",
        reason="git_not_found" if git_state is GitRunState.UNAVAILABLE else None,
    )
    result = GitRunResult(
        state=git_state,
        returncode=None if git_state is GitRunState.UNAVAILABLE else 128,
        stdout=b"",
        stderr=b"git unavailable",
        capability=capability,
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: result,
    )
    provider = _ScriptedProvider([("submit",), ("final",)])
    tool_context = ToolContext(workspace_dir=str(workspace))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path),
        tool_handler=_make_tool_handler(workspace, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Submit the work")]

    submit_results = _submit_results(events)
    assert len(provider.calls) == 1
    assert len(submit_results) == 1
    assert submit_results[0].is_error is True
    payload = json.loads(submit_results[0].result)
    assert payload["code"] == expected_code
    assert payload["git_state"] == git_state.value
    assert payload["retryable"] is False
    assert "no workspace changes yet" not in submit_results[0].result


@pytest.mark.asyncio
async def test_implicit_submit_review_skips_git_unknown_without_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = GitCapability(
        state=GitCapabilityState.UNAVAILABLE,
        reason="git_not_found",
    )
    unavailable = GitRunResult(
        state=GitRunState.UNAVAILABLE,
        returncode=None,
        stdout=b"",
        stderr=b"git_not_found",
        capability=capability,
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: unavailable,
    )
    provider = _ScriptedProvider([("final",), ("final",)])
    tool_context = ToolContext(workspace_dir=str(workspace))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path),
        tool_handler=_make_tool_handler(workspace, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Submit the work")]

    assert len(provider.calls) == 1
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata


@pytest.mark.asyncio
async def test_submit_review_captures_staged_diff_in_unborn_repository(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    source = workspace / "new.py"
    source.write_text("print('new')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "new.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    agent = Agent(
        provider=None,  # type: ignore[arg-type]
        config=_config(tmp_path),
        tool_context=ToolContext(workspace_dir=str(workspace)),
    )

    capture = await agent._workspace_submit_review_capture()

    assert capture is not None
    file_index, diff_text = capture
    assert agent._submit_review_git_state is GitRunState.OK
    assert "A  new.py" in file_index
    assert "diff --git a/new.py b/new.py" in diff_text
    assert "+print('new')" in diff_text


@pytest.mark.asyncio
async def test_implicit_review_suppressed_by_unresolved_red_evidence(tmp_path) -> None:
    ws = tmp_path / "ws"
    _init_git_workspace(ws)
    # Edit, run a red command, then finish twice. The red-evidence gate owns
    # the first finish; on the second (same red state) it is suppressed but the
    # red flag still excludes the submit review — the two must not double-fire.
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(ws))
    agent = Agent(
        provider=provider,
        config=_config(tmp_path, finalize_evidence_gate_enabled=True),
        tool_handler=_make_tool_handler(ws, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 4
    # The submit review never fired: red evidence is the red-gate's domain.
    assert _review_warnings(events) == []
    assert "submit_review_implicit_recoveries" not in agent.config.metadata
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 2
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 4"
