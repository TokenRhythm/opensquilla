"""Snapshot regression net for ``PromptAssemblerStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. The harness patches the seven
dependencies the prompt-assembler slice needs (``_assemble_prompt``,
``_run_pipeline``, ``_router_previous_assistant_context``,
``_resolve_prompt_config``, ``_resolve_session_id_for_log``, the
memory-fingerprint config method, and the ``build_prompt_report`` module
helper) so the slice runs against deterministic stubs.

It then probes ``_resolve_agent_runtime_timeout`` (the line immediately
after the slice in ``_run_turn``) to capture the post-slice locals and
raise a sentinel ``BaseException`` that halts the generator without
touching downstream stages. The raising-stub case (#13) exercises the
propagation path through the runtime's terminal ``except Exception``
handler.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent
from opensquilla.observability.prompt_report import PromptReport
from opensquilla.provider.types import ProviderRequestCorrelation

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider:
    name: str = "stub"
    provider_name: str = ""

    def __post_init__(self):
        if not self.provider_name:
            self.provider_name = self.name


@dataclass
class _StubSelector:
    label: str = "selector"
    overridden_models: list[str] = field(default_factory=list)
    resolve_returns: Any = None
    current_model: str = "claude-sonnet-4.5"

    @property
    def current_config(self):
        return SimpleNamespace(model=self.current_model)

    def clone(self):
        return self

    def override_model(self, model: str) -> None:
        self.overridden_models.append(model)

    def resolve(self):
        return self.resolve_returns or _StubProvider("resolved-after-override")


# ---------------------------------------------------------------------------
# Sentinel for halting the generator after the slice
# ---------------------------------------------------------------------------


class _SliceCapture(BaseException):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot


def _capture_locals_at_post_slice() -> dict[str, Any]:
    """Read ``_run_turn``'s locals at the boundary right after the slice.

    The probe is hooked onto ``_resolve_agent_runtime_timeout`` (the very
    next call site after the prompt-assembler slice). At entry, the
    caller's frame contains every local the prompt-assembler boundary must
    populate.
    """

    frame = sys._getframe(2)  # skip probe + adapter frames
    # Walk back until we land on _run_turn (the only frame with these locals).
    while frame is not None:
        if "turn_obj" in frame.f_locals and "final_prompt_str" in frame.f_locals:
            break
        frame = frame.f_back
    assert frame is not None, "could not locate _run_turn frame"
    locs = frame.f_locals

    turn = locs.get("turn")
    provider = locs.get("provider")
    metadata = getattr(turn, "metadata", {}) if turn is not None else {}
    return {
        "outcome": "success",
        "provider_name": getattr(provider, "provider_name", "") or (
            type(provider).__name__ if provider is not None else None
        ),
        "provider_is_fallback_wrapped": (
            type(provider).__name__ == "_SelectorFallbackProvider"
            if provider is not None
            else False
        ),
        "turn_message": getattr(turn, "message", None),
        "turn_model": getattr(turn, "model", None),
        "turn_metadata_keys": tuple(sorted(metadata.keys())),
        "turn_metadata_routed_tier": metadata.get("routed_tier"),
        "turn_tool_defs_count": len(getattr(turn, "tool_defs", []) or []),
        "effective_runtime_message": locs.get("effective_runtime_message"),
        "final_prompt": locs.get("final_prompt"),
        "final_prompt_str": locs.get("final_prompt_str"),
        "cache_breakpoints": locs.get("cache_breakpoints"),
        "request_context_prompt": locs.get("request_context_prompt"),
        "resolved_model": locs.get("resolved_model"),
        "selector_model": locs.get("selector_model"),
        "session_id_for_log": locs.get("session_id_for_log"),
        "trace_context_session_id": getattr(
            locs.get("trace_context"), "session_id", None,
        ),
        "prompt_report_chars": (
            locs.get("prompt_report_for_log").system_chars
            if isinstance(locs.get("prompt_report_for_log"), PromptReport)
            else None
        ),
        "prompt_report_session_id": (
            locs.get("prompt_report_for_log").session_id
            if isinstance(locs.get("prompt_report_for_log"), PromptReport)
            else None
        ),
        "prompt_report_tool_profile": (
            locs.get("prompt_report_for_log").tool_profile
            if isinstance(locs.get("prompt_report_for_log"), PromptReport)
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Patch helpers — bind stubbed implementations onto the TurnRunner instance.
# ---------------------------------------------------------------------------


def _patch_resolver(runner, provider, cloned_selector):
    def _resolve_provider(self):  # noqa: ARG001
        return provider, cloned_selector

    runner._resolve_provider = _resolve_provider.__get__(runner, TurnRunner)


def _patch_builder(runner, tool_defs, tool_handler, metadata):
    def _build_tools(self, ctx=None, metadata=None):  # noqa: ARG001
        if metadata is not None:
            metadata.update(metadata_value)
        return list(tool_defs), tool_handler

    metadata_value = dict(metadata or {})
    runner._build_tools = _build_tools.__get__(runner, TurnRunner)


def _patch_ctx_mutators(runner):
    async def _with_artifact_context(self, ctx, session_key):  # noqa: ARG001
        return replace(ctx, artifact_session_id=session_key.split(":")[-1] or session_key)

    def _with_runtime_write_callbacks(self, ctx, agent_id):  # noqa: ARG001
        return replace(ctx, agent_id=agent_id)

    runner._with_artifact_context = _with_artifact_context.__get__(runner, TurnRunner)
    runner._with_runtime_write_callbacks = (
        _with_runtime_write_callbacks.__get__(runner, TurnRunner)
    )


def _patch_assemble_prompt(runner, base_prompt, prompt_metadata):
    pm_to_emit = dict(prompt_metadata)

    def _assemble_prompt(
        self, agent_id, tool_defs, *, session_key=None, semantic_message=None,
        extra_context=None, prompt_metadata=None, bootstrap_context_mode=None,
        fresh_user_session=False,
    ):  # noqa: ARG001
        if prompt_metadata is not None:
            prompt_metadata.update(pm_to_emit)
        return base_prompt

    runner._assemble_prompt = _assemble_prompt.__get__(runner, TurnRunner)


def _patch_run_pipeline(runner, turn_factory, provider, raises=None):
    async def _run_pipeline(
        self, message, session_key, in_provider, cloned_selector,
        tool_defs, base_prompt, attachments, *,
        semantic_message=None, ingress_pipeline_steps=None,
        prev_assistant_text=None, prev_assistant_usage=None,
        history_user_texts=None,
        history_has_recent_image=False,
        history_image_turn_count=0,
        vision_sticky_remaining=0,
        turns_since_last_image=None,
        last_image_turn_text=None,
        vision_candidate_turns=0,
        flags_text_override=None,
        tool_context=None,
        normalization_metadata=None,
        usage_execution_context=None,
        provider_request_correlation=None,
    ):  # noqa: ARG001
        runner._captured_provider_request_correlation = (
            provider_request_correlation
        )
        if raises is not None:
            raise raises("equivalence pipeline boom")
        return turn_factory(), provider

    runner._run_pipeline = _run_pipeline.__get__(runner, TurnRunner)


def _patch_router_context(runner, ctx_payload):
    async def _router_previous_assistant_context(
        self, session_key, *, exclude_last_user=False,  # noqa: ARG001, ARG002
        bound_user_message_id=None,  # noqa: ARG001
    ):
        return dict(ctx_payload)

    runner._router_previous_assistant_context = (
        _router_previous_assistant_context.__get__(runner, TurnRunner)
    )


def _patch_resolve_prompt_config(runner, final_prompt, breakpoints, request_ctx):
    def _resolve_prompt_config(self, turn):  # noqa: ARG001
        return final_prompt, breakpoints, request_ctx

    runner._resolve_prompt_config = _resolve_prompt_config.__get__(runner, TurnRunner)


def _patch_session_id(runner, session_id):
    async def _resolve_session_id_for_log(self, session_key):  # noqa: ARG001, ARG002
        return session_id

    runner._resolve_session_id_for_log = (
        _resolve_session_id_for_log.__get__(runner, TurnRunner)
    )


def _patch_post_slice_probe(runner):
    """Hook _resolve_agent_runtime_timeout (first call past the slice)."""

    def _probe(self, session_key):  # noqa: ARG001, ARG002
        snapshot = _capture_locals_at_post_slice()
        raise _SliceCapture(snapshot)

    runner._resolve_agent_runtime_timeout = _probe.__get__(runner, TurnRunner)


def _patch_observability(runner):
    """Silence trace emit and persist so the slice can run end-to-end."""

    def _emit_turn_event(self, *args, **kwargs):  # noqa: ARG001, ARG002
        return None

    async def _persist_turn_error(self, *args, **kwargs):  # noqa: ARG001, ARG002
        return None

    runner._emit_turn_event = _emit_turn_event.__get__(runner, TurnRunner)
    runner._persist_turn_error = _persist_turn_error.__get__(runner, TurnRunner)


def _build_runner() -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=None,
        memory_retrievers=None,
        turn_capture_services=None,
        session_flush_service=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _make_turn_factory(
    *,
    message="EFFECTIVE",
    metadata=None,
    tool_defs=None,
    model="",
):
    def _factory():
        return SimpleNamespace(
            message=message,
            metadata=dict(metadata or {}),
            tool_defs=list(tool_defs or []),
            model=model,
        )
    return _factory


# ---------------------------------------------------------------------------
# Corpus — 13 cases.
# ---------------------------------------------------------------------------


_CASE_BASE: dict[str, Any] = dict(
    base_prompt="BASE",
    prompt_metadata={"skill_count": 0, "skills_prompt_chars": 0},
    turn_message="EFFECTIVE",
    turn_metadata={"tool_profile": "agent"},
    turn_tool_defs=[],
    turn_model="",
    router_ctx={},
    final_prompt="FINAL",
    cache_breakpoints=None,
    request_context_prompt=None,
    session_id="sess-1",
    pipeline_raises=None,
)


def _case(case_id: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = dict(_CASE_BASE)
    payload.update(overrides)
    return case_id, payload


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    _case("plain_user_turn"),
    _case("with_tool_ctx"),
    _case(
        "history_with_prior_assistant",
        router_ctx={
            "prev_assistant_text": "prior reply",
            "prev_assistant_usage": {"output_tokens": 32},
            "history_user_texts": ["q1"],
        },
    ),
    _case(
        "squilla_router_fires",
        turn_metadata={"tool_profile": "agent", "routed_tier": "premium"},
        turn_model="claude-sonnet-4.5",
    ),
    _case(
        "squilla_router_with_skills",
        prompt_metadata={"skill_count": 2, "skills_prompt_chars": 1024},
        turn_metadata={"tool_profile": "agent", "skill_count": 2},
    ),
    _case(
        "prompt_cache_miss",
        final_prompt="BASE",
        cache_breakpoints=[{"text": "BASE", "cache": "true"}],
        request_context_prompt="DYNAMIC",
    ),
    _case(
        "prompt_cache_hit_str",
        final_prompt="CACHED",
        cache_breakpoints=[{"text": "CACHED", "cache": "true"}],
        request_context_prompt=None,
    ),
    _case(
        "no_cache",
        cache_breakpoints=None,
        request_context_prompt=None,
    ),
    _case("model_override_at_call_site", model="claude-haiku-4.5"),
    _case("no_session_manager", session_id=None),
    _case(
        "memory_fingerprint_conflict",
        turn_metadata={
            "tool_profile": "agent",
            "memory_mode_fingerprint": {"embed": "v2"},
        },
    ),
    _case("history_persist_excludes_last_user", persist_input=True),
    _case("pipeline_raises", pipeline_raises=ValueError),
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _drive(
    runner,
    case,
    *,
    run_kind: str = "default",
    root_turn_id: str = "root-turn-1",
    provider_request_correlation: ProviderRequestCorrelation | None = None,
):
    captured = None
    raised = None
    yielded: list[Any] = []
    gen = runner._run_turn(
        message="hi",
        session_key="agent:main:s1",
        agent_id="agent:main",
        model=case.get("model"),
        attachments=[],
        tool_context=None,
        input_mode="user",
        persist_input=case.get("persist_input", False),
        input_provenance=None,
        history_has_persisted_user=True,
        semantic_message=None,
        run_kind=run_kind,
        root_turn_id=root_turn_id,
        provider_request_correlation=provider_request_correlation,
    )
    try:
        async for event in gen:
            yielded.append(event)
    except _SliceCapture as cap:
        captured = cap.snapshot
    except BaseException as exc:  # noqa: BLE001
        raised = type(exc)
    finally:
        await gen.aclose()
    return captured, yielded, raised


def _setup_runner(case: dict[str, Any]) -> TurnRunner:
    runner = _build_runner()
    selector = _StubSelector(
        "sel",
        current_model="claude-sonnet-4.5",
        resolve_returns=_StubProvider("override-resolved"),
    )
    _patch_resolver(runner, _StubProvider("p"), selector)
    _patch_builder(runner, [SimpleNamespace(name="t1")], object(), case["turn_metadata"])
    _patch_ctx_mutators(runner)
    _patch_assemble_prompt(runner, case["base_prompt"], case["prompt_metadata"])
    _patch_run_pipeline(
        runner,
        _make_turn_factory(
            message=case["turn_message"],
            metadata=case["turn_metadata"],
            tool_defs=case["turn_tool_defs"],
            model=case["turn_model"],
        ),
        provider=_StubProvider("post-pipeline"),
        raises=case["pipeline_raises"],
    )
    _patch_router_context(runner, case["router_ctx"])
    _patch_resolve_prompt_config(
        runner, case["final_prompt"], case["cache_breakpoints"],
        case["request_context_prompt"],
    )
    _patch_session_id(runner, case["session_id"])
    _patch_post_slice_probe(runner)
    _patch_observability(runner)
    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=[c[0] for c in _CORPUS])
@pytest.mark.asyncio
async def test_prompt_assembler_stage_snapshot(
    case_id, case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        raising=False,
    )
    runner = _setup_runner(case)
    captured, yielded, raised = await _drive(runner, case)

    if case["pipeline_raises"] is not None:
        # Exception propagates through the runtime's terminal handler ->
        # ErrorEvent yielded; no probe capture.
        assert captured is None
        assert raised is None
        assert len(yielded) == 1
        assert isinstance(yielded[0], ErrorEvent)
        assert yielded[0].code == "agent_error"
        return

    assert raised is None, f"{case_id} raised: {raised}"
    assert captured is not None, f"{case_id} captured nothing"

    # Successful path: build expected snapshot from the case definition.
    metadata_after_merge = dict(case["turn_metadata"])
    metadata_after_merge.update(case["prompt_metadata"])
    if case.get("model"):
        # Explicit per-turn model resolution now records the actual deployment
        # identity before prompt assembly for additive Router telemetry.
        metadata_after_merge.update(
            executed_provider="override-resolved",
            executed_model=case["model"],
        )
    expected_resolved_model = (
        case.get("model") or case["turn_model"] or "claude-sonnet-4.5"
    )
    expected_provider_name = "override-resolved" if case.get("model") else "post-pipeline"
    expected_snapshot = {
        "outcome": "success",
        "provider_name": expected_provider_name,
        "provider_is_fallback_wrapped": True,
        "turn_message": case["turn_message"],
        "turn_model": case["turn_model"],
        "turn_metadata_keys": tuple(sorted(metadata_after_merge.keys())),
        "turn_metadata_routed_tier": metadata_after_merge.get("routed_tier"),
        "turn_tool_defs_count": len(case["turn_tool_defs"]),
        "effective_runtime_message": case["turn_message"],
        "final_prompt": case["final_prompt"],
        "final_prompt_str": case["final_prompt"],
        "cache_breakpoints": case["cache_breakpoints"],
        "request_context_prompt": case["request_context_prompt"],
        "resolved_model": expected_resolved_model,
        "selector_model": "claude-sonnet-4.5",
        "session_id_for_log": case["session_id"],
        "trace_context_session_id": case["session_id"],
        "prompt_report_chars": len(case["final_prompt"]),
        "prompt_report_session_id": case["session_id"],
        "prompt_report_tool_profile": metadata_after_merge.get("tool_profile"),
    }
    assert captured == expected_snapshot, (
        f"case={case_id}: snapshot diverged.\n"
        f"  expected={expected_snapshot}\n  actual  ={captured}"
    )
    correlation = getattr(runner, "_captured_provider_request_correlation", None)
    if case["session_id"] is None:
        assert correlation is None
    else:
        assert correlation.session_id == case["session_id"]
        assert correlation.turn_id == "root-turn-1"
        assert correlation.execution_id != correlation.turn_id
        assert correlation.call_kind == "agent.chat"
        assert runner._usage_event_sink is None


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_source", ["config", "env"])
async def test_root_provider_correlation_respects_dedicated_privacy_switch(
    disable_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        raising=False,
    )
    case = dict(_CASE_BASE)
    runner = _setup_runner(case)
    if disable_source == "config":
        runner._config = SimpleNamespace(
            privacy=SimpleNamespace(disable_network_observability=True),
        )
    else:
        monkeypatch.setenv(
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
            "true",
        )

    captured, yielded, raised = await _drive(runner, case)

    assert captured is not None
    assert yielded == []
    assert raised is None
    assert runner._captured_provider_request_correlation is None


@pytest.mark.asyncio
async def test_runtime_subagent_reuses_explicit_parent_root_and_run_execution() -> None:
    case = dict(_CASE_BASE)
    case["session_id"] = "child-durable-session"
    runner = _setup_runner(case)
    correlation = ProviderRequestCorrelation(
        session_id="parent-durable-session",
        turn_id="parent-root-turn",
        execution_id="subagent-run-id",
        call_kind="subagent.chat",
    )

    captured, yielded, raised = await _drive(
        runner,
        case,
        run_kind="subagent",
        provider_request_correlation=correlation,
    )

    assert captured is not None
    assert yielded == []
    assert raised is None
    assert runner._captured_provider_request_correlation == correlation


@pytest.mark.asyncio
async def test_runtime_same_session_assigns_distinct_turn_and_execution_ids() -> None:
    case = dict(_CASE_BASE)
    case["session_id"] = "durable-session-shared"
    correlations: list[ProviderRequestCorrelation] = []

    for root_turn_id in ("root-turn-a", "root-turn-b"):
        runner = _setup_runner(case)
        captured, yielded, raised = await _drive(
            runner,
            case,
            root_turn_id=root_turn_id,
        )

        assert captured is not None
        assert yielded == []
        assert raised is None
        correlation = runner._captured_provider_request_correlation
        assert isinstance(correlation, ProviderRequestCorrelation)
        correlations.append(correlation)

    assert correlations[0].session_id == correlations[1].session_id
    assert correlations[0].turn_id != correlations[1].turn_id
    assert correlations[0].execution_id != correlations[1].execution_id
