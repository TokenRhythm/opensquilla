"""Agent-loop tests for the finalize-time patch hygiene hard block.

Scripted-provider tests covering the loop-level contract of
OPENSQUILLA_PATCH_HYGIENE_BLOCK: off by default, challenge injection while
the live diff still touches offending paths (test-classified in
``test_paths`` mode; deployment write-deny-glob matches in
``protected_paths`` mode), dedup on the same offending path set, the
challenge cap, headroom suppression, and that the block never fires on
diffs the mode does not cover.
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
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _patch_hygiene_block_from_env,
)
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.types import ToolContext


def _init_git_workspace(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "src.py").write_text("old\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
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


class _ScriptedProvider:
    """Replays a fixed per-call script of tool calls and final texts.

    Script entries are ``("edit", path)`` (writes "new"), ``("restore",
    path)`` (writes the original "old" content back), or ``("final",)``. Any
    call past the end of the script yields a final text.
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
        if entry[0] in ("edit", "restore"):
            tool_use_id = f"{entry[0]}-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={
                    "path": entry[1],
                    "old_text": "old" if entry[0] == "edit" else "new",
                    "new_text": "new" if entry[0] == "edit" else "old",
                },
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _make_tool_handler(tmp_path, tool_context: ToolContext):
    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            relative = str(call.arguments["path"])
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{call.arguments['new_text']}\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": relative, "path": str(target)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    return _tool


def _block_config(tmp_path, *, mode: str = "test_paths", **overrides: Any) -> AgentConfig:
    return AgentConfig(
        max_iterations=10,
        flush_enabled=False,
        # "log" keeps the pre-existing failed-tool/empty-diff warn_model
        # recoveries out of the way so only the hygiene block injects here.
        progress_watchdog_mode="log",
        tool_failure_loop_block_threshold=0,
        patch_hygiene_block_mode=mode,
        **overrides,
    )


def _block_warnings(events: list[Any]) -> list[WarningEvent]:
    return [
        event
        for event in events
        if isinstance(event, WarningEvent)
        and event.code == "patch_hygiene_block_recovery"
    ]


def _challenge_events(path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    logged = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        event for event in logged if event.get("name") == "patch_hygiene_block.challenge"
    ]


@pytest.mark.asyncio
async def test_block_off_by_default_test_diff_is_accepted(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
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
    assert config.patch_hygiene_block_mode == "off"
    agent = Agent(
        provider=provider,
        config=config,
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _block_warnings(events) == []
    assert "patch_hygiene_block_detections" not in agent.config.metadata
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 2"


@pytest.mark.asyncio
async def test_block_challenges_test_diff_then_accepts_reverted_final(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("edit", "src.py"),
            ("final",),
            ("restore", "tests/test_a.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path, runtime_events_path=str(runtime_events_path)),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # Call 3's final was challenged; call 4 reverts; call 5's final passes.
    assert len(provider.calls) == 5
    warnings = _block_warnings(events)
    assert len(warnings) == 1
    challenge = provider.calls[3][-1]
    assert challenge.role == "user"
    assert challenge.content.startswith("[Patch hygiene check]")
    assert "tests/test_a.py" in challenge.content
    assert "src.py," not in challenge.content
    assert agent.config.metadata["patch_hygiene_block_detections"] == 1
    assert agent.config.metadata["patch_hygiene_block_recoveries"] == 1
    recorded = _challenge_events(runtime_events_path)
    assert len(recorded) == 1
    assert recorded[0]["feature"] == "patch_hygiene_block"
    assert recorded[0]["reason"] == "test_paths_in_final_diff"
    assert recorded[0]["injected_to_model"] is True
    assert recorded[0]["details"]["offending_paths"] == ["tests/test_a.py"]
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 5"
    # The source fix survives; the test edit is reverted.
    assert (tmp_path / "src.py").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "tests" / "test_a.py").read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_block_same_offending_paths_never_refire(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("final",),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # One challenge on call 2's final; call 3's final repeats the same
    # offending set and is accepted (detection counted, no injection).
    assert len(provider.calls) == 3
    assert len(_block_warnings(events)) == 1
    assert agent.config.metadata["patch_hygiene_block_detections"] == 2
    assert agent.config.metadata["patch_hygiene_block_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


@pytest.mark.asyncio
async def test_block_challenge_cap_limits_distinct_offense_sets(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("final",),
            ("edit", "tests/test_b.py"),
            ("final",),
            ("edit", "tests/test_c.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # Calls 2 and 4 finalize with distinct offending sets -> 2 challenges
    # (the cap); call 6's final with a third distinct set is accepted.
    assert len(_block_warnings(events)) == 2
    assert agent.config.metadata["patch_hygiene_block_recoveries"] == 2
    assert agent.config.metadata["patch_hygiene_block_detections"] == 3
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events, "turn must still finalize after the cap"


@pytest.mark.asyncio
async def test_block_headroom_suppression_accepts_final(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(
            tmp_path,
            max_turn_llm_calls=2,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    # A challenge would spend the last LLM call: detection recorded, no
    # injection, turn ends with the model's answer instead of a budget error.
    assert len(provider.calls) == 2
    assert _block_warnings(events) == []
    assert agent.config.metadata["patch_hygiene_block_detections"] == 1
    assert "patch_hygiene_block_recoveries" not in agent.config.metadata
    recorded = _challenge_events(runtime_events_path)
    assert len(recorded) == 1
    assert recorded[0]["injected_to_model"] is False
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 2"


@pytest.mark.asyncio
async def test_block_source_only_diff_is_never_challenged(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _block_warnings(events) == []
    assert "patch_hygiene_block_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_block_untracked_new_test_file_is_challenged(tmp_path) -> None:
    # The adapter collects the final patch via `git add -A`, so a newly
    # created (untracked) test file flows into the graded patch and must be
    # challenged like a modification.
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("edit", "tests/test_new_case.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    warnings = _block_warnings(events)
    assert len(warnings) == 1
    challenge = provider.calls[3][-1]
    assert "tests/test_new_case.py" in challenge.content


def test_porcelain_status_test_paths_classification() -> None:
    status = (
        " M src/core.py\n"
        " M tests/test_a.py\n"
        "?? tests/test_new.py\n"
        "?? docs/notes.md\n"
        # Renames count both sides; the source side is the test path here.
        "R  tests/test_b.py -> aside/kept_b.py\n"
        "?? repro.py\n"
    )
    assert Agent._porcelain_status_test_paths(status) == [
        "tests/test_a.py",
        "tests/test_new.py",
        "tests/test_b.py",
    ]
    assert Agent._porcelain_status_test_paths(None) == []
    assert Agent._porcelain_status_test_paths("") == []


def test_patch_hygiene_block_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", raising=False)
    assert _patch_hygiene_block_from_env() == "off"
    assert _patch_hygiene_block_from_env("test_paths") == "test_paths"
    monkeypatch.setenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", "test_paths")
    assert _patch_hygiene_block_from_env() == "test_paths"
    monkeypatch.setenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", "OFF")
    assert _patch_hygiene_block_from_env("test_paths") == "off"
    monkeypatch.setenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", "")
    assert _patch_hygiene_block_from_env("test_paths") == "test_paths"
    monkeypatch.setenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", "bogus")
    with pytest.raises(ValueError):
        _patch_hygiene_block_from_env()


# ---------------------------------------------------------------------------
# protected_paths mode: offending set comes from the deployment's
# workspace write-deny globs, not any built-in path taxonomy
# ---------------------------------------------------------------------------


def test_patch_hygiene_block_env_parsing_protected_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PATCH_HYGIENE_BLOCK", "protected_paths")
    assert _patch_hygiene_block_from_env() == "protected_paths"
    assert _patch_hygiene_block_from_env("test_paths") == "protected_paths"


@pytest.mark.asyncio
async def test_protected_paths_challenges_deny_glob_diff_then_accepts_revert(
    tmp_path,
) -> None:
    _init_git_workspace(tmp_path)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("edit", "src.py"),
            ("final",),
            ("restore", "tests/test_a.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(
        workspace_dir=str(tmp_path),
        workspace_write_deny_globs=["tests/**"],
    )
    agent = Agent(
        provider=provider,
        config=_block_config(
            tmp_path,
            mode="protected_paths",
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 5
    warnings = _block_warnings(events)
    assert len(warnings) == 1
    assert "write-policy-protected" in warnings[0].message
    challenge = provider.calls[3][-1]
    assert challenge.role == "user"
    assert challenge.content.startswith("[Patch hygiene check]")
    assert "write policy protects" in challenge.content
    assert "tests/test_a.py" in challenge.content
    assert "src.py," not in challenge.content
    recorded = _challenge_events(runtime_events_path)
    assert len(recorded) == 1
    assert recorded[0]["reason"] == "protected_paths_in_final_diff"
    assert recorded[0]["details"]["offending_paths"] == ["tests/test_a.py"]
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 5"
    assert (tmp_path / "src.py").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "tests" / "test_a.py").read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_protected_paths_quiet_without_deny_globs(tmp_path) -> None:
    # No deny globs configured -> the mode has nothing to protect and the
    # same test-file diff sails through: policy lives in deployment config.
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "tests/test_a.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path, mode="protected_paths"),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _block_warnings(events) == []
    assert "patch_hygiene_block_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_protected_paths_ignores_unprotected_diff(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(
        workspace_dir=str(tmp_path),
        workspace_write_deny_globs=["tests/**"],
    )
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path, mode="protected_paths"),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    assert len(provider.calls) == 2
    assert _block_warnings(events) == []
    assert "patch_hygiene_block_detections" not in agent.config.metadata


@pytest.mark.asyncio
async def test_protected_paths_untracked_new_file_is_challenged(tmp_path) -> None:
    _init_git_workspace(tmp_path)
    provider = _ScriptedProvider(
        [
            ("edit", "src.py"),
            ("edit", "tests/test_new_case.py"),
            ("final",),
        ]
    )
    tool_context = ToolContext(
        workspace_dir=str(tmp_path),
        workspace_write_deny_globs=["tests/**"],
    )
    agent = Agent(
        provider=provider,
        config=_block_config(tmp_path, mode="protected_paths"),
        tool_handler=_make_tool_handler(tmp_path, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the bug")]

    warnings = _block_warnings(events)
    assert len(warnings) == 1
    challenge = provider.calls[3][-1]
    assert "tests/test_new_case.py" in challenge.content


def test_porcelain_status_protected_paths_classification(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    status = (
        " M src/core.py\n"
        " M tests/test_a.py\n"
        "?? tests/test_new.py\n"
        "?? docs/notes.md\n"
        "R  tests/test_b.py -> aside/kept_b.py\n"
        "?? repro-check.py\n"
    )
    ctx = ToolContext(
        workspace_dir=str(tmp_path),
        workspace_write_deny_globs=["tests/**", "repro-*"],
    )
    agent = Agent(
        provider=_ScriptedProvider([]),
        config=_block_config(tmp_path, mode="protected_paths"),
        tool_handler=_make_tool_handler(tmp_path, ctx),
        tool_context=ctx,
    )
    assert agent._porcelain_status_protected_paths(status) == [
        "tests/test_a.py",
        "tests/test_new.py",
        "tests/test_b.py",
        "repro-check.py",
    ]
    assert agent._porcelain_status_protected_paths(None) == []
    assert agent._porcelain_status_protected_paths("") == []
