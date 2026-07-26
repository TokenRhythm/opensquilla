"""Unit tests for ``PromptAssemblerStage`` driven directly (no full
TurnRunner stack).

Drives a 13-case corpus through ``PromptAssemblerStage.run`` with seven
recording fakes (one per port). Case #13 registers a raising fake so the
propagation contract is exercised without the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.outcome import StageOutcome
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    MemoryFingerprintPort,
    PipelineExecutionPort,
    PromptAssemblerPort,
    PromptAssemblerStage,
    PromptAssemblerStageInput,
    PromptConfigResolverPort,
    PromptReportBuilderPort,
    RouterContextPort,
    RunPipelineRequest,
    SessionIdResolverPort,
)
from opensquilla.observability.prompt_report import PromptReport

# ---------------------------------------------------------------------------
# Recording fakes (one per port)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingPromptAssembler:
    base_prompt: Any = "BASE"
    metadata_to_emit: dict[str, Any] = field(default_factory=dict)
    calls: int = 0
    last_kwargs: dict[str, Any] = field(default_factory=dict)

    def assemble_prompt(
        self,
        agent_id,
        tool_defs,
        *,
        session_key,
        semantic_message,
        extra_context,
        prompt_metadata,
        bootstrap_context_mode,
        fresh_user_session=False,
    ):
        self.calls += 1
        self.last_kwargs = dict(
            agent_id=agent_id,
            tool_defs=list(tool_defs),
            session_key=session_key,
            semantic_message=semantic_message,
            extra_context=extra_context,
            bootstrap_context_mode=bootstrap_context_mode,
            fresh_user_session=fresh_user_session,
        )
        prompt_metadata.update(self.metadata_to_emit)
        return self.base_prompt


@dataclass
class _RecordingRouterContext:
    context: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, bool]] = field(default_factory=list)
    bound_user_message_ids: list[str | None] = field(default_factory=list)

    async def fetch_router_context(
        self, session_key, *, exclude_last_user, bound_user_message_id=None
    ):
        self.calls.append((session_key, exclude_last_user))
        self.bound_user_message_ids.append(bound_user_message_id)
        return dict(self.context)


@dataclass
class _RecordingPipelineExecutor:
    turn: Any = None
    provider: Any = None
    raises: type[BaseException] | None = None
    requests: list[RunPipelineRequest] = field(default_factory=list)

    async def run_pipeline(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises("recording pipeline executor boom")
        return self.turn, self.provider


@dataclass
class _RecordingPromptConfigResolver:
    final_prompt: str = "FINAL"
    cache_breakpoints: list[Any] | None = None
    request_context_prompt: str | None = None
    calls: int = 0

    def resolve_prompt_config(self, turn):
        self.calls += 1
        return self.final_prompt, self.cache_breakpoints, self.request_context_prompt


@dataclass
class _RecordingPromptReportBuilder:
    last_kwargs: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    def build_prompt_report(self, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return PromptReport(
            turn_id=kwargs["turn_id"],
            session_key=kwargs["session_key"],
            session_id=kwargs.get("session_id"),
            agent_id=kwargs.get("agent_id", ""),
            system_chars=len(kwargs.get("system_prompt", "")),
            tool_count=len(kwargs.get("tool_defs", [])),
            tool_profile=kwargs.get("tool_profile"),
        )


@dataclass
class _RecordingSessionIdResolver:
    session_id: str | None = "session-id"
    calls: int = 0

    async def resolve_session_id_for_log(self, session_key):  # noqa: ARG002
        self.calls += 1
        return self.session_id


@dataclass
class _RecordingMemoryFingerprint:
    fingerprint: dict[str, str] | None = None
    calls: int = 0

    def memory_mode_fingerprint(self):
        self.calls += 1
        return dict(self.fingerprint) if self.fingerprint is not None else None


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

    def override_model(self, model: str) -> None:
        self.overridden_models.append(model)

    def resolve(self):
        return self.resolve_returns


@dataclass
class _RaisingCurrentConfigSelector(_StubSelector):
    def __post_init__(self):
        # current_config getter raises
        pass

    @property
    def current_config(self):
        raise RuntimeError("config raises")


def _make_turn(
    *,
    message: str = "EFFECTIVE",
    metadata: dict[str, Any] | None = None,
    tool_defs: list[Any] | None = None,
    model: str = "",
):
    return SimpleNamespace(
        message=message,
        metadata=dict(metadata or {}),
        tool_defs=list(tool_defs or []),
        model=model,
    )


def _make_input(
    *,
    runtime_message="hi",
    semantic_input="hi",
    extra_prompt_context=None,
    provider=None,
    cloned_selector=None,
    tool_defs=None,
    effective_tool_context=None,
    tool_metadata=None,
    session_key="agent:main:s1",
    agent_id="agent:main",
    turn_id="t-1",
    attachments=None,
    bootstrap_context_mode=None,
    model=None,
    history_has_persisted_user=True,
    persist_input=False,
    fresh_user_session=False,
    bound_user_message_id=None,
    ingress_pipeline_steps=None,
    input_provenance=None,
    skill_catalog=None,
):
    return PromptAssemblerStageInput(
        runtime_message=runtime_message,
        semantic_input=semantic_input,
        extra_prompt_context=extra_prompt_context,
        provider=provider if provider is not None else _StubProvider("p_in"),
        cloned_selector=cloned_selector,
        tool_defs=list(tool_defs or []),
        effective_tool_context=effective_tool_context,
        tool_metadata=dict(tool_metadata or {}),
        session_key=session_key,
        agent_id=agent_id,
        turn_id=turn_id,
        attachments=list(attachments or []),
        bootstrap_context_mode=bootstrap_context_mode,
        model=model,
        history_has_persisted_user=history_has_persisted_user,
        persist_input=persist_input,
        fresh_user_session=fresh_user_session,
        bound_user_message_id=bound_user_message_id,
        ingress_pipeline_steps=ingress_pipeline_steps,
        input_provenance=input_provenance,
        skill_catalog=skill_catalog,
    )


def _make_stage(
    *,
    assembler=None,
    router=None,
    executor=None,
    resolver=None,
    builder=None,
    session_id=None,
    fingerprint=None,
):
    return PromptAssemblerStage(
        prompt_assembler=assembler or _RecordingPromptAssembler(),
        pipeline_executor=executor or _RecordingPipelineExecutor(turn=_make_turn()),
        router_context=router or _RecordingRouterContext(),
        prompt_config_resolver=resolver or _RecordingPromptConfigResolver(),
        prompt_report_builder=builder or _RecordingPromptReportBuilder(),
        session_id_resolver=session_id or _RecordingSessionIdResolver(),
        memory_fingerprint=fingerprint or _RecordingMemoryFingerprint(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case01_plain_user_turn() -> None:
    """Base case: no router context, no fingerprint, no override."""
    selector = _StubSelector("sel1")
    provider_after_pipeline = _StubProvider("pp")
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(metadata={"routed_tier": "T1"}, tool_defs=[1, 2]),
        provider=provider_after_pipeline,
    )
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=selector, tool_defs=[1, 2])
    out = await stage.run(inp)
    assert isinstance(out, StageOutcome)
    o = out.output
    assert o.effective_runtime_message == "EFFECTIVE"
    assert o.final_prompt == "FINAL"
    assert o.resolved_model == "claude-sonnet-4.5"
    assert o.selector_model == "claude-sonnet-4.5"
    # Provider gets wrapped in _SelectorFallbackProvider
    assert type(o.provider).__name__ == "_SelectorFallbackProvider"
    assert o.squilla_router_tier == "T1"
    assert o.session_id_for_log == "session-id"
    assert o.trace_context_session_id == "session-id"


@pytest.mark.asyncio
async def test_provider_name_uses_selector_registry_identity_not_adapter_family() -> None:
    selector = _StubSelector("selector")
    selector.active_provider_id = "dashscope"
    adapter = _StubProvider(name="adapter", provider_name="openai")
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=adapter)

    out = await _make_stage(executor=executor).run(
        _make_input(cloned_selector=selector)
    )

    assert out.output.provider_name == "dashscope"


@pytest.mark.asyncio
async def test_provider_name_uses_direct_adapter_configured_identity() -> None:
    adapter = _StubProvider(name="adapter", provider_name="anthropic")
    adapter.provider_id = "minimax"
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=adapter)

    out = await _make_stage(executor=executor).run(
        _make_input(provider=adapter, cloned_selector=None)
    )

    assert out.output.provider_name == "minimax"


@pytest.mark.asyncio
async def test_prompt_assembler_forwards_fresh_user_session_flag():
    prompt_assembler = _RecordingPromptAssembler()
    stage = _make_stage(assembler=prompt_assembler)

    await stage.run(_make_input(fresh_user_session=True))

    assert prompt_assembler.last_kwargs["fresh_user_session"] is True


async def test_prompt_assembler_forwards_bound_user_message_id_to_router_context():
    router_context = _RecordingRouterContext()
    stage = _make_stage(router=router_context)

    await stage.run(_make_input(bound_user_message_id="msg-bound"))

    assert router_context.calls == [("agent:main:s1", True)]
    assert router_context.bound_user_message_ids == ["msg-bound"]


@pytest.mark.asyncio
async def test_case02_with_tool_ctx_threads_into_pipeline() -> None:
    sentinel = object()
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=_StubProvider())
    stage = _make_stage(executor=executor)
    inp = _make_input(
        cloned_selector=_StubSelector(),
        effective_tool_context=sentinel,  # type: ignore[arg-type]
    )
    await stage.run(inp)
    assert executor.requests[0].tool_context is sentinel


@pytest.mark.asyncio
async def test_input_provenance_threads_into_pipeline() -> None:
    provenance = {"kind": "clarify_form", "source": "webui"}
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=_StubProvider())
    stage = _make_stage(executor=executor)
    inp = _make_input(input_provenance=provenance)

    await stage.run(inp)

    assert executor.requests[0].input_provenance == provenance


@pytest.mark.asyncio
async def test_case03_history_router_context_threading() -> None:
    router = _RecordingRouterContext(
        context={
            "prev_assistant_text": "prior reply",
            "prev_assistant_usage": {"output_tokens": 32},
            "history_user_texts": ["q1"],
            "history_has_recent_image": True,
            "history_image_turn_count": 2,
            "vision_sticky_remaining": 1,
            "turns_since_last_image": 1,
            "last_image_turn_text": "Describe this screenshot.",
            "vision_candidate_turns": 8,
        }
    )
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=_StubProvider())
    stage = _make_stage(router=router, executor=executor)
    inp = _make_input(cloned_selector=_StubSelector())
    await stage.run(inp)
    req = executor.requests[0]
    assert req.prev_assistant_text == "prior reply"
    assert req.prev_assistant_usage == {"output_tokens": 32}
    assert req.history_user_texts == ["q1"]
    assert req.history_has_recent_image is True
    assert req.history_image_turn_count == 2
    assert req.vision_sticky_remaining == 1
    assert req.turns_since_last_image == 1
    assert req.last_image_turn_text == "Describe this screenshot."
    assert req.vision_candidate_turns == 8


@pytest.mark.asyncio
async def test_case04_squilla_router_fires_overrides_model() -> None:
    selector = _StubSelector("sel4", current_model="claude-opus-4.5")
    routed_provider = _StubProvider("opus_routed")
    selector.resolve_returns = routed_provider
    provider_after_pipeline = _StubProvider("post_pipeline")
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(metadata={"routed_tier": "premium"}, model="claude-sonnet-4.5"),
        provider=provider_after_pipeline,
    )
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=selector, tool_defs=[1])
    out = await stage.run(inp)
    assert selector.overridden_models == []
    inner = getattr(out.output.provider, "_provider", None)
    assert inner is provider_after_pipeline
    assert out.output.resolved_model == "claude-sonnet-4.5"
    assert out.output.squilla_router_tier == "premium"


@pytest.mark.asyncio
async def test_blocked_cross_provider_route_resolves_primary_selector_model() -> None:
    selector = _StubSelector("primary", current_model="qwen3.7-plus")
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(
            metadata={
                "routed_provider": "volcengine",
                "routed_model": "doubao-seed-1-6-251015",
                "routed_provider_blocked": "missing_credential",
                "executed_provider": "dashscope",
                "executed_model": "qwen3.7-plus",
            },
            model="doubao-seed-1-6-251015",
        ),
        provider=_StubProvider("dashscope-primary"),
    )

    out = await _make_stage(executor=executor).run(
        _make_input(cloned_selector=selector)
    )

    assert out.output.turn.model == "doubao-seed-1-6-251015"
    assert out.output.selector_model == "qwen3.7-plus"
    assert out.output.resolved_model == "qwen3.7-plus"


@pytest.mark.asyncio
async def test_explicit_model_override_reconciles_routed_model_and_clears_savings() -> None:
    selector = _StubSelector("sel", current_model="claude-opus-4.5")
    selector.resolve_returns = _StubProvider("routed")
    turn = _make_turn(
        metadata={
            "routed_model": "claude-sonnet-4.5",
            "savings_pct": 50.0,
            "savings_max_price_per_m": 9.0,
            "savings_routed_price_per_m": 3.0,
        },
        model="claude-sonnet-4.5",
    )
    executor = _RecordingPipelineExecutor(turn=turn, provider=_StubProvider("post"))
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=selector, model="claude-haiku-4.5")

    await stage.run(inp)

    # routed_model realigned to the model that actually ran; savings dropped.
    assert turn.metadata["routed_model"] == "claude-haiku-4.5"
    assert turn.metadata["savings_pct"] == 0.0
    assert turn.metadata["savings_max_price_per_m"] == 0.0
    assert turn.metadata["savings_routed_price_per_m"] == 0.0


@pytest.mark.asyncio
async def test_explicit_model_equal_to_routed_keeps_savings() -> None:
    selector = _StubSelector("sel", current_model="claude-haiku-4.5")
    selector.resolve_returns = _StubProvider("routed")
    turn = _make_turn(
        metadata={"routed_model": "claude-haiku-4.5", "savings_pct": 42.0},
        model="claude-haiku-4.5",
    )
    executor = _RecordingPipelineExecutor(turn=turn, provider=_StubProvider("post"))
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=selector, model="claude-haiku-4.5")

    await stage.run(inp)

    # Explicit model matches the routed choice: no mismatch, savings preserved.
    assert turn.metadata["routed_model"] == "claude-haiku-4.5"
    assert turn.metadata["savings_pct"] == 42.0


@pytest.mark.asyncio
async def test_case05_pipeline_filter_skills_metadata_merge() -> None:
    assembler = _RecordingPromptAssembler(metadata_to_emit={"skill_count": 2})
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(metadata={"skills_prompt_chars": 1234}),
        provider=_StubProvider(),
    )
    builder = _RecordingPromptReportBuilder()
    stage = _make_stage(assembler=assembler, executor=executor, builder=builder)
    inp = _make_input(cloned_selector=_StubSelector())
    await stage.run(inp)
    # turn.metadata is mutated by stage with the prompt_metadata + tool_metadata
    assert builder.last_kwargs["metadata"]["skill_count"] == 2
    assert builder.last_kwargs["metadata"]["skills_prompt_chars"] == 1234


@pytest.mark.asyncio
async def test_case06_prompt_cache_miss_tuple_form() -> None:
    resolver = _RecordingPromptConfigResolver(
        final_prompt="BASE_ONLY",
        cache_breakpoints=[{"text": "BASE_ONLY", "cache": "true"}],
        request_context_prompt="DYNAMIC_PART",
    )
    stage = _make_stage(resolver=resolver)
    inp = _make_input(cloned_selector=_StubSelector())
    out = await stage.run(inp)
    assert out.output.final_prompt == "BASE_ONLY"
    assert out.output.cache_breakpoints == [{"text": "BASE_ONLY", "cache": "true"}]
    assert out.output.request_context_prompt == "DYNAMIC_PART"


@pytest.mark.asyncio
async def test_case07_prompt_cache_hit_str_form() -> None:
    resolver = _RecordingPromptConfigResolver(
        final_prompt="CACHED",
        cache_breakpoints=[{"text": "CACHED", "cache": "true"}],
        request_context_prompt=None,
    )
    stage = _make_stage(resolver=resolver)
    inp = _make_input(cloned_selector=_StubSelector())
    out = await stage.run(inp)
    assert out.output.final_prompt == "CACHED"
    assert out.output.cache_breakpoints == [{"text": "CACHED", "cache": "true"}]
    assert out.output.request_context_prompt is None


@pytest.mark.asyncio
async def test_case08_no_cache() -> None:
    resolver = _RecordingPromptConfigResolver(
        final_prompt="NOCACHE", cache_breakpoints=None, request_context_prompt=None,
    )
    stage = _make_stage(resolver=resolver)
    inp = _make_input(cloned_selector=_StubSelector())
    out = await stage.run(inp)
    assert out.output.cache_breakpoints is None
    assert out.output.request_context_prompt is None


@pytest.mark.asyncio
async def test_case09_model_override_at_call_site() -> None:
    selector = _StubSelector("sel9", current_model="claude-sonnet-4.5")
    overridden_provider = _StubProvider("after_override")
    selector.resolve_returns = overridden_provider
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(model=""), provider=_StubProvider("pp"),
    )
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=selector, model="claude-haiku-4.5")
    out = await stage.run(inp)
    assert selector.overridden_models == ["claude-haiku-4.5"]
    # provider gets wrapped, but inner provider should be the override
    inner = getattr(out.output.provider, "_provider", None)
    assert inner is overridden_provider
    assert out.output.resolved_model == "claude-haiku-4.5"


@pytest.mark.asyncio
async def test_case10_no_session_manager() -> None:
    session_id = _RecordingSessionIdResolver(session_id=None)
    stage = _make_stage(session_id=session_id)
    inp = _make_input(cloned_selector=_StubSelector())
    out = await stage.run(inp)
    assert out.output.session_id_for_log is None
    assert out.output.trace_context_session_id is None
    assert out.output.prompt_report.session_id is None


@pytest.mark.asyncio
async def test_case11_memory_fingerprint_conflict() -> None:
    fingerprint = _RecordingMemoryFingerprint(
        fingerprint={"mode": "stateful", "embed": "v1"},
    )
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(metadata={"memory_mode_fingerprint": {"embed": "v2"}}),
        provider=_StubProvider(),
    )
    stage = _make_stage(executor=executor, fingerprint=fingerprint)
    inp = _make_input(cloned_selector=_StubSelector())
    out = await stage.run(inp)
    merged = out.output.turn.metadata["memory_mode_fingerprint"]
    # prompt fingerprint values overwrite config defaults via update()
    assert merged["embed"] == "v2"
    assert merged["mode"] == "stateful"


@pytest.mark.asyncio
async def test_case12_history_persist_excludes_last_user() -> None:
    router = _RecordingRouterContext()
    stage = _make_stage(router=router)
    inp = _make_input(
        cloned_selector=_StubSelector(),
        history_has_persisted_user=True,
        persist_input=False,
    )
    await stage.run(inp)
    assert router.calls == [("agent:main:s1", True)]


@pytest.mark.asyncio
async def test_case13_pipeline_executor_raises_propagates() -> None:
    executor = _RecordingPipelineExecutor(raises=ValueError)
    stage = _make_stage(executor=executor)
    inp = _make_input(cloned_selector=_StubSelector())
    with pytest.raises(ValueError, match="recording pipeline executor boom"):
        await stage.run(inp)


@pytest.mark.asyncio
async def test_selector_current_config_raises_resolves_empty() -> None:
    selector = _RaisingCurrentConfigSelector(label="sel14")
    stage = _make_stage()
    inp = _make_input(cloned_selector=selector)
    out = await stage.run(inp)
    assert out.output.selector_model == ""


@pytest.mark.asyncio
async def test_no_cloned_selector_skips_selector_block() -> None:
    """When ``cloned_selector`` is None, no override / no fallback wrap."""
    in_provider = _StubProvider("input_provider")
    executor = _RecordingPipelineExecutor(
        turn=_make_turn(), provider=_StubProvider("pp"),
    )
    stage = _make_stage(executor=executor)
    inp = _make_input(provider=in_provider, cloned_selector=None)
    out = await stage.run(inp)
    # Provider is the post-pipeline provider unchanged (no wrap)
    assert type(out.output.provider).__name__ == "_StubProvider"
    assert out.output.selector_model == ""


@pytest.mark.asyncio
async def test_skill_catalog_is_forwarded_to_pipeline_unchanged() -> None:
    catalog = object()
    executor = _RecordingPipelineExecutor(turn=_make_turn(), provider=_StubProvider("pp"))
    stage = _make_stage(executor=executor)

    await stage.run(_make_input(skill_catalog=catalog))

    assert executor.requests[0].skill_catalog is catalog


def test_run_pipeline_request_is_frozen() -> None:
    req = RunPipelineRequest(
        runtime_message="m", session_key="s", provider=None,
        cloned_selector=None, tool_defs=[], base_prompt="b", attachments=[],
    )
    with pytest.raises(Exception):  # noqa: BLE001 - dataclass FrozenInstanceError
        req.runtime_message = "x"  # type: ignore[misc]


def test_stage_name_constant() -> None:
    assert PromptAssemblerStage.name == "prompt_assembler_stage"


def test_ports_runtime_checkable() -> None:
    assert isinstance(_RecordingPromptAssembler(), PromptAssemblerPort)
    assert isinstance(_RecordingPipelineExecutor(), PipelineExecutionPort)
    assert isinstance(_RecordingRouterContext(), RouterContextPort)
    assert isinstance(_RecordingPromptConfigResolver(), PromptConfigResolverPort)
    assert isinstance(_RecordingPromptReportBuilder(), PromptReportBuilderPort)
    assert isinstance(_RecordingSessionIdResolver(), SessionIdResolverPort)
    assert isinstance(_RecordingMemoryFingerprint(), MemoryFingerprintPort)


# replace lint suppress
_ = replace
