"""Opt-in levers: final-diff salvage and endgame git freeze.

Covers OPENSQUILLA_FINAL_DIFF_SALVAGE and
OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS (both off by default).
Motivation: a run that reverts or loses its own source edits shortly before
the deadline ends with an empty collected patch even though a working diff
existed earlier — salvage re-applies the newest captured per-path diff
candidate when the turn finishes with prior source writes but an empty
worktree, and the freeze arms a ToolContext flag near the deadline so shell
tools block workspace-reverting git commands outright.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
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
from opensquilla.tools.types import CallerKind, ToolContext


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
            # stream, so total-deadline tests can cross the turn timeout.
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


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "agent@test.invalid")
    _run_git(repo, "config", "user.name", "agent")
    target = repo / "pkg.py"
    target.write_text("value = 1\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")
    return repo, target


def _candidate(
    repo: Path,
    target: Path,
    new_text: str,
    *,
    candidate_id: str,
    revert: bool = True,
) -> dict[str, Any]:
    """Capture a real cumulative diff candidate, then (optionally) lose it."""

    target.write_text(new_text, encoding="utf-8")
    patch = _run_git(repo, "diff", "--", target.name)
    if revert:
        _run_git(repo, "checkout", "--", target.name)
    return {
        "candidate_id": candidate_id,
        "paths": [target.name],
        "patch": patch,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "workspace_epoch": 1,
        "receipt_id": None,
        "tool_name": "edit_file",
        "lost": True,
        "lost_reason": "source_diff_revert_observed",
        "lost_command": f"git checkout -- {target.name}",
        "restored": False,
        "created_at": "2026-07-09T00:00:00+00:00",
    }


def _ctx(repo: Path, candidates: list[dict[str, Any]] | None = None) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        session_key="agent:main:test",
        workspace_dir=str(repo),
        source_diff_candidates=list(candidates or []),
    )


@pytest.mark.asyncio
async def test_final_diff_salvage_reapplies_lost_candidate_at_finalize(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    events_path = tmp_path / "events.jsonl"
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True
    assert _run_git(repo, "diff", "--name-only").split() == ["pkg.py"]
    recorded = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if '"final_diff_salvage' in line
    ]
    applied = [event for event in recorded if event["name"] == "final_diff_salvage.applied"]
    assert len(applied) == 1
    assert applied[0]["candidate_id"] == "srcdiff-1"
    assert applied[0]["trigger"] == "finalize"


@pytest.mark.asyncio
async def test_final_diff_salvage_default_off_leaves_worktree_alone(
    tmp_path: Path,
) -> None:
    # Documents the default gap the lever closes: the lost candidate stays
    # lost and the turn ends with an empty diff.
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert candidate["restored"] is False
    assert _run_git(repo, "diff", "--name-only").strip() == ""


@pytest.mark.asyncio
async def test_final_diff_salvage_skips_when_workspace_diff_exists(
    tmp_path: Path,
) -> None:
    # A live diff means collection will not be empty; salvage must never
    # stack a stale candidate on top of newer in-worktree work.
    repo, target = _init_repo(tmp_path)
    stale = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    target.write_text("value = 9\n", encoding="utf-8")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [stale]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 9\n"
    assert stale["restored"] is False


@pytest.mark.asyncio
async def test_final_diff_salvage_never_resurrects_reverted_path_next_to_kept_work(
    tmp_path: Path,
) -> None:
    # Abandon-approach-A, fix-file-B: the agent captures a candidate on
    # pkg.py, deliberately reverts it, then lands its real fix in a second
    # tracked file. The final diff is healthy and non-empty, so salvage must
    # not append the abandoned pkg.py edits to the scored patch.
    repo, target = _init_repo(tmp_path)
    other = repo / "other.py"
    other.write_text("keep = 1\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "add other")
    abandoned = _candidate(repo, target, "value = 999\n", candidate_id="srcdiff-1")
    other.write_text("keep = 2\n", encoding="utf-8")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [abandoned]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert abandoned["restored"] is False
    assert _run_git(repo, "diff", "--name-only").split() == ["other.py"]


@pytest.mark.asyncio
async def test_final_diff_salvage_applies_newest_candidate_per_path(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    older = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    newer = _candidate(repo, target, "value = 3\n", candidate_id="srcdiff-2")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [older, newer]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 3\n"
    assert newer["restored"] is True
    assert older["restored"] is False


@pytest.mark.asyncio
async def test_final_diff_salvage_falls_back_to_older_candidate_on_check_failure(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    older = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    broken = _candidate(repo, target, "value = 3\n", candidate_id="srcdiff-2")
    broken["patch"] = "this is not a valid unified diff\n"
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [older, broken]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert older["restored"] is True
    assert broken["restored"] is False


@pytest.mark.asyncio
async def test_final_diff_salvage_applies_on_terminal_timeout(tmp_path: Path) -> None:
    # The salvage site sits after the run loop, so a turn ending in the
    # total-deadline TimeoutError still recovers the candidate before the
    # runner collects the (otherwise empty) patch.
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    provider = _SequenceProvider([[0.3, *_echo_tool_call("use-1")], _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            final_diff_salvage=True,
            timeout=0.1,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(
        event.kind == "error" and event.code == "agent_runtime_timeout"
        for event in events
    )
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True


@pytest.mark.asyncio
async def test_final_diff_salvage_ignores_unrelated_untracked_files(
    tmp_path: Path,
) -> None:
    # The gate is per path: an untracked scratch file elsewhere in the
    # worktree does not mean the reverted candidate path was collected.
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    scratch = repo / "notes.txt"
    scratch.write_text("scratch\n", encoding="utf-8")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True
    assert scratch.read_text(encoding="utf-8") == "scratch\n"


@pytest.mark.asyncio
async def test_final_diff_salvage_recovers_again_after_later_turn_revert(
    tmp_path: Path,
) -> None:
    # The restored marker only reflects the pass that set it; if the path's
    # diff disappears again in a later turn, the candidate must stay eligible.
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True),
        tool_context=_ctx(repo, [candidate]),
    )

    first = [event async for event in agent.run_turn("fix the bug")]
    assert any(event.kind == "done" for event in first)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True

    _run_git(repo, "checkout", "--", target.name)
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    second = [event async for event in agent.run_turn("try again")]

    assert any(event.kind == "done" for event in second)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True


@pytest.mark.asyncio
async def test_final_diff_salvage_falls_back_when_apply_fails_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `git apply --check` passing does not guarantee the real apply succeeds
    # (e.g. the worktree changes between the two calls); a failed apply must
    # fall through to the older candidate, not mark the path handled.
    repo, target = _init_repo(tmp_path)
    older = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    newer = _candidate(repo, target, "value = 3\n", candidate_id="srcdiff-2")
    events_path = tmp_path / "events.jsonl"
    real_apply = Agent._apply_final_diff_salvage_patch

    def flaky_apply(self, workspace, patch, *, check_only):
        if patch == newer["patch"] and not check_only:
            return False
        return real_apply(self, workspace, patch, check_only=check_only)

    monkeypatch.setattr(Agent, "_apply_final_diff_salvage_patch", flaky_apply)
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [older, newer]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert newer["restored"] is False
    assert older["restored"] is True
    recorded = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if '"final_diff_salvage' in line
    ]
    names = [event["name"] for event in recorded]
    assert "final_diff_salvage.apply_failed" in names
    assert "final_diff_salvage.applied" in names


@pytest.mark.asyncio
async def test_final_diff_salvage_stops_when_time_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(Agent, "_FINAL_DIFF_SALVAGE_TIME_BUDGET_SECONDS", 0.0)
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert candidate["restored"] is False
    recorded = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if '"final_diff_salvage' in line
    ]
    assert [event["name"] for event in recorded] == [
        "final_diff_salvage.time_budget_exhausted"
    ]


def _unlost(candidate: dict[str, Any]) -> dict[str, Any]:
    """Model a candidate whose path diff vanished without an observed revert."""

    candidate["lost"] = False
    candidate["lost_reason"] = None
    candidate["lost_command"] = None
    return candidate


def _salvage_events(events_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if '"final_diff_salvage' in line
    ]


@pytest.mark.asyncio
async def test_final_diff_salvage_veto_skips_lost_candidate(tmp_path: Path) -> None:
    # The agent explicitly reverted this candidate; with the veto lever on,
    # salvage must not resurrect edits the agent chose to abandon.
    repo, target = _init_repo(tmp_path)
    candidate = _candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1")
    events_path = tmp_path / "events.jsonl"
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            final_diff_salvage_veto=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert candidate["restored"] is False
    assert [event["action"] for event in _salvage_events(events_path)] == ["vetoed_lost"]


@pytest.mark.asyncio
async def test_final_diff_salvage_veto_skips_instrumentation_only_candidate(
    tmp_path: Path,
) -> None:
    # An instrumentation-only candidate is leftover diagnostic output, not a
    # fix; salvaging it would pollute the collected patch with debug prints.
    repo, target = _init_repo(tmp_path)
    candidate = _unlost(
        _candidate(
            repo,
            target,
            'value = 1\nprint("debug")\n',
            candidate_id="srcdiff-1",
        )
    )
    events_path = tmp_path / "events.jsonl"
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            final_diff_salvage_veto=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert candidate["restored"] is False
    assert [event["action"] for event in _salvage_events(events_path)] == [
        "vetoed_instrumentation"
    ]


@pytest.mark.asyncio
async def test_final_diff_salvage_veto_falls_back_to_older_clean_candidate(
    tmp_path: Path,
) -> None:
    # Vetoed candidates must not mark their path handled: an older candidate
    # that was never reverted may still carry the real fix.
    repo, target = _init_repo(tmp_path)
    older = _unlost(_candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1"))
    newer = _candidate(repo, target, "value = 3\n", candidate_id="srcdiff-2")
    events_path = tmp_path / "events.jsonl"
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(
            final_diff_salvage=True,
            final_diff_salvage_veto=True,
            runtime_events_path=str(events_path),
        ),
        tool_context=_ctx(repo, [older, newer]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert newer["restored"] is False
    assert older["restored"] is True
    assert [event["action"] for event in _salvage_events(events_path)] == [
        "vetoed_lost",
        "applied",
    ]


@pytest.mark.asyncio
async def test_final_diff_salvage_veto_still_applies_substantive_clean_candidate(
    tmp_path: Path,
) -> None:
    # The veto narrows salvage; it must not disable it. A substantive
    # candidate that was never explicitly reverted still applies.
    repo, target = _init_repo(tmp_path)
    candidate = _unlost(_candidate(repo, target, "value = 2\n", candidate_id="srcdiff-1"))
    agent = Agent(
        provider=_SequenceProvider([_final_text()]),
        config=AgentConfig(final_diff_salvage=True, final_diff_salvage_veto=True),
        tool_context=_ctx(repo, [candidate]),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert candidate["restored"] is True


@pytest.mark.asyncio
async def test_endgame_git_freeze_arms_instrumentation_exempt_flag() -> None:
    # The exempt flag rides the freeze reset: with both levers on, the tools
    # see the exemption as soon as the turn starts.
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    provider = _SequenceProvider([_final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_git_freeze_margin_seconds=60,
            endgame_git_freeze_instrumentation_exempt=True,
        ),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_instrumentation_exempt is True


@pytest.mark.asyncio
async def test_endgame_git_freeze_resets_stale_exempt_flag() -> None:
    # Same lifetime rule as the freeze flag itself: the context outlives the
    # turn, so a stale exemption must be cleared when the config says off.
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    ctx.endgame_git_freeze_instrumentation_exempt = True
    provider = _SequenceProvider([_final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(timeout=3600.0, endgame_git_freeze_margin_seconds=60),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_instrumentation_exempt is False


@pytest.mark.asyncio
async def test_endgame_git_freeze_arms_tool_context_flag() -> None:
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    # margin > timeout: the freeze arms at the first loop-top check.
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_git_freeze_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_active is True


@pytest.mark.asyncio
async def test_endgame_git_freeze_not_armed_when_margin_not_reached() -> None:
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    provider = _SequenceProvider([_final_text()])
    # Large timeout, small margin: the trigger stays far in the future.
    agent = _echo_agent(
        provider,
        AgentConfig(timeout=3600.0, endgame_git_freeze_margin_seconds=60),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_active is False


@pytest.mark.asyncio
async def test_endgame_git_freeze_default_off_never_touches_flag() -> None:
    # Margin unset: the engine neither arms nor resets the flag, keeping
    # unset behavior byte-identical even for a context someone pre-armed.
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    ctx.endgame_git_freeze_active = True
    provider = _SequenceProvider([_final_text()])
    agent = _echo_agent(provider, AgentConfig(timeout=30.0), tool_context=ctx)

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_active is True


@pytest.mark.asyncio
async def test_endgame_git_freeze_resets_stale_flag_at_turn_start() -> None:
    # The ToolContext outlives the turn; with the lever on, a flag armed by a
    # previous turn must not freeze a fresh turn that is far from its deadline.
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    ctx.endgame_git_freeze_active = True
    provider = _SequenceProvider([_final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(timeout=3600.0, endgame_git_freeze_margin_seconds=60),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_active is False


def test_env_plumbing_for_both_levers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _bool_from_env,
        _nonnegative_int_from_env,
    )

    monkeypatch.delenv("OPENSQUILLA_FINAL_DIFF_SALVAGE", raising=False)
    monkeypatch.delenv("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", raising=False)
    assert _bool_from_env("OPENSQUILLA_FINAL_DIFF_SALVAGE", False) is False
    assert (
        _nonnegative_int_from_env("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", 0)
        == 0
    )
    monkeypatch.setenv("OPENSQUILLA_FINAL_DIFF_SALVAGE", "1")
    monkeypatch.setenv("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", "300")
    assert _bool_from_env("OPENSQUILLA_FINAL_DIFF_SALVAGE", False) is True
    assert (
        _nonnegative_int_from_env("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", 0)
        == 300
    )


def test_agent_config_defaults_keep_both_levers_off() -> None:
    config = AgentConfig()

    assert config.final_diff_salvage is False
    assert config.endgame_git_freeze_margin_seconds == 0
