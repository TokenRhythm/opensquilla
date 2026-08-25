"""Opt-in mid-budget no-source-diff nudge lever.

Covers OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE (off by default). Motivation: a
run that spends most of its wall-clock budget investigating without ever
editing a file usually ends with no diff at all; a one-shot progress nudge
when 50% and again when 75% of the budget is spent with no workspace change
prompts the model to start implementing while there is still time. The nudge
stays quiet whenever change evidence from this agent's own run exists (write
receipts, captured diff candidates, or a live tracked diff); untracked
scratch artifacts do not count.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.git_runtime import GitRunState
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

_NUDGE_MARKER = "Progress check: about"


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            # Float entries model wall-clock time passing inside the provider
            # stream, so budget-fraction tests can cross their checkpoints.
            if isinstance(event, float):
                await asyncio.sleep(event)
                continue
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _final_text() -> list[Any]:
    return [
        ProviderText(text="ok"),
        ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1),
    ]


def _echo_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={"value": "hi"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _echo_agent(
    provider: _SequenceProvider,
    config: AgentConfig,
    tool_context: ToolContext | None = None,
) -> Agent:
    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    return Agent(
        provider=provider,
        config=config,
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
        tool_context=tool_context,
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _nudge_texts(call: dict[str, Any]) -> list[str]:
    return [
        _message_text(message)
        for message in call["messages"]
        if getattr(message, "role", "") == "user"
        and _NUDGE_MARKER in _message_text(message)
    ]


def _nudge_percent(text: str) -> int:
    match = re.search(r"about (\d+)% of the wall-clock budget", text)
    assert match is not None, text
    return int(match.group(1))


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "agent@test.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "agent"], check=True)
    (repo / "pkg.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


def _workspace_ctx(repo: Path) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        session_key="agent:main:test",
        workspace_dir=str(repo),
    )


# The budget is generous relative to the sleeps so scheduling overhead on a
# loaded runner cannot push a turn past the next checkpoint (or the hard
# deadline) and flip an assertion: every window leaves >= 1.2s of slack.
def _lever_config(**overrides: Any) -> AgentConfig:
    settings: dict[str, Any] = {
        "mid_budget_no_diff_nudge": True,
        "timeout": 6.0,
        "max_iterations": 6,
        "retry_base_backoff_ms": 0,
        "retry_max_backoff_ms": 0,
    }
    settings.update(overrides)
    return AgentConfig(**settings)


@pytest.mark.asyncio
async def test_nudge_fires_once_past_half_budget_without_diff() -> None:
    provider = _SequenceProvider([[3.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config())

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert _nudge_texts(provider.calls[0]) == []
    nudges = _nudge_texts(provider.calls[1])
    assert len(nudges) == 1
    # The message reports real elapsed budget, not the checkpoint constant.
    # Windows timers may wake just before the requested 3.3s boundary, and
    # the displayed percentage is truncated, so assert the stable contract:
    # it is beyond the 50% checkpoint but before the 75% checkpoint.
    assert 50 < _nudge_percent(nudges[0]) < 75


@pytest.mark.asyncio
async def test_nudge_default_off() -> None:
    provider = _SequenceProvider([[3.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=6.0,
            max_iterations=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert all(_nudge_texts(call) == [] for call in provider.calls)


@pytest.mark.asyncio
async def test_nudge_not_fired_before_half_budget() -> None:
    provider = _SequenceProvider([[0.9, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config())

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert all(_nudge_texts(call) == [] for call in provider.calls)


@pytest.mark.asyncio
async def test_nudge_fires_at_both_checkpoints_in_sequence() -> None:
    provider = _SequenceProvider(
        [
            [3.3, *_echo_tool_call("use-1")],
            [1.5, *_echo_tool_call("use-2")],
            _final_text(),
        ]
    )
    agent = _echo_agent(provider, _lever_config())

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    second_call = _nudge_texts(provider.calls[1])
    assert len(second_call) == 1
    assert 50 < _nudge_percent(second_call[0]) < 75
    third_call = _nudge_texts(provider.calls[2])
    assert len(third_call) == 2
    assert 50 < _nudge_percent(third_call[0]) < 75
    # The later message reports progress beyond the second checkpoint rather
    # than echoing the checkpoint constant.
    assert _nudge_percent(third_call[1]) > 75


@pytest.mark.asyncio
async def test_crossing_both_checkpoints_at_once_fires_single_nudge() -> None:
    # One long stream past 75%: both checkpoints are consumed but only one
    # nudge is emitted — never two messages in the same iteration.
    provider = _SequenceProvider([[4.8, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config())

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    nudges = _nudge_texts(provider.calls[1])
    assert len(nudges) == 1
    # The message must report progress beyond the 75% checkpoint rather than
    # echoing the checkpoint constant. Timer resolution may truncate 80% to
    # 79% on platforms that wake just before the requested sleep boundary.
    assert _nudge_percent(nudges[0]) > 75


@pytest.mark.asyncio
async def test_nudge_suppressed_by_captured_diff_candidates() -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        session_key="agent:main:test",
        source_diff_candidates=[{"candidate_id": "srcdiff-1", "paths": ["pkg.py"]}],
    )
    provider = _SequenceProvider([[3.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config(), tool_context=ctx)

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert all(_nudge_texts(call) == [] for call in provider.calls)


@pytest.mark.asyncio
async def test_nudge_suppressed_by_live_tracked_diff(tmp_path: Path) -> None:
    # Shell-made edits leave no receipts or candidates; the live tracked
    # diff must still count as change evidence.
    repo = _init_repo(tmp_path / "workspace")
    (repo / "pkg.py").write_text("value = 2\n", encoding="utf-8")
    provider = _SequenceProvider([[3.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config(), tool_context=_workspace_ctx(repo))

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert all(_nudge_texts(call) == [] for call in provider.calls)


@pytest.mark.asyncio
async def test_nudge_not_suppressed_by_untracked_scratch_files(tmp_path: Path) -> None:
    # Untracked artifacts from merely running the code (caches, coverage
    # files, scratch notes) are not source progress; the nudge still fires.
    repo = _init_repo(tmp_path / "workspace")
    (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "pkg.cpython-312.pyc").write_bytes(b"\x00")
    provider = _SequenceProvider([[3.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(provider, _lever_config(), tool_context=_workspace_ctx(repo))

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    nudges = _nudge_texts(provider.calls[1])
    assert len(nudges) == 1


def test_evidence_check_reads_own_workspace_not_contextvar(tmp_path: Path) -> None:
    # A child agent is constructed without a ToolContext of its own, and the
    # engine-loop task inherits the PARENT context via contextvar. Evidence
    # must come from the agent's own configured workspace, not the parent's
    # receipts or diff.
    parent_repo = _init_repo(tmp_path / "parent")
    (parent_repo / "pkg.py").write_text("value = 2\n", encoding="utf-8")
    child_repo = _init_repo(tmp_path / "child")
    parent_ctx = _workspace_ctx(parent_repo)
    parent_ctx.workspace_file_writes = [{"path": "pkg.py", "relative_path": "pkg.py"}]
    token = current_tool_context.set(parent_ctx)
    try:
        agent = _echo_agent(
            _SequenceProvider([_final_text()]),
            _lever_config(workspace_dir=str(child_repo)),
        )
        assert agent._workspace_has_source_change_evidence() is False
    finally:
        current_tool_context.reset(token)


def test_evidence_check_uses_config_workspace_for_contextless_agent(
    tmp_path: Path,
) -> None:
    # Child agents carry the workspace on AgentConfig; a tracked change
    # there counts even with no ToolContext attached to the agent.
    repo = _init_repo(tmp_path / "workspace")
    (repo / "pkg.py").write_text("value = 2\n", encoding="utf-8")
    agent = _echo_agent(
        _SequenceProvider([_final_text()]),
        _lever_config(workspace_dir=str(repo)),
    )

    assert agent._workspace_has_source_change_evidence() is True


def test_evidence_check_treats_git_unavailable_as_unknown_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "workspace")
    agent = _echo_agent(
        _SequenceProvider([_final_text()]),
        _lever_config(),
        tool_context=_workspace_ctx(repo),
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
        ),
    )

    assert agent._workspace_tracked_diff_paths_for_nudge() is None
    assert agent._workspace_has_source_change_evidence() is True


@pytest.mark.asyncio
async def test_nudge_noops_without_wall_clock_budget() -> None:
    # timeout=0 means no total deadline; there is no budget fraction to
    # nudge against, so the lever must stay silent instead of dividing by
    # zero or guessing.
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(provider, _lever_config(timeout=0.0))

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert all(_nudge_texts(call) == [] for call in provider.calls)


class _OneShotPendingInput:
    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)

    def drain_pending(self) -> list[str]:
        texts, self._texts = self._texts, []
        return texts


@pytest.mark.asyncio
async def test_stacked_nudge_keeps_empty_response_retry_alive() -> None:
    # Tail [tool_results, pending input, nudge]: the two messages after the
    # tool results push it out of the plain lookback window, so the turn no
    # longer counts as post-tool once the malformed-empty retry budget is
    # spent and the warn_model continue-once recovery is suppressed. The
    # nudge is runtime-injected: the turn must still count as post-tool and
    # the recovery must fire instead of ending the turn with a terminal
    # provider error. Same shape as watchdog guidance stacking.
    empty = [ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=0)]
    provider = _SequenceProvider(
        [
            [3.3, *_echo_tool_call("use-1")],
            empty,
            empty,
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        _lever_config(post_tool_empty_recovery_mode="warn_model"),
    )

    events = [
        event
        async for event in agent.run_turn(
            "fix the bug",
            pending_input_provider=_OneShotPendingInput(["also check the docs"]),
        )
    ]

    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 4
    nudges = _nudge_texts(provider.calls[1])
    assert len(nudges) == 1


def test_tail_shape_helper_skips_nudges_only() -> None:
    from opensquilla.engine.agent import (
        _MID_BUDGET_NO_DIFF_NUDGE_TEMPLATE,
        _tail_has_tool_result_ignoring_nudges,
    )
    from opensquilla.provider import ContentBlockToolResult

    tool_results = Message(
        role="user",
        content=[
            ContentBlockToolResult(tool_use_id="use-1", content="tool ok"),
        ],
    )
    nudge = Message(
        role="user",
        content=_MID_BUDGET_NO_DIFF_NUDGE_TEMPLATE.format(percent=55),
    )
    guidance = Message(role="user", content="[Progress warning] no forward progress")

    assert _tail_has_tool_result_ignoring_nudges([tool_results, guidance, nudge])
    assert _tail_has_tool_result_ignoring_nudges([tool_results, nudge, guidance])
    # Without a tool result in the tail the shape stays non-post-tool: the
    # helper only removes nudges, it never widens what counts as a tool turn.
    assert not _tail_has_tool_result_ignoring_nudges(
        [Message(role="user", content="question"), guidance, nudge]
    )
    assert not _tail_has_tool_result_ignoring_nudges([nudge])
    assert not _tail_has_tool_result_ignoring_nudges([])


def test_env_plumbing_for_mid_budget_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import _bool_from_env

    monkeypatch.delenv("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", raising=False)
    assert _bool_from_env("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", False) is False
    monkeypatch.setenv("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", "1")
    assert _bool_from_env("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", False) is True


def test_agent_config_default_keeps_lever_off() -> None:
    assert AgentConfig().mid_budget_no_diff_nudge is False
