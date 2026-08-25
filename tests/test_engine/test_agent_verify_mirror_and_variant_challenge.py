"""Tests for the scratch verify-mirror and finalize variant-challenge levers.

Covers OPENSQUILLA_SCRATCH_VERIFY_MIRROR and
OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE (both off by default): bootstrap env
parsing, deny-message mirror guidance, the anti-weakening hash guard that
withholds evidence credit when mirror copies diverge from their workspace
originals, and the one-shot variant-sweep challenge injection.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from types import SimpleNamespace
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
    _scratch_verify_mirror_from_env,
)
from opensquilla.git_runtime import GitRunState
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools import write_policy
from opensquilla.tools.types import ToolContext

_MIRROR_ENV = "OPENSQUILLA_SCRATCH_VERIFY_MIRROR"
_VARIANT_ENV = "OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE"


# ---------------------------------------------------------------------------
# Bootstrap env parsing (house ON/OFF pattern)
# ---------------------------------------------------------------------------


def test_bootstrap_scratch_verify_mirror_env_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv(_MIRROR_ENV, raising=False)

    assert _scratch_verify_mirror_from_env() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_bootstrap_scratch_verify_mirror_env_on(monkeypatch, value: str) -> None:
    monkeypatch.setenv(_MIRROR_ENV, value)

    assert _scratch_verify_mirror_from_env() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "NO", "  "])
def test_bootstrap_scratch_verify_mirror_env_off_or_blank(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv(_MIRROR_ENV, value)

    assert _scratch_verify_mirror_from_env() is False


def test_bootstrap_scratch_verify_mirror_env_rejects_unrecognized_value(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_MIRROR_ENV, "enabled")

    with pytest.raises(ValueError, match=_MIRROR_ENV):
        _scratch_verify_mirror_from_env()


def test_bootstrap_scratch_verify_mirror_uses_config_value_when_env_absent(
    monkeypatch,
) -> None:
    monkeypatch.delenv(_MIRROR_ENV, raising=False)

    assert _scratch_verify_mirror_from_env(True) is True
    assert _scratch_verify_mirror_from_env(False) is False


def test_bootstrap_scratch_verify_mirror_env_off_overrides_config_on(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_MIRROR_ENV, "off")

    assert _scratch_verify_mirror_from_env(True) is False


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


def test_agent_config_defaults_keep_both_levers_off() -> None:
    config = AgentConfig()

    assert config.scratch_verify_mirror is False
    assert config.finalize_variant_challenge is False


# ---------------------------------------------------------------------------
# Deny-message mirror guidance (write_policy seam)
# ---------------------------------------------------------------------------


def _deny_match(workspace, target) -> write_policy.WorkspaceWriteDenyMatch:
    match = write_policy.match_workspace_write_deny(
        target,
        workspace=workspace,
        ctx=None,
    )
    assert match is not None
    return match


def _mirror_ctx(tmp_path, *, active: bool) -> ToolContext:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    scratch.mkdir(exist_ok=True)
    ctx = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        workspace_write_deny_globs=["tests/**"],
    )
    ctx.scratch_verify_mirror_active = active
    return ctx


def test_deny_message_appends_mirror_guidance_when_lever_active(tmp_path) -> None:
    ctx = _mirror_ctx(tmp_path, active=True)
    workspace = tmp_path / "workspace"
    token = write_policy.current_tool_context.set(ctx)
    try:
        match = write_policy.match_workspace_write_deny(
            workspace / "tests" / "test_a.py", workspace=workspace, ctx=ctx
        )
        assert match is not None
        block = write_policy.workspace_write_deny_block("write_file", match)
    finally:
        write_policy.current_tool_context.reset(token)

    message = str(block["message"])
    expected_mirror = (tmp_path / "scratch" / "verify-mirror" / "tests" / "test_a.py").as_posix()
    assert expected_mirror in message
    assert "keep the mirror copy identical to the workspace original" in message


def test_deny_message_has_no_mirror_guidance_by_default(tmp_path) -> None:
    ctx = _mirror_ctx(tmp_path, active=False)
    workspace = tmp_path / "workspace"
    token = write_policy.current_tool_context.set(ctx)
    try:
        match = write_policy.match_workspace_write_deny(
            workspace / "tests" / "test_a.py", workspace=workspace, ctx=ctx
        )
        assert match is not None
        block = write_policy.workspace_write_deny_block("write_file", match)
    finally:
        write_policy.current_tool_context.reset(token)

    assert "verify-mirror" not in str(block["message"])


def test_verify_mirror_path_requires_workspace_membership(tmp_path) -> None:
    ctx = _mirror_ctx(tmp_path, active=True)

    outside = write_policy.verify_mirror_path(
        "/etc/passwd", "/etc/passwd", ctx
    )

    assert outside is None


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
# Agent-side hash guard
# ---------------------------------------------------------------------------


def _guard_agent(tmp_path, *, scratch: bool = True) -> Agent:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(exist_ok=True)
    tool_context = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch_dir) if scratch else None,
    )
    return Agent(
        provider=None,
        config=AgentConfig(scratch_verify_mirror=True),
        tool_context=tool_context,
    )


def test_hash_guard_credits_command_not_referencing_mirror(tmp_path) -> None:
    agent = _guard_agent(tmp_path)

    assert agent._scratch_verify_mirror_evidence_credit("pytest tests/") is True


def test_hash_guard_credits_matching_mirror_copy(tmp_path) -> None:
    agent = _guard_agent(tmp_path)
    original = tmp_path / "workspace" / "tests" / "test_a.py"
    original.parent.mkdir(parents=True)
    original.write_text("assert a\n", encoding="utf-8")
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_a.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert a\n", encoding="utf-8")

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is True


def test_hash_guard_withholds_credit_for_diverged_mirror_copy(tmp_path) -> None:
    agent = _guard_agent(tmp_path)
    original = tmp_path / "workspace" / "tests" / "test_a.py"
    original.parent.mkdir(parents=True)
    original.write_text("assert a\n", encoding="utf-8")
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_a.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert True  # weakened\n", encoding="utf-8")

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is False


def test_hash_guard_withholds_credit_for_missing_original_outside_repository(
    tmp_path,
) -> None:
    agent = _guard_agent(tmp_path)
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_extra.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert extra_case()\n", encoding="utf-8")

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is False


def test_hash_guard_allows_new_check_files_shadowing_nothing(tmp_path) -> None:
    # The workspace is a git repo with no tests/test_extra.py anywhere: the
    # mirror file is the model's own new check, not a weakened copy.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    (workspace / "src.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "src.py"], cwd=workspace, check=True, capture_output=True
    )
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
    agent = _guard_agent(tmp_path)
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_extra.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert extra_case()\n", encoding="utf-8")

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is True


def test_hash_guard_withholds_credit_when_head_blob_probe_becomes_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _guard_agent(tmp_path)
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_extra.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert extra_case()\n", encoding="utf-8")

    def fake_run(args, **_kwargs):
        if args[0] == "rev-parse":
            return SimpleNamespace(ok=True, state=GitRunState.OK, stdout=b"true\n")
        assert args[0] == "show"
        return SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
            stdout=b"",
            stderr_text="git_not_found",
        )

    monkeypatch.setattr("opensquilla.engine.agent.run_git", fake_run)

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is False


def test_hash_guard_withholds_credit_for_deleted_original_with_diverged_head(
    tmp_path,
) -> None:
    # Original committed then deleted from the worktree: the HEAD blob is
    # still the reference and the diverged mirror must not earn credit.
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "tests/test_a.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
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
    (tests_dir / "test_a.py").unlink()
    agent = _guard_agent(tmp_path)
    mirror = tmp_path / "scratch" / "verify-mirror" / "tests" / "test_a.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("assert True  # weakened\n", encoding="utf-8")

    command = f"pytest {mirror.as_posix()}"

    assert agent._scratch_verify_mirror_evidence_credit(command) is False


def test_hash_guard_credits_when_no_scratch_dir_configured(tmp_path) -> None:
    agent = _guard_agent(tmp_path, scratch=False)

    command = "pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py"

    assert agent._scratch_verify_mirror_evidence_credit(command) is True


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
async def test_variant_challenge_arms_mirror_guidance_flag(tmp_path) -> None:
    # The scratch_verify_mirror lever arms the ToolContext flag at turn
    # start so deny messages carry the mirror guidance for the whole turn.
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider([("final",)])
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    assert tool_context.scratch_verify_mirror_active is False
    agent = Agent(
        provider=provider,
        config=_variant_config(scratch_verify_mirror=True),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    [event async for event in agent.run_turn("Fix the bug")]

    assert agent._tool_context is not None
    assert agent._tool_context.scratch_verify_mirror_active is True
