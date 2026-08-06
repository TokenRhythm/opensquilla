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

import hashlib
import json
from pathlib import Path
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

    def __init__(self, awaiting: Any = None, record: Any = None) -> None:
        self._awaiting = awaiting
        self._record = record
        self.peek_calls: list[dict[str, Any]] = []

    def peek_awaiting(self, *, session_id: str) -> Any:
        self.peek_calls.append({"session_id": session_id})
        return self._awaiting

    def get_run(self, run_id: str) -> Any:
        if self._record is not None and self._record.run_id == run_id:
            return self._record
        return None


def _meta_spec(
    name: str = "meta-tiny",
    *,
    disable_model_invocation: bool = False,
    description: str = "Synthetic test MetaSkill.",
) -> SimpleNamespace:
    """A synthetic meta SkillSpec-like object."""
    return SimpleNamespace(
        name=name,
        kind="meta",
        description=description,
        disable_model_invocation=disable_model_invocation,
    )


def _persisted_step(
    step_id: str,
    *,
    status: str,
    output_text: Any,
    substitute_step_id: str | None = None,
    truncated_fields: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        step_id=step_id,
        status=status,
        output_text=output_text,
        substitute_step_id=substitute_step_id,
        truncated_fields=truncated_fields,
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
        async def iter_events(self, match: Any, **kwargs: Any):
            captured["match"] = match
            captured["iter_kwargs"] = kwargs
            for event in streamed_events or []:
                yield event
            yield result

    def _spy_build_meta_orchestrator(
        *,
        workspace_dir,
        triggered_by,
        skill_loader,
        parent_spec,
        plan,
    ):
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


async def _drain_replay(agent: Any, name: str, run_id: str) -> list[Any]:
    events: list[Any] = []
    async for ev in agent._run_meta_launch(
        name,
        replay_run_id=run_id,
        replay_mode="failed-step",
    ):
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
async def test_run_meta_resume_does_not_trust_parent_name_without_persisted_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume name and forged provider callable are insufficient authority."""
    from opensquilla.skills.meta.readiness import (
        META_OPENROUTER_API_KEY_ENV,
        META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY,
    )
    from opensquilla.skills.meta.types import MetaResult

    secret = "synthetic-resume-only-openrouter-key"
    loader = _StubLoader(_meta_spec("meta-short-drama"))
    agent = _build_agent(
        loader=loader,
        writer=_StubWriter(),
        extra_metadata={
            META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY: lambda _parent, _plan: {
                "nano-banana-pro": {META_OPENROUTER_API_KEY_ENV: secret},
                "seedance-2-prompt": {META_OPENROUTER_API_KEY_ENV: secret},
            },
        },
    )
    original_builder = agent._build_meta_orchestrator
    captured: dict[str, Any] = {}

    def capture_builder(**kwargs: Any):
        orch, llm_chat, tool_invoker = original_builder(**kwargs)
        captured["triggered_by"] = kwargs.get("triggered_by")
        captured["skill_runtime_env"] = orch._skill_runtime_env

        async def fake_iter_resume_events(*, payload: Any, filled_fields: Any):
            captured["payload"] = payload
            captured["filled_fields"] = filled_fields
            yield MetaResult(ok=True, final_text="resume-complete")

        orch.iter_resume_events = fake_iter_resume_events
        return orch, llm_chat, tool_invoker

    monkeypatch.setattr(agent, "_build_meta_orchestrator", capture_builder)
    claim = SimpleNamespace(run_id="run-awaiting", plan_snapshot_json="{}")
    filled_fields = {"approved": True}
    events = [
        event
        async for event in agent._run_meta_resume((claim, filled_fields))
    ]

    done = _done_event_of(events)
    assert done.text == "resume-complete"
    assert captured == {
        "triggered_by": "resume",
        "skill_runtime_env": {},
        "payload": claim,
        "filled_fields": filled_fields,
    }
    assert secret not in repr(events)
    assert secret not in repr(agent.config.metadata)


@pytest.mark.asyncio
async def test_run_meta_resume_binds_current_bundled_parent_to_persisted_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.parser import parse_meta_plan
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.readiness import (
        META_OPENROUTER_API_KEY_ENV,
        META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY,
        configured_meta_skill_runtime_env,
    )
    from opensquilla.skills.meta.types import MetaResult

    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "resume-trust-snapshot.json",
    )
    parent_spec = loader.get_by_name("meta-short-drama")
    assert parent_spec is not None
    plan = parse_meta_plan(parent_spec)
    assert plan is not None
    snapshot = json.dumps(to_jsonable(plan), sort_keys=True)
    record = SimpleNamespace(
        run_id="run-trusted-awaiting",
        session_key="agent:main:test-launch",
        meta_skill_name="meta-short-drama",
        plan_snapshot_json=snapshot,
    )
    secret = "synthetic-trusted-resume-key"
    provider_config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key=secret,
            api_key_env="",
            base_url="https://openrouter.ai/api/v1",
            proxy="",
            provider_routing={},
        ),
        llm_profiles={},
    )
    agent = _build_agent(
        loader=loader,
        writer=_StubWriter(record=record),
        extra_metadata={
            META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY: (
                lambda spec, persisted_plan: configured_meta_skill_runtime_env(
                    provider_config,
                    parent_spec=spec,
                    plan=persisted_plan,
                    session_key="agent:main:test-launch",
                    skill_resolver=loader,
                )
            ),
        },
    )
    original_builder = agent._build_meta_orchestrator
    captured: dict[str, Any] = {}

    def capture_builder(**kwargs: Any):
        orch, llm_chat, tool_invoker = original_builder(**kwargs)
        captured["skill_runtime_env"] = orch._skill_runtime_env

        async def fake_iter_resume_events(*, payload: Any, filled_fields: Any):
            yield MetaResult(ok=True, final_text="trusted-resume-complete")

        orch.iter_resume_events = fake_iter_resume_events
        return orch, llm_chat, tool_invoker

    monkeypatch.setattr(agent, "_build_meta_orchestrator", capture_builder)
    claim = SimpleNamespace(
        run_id=record.run_id,
        plan_snapshot_json=snapshot,
    )

    events = [
        event
        async for event in agent._run_meta_resume((claim, {"approved": True}))
    ]

    assert _done_event_of(events).text == "trusted-resume-complete"
    assert captured["skill_runtime_env"]["nano-banana-pro"][
        META_OPENROUTER_API_KEY_ENV
    ] == secret
    assert captured["skill_runtime_env"]["seedance-2-prompt"][
        META_OPENROUTER_API_KEY_ENV
    ] == secret
    assert secret not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_binding",
    [
        "empty_claim_run_id",
        "empty_claim_snapshot",
        "empty_record_session",
        "different_record_session",
        "different_claim_snapshot",
    ],
)
async def test_run_meta_resume_with_incomplete_durable_binding_gets_no_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_binding: str,
) -> None:
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.parser import parse_meta_plan
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.readiness import (
        META_OPENROUTER_API_KEY_ENV,
        META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY,
    )
    from opensquilla.skills.meta.types import MetaResult

    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / f"resume-{invalid_binding}-snapshot.json",
    )
    parent_spec = loader.get_by_name("meta-short-drama")
    assert parent_spec is not None
    plan = parse_meta_plan(parent_spec)
    assert plan is not None
    snapshot = json.dumps(to_jsonable(plan), sort_keys=True)

    claim_run_id = "run-awaiting"
    record_run_id = claim_run_id
    claim_snapshot = snapshot
    record_snapshot = snapshot
    record_session = "agent:main:test-launch"
    if invalid_binding == "empty_claim_run_id":
        claim_run_id = record_run_id = ""
    elif invalid_binding == "empty_claim_snapshot":
        claim_snapshot = record_snapshot = ""
    elif invalid_binding == "empty_record_session":
        record_session = ""
    elif invalid_binding == "different_record_session":
        record_session = "agent:other:session"
    elif invalid_binding == "different_claim_snapshot":
        claim_snapshot = "{}"

    record = SimpleNamespace(
        run_id=record_run_id,
        session_key=record_session,
        meta_skill_name="meta-short-drama",
        plan_snapshot_json=record_snapshot,
    )
    secret = "synthetic-must-not-be-leased"
    provider_calls: list[tuple[Any, Any]] = []

    def runtime_env_provider(spec: Any, persisted_plan: Any) -> dict[str, dict[str, str]]:
        provider_calls.append((spec, persisted_plan))
        return {
            "nano-banana-pro": {META_OPENROUTER_API_KEY_ENV: secret},
            "seedance-2-prompt": {META_OPENROUTER_API_KEY_ENV: secret},
        }

    agent = _build_agent(
        loader=loader,
        writer=_StubWriter(record=record),
        extra_metadata={
            META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY: runtime_env_provider,
        },
    )
    original_builder = agent._build_meta_orchestrator
    captured: dict[str, Any] = {}

    def capture_builder(**kwargs: Any):
        orch, llm_chat, tool_invoker = original_builder(**kwargs)
        captured["skill_runtime_env"] = orch._skill_runtime_env

        async def fake_iter_resume_events(*, payload: Any, filled_fields: Any):
            yield MetaResult(ok=True, final_text="resume-without-credential")

        orch.iter_resume_events = fake_iter_resume_events
        return orch, llm_chat, tool_invoker

    monkeypatch.setattr(agent, "_build_meta_orchestrator", capture_builder)
    claim = SimpleNamespace(
        run_id=claim_run_id,
        plan_snapshot_json=claim_snapshot,
    )

    events = [
        event
        async for event in agent._run_meta_resume((claim, {"approved": True}))
    ]

    assert _done_event_of(events).text == "resume-without-credential"
    assert provider_calls == []
    assert captured["skill_runtime_env"] == {}
    assert secret not in repr(events)


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


@pytest.mark.asyncio
async def test_turn_generator_routes_explicit_request_to_meta_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request payload is carried separately from the hidden command."""
    agent = _build_agent(
        loader=_StubLoader(_meta_spec("meta-tiny")),
        writer=_StubWriter(awaiting=None),
        extra_metadata={
            "meta_launch": {
                "name": "meta-tiny",
                "request": "Write a ten-page paper with real citations.",
            }
        },
    )
    seen: dict[str, Any] = {}

    async def _fake_run_meta_launch(name: str, *, user_request: str | None = None):
        from opensquilla.engine.types import DoneEvent

        seen.update(name=name, user_request=user_request)
        yield DoneEvent(text="from-launch", input_tokens=0, output_tokens=0)

    monkeypatch.setattr(agent, "_run_meta_launch", _fake_run_meta_launch)

    _ = [
        event
        async for event in agent.run_turn(
            "/meta meta-tiny -- Write a ten-page paper with real citations."
        )
    ]

    assert seen == {
        "name": "meta-tiny",
        "user_request": "Write a ten-page paper with real citations.",
    }


@pytest.mark.asyncio
async def test_turn_generator_routes_meta_replay_marker_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        loader=_StubLoader(_meta_spec("meta-paper-write")),
        writer=_StubWriter(),
        extra_metadata={
            "meta_replay": {
                "name": "meta-paper-write",
                "run_id": "run-source",
                "mode": "failed-step",
            },
        },
    )
    seen: dict[str, Any] = {}

    async def _fake_run_meta_launch(
        name: str,
        *,
        user_request: str | None = None,
        replay_run_id: str | None = None,
        replay_mode: str | None = None,
    ):
        from opensquilla.engine.types import DoneEvent

        seen.update(
            name=name,
            user_request=user_request,
            replay_run_id=replay_run_id,
            replay_mode=replay_mode,
        )
        yield DoneEvent(text="from-replay", input_tokens=0, output_tokens=0)

    monkeypatch.setattr(agent, "_run_meta_launch", _fake_run_meta_launch)

    events = [event async for event in agent.run_turn("/meta-replay")]

    assert seen == {
        "name": "meta-paper-write",
        "user_request": None,
        "replay_run_id": "run-source",
        "replay_mode": "failed-step",
    }
    assert _done_event_of(events).text == "from-replay"


@pytest.mark.asyncio
async def test_run_meta_launch_uses_request_in_orchestrator_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.meta.types import MetaResult

    agent = _build_agent(
        loader=_StubLoader(_meta_spec("meta-tiny")),
        writer=_StubWriter(awaiting=None),
    )
    agent._current_turn_message = "/meta meta-tiny -- hidden envelope"  # type: ignore[attr-defined]
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="LAUNCHED"),
    )

    events: list[Any] = []
    async for event in agent._run_meta_launch(
        "meta-tiny",
        user_request="Write a ten-page paper with real citations.",
    ):
        events.append(event)

    assert _done_event_of(events).text == "LAUNCHED"
    assert captured["match"].inputs["user_message"] == (
        "Write a ten-page paper with real citations."
    )


@pytest.mark.asyncio
async def test_run_meta_replay_rehydrates_retired_plan_and_preserves_successful_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="meta-paper-write",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(id="outline", skill="writer", kind="agent"),
            MetaStep(
                id="compile_pdf",
                skill="pdf-builder",
                kind="agent",
                depends_on=("outline",),
            ),
        ),
        request_template={"requires_confirmation": True},
    )
    record = SimpleNamespace(
        run_id="run-source",
        meta_skill_name="meta-paper-write",
        session_key="agent:main:test-launch",
        status="failed",
        failed_step_id="compile_pdf",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        inputs_json=json.dumps({"user_message": "Write a cited paper"}),
        steps=(
            _persisted_step(
                "outline",
                status="ok",
                output_text="artifact:/workspace/paper/outline.md",
            ),
            _persisted_step(
                "compile_pdf",
                status="failed",
                output_text=None,
            ),
        ),
    )
    agent = _build_agent(
        loader=_StubLoader(
            _meta_spec("meta-paper-write", disable_model_invocation=True)
        ),
        writer=_StubWriter(record=record),
        extra_metadata={
            "meta_replay": {
                "run_id": "run-source",
                "name": "meta-paper-write",
                "mode": "failed-step",
            },
        },
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="replayed"),
    )

    events = [
        event
        async for event in agent._run_meta_launch(
            "meta-paper-write",
            replay_run_id="run-source",
            replay_mode="failed-step",
        )
    ]

    assert _done_event_of(events).text == "replayed"
    assert captured["match"].plan.name == "meta-paper-write"
    assert captured["match"].inputs["meta_replay_source_run_id"] == "run-source"
    # Legacy source rows have no reserved artifact id. Replay deterministically
    # maps the persisted source run id instead of creating a new directory.
    assert captured["match"].inputs["meta_run_id"] == "run-source"
    assert captured["iter_kwargs"] == {
        "seed_outputs": {
            "outline": "artifact:/workspace/paper/outline.md",
        },
        "trusted_preflight_replay": True,
        "trusted_replay_meta_run_id": "run-source",
    }
    assert "meta_replay" not in (agent.config.metadata or {})


@pytest.mark.asyncio
async def test_run_meta_replay_aliases_complete_substitute_output_to_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fallback is reused under both durable DAG identities."""
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="meta-failover",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="primary",
                skill="paid-primary",
                kind="agent",
                on_failure="fallback",
            ),
            MetaStep(id="fallback", skill="local-fallback", kind="agent"),
            MetaStep(
                id="delivery",
                skill="delivery",
                kind="agent",
                depends_on=("primary",),
            ),
        ),
    )
    record = SimpleNamespace(
        run_id="run-failover",
        meta_skill_name=plan.name,
        session_key="agent:main:test-launch",
        status="failed",
        failed_step_id="delivery",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        inputs_json="{}",
        steps=(
            _persisted_step(
                "primary",
                status="substituted",
                output_text=None,
                substitute_step_id="fallback",
            ),
            _persisted_step(
                "fallback",
                status="ok",
                output_text="artifact:/workspace/fallback.mp4",
            ),
            _persisted_step("delivery", status="failed", output_text=None),
        ),
    )
    agent = _build_agent(
        loader=_StubLoader(_meta_spec(plan.name, disable_model_invocation=True)),
        writer=_StubWriter(record=record),
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="replayed"),
    )

    await _drain_replay(agent, plan.name, record.run_id)

    assert captured["iter_kwargs"] == {
        "seed_outputs": {
            "primary": "artifact:/workspace/fallback.mp4",
            "fallback": "artifact:/workspace/fallback.mp4",
        },
        "trusted_preflight_replay": True,
        "trusted_replay_meta_run_id": "run-failover",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback_case",
    [
        "missing",
        "failed",
        "truncated",
        "none-output",
        "missing-truncation-metadata",
    ],
)
async def test_run_meta_replay_does_not_seed_incomplete_failover_pair(
    monkeypatch: pytest.MonkeyPatch,
    fallback_case: str,
) -> None:
    """Missing, failed, truncated, or corrupt fallback evidence is rerun."""
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="meta-failover",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="primary",
                skill="paid-primary",
                kind="agent",
                on_failure="fallback",
            ),
            MetaStep(id="fallback", skill="local-fallback", kind="agent"),
            MetaStep(id="kept", skill="kept", kind="agent"),
            MetaStep(id="truncated", skill="truncated", kind="agent"),
            MetaStep(
                id="delivery",
                skill="delivery",
                kind="agent",
                depends_on=("primary", "kept", "truncated"),
            ),
        ),
    )
    fallback: SimpleNamespace | None
    if fallback_case == "missing":
        fallback = None
    elif fallback_case == "failed":
        fallback = _persisted_step("fallback", status="failed", output_text=None)
    elif fallback_case == "truncated":
        fallback = _persisted_step(
            "fallback",
            status="ok",
            output_text="partial fallback",
            truncated_fields=("output_text",),
        )
    elif fallback_case == "none-output":
        fallback = _persisted_step("fallback", status="ok", output_text=None)
    else:
        fallback = SimpleNamespace(
            step_id="fallback",
            status="ok",
            output_text="unbounded legacy value",
            substitute_step_id=None,
        )
    steps = [
        _persisted_step(
            "primary",
            status="substituted",
            output_text=None,
            substitute_step_id="fallback",
        ),
        _persisted_step("kept", status="ok", output_text="complete output"),
        _persisted_step(
            "truncated",
            status="ok",
            output_text="partial output",
            truncated_fields=("output_text",),
        ),
        _persisted_step("delivery", status="failed", output_text=None),
    ]
    if fallback is not None:
        steps.append(fallback)
    record = SimpleNamespace(
        run_id=f"run-{fallback_case}",
        meta_skill_name=plan.name,
        session_key="agent:main:test-launch",
        status="failed",
        failed_step_id="delivery",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        inputs_json="{}",
        steps=tuple(steps),
    )
    agent = _build_agent(
        loader=_StubLoader(_meta_spec(plan.name, disable_model_invocation=True)),
        writer=_StubWriter(record=record),
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="replayed"),
    )

    await _drain_replay(agent, plan.name, record.run_id)

    assert captured["iter_kwargs"] == {
        "seed_outputs": {"kept": "complete output"},
        "trusted_preflight_replay": True,
        "trusted_replay_meta_run_id": f"run-{fallback_case}",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_inputs", "source_run_id", "expected_meta_run_id"),
    [
        (
            {"user_message": "retry", "meta_run_id": "../../caller-controlled"},
            "run-malicious-persisted-id",
            "run-"
            + hashlib.sha256(b"../../caller-controlled").hexdigest()[:24],
        ),
        (
            {"user_message": "retry"},
            "legacy-source-run",
            "legacy-source-run",
        ),
    ],
)
async def test_run_meta_replay_safely_recovers_persisted_or_legacy_artifact_id(
    monkeypatch: pytest.MonkeyPatch,
    persisted_inputs: dict[str, str],
    source_run_id: str,
    expected_meta_run_id: str,
) -> None:
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="meta-short-drama",
        triggers=(),
        priority=0,
        steps=(MetaStep(id="delivery", skill="delivery", kind="agent"),),
    )
    record = SimpleNamespace(
        run_id=source_run_id,
        meta_skill_name=plan.name,
        session_key="agent:main:test-launch",
        status="failed",
        failed_step_id="delivery",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        inputs_json=json.dumps(persisted_inputs),
        steps=(_persisted_step("delivery", status="failed", output_text=None),),
    )
    agent = _build_agent(
        loader=_StubLoader(_meta_spec(plan.name, disable_model_invocation=True)),
        writer=_StubWriter(record=record),
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="replayed"),
    )

    await _drain_replay(agent, plan.name, source_run_id)

    assert captured["match"].inputs["meta_run_id"] == expected_meta_run_id
    assert captured["iter_kwargs"]["trusted_replay_meta_run_id"] == expected_meta_run_id
    assert "/" not in expected_meta_run_id
    assert "\\" not in expected_meta_run_id


@pytest.mark.asyncio
async def test_run_meta_replay_refuses_persisted_modified_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn boundary independently rejects non-exact saved requests."""
    from opensquilla.skills.meta.plan_serde import to_jsonable
    from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="meta-modified-inputs",
        triggers=(),
        priority=0,
        steps=(MetaStep(id="draft", skill="draft", kind="agent"),),
    )
    record = SimpleNamespace(
        run_id="run-modified-inputs",
        meta_skill_name=plan.name,
        session_key="agent:main:test-launch",
        status="failed",
        failed_step_id="draft",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        inputs_json='{"user_message": "[REDACTED]"}',
        truncated_fields=("inputs_json_modified",),
        steps=(),
    )
    agent = _build_agent(
        loader=_StubLoader(_meta_spec(plan.name)),
        writer=_StubWriter(record=record),
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="must not run"),
    )

    events = await _drain_replay(agent, plan.name, record.run_id)

    assert captured["called"] is False
    assert "saved request was redacted or truncated" in _done_event_of(events).text
    assert "Start a new meta-skill run" in _streamed_text(events)


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


@pytest.mark.asyncio
async def test_run_meta_launch_explains_retired_compatibility_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.meta.types import MetaResult

    spec = _meta_spec(
        "meta-retired",
        disable_model_invocation=True,
        description="Retired compatibility definition for historical runs.",
    )
    agent = _build_agent(
        loader=_StubLoader(spec),
        writer=_StubWriter(awaiting=None),
        extra_metadata={"meta_launch": {"name": "meta-retired"}},
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="SHOULD NOT RUN"),
    )

    events = await _drain(agent, "meta-retired")
    text = _streamed_text(events)

    assert "has been retired" in text
    assert "not available for new runs" in text
    assert "saved runs remain available" in text
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_run_meta_launch_refuses_missing_runtime_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale launch marker cannot bypass the shared readiness preflight."""
    from opensquilla.skills.meta.types import MetaResult
    from opensquilla.skills.types import SkillPlatformMeta, SkillRequires

    spec = _meta_spec("meta-needs-setup")
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(bins=["opensquilla-definitely-missing-binary"])
    )
    spec.composition_raw = None
    agent = _build_agent(loader=_StubLoader(spec), writer=_StubWriter())
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="SHOULD NOT RUN"),
    )

    events = await _drain(agent, "meta-needs-setup")

    assert "requires setup" in _streamed_text(events)
    assert "opensquilla-definitely-missing-binary" in _streamed_text(events)
    assert captured["called"] is False
    _done_event_of(events)


@pytest.mark.asyncio
async def test_run_meta_launch_rejects_global_alias_for_untrusted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual launch cannot consume an alias without a trusted parent plan."""
    from opensquilla.skills.meta.types import MetaResult
    from opensquilla.skills.types import SkillPlatformMeta, SkillRequires

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    spec = _meta_spec("meta-config-key")
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
    )
    spec.composition_raw = None
    agent = _build_agent(
        loader=_StubLoader(spec),
        writer=_StubWriter(),
        extra_metadata={
            "meta_readiness_env_aliases": lambda _parent, _plan: (
                "OPENROUTER_API_KEY",
            ),
        },
    )
    captured = _install_orchestrator_spy(
        agent,
        monkeypatch,
        result=MetaResult(ok=True, final_text="configured credential accepted"),
    )

    events = await _drain(agent, "meta-config-key")

    assert captured["called"] is False
    assert "requires setup" in _streamed_text(events)
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
