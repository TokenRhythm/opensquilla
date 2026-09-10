"""Tests for finalize variant challenges and verification credit.

The retired scratch mirror fields remain inert compatibility slots.
"""

from __future__ import annotations

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
from opensquilla.engine.finalize_evidence_gate import FinalizeEvidenceTracker
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _finalize_variant_challenge_from_env,
)
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools import write_policy
from opensquilla.tools.types import ToolContext

_VARIANT_ENV = "OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE"


# ---------------------------------------------------------------------------
# Bootstrap env parsing (house ON/OFF pattern)
# ---------------------------------------------------------------------------


def test_bootstrap_finalize_variant_challenge_env_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv(_VARIANT_ENV, raising=False)

    assert _finalize_variant_challenge_from_env() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_bootstrap_finalize_variant_challenge_env_on(monkeypatch, value: str) -> None:
    monkeypatch.setenv(_VARIANT_ENV, value)

    assert _finalize_variant_challenge_from_env() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "NO", "  "])
def test_bootstrap_finalize_variant_challenge_env_off_or_blank(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv(_VARIANT_ENV, value)

    assert _finalize_variant_challenge_from_env() is False


def test_bootstrap_finalize_variant_challenge_env_rejects_unrecognized_value(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_VARIANT_ENV, "enabled")

    with pytest.raises(ValueError, match=_VARIANT_ENV):
        _finalize_variant_challenge_from_env()


def test_bootstrap_finalize_variant_challenge_env_off_overrides_config_on(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_VARIANT_ENV, "off")

    assert _finalize_variant_challenge_from_env(True) is False


def test_agent_config_defaults_keep_variant_challenge_off() -> None:
    config = AgentConfig()

    assert config.finalize_variant_challenge is False


@pytest.mark.parametrize("retired_mirror_active", [False, True])
def test_retired_mirror_slot_does_not_change_write_denial(
    tmp_path, monkeypatch, retired_mirror_active
) -> None:
    monkeypatch.delenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE", raising=False)
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    ctx = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        workspace_write_deny_globs=["tests/**"],
        scratch_verify_mirror_active=retired_mirror_active,
    )
    token = write_policy.current_tool_context.set(ctx)
    try:
        match = write_policy.match_workspace_write_deny(
            workspace / "tests" / "test_a.py", workspace=workspace, ctx=ctx
        )
        assert match is not None
        block = write_policy.workspace_write_deny_block("write_file", match)
    finally:
        write_policy.current_tool_context.reset(token)

    assert block["reason"] == "workspace_write_deny"
    assert block["retryable"] is False
    assert str(scratch) in str(block["message"])
    assert "verify-mirror" not in str(block["message"])


# ---------------------------------------------------------------------------
# Tracker: evidence_credit=False withholds all verification crediting
# ---------------------------------------------------------------------------


def test_tracker_uncredited_green_does_not_clear_red_evidence() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest tests/test_a.py",
        red=True,
        exit_code=1,
        failure_anchors=["FAILED tests/test_a.py::test_x"],
        iteration=2,
    )
    tracker.observe_execution(
        "pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=3,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    # The uncredited mirror green must not become the trailing post-edit
    # record: the earlier red is still the latest credited execution.
    assert observation.should_challenge is True
    assert observation.triggers[0] == "red_execution_after_final_edit"


def test_tracker_uncredited_run_counts_no_verification_in_strict_mode() -> None:
    tracker = FinalizeEvidenceTracker(strict=True)
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=2,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert observation.verification_command_count == 0
    assert "zero_verification" in observation.triggers


def test_tracker_uncredited_run_still_tracks_deletion_side_effects() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=1)
    tracker.observe_write("src/main.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py",
        red=True,
        exit_code=1,
        iteration=3,
    )
    # The uncredited command still deletes the artifact: side effects are
    # facts about the filesystem, not verification evidence.
    tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py"
        " && pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=4,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert "never_green_repro_deleted" in observation.triggers


def test_tracker_evidence_credit_defaults_true() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest tests/test_a.py", red=False, exit_code=0, iteration=2
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert observation.should_challenge is False
    assert observation.verification_command_count == 1


# ---------------------------------------------------------------------------
# Variant-challenge loop behavior (scripted provider)
# ---------------------------------------------------------------------------


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
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="exit_code=0\nok",
                execution_status={
                    "version": 1,
                    "status": "success",
                    "exit_code": 0,
                    "timed_out": False,
                    "truncated": False,
                    "reason": None,
                    "source": "adapter",
                    "preservation_class": "normal",
                },
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    return _tool


def _variant_config(**overrides: Any) -> AgentConfig:
    return AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
        **overrides,
    )


def _variant_warnings(events: list[Any]) -> list[WarningEvent]:
    return [
        event
        for event in events
        if isinstance(event, WarningEvent)
        and event.code == "finalize_variant_challenge_recovery"
    ]


@pytest.mark.asyncio
async def test_variant_challenge_off_by_default(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider([("edit", "src.py"), ("final",)])
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    config = _variant_config()
    assert config.finalize_variant_challenge is False
    agent = Agent(
        provider=provider,
        config=config,
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _variant_warnings(events) == []
    assert "finalize_variant_challenge_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_variant_challenge_fires_once_then_accepts(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("final",),
            ("exec", "pytest tests/"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_variant_config(finalize_variant_challenge=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # edit, challenged final, post-challenge exec, accepted final.
    assert len(provider.calls) == 4
    assert len(_variant_warnings(events)) == 1
    challenge_messages = [
        message.content
        for call in provider.calls
        for message in call
        if message.role == "user"
        and isinstance(message.content, str)
        and message.content.startswith("[Variant sweep check]")
    ]
    assert challenge_messages
    challenge = challenge_messages[0]
    assert "input or construct classes" in challenge
    for banned in ("minimal", "localized", "not sufficient"):
        assert banned not in challenge
    assert agent.config.metadata["finalize_variant_challenge_detections"] == 1
    assert agent.config.metadata["finalize_variant_challenge_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 4"


@pytest.mark.asyncio
async def test_variant_challenge_never_fires_twice(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    # The model finalizes immediately again after the challenge: the second
    # finalize must be accepted, not re-challenged.
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
        config=_variant_config(finalize_variant_challenge=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 3
    assert len(_variant_warnings(events)) == 1
    assert agent.config.metadata["finalize_variant_challenge_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


@pytest.mark.asyncio
async def test_variant_challenge_quiet_without_workspace_diff(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider([("final",)])
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_variant_config(finalize_variant_challenge=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 1
    assert _variant_warnings(events) == []
    assert "finalize_variant_challenge_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_variant_challenge_suppressed_without_llm_call_headroom(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider([("edit", "src.py"), ("final",)])
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_variant_config(
            finalize_variant_challenge=True,
            max_turn_llm_calls=2,
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # The second call is the last allowed one: injecting would discard the
    # final answer with no headroom for a follow-up, so the gate detects but
    # does not inject.
    assert len(provider.calls) == 2
    assert _variant_warnings(events) == []
    assert agent.config.metadata["finalize_variant_challenge_detections"] == 1
    assert "finalize_variant_challenge_recoveries" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 2"


@pytest.mark.asyncio
async def test_retired_mirror_config_does_not_arm_tool_context(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider([("final",)])
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_variant_config(scratch_verify_mirror=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Inspect the workspace")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert tool_context.scratch_verify_mirror_active is False
