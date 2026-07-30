"""Unit tests for ``ProviderAndToolsStage`` driven directly (no full
TurnRunner stack).

Drives a 10-case corpus through ``ProviderAndToolsStage.run`` with
recording fakes for ``ProviderResolverPort`` and ``ToolBuilderPort``.

Two cases (#9 and #10) register raising fakes so the propagation contract is
exercised without the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.outcome import StageOutcome
from opensquilla.engine.turn_runner.provider_and_tools_stage import (
    ProviderAndToolsStage,
    ProviderAndToolsStageInput,
    ProviderResolverPort,
    ToolBuilderPort,
)
from opensquilla.engine.types import ErrorEvent
from opensquilla.tools.types import CallerKind, ToolContext


@dataclass
class _StubProvider:
    name: str = "stub"


@dataclass
class _StubSelector:
    label: str = "selector"


@dataclass
class _RecordingProviderResolver:
    provider: Any | None = field(default_factory=_StubProvider)
    cloned_selector: Any | None = field(default_factory=_StubSelector)
    raises: type[BaseException] | None = None
    calls: int = 0

    def resolve_provider(self) -> tuple[Any | None, Any | None]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises("recording provider resolver boom")
        return self.provider, self.cloned_selector


@dataclass
class _RecordingToolBuilder:
    tool_defs: list[Any] = field(default_factory=list)
    tool_handler: Any | None = None
    raises_build: type[BaseException] | None = None
    artifact_calls: list[str] = field(default_factory=list)
    runtime_writes_calls: list[str] = field(default_factory=list)
    build_calls: int = 0
    metadata_to_emit: dict[str, Any] = field(
        default_factory=lambda: {"tool_profile": "agent"}
    )

    async def with_artifact_context(
        self, ctx: ToolContext, session_key: str
    ) -> ToolContext:
        self.artifact_calls.append(session_key)
        return replace(ctx, artifact_session_id=session_key.split(":")[-1] or session_key)

    def with_runtime_write_callbacks(
        self, ctx: ToolContext, agent_id: str
    ) -> ToolContext:
        self.runtime_writes_calls.append(agent_id)
        return replace(ctx, agent_id=agent_id)

    def build_tools(
        self, ctx: ToolContext | None, *, metadata: dict[str, Any] | None = None
    ) -> tuple[list[Any], Any | None]:
        self.build_calls += 1
        if self.raises_build is not None:
            raise self.raises_build("recording tool builder boom")
        if metadata is not None:
            metadata.update(self.metadata_to_emit)
        return list(self.tool_defs), self.tool_handler


@dataclass
class _RecordingCatalogResolver:
    catalog: Any
    calls: int = 0

    async def resolve_skill_catalog(self) -> Any:
        self.calls += 1
        return self.catalog


@dataclass
class _CatalogAwareToolBuilder(_RecordingToolBuilder):
    catalogs: list[Any] = field(default_factory=list)

    def build_tools_for_catalog(
        self,
        ctx: ToolContext | None,
        *,
        skill_catalog: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Any], Any | None]:
        self.catalogs.append(skill_catalog)
        return self.build_tools(ctx, metadata=metadata)


def _ctx(kind: CallerKind = CallerKind.AGENT, **extra: Any) -> ToolContext:
    return ToolContext(caller_kind=kind, agent_id="agent:main", **extra)


def _snapshot(outcome, raised, resolver, builder):
    if raised is not None:
        return {"outcome": "exception", "exception_type": raised.__name__}
    if outcome.terminate:
        ev = outcome.early_yield
        assert isinstance(ev, ErrorEvent)
        return {"outcome": "early_yield", "message": ev.message, "code": ev.code}
    out = outcome.output
    eff = out.effective_tool_context
    return {
        "outcome": "success",
        "provider_name": getattr(out.provider, "name", None),
        "cloned_selector_label": getattr(out.cloned_selector, "label", None),
        "cloned_selector_differs": (
            out.cloned_selector is not None
            and out.cloned_selector is resolver.cloned_selector
        ),
        "tool_defs_repr": repr(out.tool_defs),
        "tool_handler_present": out.tool_handler is not None,
        "ctx_artifact_session_id": (eff.artifact_session_id if eff else None),
        "ctx_agent_id": (eff.agent_id if eff else None),
        "ctx_caller_kind": (eff.caller_kind.value if eff else None),
        "tool_metadata_items": tuple(sorted(out.tool_metadata.items())),
        "artifact_call_count": len(builder.artifact_calls),
        "runtime_writes_call_count": len(builder.runtime_writes_calls),
    }


_HANDLER = object()
_DEFS_5 = [{"name": f"t{i}"} for i in range(1, 6)]
_DEFS_3 = [{"name": f"t{i}"} for i in range(1, 4)]
_DEFS_2 = [{"name": "t1"}, {"name": "t2"}]
_DEFS_1 = [{"name": "t1"}]


def _expect_success(
    *, provider, selector, defs, handler_present, ctx_sid, ctx_kind,
    metadata, artifact_calls, write_calls,
):
    return {
        "outcome": "success",
        "provider_name": provider,
        "cloned_selector_label": selector,
        "cloned_selector_differs": True,
        "tool_defs_repr": repr(defs),
        "tool_handler_present": handler_present,
        "ctx_artifact_session_id": ctx_sid,
        "ctx_agent_id": "agent:main" if ctx_kind else None,
        "ctx_caller_kind": ctx_kind,
        "tool_metadata_items": metadata,
        "artifact_call_count": artifact_calls,
        "runtime_writes_call_count": write_calls,
    }


_CORPUS: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = [
    # (case_id, input_kwargs, resolver_kwargs, builder_kwargs, expected)
    (
        "success_no_tool_ctx",
        dict(session_key="agent:main:s1", tool_context=None),
        dict(provider=_StubProvider("p1"), cloned_selector=_StubSelector("sel1")),
        dict(tool_defs=_DEFS_5, tool_handler=_HANDLER),
        _expect_success(
            provider="p1", selector="sel1", defs=_DEFS_5, handler_present=True,
            ctx_sid=None, ctx_kind=None,
            metadata=(("tool_profile", "agent"),),
            artifact_calls=0, write_calls=0,
        ),
    ),
    (
        "success_default_ctx",
        dict(session_key="agent:main:s2", tool_context=_ctx()),
        dict(provider=_StubProvider("p2"), cloned_selector=_StubSelector("sel2")),
        dict(tool_defs=_DEFS_2, tool_handler=_HANDLER),
        _expect_success(
            provider="p2", selector="sel2", defs=_DEFS_2, handler_present=True,
            ctx_sid="s2", ctx_kind="agent",
            metadata=(("tool_profile", "agent"),),
            artifact_calls=1, write_calls=1,
        ),
    ),
    (
        "success_subagent_ctx",
        dict(session_key="agent:main:s3", tool_context=_ctx(CallerKind.SUBAGENT)),
        dict(provider=_StubProvider("p3"), cloned_selector=_StubSelector("sel3")),
        dict(tool_defs=_DEFS_1, tool_handler=_HANDLER,
             metadata_to_emit={"tool_profile": "subagent"}),
        _expect_success(
            provider="p3", selector="sel3", defs=_DEFS_1, handler_present=True,
            ctx_sid="s3", ctx_kind="subagent",
            metadata=(("tool_profile", "subagent"),),
            artifact_calls=1, write_calls=1,
        ),
    ),
    (
        "success_channel_ctx",
        dict(session_key="agent:main:s4",
             tool_context=_ctx(CallerKind.CHANNEL, channel_id="C123")),
        dict(provider=_StubProvider("p4"), cloned_selector=_StubSelector("sel4")),
        dict(tool_defs=_DEFS_3, tool_handler=_HANDLER),
        _expect_success(
            provider="p4", selector="sel4", defs=_DEFS_3, handler_present=True,
            ctx_sid="s4", ctx_kind="channel",
            metadata=(("tool_profile", "agent"),),
            artifact_calls=1, write_calls=1,
        ),
    ),
    (
        "success_empty_registry",
        dict(session_key="agent:main:s5", tool_context=None),
        dict(provider=_StubProvider("p5"), cloned_selector=_StubSelector("sel5")),
        dict(tool_defs=[], tool_handler=None, metadata_to_emit={}),
        _expect_success(
            provider="p5", selector="sel5", defs=[], handler_present=False,
            ctx_sid=None, ctx_kind=None, metadata=(),
            artifact_calls=0, write_calls=0,
        ),
    ),
    (
        "success_registry_denies_all",
        dict(session_key="agent:main:s6", tool_context=_ctx()),
        dict(provider=_StubProvider("p6"), cloned_selector=_StubSelector("sel6")),
        dict(tool_defs=[], tool_handler=_HANDLER),
        _expect_success(
            provider="p6", selector="sel6", defs=[], handler_present=True,
            ctx_sid="s6", ctx_kind="agent",
            metadata=(("tool_profile", "agent"),),
            artifact_calls=1, write_calls=1,
        ),
    ),
    (
        "early_yield_no_provider_no_ctx",
        dict(session_key="agent:main:s7", tool_context=None),
        dict(provider=None, cloned_selector=None),
        dict(),
        {"outcome": "early_yield", "message": "No provider available", "code": "no_provider"},
    ),
    (
        "early_yield_no_provider_with_ctx",
        dict(session_key="agent:main:s8", tool_context=_ctx()),
        dict(provider=None, cloned_selector=None),
        dict(),
        {"outcome": "early_yield", "message": "No provider available", "code": "no_provider"},
    ),
    (
        "exception_resolver_raises",
        dict(session_key="agent:main:s9", tool_context=None),
        dict(raises=ConnectionError),
        dict(),
        {"outcome": "exception", "exception_type": "ConnectionError"},
    ),
    (
        "exception_build_raises",
        dict(session_key="agent:main:s10", tool_context=_ctx()),
        dict(provider=_StubProvider("p10"), cloned_selector=_StubSelector("sel10")),
        dict(raises_build=ValueError),
        {"outcome": "exception", "exception_type": "ValueError"},
    ),
]


@pytest.mark.parametrize(
    "case_id,input_kw,resolver_kw,builder_kw,expected",
    _CORPUS,
    ids=[c[0] for c in _CORPUS],
)
@pytest.mark.asyncio
async def test_provider_and_tools_stage_unit(
    case_id, input_kw, resolver_kw, builder_kw, expected
) -> None:
    resolver = _RecordingProviderResolver(**{
        **dict(provider=_StubProvider(), cloned_selector=_StubSelector()),
        **resolver_kw,
    })
    builder = _RecordingToolBuilder(**builder_kw)
    stage = ProviderAndToolsStage(provider_resolver=resolver, tool_builder=builder)

    inp = ProviderAndToolsStageInput(
        agent_id="agent:main",
        run_kind="user",
        input_mode="user",
        **input_kw,
    )

    raised = None
    outcome: StageOutcome | None = None
    try:
        outcome = await stage.run(inp)
    except BaseException as exc:  # noqa: BLE001
        raised = type(exc)

    snap = _snapshot(outcome, raised, resolver, builder)
    assert snap == expected, f"case={case_id}: snapshot diverged.\n  exp={expected}\n  got={snap}"


def test_provider_resolver_port_runtime_checkable() -> None:
    assert isinstance(_RecordingProviderResolver(), ProviderResolverPort)


def test_tool_builder_port_runtime_checkable() -> None:
    assert isinstance(_RecordingToolBuilder(), ToolBuilderPort)


@pytest.mark.asyncio
async def test_catalog_generation_is_pinned_before_tool_build() -> None:
    catalog = object()
    catalog_resolver = _RecordingCatalogResolver(catalog)
    builder = _CatalogAwareToolBuilder(tool_defs=_DEFS_1)
    stage = ProviderAndToolsStage(
        provider_resolver=_RecordingProviderResolver(),
        tool_builder=builder,
        skill_catalog_resolver=catalog_resolver,
    )

    outcome = await stage.run(
        ProviderAndToolsStageInput(
            session_key="agent:main:snapshot",
            agent_id="agent:main",
            tool_context=_ctx(),
            run_kind="user",
            input_mode="user",
        )
    )

    assert outcome.output is not None
    assert outcome.output.skill_catalog is catalog
    assert outcome.output.effective_tool_context is not None
    assert outcome.output.effective_tool_context.skill_catalog is catalog
    assert builder.catalogs == [catalog]
    assert catalog_resolver.calls == 1


def test_stage_name_constant() -> None:
    assert ProviderAndToolsStage.name == "provider_and_tools_stage"
