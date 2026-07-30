from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.engine.types import DoneEvent, RouterControlReplayEvent
from opensquilla.gateway.config import (
    GatewayConfig,
    SquillaRouterConfig,
    _router_tier_profile_defaults,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.tools import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext


class _Strategy:
    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        return "c1", 0.9, "v4_phase3", {"route_class": "R1"}


class _ReplayProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.model = "base-model"

    def chat(self, messages: list[Any], tools=None, config=None) -> AsyncIterator[Any]:
        self.calls.append(self.model)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="old partial")
            yield ProviderToolStart(tool_use_id="tool-1", tool_name="router_control")
            yield ProviderToolEnd(
                tool_use_id="tool-1",
                tool_name="router_control",
                arguments={
                    "action": "set_hold",
                    "target_id": "tier:c3",
                    "evidence": "use c3",
                },
            )
            yield ProviderDone(model=self.model)
            return
        yield ProviderText(text="new final")
        yield ProviderDone(model=self.model)

    async def list_models(self) -> list[Any]:
        return []


class _SelectorClone:
    def __init__(self, provider: _ReplayProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> _ReplayProvider:
        return self.provider


class _Selector:
    def __init__(self, provider: _ReplayProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


@pytest.mark.asyncio
async def test_router_control_replay_event_replays_turn_once(monkeypatch) -> None:
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _cfg: _Strategy())
    provider = _ReplayProvider()
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router=SquillaRouterConfig(
            enabled=True,
            rollout_phase="full",
            require_router_runtime=False,
            tiers=_router_tier_profile_defaults("openrouter"),
        ),
    )
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=get_default_registry(),
        config=cfg,
    )

    events = [
        event
        async for event in runner.run(
            "Use c3 for this",
            "agent:main:router-control-replay",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    replay_events = [event for event in events if isinstance(event, RouterControlReplayEvent)]
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    text = "".join(getattr(event, "text", "") for event in events if event.kind == "text_delta")

    assert len(replay_events) == 1
    assert replay_events[0].target_tier == "c3"
    # Initial turn routes to c1 (the strategy returns c1); the router_control
    # hold replays it at c3. Models follow the default tier profile.
    assert provider.calls == ["deepseek/deepseek-v4-pro", "anthropic/claude-opus-4.8"]
    assert done_events[-1].text == "new final"
    assert text.endswith("new final")


@dataclass
class _TranscriptEntry:
    role: str
    content: str
    message_id: str
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None
    token_count: int | None = None


@dataclass
class _SessionNode:
    session_key: str
    session_id: str


class _FakeSessionManager:
    def __init__(self) -> None:
        self._nodes: dict[str, _SessionNode] = {}
        self._transcripts: dict[str, list[_TranscriptEntry]] = {}
        self._counter = 0

    async def create(self, session_key: str) -> _SessionNode:
        node = _SessionNode(session_key=session_key, session_id=f"id-{len(self._nodes) + 1}")
        self._nodes[session_key] = node
        self._transcripts.setdefault(session_key, [])
        return node

    async def append_message(
        self, session_key: str, role: str, content: str, **_kw: Any
    ) -> _TranscriptEntry:
        self._counter += 1
        entry = _TranscriptEntry(role=role, content=content, message_id=f"m{self._counter}")
        self._transcripts.setdefault(session_key, []).append(entry)
        return entry

    async def get_transcript(self, session_key: str) -> list[_TranscriptEntry]:
        return list(self._transcripts.get(session_key, []))

    async def get_session(self, session_key: str) -> _SessionNode | None:
        return self._nodes.get(session_key)

    async def get_context_states(self, session_key: str) -> list[Any]:  # noqa: ARG002
        return []

    async def get_summaries(self, session_key: str) -> list[Any]:  # noqa: ARG002
        return []


class _MessageCapturingReplayProvider(_ReplayProvider):
    def __init__(self) -> None:
        super().__init__()
        self.message_calls: list[list[Any]] = []

    def chat(self, messages: list[Any], tools=None, config=None) -> AsyncIterator[Any]:
        self.message_calls.append(list(messages))
        return super().chat(messages, tools=tools, config=config)


@pytest.mark.asyncio
async def test_replayed_turn_keeps_user_message_binding(monkeypatch) -> None:
    # Queued sends: B and C were persisted at ingress while turn A runs. The
    # replayed turn must bind to A's message id exactly like the initial turn.
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _cfg: _Strategy())
    provider = _MessageCapturingReplayProvider()
    manager = _FakeSessionManager()
    key = "agent:main:router-control-replay-queued"
    await manager.create(key)
    entry_a = await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "user", "Second question B")
    await manager.append_message(key, "user", "Third question C")
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router=SquillaRouterConfig(
            enabled=True,
            rollout_phase="full",
            require_router_runtime=False,
            tiers=_router_tier_profile_defaults("openrouter"),
        )
    )
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        tool_registry=get_default_registry(),
        config=cfg,
    )
    captured_assistant: list[tuple[str | None, str]] = []

    events = [
        event
        async for event in runner.run(
            "First question A",
            key,
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            persist_input=False,
            history_has_persisted_user=True,
            bound_user_message_id=entry_a.message_id,
            no_memory_capture=True,
            assistant_message_sink=lambda message_id, content: captured_assistant.append(
                (message_id, content)
            ),
        )
    ]

    replay_events = [event for event in events if isinstance(event, RouterControlReplayEvent)]
    assert len(replay_events) == 1
    assert len(provider.message_calls) == 2
    assert captured_assistant == [("m4", "new final")]

    for call in provider.message_calls:
        users = [m.content for m in call if m.role == "user" and isinstance(m.content, str)]
        assert sum(1 for t in users if t.startswith("First question A")) == 1
        assert not any("Second question B" in t for t in users)
        assert not any("Third question C" in t for t in users)
