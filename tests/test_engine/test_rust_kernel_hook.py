"""Tests for the optional Rust turn kernel hook (OSPP_RUST_KERNEL).

The hook is off by default (zero behaviour change), falls back to the
Python kernel on any error, respects meta resume/replay guards, and
(only when the optional ospp_core extension is importable) delegates
no-tool turns to the Rust state machine with an equivalent event stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import (
    DoneEvent as EngineDone,
)
from opensquilla.engine.types import (
    StateChangeEvent,
)
from opensquilla.engine.types import (
    TextDeltaEvent as EngineText,
)
from opensquilla.provider import (
    ChatConfig,
    Message,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText


class _SequenceProvider:
    """Synthetic provider: no network, fixed text stream."""

    provider_name = "synthetic"

    def __init__(self, text: str = "tok0 tok1 tok2 ") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del tools, config
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text=self.text)
        yield ProviderDone(
            stop_reason="end_turn",
            input_tokens=7,
            output_tokens=3,
            model="synthetic",
        )

    async def list_models(self) -> list[Any]:
        return []


def _make_agent(provider: Any, **config_kwargs: Any) -> Agent:
    return Agent(provider=provider, config=AgentConfig(**config_kwargs))


async def _collect(agent: Agent, prompt: str) -> list[Any]:
    events: list[Any] = []
    async for ev in agent.run_turn(prompt):
        events.append(ev)
    return events


def _text_of(events: list[Any]) -> str:
    return "".join(ev.text for ev in events if isinstance(ev, EngineText))


async def test_rust_hook_off_by_default() -> None:
    """Without OSPP_RUST_KERNEL the Python kernel runs unchanged."""
    agent = _make_agent(_SequenceProvider())
    events = await _collect(agent, "hi")

    assert isinstance(events[0], StateChangeEvent)
    assert events[0].to_state == "thinking"
    assert _text_of(events) == "tok0 tok1 tok2 "
    assert isinstance(events[-1], EngineDone)
    assert events[-1].model == "synthetic"


async def test_rust_hook_fallback_without_ospp_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSPP_RUST_KERNEL=1 with no ospp_core import falls back to Python."""
    monkeypatch.setenv("OSPP_RUST_KERNEL", "1")

    _real_import = __import__
    monkeypatch.setitem(__import__("sys").modules, "ospp_core", None)

    def _no_ospp_core(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ospp_core" or name.startswith("ospp_core."):
            raise ImportError(f"no {name}")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr(
        __import__("builtins"),
        "__import__",
        _no_ospp_core,
        raising=False,
    )

    agent = _make_agent(_SequenceProvider())
    events = await _collect(agent, "hi")

    assert isinstance(events[0], StateChangeEvent)
    assert _text_of(events) == "tok0 tok1 tok2 "
    assert isinstance(events[-1], EngineDone)


async def test_rust_hook_meta_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta_replay_error keeps the turn on the Python path."""
    monkeypatch.setenv("OSPP_RUST_KERNEL", "1")
    agent = _make_agent(
        _SequenceProvider(),
        metadata={"meta_replay_error": "blocked-by-meta"},
    )
    events = await _collect(agent, "hi")

    # Python path handles meta_replay_error by emitting the terminal text.
    assert _text_of(events) == "blocked-by-meta"


pytestmark_rust = pytest.mark.rust_kernel
try:
    import ospp_core  # noqa: F401

    _HAS_OSPP_CORE = True
except ImportError:  # pragma: no cover - depends on optional extension
    _HAS_OSPP_CORE = False

_need_ospp_core = pytest.mark.skipif(
    not _HAS_OSPP_CORE, reason="ospp_core Rust extension not installed"
)


@_need_ospp_core
@pytest.mark.rust_kernel
async def test_rust_hook_active_with_ospp_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSPP_RUST_KERNEL=1 + ospp_core delegates to the Rust state machine."""
    import scripts.ospp_bridge  # noqa: F401  (ensure bridge importable)

    monkeypatch.setenv("OSPP_RUST_KERNEL", "1")
    agent = _make_agent(_SequenceProvider())
    events = await _collect(agent, "hi")

    # Rust kernel emits the streaming transition before the first delta.
    transitions = [
        ev
        for ev in events
        if isinstance(ev, StateChangeEvent)
    ]
    assert len(transitions) >= 2, transitions
    assert transitions[0].to_state == "thinking"
    assert any(ev.to_state == "streaming" for ev in transitions)
    assert _text_of(events) == "tok0 tok1 tok2 "
    assert isinstance(events[-1], EngineDone)
    assert events[-1].model == "synthetic"


