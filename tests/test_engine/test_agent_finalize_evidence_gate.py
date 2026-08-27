"""Agent-loop tests for the finalize-time red-evidence gate.

Scripted-provider tests covering the loop-level contract: off by default,
challenge injection with polarity-correct message, same-state dedup, the
2-challenge cap, and that the gate never blocks finalization.
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
    WarningEvent,
)
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.mutation_receipts import (
    fingerprint_path,
    record_semantic_mutation_receipt,
)
from opensquilla.tools.types import ToolContext

_RED_MARKER = "fail-run"


def _init_git_workspace(tmp_path) -> Any:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
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
    ``("read", path)``, or ``("final",)``. Any call past the end of the
    script yields a final text.
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
        if entry[0] == "read":
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": entry[1]},
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
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _make_tool_handler(tmp_path, tool_context: ToolContext):
    source = tmp_path / "src.py"

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
                    "exit_code=1\nFAILED: assertion did not hold"
                    if red
                    else "exit_code=0\nok"
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


def _gate_config(tmp_path, *, enabled: bool = True, **overrides: Any) -> AgentConfig:
    return AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        # "log" keeps the pre-existing failed-tool/empty-diff warn_model
        # recoveries out of the way so only the gate injects here.
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
        finalize_evidence_gate_enabled=enabled,
        **overrides,
    )


def _gate_warnings(events: list[Any]) -> list[WarningEvent]:
    return [
        event
        for event in events
        if isinstance(event, WarningEvent)
        and event.code == "finalize_evidence_gate_recovery"
    ]


@pytest.mark.asyncio
async def test_gate_off_by_default_red_final_is_accepted(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    config = AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
    )
    assert config.finalize_evidence_gate_enabled is False
    agent = Agent(
        provider=provider,
        config=config,
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


@pytest.mark.asyncio
async def test_gate_skips_when_git_status_is_unavailable(tmp_path, monkeypatch) -> None:
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    async def unavailable_status() -> None:
        return None

    monkeypatch.setattr(agent, "_workspace_git_status_porcelain", unavailable_status)

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_gate_challenges_red_final_then_accepts_verified_final(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
            ("exec", "python /tmp/squilla-scratch/rerun_fixed.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 5
    challenge_messages = [
        message.content
        for message in provider.calls[3]
        if isinstance(message.content, str)
        and message.content.startswith("[Finalize evidence check]")
    ]
    assert len(challenge_messages) == 1
    assert "python /tmp/squilla-scratch/fail-run.py" in challenge_messages[0]
    assert "binding evidence" in challenge_messages[0]
    assert "minimal" not in challenge_messages[0].lower()
    assert len(_gate_warnings(events)) == 1
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 1
    assert agent.config.metadata["finalize_evidence_gate_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 5"

    logged = [
        json.loads(line) for line in runtime_events_path.read_text().splitlines()
    ]
    challenges = [
        event for event in logged if event.get("name") == "finalize_evidence_gate.challenge"
    ]
    assert len(challenges) == 1
    assert challenges[0]["feature"] == "finalize_evidence_gate"
    assert challenges[0]["reason"] == "red_execution_after_final_edit"
    assert challenges[0]["injected_to_model"] is True
    assert challenges[0]["details"]["triggers"] == ["red_execution_after_final_edit"]
    assert challenges[0]["details"]["red_exit_code"] == 1


@pytest.mark.asyncio
async def test_gate_same_red_state_challenges_once_and_never_blocks(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
            # The model finalizes again without re-running anything: the
            # observation key is unchanged, so the gate stays quiet.
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 4
    assert len(_gate_warnings(events)) == 1
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 2
    assert agent.config.metadata["finalize_evidence_gate_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 4"

    logged = [
        json.loads(line) for line in runtime_events_path.read_text().splitlines()
    ]
    challenges = [
        event for event in logged if event.get("name") == "finalize_evidence_gate.challenge"
    ]
    assert [event["injected_to_model"] for event in challenges] == [True, False]


@pytest.mark.asyncio
async def test_gate_caps_at_two_challenges_then_accepts_red_final(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run-a.py"),
            ("final",),
            ("exec", "python /tmp/squilla-scratch/fail-run-b.py"),
            ("final",),
            ("exec", "python /tmp/squilla-scratch/fail-run-c.py"),
            # Third distinct red state: the cap (2) is reached, the run must
            # finish even though the evidence is still red.
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 7
    assert len(_gate_warnings(events)) == 2
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 3
    assert agent.config.metadata["finalize_evidence_gate_recoveries"] == 2
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 7"


@pytest.mark.asyncio
async def test_gate_quiet_when_final_edit_is_verified_green(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/repro_check.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


@pytest.mark.asyncio
async def test_gate_suppressed_without_llm_call_budget_headroom(tmp_path) -> None:
    """A challenge must never spend the run's last LLM call: with no headroom
    for a follow-up call the injection would discard the model's final answer
    and end the turn in a hard budget error instead of a submission."""

    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(
            tmp_path,
            max_turn_llm_calls=3,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # The red state is detected but not injected: the final answer stands.
    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 1
    assert "finalize_evidence_gate_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"

    logged = [
        json.loads(line) for line in runtime_events_path.read_text().splitlines()
    ]
    challenges = [
        event for event in logged if event.get("name") == "finalize_evidence_gate.challenge"
    ]
    assert len(challenges) == 1
    assert challenges[0]["injected_to_model"] is False


@pytest.mark.asyncio
async def test_gate_defers_to_post_write_convergence_finalization(tmp_path) -> None:
    """When post-write convergence has already forced a tools-disabled
    wrap-up, the gate must not challenge that wrap-up: it would contradict
    the convergence instruction ("do not call tools") with its own ("re-run
    the command"), and the model cannot satisfy both."""

    source = _init_git_workspace(tmp_path)
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            # Self-written repro stays red: the gate would normally hold the
            # run open as red_repro_outstanding_after_final_edit.
            ("edit", "/tmp/squilla-scratch/fail-run-repro.py"),
            ("exec", "python /tmp/squilla-scratch/fail-run-repro.py"),
            # Green focused verification: convergence-eligible from here on.
            ("exec", "pytest tests/test_src.py"),
            *[("read", "src.py")] * 6,
        ]
    )

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            path = str(call.arguments.get("path") or "")
            if path == "src.py":
                before = fingerprint_path(source)
                source.write_text("new\n", encoding="utf-8")
                after = fingerprint_path(source)
                record_semantic_mutation_receipt(
                    tool_name="edit_file",
                    path=source,
                    operation="edit_file",
                    before=before,
                    after=after,
                    partial=False,
                    ctx=tool_context,
                )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "read_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=source.read_text(encoding="utf-8"),
            )
        if call.tool_name == "exec_command":
            command = str(call.arguments.get("command") or "")
            red = _RED_MARKER in command
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=(
                    "exit_code=1\nFAILED: assertion did not hold"
                    if red
                    else "test result: ok. 4 passed; 0 failed\n"
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

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=15,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            post_write_convergence_enabled=True,
            finalize_evidence_gate_enabled=True,
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # The convergence wrap-up finished the run in one final call.
    assert agent.config.metadata["post_write_convergence_finalizations"] == 1
    assert len(provider.calls) == 11
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 11"
    # The gate never evaluated the wrap-up, despite the outstanding red repro.
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata
    assert not any(
        isinstance(message.content, str)
        and message.content.startswith("[Finalize evidence check]")
        for call in provider.calls
        for message in call
    )


_DENIED_MARKER = "blocked-run"


@pytest.mark.asyncio
async def test_gate_quiet_when_trailing_execution_was_denied(tmp_path) -> None:
    """A policy-denied execution is not red evidence: with a green
    verification before it, the gate must accept the final answer instead of
    challenging on the denied command (live valid1 defect: sandbox blocks
    dominated challenge reds)."""

    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "pytest tests/test_src.py"),
            ("exec", f"python /tmp/{_DENIED_MARKER}.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    base_handler = _make_tool_handler(tmp_path, tool_context)

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "exec_command" and _DENIED_MARKER in str(
            call.arguments.get("command") or ""
        ):
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content='{"status": "policy_denied", "message": "blocked by sandbox policy"}',
                is_error=True,
                execution_status={
                    "version": 1,
                    "status": "error",
                    "exit_code": None,
                    "timed_out": False,
                    "truncated": False,
                    "reason": "denied",
                    "source": "tool_runtime",
                    "preservation_class": "diagnostic",
                },
            )
        return await base_handler(call)

    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 4
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 4"


# ---------------------------------------------------------------------------
# Strict mode (OPENSQUILLA_FINALIZE_EVIDENCE_STRICT)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_gate_accepts_green_only_run(tmp_path) -> None:
    """A green-only run (edit, suite passes, finalize) must sail through
    strict mode unchallenged: red-first state is report-only because
    never-red is a routine signature of legitimately solved runs."""

    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "pytest tests/test_src.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(
            tmp_path,
            finalize_evidence_strict=True,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"

    if runtime_events_path.exists():
        logged = [
            json.loads(line) for line in runtime_events_path.read_text().splitlines()
        ]
        assert not [
            event
            for event in logged
            if event.get("name") == "finalize_evidence_gate.challenge"
        ]


@pytest.mark.asyncio
async def test_strict_gate_zero_verification_challenge_never_blocks(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("final",),
            # The model finalizes again without running anything: same
            # observation key, so the gate stays quiet and the run finishes.
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(
            tmp_path,
            finalize_evidence_strict=True,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    challenge_messages = [
        message.content
        for message in provider.calls[2]
        if isinstance(message.content, str)
        and message.content.startswith("[Finalize evidence check]")
    ]
    assert len(challenge_messages) == 1
    assert "no execution-level command ran at any point" in challenge_messages[0]
    assert len(_gate_warnings(events)) == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"

    logged = [
        json.loads(line) for line in runtime_events_path.read_text().splitlines()
    ]
    challenges = [
        event for event in logged if event.get("name") == "finalize_evidence_gate.challenge"
    ]
    assert [event["injected_to_model"] for event in challenges] == [True, False]
    assert "zero_verification" in challenges[0]["details"]["triggers"]


@pytest.mark.asyncio
async def test_strict_flag_activates_tracker_without_base_gate(tmp_path) -> None:
    # zero_verification is the only strict trigger; an edit-then-finalize run
    # with no execution at all must draw the challenge even when the base
    # gate flag is off.
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("final",),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_gate_config(tmp_path, enabled=False, finalize_evidence_strict=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert len(_gate_warnings(events)) == 1
    assert agent.config.metadata["finalize_evidence_gate_detections"] == 2
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


@pytest.mark.asyncio
async def test_base_gate_without_strict_accepts_never_red_run(tmp_path) -> None:
    """Default-off safety: the strict triggers must not fire when only the
    base gate is enabled, even on a green-only (never-red) run."""

    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("exec", "pytest tests/test_src.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    config = _gate_config(tmp_path)
    assert config.finalize_evidence_strict is False
    agent = Agent(
        provider=provider,
        config=config,
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert _gate_warnings(events) == []
    assert "finalize_evidence_gate_detections" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"
