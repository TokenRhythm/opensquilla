"""Opt-in endgame levers: cap extension, act-now directives, sticky thinking-off.

Covers OPENSQUILLA_MAX_ITERATIONS_DEADLINE_EXTEND_SECONDS,
OPENSQUILLA_REASONING_ONLY_ACT_NOW,
OPENSQUILLA_ENDGAME_FIX_DIRECTIVE_MARGIN_SECONDS, and
OPENSQUILLA_DEADLINE_WRAPUP_STICKY_THINKING_OFF (all off by default).
Motivation: runs that hit the iteration cap with wall clock to spare finalize
early for no reason; reasoning-only responses retried verbatim usually repeat;
a deadline crossed with only diagnostic instrumentation in the workspace needs
an explicit commit-to-a-fix push; and a wrap-up preempt that re-enables thinking
next iteration can spend the whole remaining margin on another reasoning stream.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.engine.agent import (
    _ENDGAME_FIX_DIRECTIVE_PREFIX,
    _REASONING_ONLY_ACT_NOW_DIRECTIVE,
)
from opensquilla.git_runtime import GitRunState
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
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
            # stream, so deadline tests can cross their margins mid-flight.
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


def _reasoning_only_done() -> list[Any]:
    return [
        ProviderDone(
            stop_reason="stop",
            input_tokens=10,
            output_tokens=5,
            reasoning_tokens=5,
            reasoning_content="internal reasoning",
        )
    ]


def _tool_call(tool_use_id: str, tool_name: str, arguments: dict[str, Any]) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=tool_name),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            arguments=arguments,
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _echo_tool_call(tool_use_id: str) -> list[Any]:
    return _tool_call(tool_use_id, "echo", {"value": "hi"})


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
            ),
        ],
        tool_handler=tool_handler,
        tool_context=tool_context,
    )


def _user_texts(messages: list[Message]) -> list[str]:
    return [
        message.content
        for message in messages
        if message.role == "user" and isinstance(message.content, str)
    ]


def _runtime_events(events_path: Path, feature: str) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [event for event in events if event.get("feature") == feature]


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


def _workspace_ctx(repo: Path) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        session_key="agent:main:test",
        workspace_dir=str(repo),
    )


# ---------------------------------------------------------------------------
# Iteration-cap deadline extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_cap_defers_while_deadline_headroom_remains(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=3600.0,
            max_iterations=1,
            max_iterations_deadline_extend_seconds=60,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert len(provider.calls) == 2
    # The second iteration ran as a normal one: no finalization directive.
    assert not [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit" in text
    ]
    recorded = _runtime_events(events_path, "max_iterations_deadline_extension")
    assert [event["name"] for event in recorded] == [
        "max_iterations_deadline_extension.active"
    ]
    assert recorded[0]["action"] == "defer_finalization"
    assert recorded[0]["max_iterations"] == 1


@pytest.mark.asyncio
async def test_max_iterations_cap_applies_inside_extension_margin(
    tmp_path: Path,
) -> None:
    # remaining wall clock (30s) is already below the extension margin (60s):
    # the cap applies exactly as it would without the lever.
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            max_iterations=1,
            max_iterations_deadline_extend_seconds=60,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit" in text
    ]
    assert _runtime_events(events_path, "max_iterations_deadline_extension") == []


@pytest.mark.asyncio
async def test_max_iterations_extension_logged_once_across_iterations(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider(
        [_echo_tool_call("use-1"), _echo_tool_call("use-2"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=3600.0,
            max_iterations=1,
            max_iterations_deadline_extend_seconds=60,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    recorded = _runtime_events(events_path, "max_iterations_deadline_extension")
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_max_iterations_cap_default_finalizes_with_headroom() -> None:
    # Documents the gap the lever closes: without it the cap finalizes even
    # with an hour of wall clock remaining.
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=3600.0,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit" in text
    ]


# ---------------------------------------------------------------------------
# Reasoning-only act-now directive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_only_act_now_injects_directive_on_retry(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_reasoning_only_done(), _final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            reasoning_only_act_now=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert len(provider.calls) == 2
    assert _REASONING_ONLY_ACT_NOW_DIRECTIVE not in _user_texts(
        provider.calls[0]["messages"]
    )
    retry_texts = _user_texts(provider.calls[1]["messages"])
    assert retry_texts[-1] == _REASONING_ONLY_ACT_NOW_DIRECTIVE
    recorded = _runtime_events(events_path, "reasoning_only_act_now")
    assert [event["name"] for event in recorded] == ["reasoning_only_act_now.injected"]
    assert recorded[0]["action"] == "retry_with_act_now_directive"
    assert recorded[0]["reasoning_chars"] == len("internal reasoning")


@pytest.mark.asyncio
async def test_reasoning_only_act_now_default_off_keeps_bare_retry() -> None:
    provider = _SequenceProvider([_reasoning_only_done(), _final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert _REASONING_ONLY_ACT_NOW_DIRECTIVE not in _user_texts(
        provider.calls[1]["messages"]
    )


@pytest.mark.asyncio
async def test_reasoning_only_act_now_grants_directive_its_own_retry(
    tmp_path: Path,
) -> None:
    # The lever budgets a second reasoning-only retry so the directive gets
    # one delivery attempt of its own; the spliced message rides both retries
    # but is injected (and logged) only once.
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider(
        [_reasoning_only_done(), _reasoning_only_done(), _final_text()]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            reasoning_only_act_now=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert len(provider.calls) == 3
    for call in provider.calls[1:]:
        texts = _user_texts(call["messages"])
        assert texts.count(_REASONING_ONLY_ACT_NOW_DIRECTIVE) == 1
    recorded = _runtime_events(events_path, "reasoning_only_act_now")
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_reasoning_only_act_now_directive_not_carried_into_next_iteration() -> None:
    # The directive answers one reasoning-only failure; it is spliced into the
    # retry request only and must vanish from the next iteration's history.
    provider = _SequenceProvider(
        [_reasoning_only_done(), _echo_tool_call("use-1"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            reasoning_only_act_now=True,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    assert _REASONING_ONLY_ACT_NOW_DIRECTIVE in _user_texts(
        provider.calls[1]["messages"]
    )
    assert _REASONING_ONLY_ACT_NOW_DIRECTIVE not in _user_texts(
        provider.calls[2]["messages"]
    )


# ---------------------------------------------------------------------------
# Sticky wrap-up thinking-off
# ---------------------------------------------------------------------------


def _preempted_reasoning_stream() -> list[Any]:
    # timeout 8 / wrap-up margin 6: the preempt threshold sits 2s in; the
    # first reasoning delta arrives before it, the second lands past it.
    return [
        ProviderReasoning(text="deep thought"),
        2.5,
        ProviderReasoning(text="more thought"),
        ProviderText(text="never reached in the preempted attempt"),
        ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_sticky_thinking_off_covers_later_calls(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider(
        [_preempted_reasoning_stream(), _echo_tool_call("use-1"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            deadline_wrapup_sticky_thinking_off=True,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is False
    # Sticky: the next iteration stays thinking-disabled instead of burning
    # the remaining margin on another reasoning stream.
    assert provider.calls[2]["config"].thinking is False
    recorded = _runtime_events(events_path, "deadline_wrapup")
    sticky = [
        event
        for event in recorded
        if event["name"] == "deadline_wrapup.sticky_thinking_off"
    ]
    assert len(sticky) == 1
    assert sticky[0]["action"] == "disable_thinking_until_deadline"
    assert sticky[0]["reason"] == "reasoning_stream_preempt"


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_default_reenables_thinking(
    tmp_path: Path,
) -> None:
    # Documents the gap the sticky lever closes: the one-shot preempt covers
    # only the retry, and the next iteration thinks again.
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider(
        [_preempted_reasoning_stream(), _echo_tool_call("use-1"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    assert provider.calls[1]["config"].thinking is False
    assert provider.calls[2]["config"].thinking is True
    recorded = _runtime_events(events_path, "deadline_wrapup")
    assert not [
        event
        for event in recorded
        if event["name"] == "deadline_wrapup.sticky_thinking_off"
    ]


# ---------------------------------------------------------------------------
# Endgame fix directive
# ---------------------------------------------------------------------------


def _fix_directive_texts(messages: list[Message]) -> list[str]:
    return [
        text
        for text in _user_texts(messages)
        if text.startswith(_ENDGAME_FIX_DIRECTIVE_PREFIX)
    ]


@pytest.mark.asyncio
async def test_endgame_fix_directive_fires_when_no_source_fix_exists(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    # margin > timeout: the margin is already crossed at the first
    # post-tool-results check; no workspace diff means no fix yet.
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not _fix_directive_texts(provider.calls[0]["messages"])
    assert len(_fix_directive_texts(provider.calls[1]["messages"])) == 1
    recorded = _runtime_events(events_path, "endgame_fix_directive")
    assert [event["name"] for event in recorded] == ["endgame_fix_directive.injected"]
    assert recorded[0]["action"] == "append_fix_directive"
    assert recorded[0]["reason"] == "deadline_margin_no_fix"


@pytest.mark.asyncio
async def test_endgame_fix_directive_fires_once(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [_echo_tool_call("use-1"), _echo_tool_call("use-2"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    # The directive appended after iteration 1 persists in history but is
    # never appended again.
    assert len(_fix_directive_texts(provider.calls[2]["messages"])) == 1


@pytest.mark.asyncio
async def test_endgame_fix_directive_suppressed_by_substantive_diff(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
        tool_context=_workspace_ctx(repo),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not _fix_directive_texts(provider.calls[1]["messages"])
    assert _runtime_events(events_path, "endgame_fix_directive") == []


@pytest.mark.asyncio
async def test_endgame_fix_directive_skips_when_git_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _target = _init_repo(tmp_path)
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(
        "opensquilla.engine.agent.run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
        ),
    )
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
        tool_context=_workspace_ctx(repo),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert not _fix_directive_texts(provider.calls[1]["messages"])
    assert _runtime_events(events_path, "endgame_fix_directive") == []


@pytest.mark.asyncio
async def test_endgame_fix_directive_fires_on_instrumentation_only_diff(
    tmp_path: Path,
) -> None:
    # Added debug prints are investigation, not a fix: the directive fires.
    repo, target = _init_repo(tmp_path)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_context=_workspace_ctx(repo),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(_fix_directive_texts(provider.calls[1]["messages"])) == 1


@pytest.mark.asyncio
async def test_endgame_fix_directive_waits_for_margin(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    # Large timeout, small margin: the crossing stays far in the future.
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=3600.0,
            endgame_fix_directive_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            runtime_events_path=str(events_path),
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not _fix_directive_texts(provider.calls[1]["messages"])
    assert _runtime_events(events_path, "endgame_fix_directive") == []


# ---------------------------------------------------------------------------
# Env plumbing and defaults
# ---------------------------------------------------------------------------


def test_env_plumbing_for_endgame_package_levers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _bool_from_env,
        _nonnegative_int_from_env,
    )

    int_envs = [
        "OPENSQUILLA_MAX_ITERATIONS_DEADLINE_EXTEND_SECONDS",
        "OPENSQUILLA_ENDGAME_FIX_DIRECTIVE_MARGIN_SECONDS",
    ]
    bool_envs = [
        "OPENSQUILLA_FINAL_DIFF_SALVAGE_VETO",
        "OPENSQUILLA_ENDGAME_GIT_FREEZE_INSTRUMENTATION_EXEMPT",
        "OPENSQUILLA_DEADLINE_WRAPUP_STICKY_THINKING_OFF",
        "OPENSQUILLA_REASONING_ONLY_ACT_NOW",
    ]
    for name in [*int_envs, *bool_envs]:
        monkeypatch.delenv(name, raising=False)
    for name in int_envs:
        assert _nonnegative_int_from_env(name, 0) == 0
        monkeypatch.setenv(name, "120")
        assert _nonnegative_int_from_env(name, 0) == 120
    for name in bool_envs:
        assert _bool_from_env(name, False) is False
        monkeypatch.setenv(name, "1")
        assert _bool_from_env(name, False) is True


def test_agent_config_defaults_keep_endgame_package_off() -> None:
    config = AgentConfig()

    assert config.max_iterations_deadline_extend_seconds == 0
    assert config.final_diff_salvage_veto is False
    assert config.endgame_git_freeze_instrumentation_exempt is False
    assert config.deadline_wrapup_sticky_thinking_off is False
    assert config.endgame_fix_directive_margin_seconds == 0
    assert config.reasoning_only_act_now is False
