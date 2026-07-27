"""Stage object for provider resolution + tool-handler construction.

Owns the source slice that previously lived inline at the top of
``TurnRunner._run_turn`` immediately after ``InputStage``. The harness
invokes ``ProviderAndToolsStage.run`` once per turn, AFTER InputStage and
BEFORE PromptAssemblyStage.
Side-effect contract: re-raises any exception from the tool registry or
provider selector exactly as the inline body did. The harness catches it
through the existing CancelledError / Exception terminal handlers in
``_run_turn``. ``ProviderAndToolsStage`` does NOT call any ``TurnHook``
or ``ToolHook`` directly — observability emit (the ``turn_error`` event
on provider-resolution failure) is performed by THE HARNESS upon
receiving a ``StageOutcome(terminate=True, early_yield=...)``, NOT by the
stage. This keeps the stage testable in isolation and the
trace-emit/persist sequence under harness control.

Asymmetry note: provider-resolution failure is modeled as an
``StageOutcome.terminate_with(ErrorEvent(...))`` because it is a known
configuration condition (no selector configured) with a stable
user-facing ``code="no_provider"``. Tool-build failure is NOT modeled as
an early-yield — it propagates as an exception because the code
does not catch tool-build exceptions and routes them through the generic
terminal handler at ``TurnRunner._run_turn``. Preserve this asymmetry unless
the tool-build failure contract changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.engine.agent import ToolHandler
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# Ports — narrow protocols so the stage is unit-testable without the full
# TurnRunner. The runtime adapters in ``harness.py`` bind these to the
# concrete TurnRunner methods.
# ---------------------------------------------------------------------------

@runtime_checkable
class ProviderResolverPort(Protocol):
    """Wraps ``TurnRunner._resolve_provider``.

    Returns ``(provider, cloned_selector)`` or ``(None, None)`` when no
    selector is configured. Mirror of the existing runtime helper — the
    stage never reaches into ``TurnRunner._provider_selector`` directly.
    """

    def resolve_provider(self) -> tuple[Any | None, Any | None]: ...


@runtime_checkable
class SkillCatalogResolverPort(Protocol):
    """Resolve the immutable skill catalog pinned to one turn."""

    async def resolve_skill_catalog(self) -> Any | None: ...

@runtime_checkable
class ToolBuilderPort(Protocol):
    """Wraps the production ``TurnRunner._build_tools`` chain plus the
    two ``ToolContext`` mutators it depends on.

    Three calls, one return value:

    - ``with_artifact_context`` mirrors ``_with_artifact_context`` (async).
    - ``with_runtime_write_callbacks`` mirrors
      ``_with_runtime_write_callbacks``.
    - ``build_tools`` mirrors ``_build_tools(ctx, metadata=...)``.

    Tool hooks are supported by the lower-level dispatch factory but are not
    registered by TurnRunner by default; callers that need them must supply a
    ToolBuilderPort that forwards ``tool_hooks=`` to ``build_tool_handler``.
    """

    async def with_artifact_context(
        self,
        ctx: ToolContext,
        session_key: str,
    ) -> ToolContext: ...

    def with_runtime_write_callbacks(
        self,
        ctx: ToolContext,
        agent_id: str,
    ) -> ToolContext: ...

    def build_tools(
        self,
        ctx: ToolContext | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Any], ToolHandler | None]: ...

# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen — stage outputs are immutable values
# the harness accumulates onto TurnContext)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderAndToolsStageInput:
    """Inputs the ``ProviderAndToolsStage`` needs at the boundary it owns.

    Mirrors the locals visible to the original inline slice at the point
    InputStage has finished. ``run_kind`` and ``input_mode`` are carried
    through for the harness's observability payload on provider-resolve
    failure; the stage body itself does not consume them.
    """

    session_key: str
    agent_id: str
    tool_context: ToolContext | None
    run_kind: str
    input_mode: str

@dataclass(frozen=True)
class ProviderAndToolsStageOutput:
    """The pieces of state ``PromptAssemblyStage`` and downstream consume.

    - ``provider``: the resolved provider instance (never None on success).
    - ``cloned_selector``: the cloned selector used for provider resolve;
      threaded into ``_run_pipeline`` to avoid shared-state races.
    - ``tool_defs``: filtered tool definition list used by prompt assembly
      and the agent loop.
    - ``tool_handler``: the dispatch handler produced by
      ``build_tool_handler`` (None when no registry is configured).
    - ``effective_tool_context``: the ``ToolContext`` after artifact-context
      and runtime-write-callback enrichment. Subsequent stages
      (PromptAssemblyStage, AgentBootstrapStage) consume this enriched
      form, NOT the input ``tool_context``.
    - ``tool_metadata``: the metadata dict ``_build_tools`` populates with
      ``tool_profile``. Threaded into the prompt-report on.
    """

    provider: Any
    cloned_selector: Any
    tool_defs: list[Any]
    tool_handler: ToolHandler | None
    effective_tool_context: ToolContext | None
    tool_metadata: dict[str, Any] = field(default_factory=dict)
    skill_catalog: Any | None = None

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class ProviderAndToolsStage:
    """Resolve provider + build tool definitions / handler.

    Stable boundary: runs ONCE per turn, after InputStage and before
    PromptAssemblyStage. Pure with respect to its inputs except for:

    - ``tool_builder.with_artifact_context`` — async filesystem read for
      the artifact media root + session-id resolution. Idempotent.
    - ``tool_builder.with_runtime_write_callbacks`` — pure ``ToolContext``
      replace; no side effect.
    - ``tool_builder.build_tools`` — synchronous registry read; no side
      effect.

    Exception model: re-raises any exception from the ports. The harness
    catches it through the existing CancelledError / Exception terminal
    handlers in ``_run_turn``. Tool-build *exceptions* propagate as-is;
    the registry returning ``([], None)`` is interpreted as "no tools"
    (success with empty defs), matching today's inline behavior.
    """

    name = "provider_and_tools_stage"

    def __init__(
        self,
        *,
        provider_resolver: ProviderResolverPort,
        tool_builder: ToolBuilderPort,
        skill_catalog_resolver: SkillCatalogResolverPort | None = None,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._tool_builder = tool_builder
        self._skill_catalog_resolver = skill_catalog_resolver

    async def run(
        self,
        inp: ProviderAndToolsStageInput,
    ) -> StageOutcome[ProviderAndToolsStageOutput]:
        # Local imports keep the module import-cycle-free.
        from opensquilla.engine.turn_runner.outcome import StageOutcome
        from opensquilla.engine.types import ErrorEvent

        # 1. Resolve the immutable skill snapshot first. Tool exposure and
        # downstream skill selection must observe this exact generation for
        # the whole turn, even if another request publishes a newer catalog.
        skill_catalog = None
        if self._skill_catalog_resolver is not None:
            skill_catalog = await self._skill_catalog_resolver.resolve_skill_catalog()

        # 2. Resolve provider (clone to avoid shared state race)
        provider, cloned_selector = self._provider_resolver.resolve_provider()
        if provider is None:
            # Construct the same ErrorEvent shape the original inline body
            # produces. The harness emits the turn_error trace + persists
            # the error after consuming this outcome.
            return StageOutcome.terminate_with(
                ErrorEvent(
                    message="No provider available",
                    code="no_provider",
                )
            )

        # 3. Build tools (filtered by tool_context)
        effective_ctx = inp.tool_context
        if effective_ctx is not None:
            effective_ctx = await self._tool_builder.with_artifact_context(
                effective_ctx, inp.session_key
            )
            effective_ctx = self._tool_builder.with_runtime_write_callbacks(
                effective_ctx, inp.agent_id
            )
            effective_ctx = replace(effective_ctx, skill_catalog=skill_catalog)

        tool_metadata: dict[str, Any] = {}
        build_for_catalog = getattr(self._tool_builder, "build_tools_for_catalog", None)
        if callable(build_for_catalog):
            tool_defs, tool_handler = build_for_catalog(
                effective_ctx,
                skill_catalog=skill_catalog,
                metadata=tool_metadata,
            )
        else:
            # Compatibility for existing external/test ToolBuilderPort
            # implementations that predate catalog snapshots.
            tool_defs, tool_handler = self._tool_builder.build_tools(
                effective_ctx, metadata=tool_metadata
            )

        return StageOutcome.success(
            ProviderAndToolsStageOutput(
                provider=provider,
                cloned_selector=cloned_selector,
                tool_defs=tool_defs,
                tool_handler=tool_handler,
                effective_tool_context=effective_ctx,
                tool_metadata=tool_metadata,
                skill_catalog=skill_catalog,
            )
        )
