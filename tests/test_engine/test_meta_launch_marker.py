"""Tests for the manual ``/meta`` launch path: ``meta_launch`` marker dispatch
and ``Agent._run_meta_launch``.

Part 2 / Task 2 of the meta-skill manual-trigger plan adds a ``meta_launch``
marker (read at the top of ``_turn_generator`` next to ``meta_resume``) that
drives a new ``Agent._run_meta_launch(name)`` method. These tests exercise
``_run_meta_launch`` directly so they need no external provider calls.

Isolation strategy (mirrors the spy patterns in
``tests/test_skills/test_meta_invoke_tool.py``):

* A stub ``SkillLoader`` exposes only ``get_by_name`` returning a hand-built
  meta-spec ``SimpleNamespace`` (kind="meta"). This avoids the SOP markdown
  compiler entirely.
* The orchestrator is isolated by monkeypatching ``agent._build_meta_orchestrator``
  to a spy that records ``triggered_by`` and returns ``(fake_orch, None, None)``
  where ``fake_orch.iter_events(match)`` is an async generator yielding a
  ``MetaResult``. ``parse_meta_plan`` is also stubbed (the spec is synthetic),
  so no real plan parsing runs.
* ``self._meta_run_writer`` is supplied through ``config.metadata['meta_run_writer']``
  with a stub whose ``peek_awaiting`` we control per-test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _NullProvider:
    provider_name = "null"

    async def chat(self, *_args: Any, **_kwargs: Any):  # pragma: no cover
        raise AssertionError("provider.chat must not be called in this test")
        yield  # make this an async generator (unreachable)

    async def list_models(self) -> list[Any]:
        return []


class _StubLoader:
    """Minimal skill loader exposing only what ``_run_meta_launch`` reads."""

    def __init__(self, spec: Any) -> None:
        self._spec = spec

    def get_by_name(self, name: str) -> Any:
        if self._spec is not None and getattr(self._spec, "name", None) == name:
            return self._spec
        return None


class _StubWriter:
    """Stub meta-run writer with a controllable ``peek_awaiting``."""

    def __init__(self, awaiting: Any = None) -> None:
        self._awaiting = awaiting
        self.peek_calls: list[dict[str, Any]] = []

    def peek_awaiting(self, *, session_id: str) -> Any:
        self.peek_calls.append({"session_id": session_id})
        return self._awaiting


def _meta_spec(
    name: str = "meta-tiny",
    *,
    disable_model_invocation: bool = False,
) -> SimpleNamespace:
    """A synthetic meta SkillSpec-like object."""
    return SimpleNamespace(
        name=name,
        kind="meta",
        disable_model_invocation=disable_model_invocation,
    )


def _build_agent(
    *,
    loader: Any,
    writer: Any,
    session_key: str = "agent:main:test-launch",
    extra_metadata: dict[str, Any] | None = None,
):
    """Construct a minimal Agent wired with a stub loader + writer."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig

    metadata: dict[str, Any] = {
        "skill_loader": loader,
        "bootstrap_workspace_dir": "/tmp/ws",
    }
    if writer is not None:
        metadata["meta_run_writer"] = writer
    if extra_metadata:
        metadata.update(extra_metadata)

    config = AgentConfig(
        model_id="stub",
        max_iterations=1,
        system_prompt="outer system prompt",
        metadata=metadata,
    )
    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=None,
        session_key=session_key,
    )
    return agent


def _install_orchestrator_spy(
    agent: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: Any,
    streamed_events: list[Any] | None = None,
) -> dict[str, Any]:
    """Monkeypatch ``_build_meta_orchestrator`` with a spy and stub
    ``parse_meta_plan`` so the synthetic spec is accepted.

    Returns a dict that records ``called`` and ``triggered_by``.
    """
    captured: dict[str, Any] = {"called": False, "triggered_by": None}

    class _FakeOrch:
        async def iter_events(self, _match: Any):
            for event in streamed_events or []:
                yield event
            yield result

    def _spy_build_meta_orchestrator(*, workspace_dir, triggered_by, skill_loader):
        captured["called"] = True
        captured["triggered_by"] = triggered_by
        captured["workspace_dir"] = workspace_dir
        captured["skill_loader"] = skill_loader
        return (_FakeOrch(), None, None)

    monkeypatch.setattr(
        agent, "_build_meta_orchestrator", _spy_build_meta_orchestrator
    )

    # The synthetic spec is not a real SkillSpec, so stub the parser to
    # return a usable MetaPlan-like object (only ``name`` is read by the
    # spy path; the real orchestrator is never built).
    import opensquilla.skills.meta.parser as parser_mod

    monkeypatch.setattr(
        parser_mod,
        "parse_meta_plan",
        lambda _spec: SimpleNamespace(name=getattr(_spec, "name", "meta-tiny")),
    )
    # The method imports parse_meta_plan from the module by name at call
    # time, so the module-level patch above is what _run_meta_launch sees.
    return captured


async def _drain(agent: Any, name: str) -> list[Any]:
    events: list[Any] = []
    async for ev in agent._run_meta_launch(name):
        events.append(ev)
    return events


def _done_event_of(events: list[Any]) -> Any:
    from opensquilla.engine.types import DoneEvent

    dones = [e for e in events if isinstance(e, DoneEvent)]
    assert dones, f"expected a terminal DoneEvent; got {events!r}"
    return dones[-1]


def _streamed_text(events: list[Any]) -> str:
    from opensquilla.engine.types import TextDeltaEvent

    return "".join(e.text for e in events if isinstance(e, TextDeltaEvent))


# ---------------------------------------------------------------------------
# 1. Launch dispatch — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_meta_launch_dispatches_and_yields_done_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered meta-skill launches: the orchestrator is built with
    triggered_by='manual_command', the terminal DoneEvent carries the
    MetaResult.final_text, and the meta_launch marker is popped."""
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec("meta-tiny")
    loader = _StubLoader(spec)
    writer = _StubWriter(awaiting=None)
    agent = _build_agent(
        loader=loader,
        writer=writer,
        extra_metadata={"meta_launch": {"name": "meta-tiny"}},
    )
    # Simulate what _turn_generator does on its first line.
    agent._current_turn_message = "do the tiny meta thing"  # type: ignore[attr-defined]

    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="LAUNCHED"),
    )

    events = await _drain(agent, "meta-tiny")

    done = _done_event_of(events)
    assert done.text == "LAUNCHED"
    assert captured["called"] is True
    assert captured["triggered_by"] == "manual_command"
    # The marker must be popped so a re-enter cannot re-run it.
    assert "meta_launch" not in (agent.config.metadata or {})
    # awaiting-guard was consulted with the agent's session key.
    assert writer.peek_calls == [{"session_id": "agent:main:test-launch"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("streamed", "terminal", "expected_live"),
    [
        ("PREFIX", "PREFIX-SUFFIX", "PREFIX-SUFFIX"),
        ("STALE", "CANONICAL", "STALE"),
    ],
)
async def test_run_meta_launch_never_reemits_a_terminal_prefix_or_conflict(
    monkeypatch: pytest.MonkeyPatch,
    streamed: str,
    terminal: str,
    expected_live: str,
) -> None:
    """Only a strict terminal suffix may be published as another delta.

    Conflicting terminal text is carried by the authoritative Done snapshot,
    allowing surfaces to replace the stale preview without first displaying an
    invalid concatenation.
    """
    from opensquilla.engine.types import TextDeltaEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec("meta-tiny")
    agent = _build_agent(loader=_StubLoader(spec), writer=_StubWriter())
    agent._current_turn_message = "run it"  # type: ignore[attr-defined]
    _install_orchestrator_spy(
        agent,
        monkeypatch,
        streamed_events=[TextDeltaEvent(text=streamed)],
        result=MetaResult(ok=True, final_text=terminal),
    )

    events = await _drain(agent, "meta-tiny")

    assert _streamed_text(events) == expected_live
    done = _done_event_of(events)
    assert done.text == terminal
    assert done.text_snapshot == terminal


@pytest.mark.asyncio
async def test_turn_generator_routes_meta_launch_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker-dispatch block in _turn_generator invokes _run_meta_launch
    with the marker's ``name`` and returns (no further turn processing)."""
    spec = _meta_spec("meta-tiny")
    loader = _StubLoader(spec)
    writer = _StubWriter(awaiting=None)
    agent = _build_agent(
        loader=loader,
        writer=writer,
        extra_metadata={"meta_launch": {"name": "meta-tiny"}},
    )

    seen: dict[str, Any] = {}

    async def _fake_run_meta_launch(name: str):
        from opensquilla.engine.types import DoneEvent

        seen["name"] = name
        yield DoneEvent(text="from-launch", input_tokens=0, output_tokens=0)

    monkeypatch.setattr(agent, "_run_meta_launch", _fake_run_meta_launch)

    from opensquilla.engine.types import DoneEvent

    events = [ev async for ev in agent.run_turn("anything")]
    assert seen.get("name") == "meta-tiny"
    assert any(
        isinstance(e, DoneEvent) and e.text == "from-launch" for e in events
    ), f"expected DoneEvent from the launch path; got {events!r}"


# ---------------------------------------------------------------------------
# 2. Disabled skill refused — orchestrator never built
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_meta_launch_refuses_disabled_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec flagged disable_model_invocation=True is refused with a
    'not available for invocation' message and the orchestrator is never
    built."""
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec("meta-hidden", disable_model_invocation=True)
    loader = _StubLoader(spec)
    writer = _StubWriter(awaiting=None)
    agent = _build_agent(
        loader=loader,
        writer=writer,
        extra_metadata={"meta_launch": {"name": "meta-hidden"}},
    )

    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="SHOULD NOT RUN"),
    )

    events = await _drain(agent, "meta-hidden")

    text = _streamed_text(events)
    assert "not available for invocation" in text, (
        f"expected refusal text; got {text!r}"
    )
    assert captured["called"] is False, (
        "orchestrator must not be built for a disabled meta-skill"
    )
    # Still finalizes with a terminal DoneEvent.
    _done_event_of(events)


# ---------------------------------------------------------------------------
# 3. Master gate — meta_skill.enabled=false refuses launch entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_meta_launch_refused_when_meta_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the master meta_skill gate is off (meta_skill_enabled=False in
    metadata), _run_meta_launch emits the 'disabled by configuration' message
    and the orchestrator is never built."""
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec("meta-tiny")
    loader = _StubLoader(spec)
    writer = _StubWriter(awaiting=None)
    agent = _build_agent(
        loader=loader,
        writer=writer,
        extra_metadata={
            "meta_launch": {"name": "meta-tiny"},
            "meta_skill_enabled": False,
        },
    )

    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="SHOULD NOT RUN"),
    )

    events = await _drain(agent, "meta-tiny")

    text = _streamed_text(events)
    assert "disabled" in text, (
        f"expected 'disabled' in refusal text; got {text!r}"
    )
    assert captured["called"] is False, (
        "orchestrator must not be built when meta_skill is disabled"
    )
    # Must still emit a terminal DoneEvent so the caller can finalize cleanly.
    _done_event_of(events)


# ---------------------------------------------------------------------------
# 4. Awaiting-guard — refuse while a prior run is waiting for input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_meta_launch_blocks_when_awaiting_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When peek_awaiting returns non-None, launch is refused with the
    'waiting for your answer' message and the orchestrator is never built."""
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec("meta-tiny")
    loader = _StubLoader(spec)
    writer = _StubWriter(awaiting=SimpleNamespace(run_id="01PENDING"))
    agent = _build_agent(
        loader=loader,
        writer=writer,
        extra_metadata={"meta_launch": {"name": "meta-tiny"}},
    )

    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="SHOULD NOT RUN"),
    )

    events = await _drain(agent, "meta-tiny")

    text = _streamed_text(events)
    assert "waiting for your answer" in text, (
        f"expected awaiting-guard text; got {text!r}"
    )
    assert captured["called"] is False, (
        "orchestrator must not be built while a prior run is awaiting input"
    )
    _done_event_of(events)
