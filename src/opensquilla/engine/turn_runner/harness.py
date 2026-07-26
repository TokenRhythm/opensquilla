"""TurnRunner harness scaffolding for TurnRunner stage decomposition.

Hosts the small adapter classes that bind ``TurnRunner`` instance methods
to the Protocol-shaped ports the stages consume. Each adapter
lazy-imports ``TurnRunner`` inside its method bodies to avoid the
runtime → turn_runner → runtime import cycle.

The full ``TurnRunnerHarness`` skeleton (the orchestrator that owns
``TurnContext`` and drives the ordered stage classes) can be introduced after
the stage boundaries are ready to sequence.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from opensquilla.attachment_workspace import (
    workspace_attachment_budget_from_config,
)
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    AgentConfigBuilderPort,
    AgentFactoryPort,
    MemorySnapshotPort,
    ModelCatalogPort,
    TimeoutBudgetPort,
    _AgentConfigAuxiliaries,
    _MemorySnapshotResult,
    _ResolvedBudgets,
    _ResolvedCatalog,
)
from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentMessageBuilderPort,
)
from opensquilla.engine.turn_runner.compaction_and_history_stage import (
    HistoryLoaderPort,
    PreflightCompactionPort,
    RequestContextPrependPort,
    T3UpgradeCompactionPort,
)
from opensquilla.engine.turn_runner.input_stage import ExtraContextResolver
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    MemoryFingerprintPort,
    PipelineExecutionPort,
    PromptAssemblerPort,
    PromptConfigResolverPort,
    PromptReportBuilderPort,
    RouterContextPort,
    RunPipelineRequest,
    SessionIdResolverPort,
)
from opensquilla.engine.turn_runner.provider_and_tools_stage import (
    ProviderResolverPort,
    SkillCatalogResolverPort,
    ToolBuilderPort,
)
from opensquilla.engine.turn_runner.stream_consumer_stage import (
    AgentRunPort,
    CompactionPersistPort,
    MemorySnapshotRefreshPort,
    MemorySyncNotifyPort,
    SystemPromptRefreshPort,
)
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    CostRollupResult,
    SessionTotalsPort,
    TranscriptAppendPort,
    TranscriptAppendResult,
    TurnErrorPersistPort,
    TurnMemoryCapturePort,
    UsageTelemetryPort,
)
from opensquilla.engine.usage_accounting import UsageExecutionContext
from opensquilla.provider.model_catalog import resolve_effective_context_window
from opensquilla.session.compaction_lifecycle import normalize_flush_triggers_strict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opensquilla.engine.agent import Agent, ToolHandler
    from opensquilla.engine.agent_injection import PendingInputProvider
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import AgentConfig, AgentEvent, DoneEvent, ErrorEvent
    from opensquilla.observability.prompt_report import PromptReport
    from opensquilla.observability.turn_call_log import TurnCallLogger
    from opensquilla.provider.protocol import LLMProvider
    from opensquilla.tools.types import ToolContext


def _coerce_flush_triggers(value: Any) -> list[str]:
    return list(normalize_flush_triggers_strict(value))


# ---------------------------------------------------------------------------
# Input stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerExtraContextAdapter(ExtraContextResolver):
    """Bind ``TurnRunner``'s two static extra-context helpers as a Protocol.

    Both helpers are ``@staticmethod`` on ``TurnRunner`` today, so the
    adapter does not need a runtime instance reference.
    """

    def extra_context_for(self, ctx: ToolContext | None) -> dict[str, str]:
        # Imported lazily to avoid a runtime → turn_runner → runtime cycle.
        from opensquilla.engine.runtime import TurnRunner

        return TurnRunner._extra_context_for_tool_context(ctx)

    def merge(
        self,
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None:
        from opensquilla.engine.runtime import TurnRunner

        return TurnRunner._merge_extra_prompt_context(base, extra)


# ---------------------------------------------------------------------------
# Provider/tools stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerProviderResolverAdapter(ProviderResolverPort):
    """Bind ``TurnRunner._resolve_provider`` as a Protocol-shaped port."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def resolve_provider(self) -> tuple[Any | None, Any | None]:
        return self._runner._resolve_provider()


class _TurnRunnerSkillCatalogResolverAdapter(SkillCatalogResolverPort):
    """Refresh and pin one immutable skill catalog for the turn."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def resolve_skill_catalog(self) -> Any | None:
        return await asyncio.to_thread(self._runner._resolve_skill_catalog)


class _TurnRunnerToolBuilderAdapter(ToolBuilderPort):
    """Bind ``TurnRunner._build_tools`` and the two ``ToolContext`` mutators.

        """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def with_artifact_context(
        self,
        ctx: ToolContext,
        session_key: str,
    ) -> ToolContext:
        return await self._runner._with_artifact_context(ctx, session_key)

    def with_runtime_write_callbacks(
        self,
        ctx: ToolContext,
        agent_id: str,
    ) -> ToolContext:
        return self._runner._with_runtime_write_callbacks(ctx, agent_id)

    def build_tools(
        self,
        ctx: ToolContext | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Any], ToolHandler | None]:
        return self._runner._build_tools(ctx, metadata=metadata)

    def build_tools_for_catalog(
        self,
        ctx: ToolContext | None,
        *,
        skill_catalog: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Any], ToolHandler | None]:
        if skill_catalog is None or "skill_catalog" not in inspect.signature(
            self._runner._build_tools
        ).parameters:
            return self._runner._build_tools(ctx, metadata=metadata)
        return self._runner._build_tools(
            ctx,
            metadata=metadata,
            skill_catalog=skill_catalog,
        )


# ---------------------------------------------------------------------------
# Prompt assembler stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerPromptAssemblerAdapter(PromptAssemblerPort):
    """Bind ``TurnRunner._assemble_prompt`` as a Protocol-shaped port."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def assemble_prompt(
        self,
        agent_id: str,
        tool_defs: list[Any],
        *,
        session_key: str | None,
        semantic_message: str | None,
        extra_context: dict[str, str] | None,
        prompt_metadata: dict[str, Any],
        bootstrap_context_mode: str | None,
        fresh_user_session: bool = False,
    ) -> str | tuple[str, str]:
        return self._runner._assemble_prompt(
            agent_id,
            tool_defs,
            session_key=session_key,
            semantic_message=semantic_message,
            extra_context=extra_context,
            prompt_metadata=prompt_metadata,
            bootstrap_context_mode=bootstrap_context_mode,
            fresh_user_session=fresh_user_session,
        )

class _TurnRunnerPipelineExecutionAdapter(PipelineExecutionPort):
    """Bind ``TurnRunner._run_pipeline`` and unpack ``RunPipelineRequest``.

        """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def run_pipeline(
        self,
        request: RunPipelineRequest,
    ) -> tuple[Any, Any]:
        from opensquilla.engine.runtime import _accepts_keyword_arg

        kwargs: dict[str, Any] = {
            "semantic_message": request.semantic_message,
            "ingress_pipeline_steps": request.ingress_pipeline_steps,
            "prev_assistant_text": request.prev_assistant_text,
            "prev_assistant_usage": request.prev_assistant_usage,
            "history_user_texts": request.history_user_texts,
            "history_has_recent_image": request.history_has_recent_image,
            "history_image_turn_count": request.history_image_turn_count,
            "vision_sticky_remaining": request.vision_sticky_remaining,
            "turns_since_last_image": request.turns_since_last_image,
            "last_image_turn_text": request.last_image_turn_text,
            "vision_candidate_turns": request.vision_candidate_turns,
            "flags_text_override": request.flags_text_override,
            "tool_context": request.tool_context,
            "normalization_metadata": request.normalization_metadata,
            "input_provenance": request.input_provenance,
            "skill_catalog": request.skill_catalog,
            "usage_execution_context": request.usage_execution_context,
            "provider_request_correlation": request.provider_request_correlation,
        }
        accepted_kwargs = {
            name: value
            for name, value in kwargs.items()
            if _accepts_keyword_arg(self._runner._run_pipeline, name)
        }
        return await self._runner._run_pipeline(
            request.runtime_message,
            request.session_key,
            request.provider,
            request.cloned_selector,
            request.tool_defs,
            request.base_prompt,
            request.attachments,
            **accepted_kwargs,
        )

class _TurnRunnerRouterContextAdapter(RouterContextPort):
    """Bind ``TurnRunner._router_previous_assistant_context``."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def fetch_router_context(
        self,
        session_key: str,
        *,
        exclude_last_user: bool,
        bound_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._runner._router_previous_assistant_context(
            session_key,
            exclude_last_user=exclude_last_user,
            bound_user_message_id=bound_user_message_id,
        )

class _TurnRunnerPromptConfigResolverAdapter(PromptConfigResolverPort):
    """Bind ``TurnRunner._resolve_prompt_config``."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def resolve_prompt_config(
        self,
        turn: Any,
    ) -> tuple[str, list[Any] | None, str | None]:
        return self._runner._resolve_prompt_config(turn)

class _PromptReportBuilderAdapter(PromptReportBuilderPort):
    """Pure shim around the module-level ``build_prompt_report`` helper.

    No runner reference needed — the helper is a free function. The lazy
    import keeps the harness module import-cycle-free.
    """

    def build_prompt_report(
        self,
        *,
        turn_id: str,
        session_key: str,
        session_id: str | None,
        agent_id: str,
        system_prompt: str,
        tool_defs: list[Any],
        metadata: dict[str, Any],
        tool_profile: str | None,
    ) -> PromptReport:
        from opensquilla.observability.prompt_report import build_prompt_report

        return build_prompt_report(
            turn_id=turn_id,
            session_key=session_key,
            session_id=session_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            metadata=metadata,
            tool_profile=tool_profile,
        )

class _TurnRunnerSessionIdResolverAdapter(SessionIdResolverPort):
    """Bind ``TurnRunner._resolve_session_id_for_log``."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def resolve_session_id_for_log(
        self,
        session_key: str,
    ) -> str | None:
        return await self._runner._resolve_session_id_for_log(session_key)

class _TurnRunnerMemoryFingerprintAdapter(MemoryFingerprintPort):
    """Bind ``TurnRunner._config.memory_mode_fingerprint`` defensively.

    Returns ``None`` when the config is absent, when the method is missing,
    or when the call raises — applying the defensive ``try/except``
    pattern used previously inline.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def memory_mode_fingerprint(self) -> dict[str, str] | None:
        config = self._runner._config
        if config is None:
            return None
        if not hasattr(config, "memory_mode_fingerprint"):
            return None
        try:
            return cast(dict[str, str] | None, config.memory_mode_fingerprint())
        except Exception:  # noqa: BLE001 - defensive
            return None


# ---------------------------------------------------------------------------
# Agent bootstrap stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerTimeoutBudgetAdapter(TimeoutBudgetPort):
    """Bind the five ``TurnRunner._resolve_agent_*`` helpers as a single port.

    The adapter composes the resolver chain in the order the inline body
    walks it. ``effective_runtime_timeout`` honors the per-call
    ``timeout`` override; the other four resolvers consume the per-call
    explicit override and the session/env/config fallback chain
    internally.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def resolve_budgets(
        self,
        *,
        session_key: str,
        timeout: float | None,
        max_iterations: int | None,
        iteration_timeout: float | None,
        tool_timeout: float | None,
        request_timeout: float | None,
        max_provider_retries: int | None,
    ) -> _ResolvedBudgets:
        runtime_timeout = (
            float(timeout)
            if timeout is not None
            else self._runner._resolve_agent_runtime_timeout(session_key)
        )
        self._runner._last_agent_max_iterations_source = "unknown"
        resolved_max_iterations = self._runner._resolve_agent_max_iterations(
            session_key, max_iterations
        )
        max_iterations_source = getattr(
            self._runner,
            "_last_agent_max_iterations_source",
            "unknown",
        )
        return _ResolvedBudgets(
            runtime_timeout=runtime_timeout,
            max_iterations=resolved_max_iterations,
            max_iterations_source=max_iterations_source,
            iteration_timeout=self._runner._resolve_agent_iteration_timeout(
                session_key, iteration_timeout
            ),
            tool_timeout=self._runner._resolve_agent_tool_timeout(
                session_key, tool_timeout
            ),
            request_timeout=self._runner._resolve_agent_request_timeout(
                session_key, request_timeout
            ),
            max_provider_retries=self._runner._resolve_agent_max_provider_retries(
                session_key, max_provider_retries
            ),
        )

def _positive_int_or_zero(value: Any) -> int:
    """Coerce a config value to a positive int, treating junk/negatives as 0."""
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return 0
    return coerced if coerced > 0 else 0


class _TurnRunnerModelCatalogAdapter(ModelCatalogPort):
    """Bind ``TurnRunner._model_catalog`` lookups with a None-fallback.

    Folds the ``self._model_catalog is None`` branch into the adapter so
    the stage body has no conditional on catalog presence.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def lookup(self, model_id: str, provider: str = "") -> _ResolvedCatalog:
        runner = self._runner
        llm_cfg = getattr(runner._config, "llm", None) if runner._config else None
        user_max_tokens = getattr(llm_cfg, "max_tokens", 0)
        # Explicit config override for the model context window. Zero (the
        # default) means "auto-resolve from the catalog". A positive value wins
        # over the catalog so operators can pin the real window for models the
        # catalog does not know (e.g. direct DashScope ids) — this feeds the
        # provider-context budget ladder, so a wrong default here makes
        # compaction fire far too early or too late.
        user_context_window = _positive_int_or_zero(
            getattr(llm_cfg, "context_window_tokens", 0)
        )
        # Explicit provider-request proof budget (chars). Positive values bypass
        # the derived context-budget ladder in ContextBudgetGovernor.from_values.
        user_proof_max_chars = _positive_int_or_zero(
            getattr(llm_cfg, "provider_request_proof_max_chars", 0)
        )
        if runner._model_catalog is not None:
            provider_name = provider or getattr(llm_cfg, "provider", "openrouter")
            base_url = getattr(llm_cfg, "base_url", "")
            max_tokens = runner._model_catalog.resolve_max_tokens(
                model_id, user_override=user_max_tokens, provider=provider_name
            )
            # Per-model [models.*] context_window overrides beat the global
            # llm.context_window_tokens value; the global still beats the catalog.
            context_window, _context_window_source = resolve_effective_context_window(
                runner._model_catalog,
                model_id,
                provider=provider_name,
                global_override=user_context_window,
            )
            capabilities = runner._model_catalog.get_capabilities(
                model_id, provider_name=provider_name, base_url=base_url
            )
        else:
            max_tokens = user_max_tokens if user_max_tokens > 0 else 16384
            context_window = user_context_window if user_context_window > 0 else 200_000
            capabilities = None
        return _ResolvedCatalog(
            max_tokens=max_tokens,
            context_window=context_window,
            capabilities=capabilities,
            temperature=getattr(llm_cfg, "temperature", None),
            top_p=getattr(llm_cfg, "top_p", None),
            provider_request_proof_max_chars=user_proof_max_chars,
        )

class _TurnRunnerAgentConfigBuilderAdapter(AgentConfigBuilderPort):
    """Bind the five ``TurnRunner`` helpers AgentConfig assembly needs.

    The inline body calls ``_resolve_turn_thinking(turn)``,
    ``_resolve_memory_source_dir(agent_id)``, and reads
    ``media_root_from_config(self._config) / "tool-results"`` plus a
    handful of ``getattr`` reads off ``_mem_cfg`` / ``_agent_token_cfg``.
    The adapter returns a typed ``_AgentConfigAuxiliaries`` value that
    the stage feeds straight into ``AgentConfig(...)``.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def build_auxiliaries(
        self,
        *,
        agent_id: str,
        session_key: str,
        session_id_for_log: str | None,
        turn: Any,
    ) -> _AgentConfigAuxiliaries:
        from opensquilla.paths import media_root_from_config

        runner = self._runner
        mem_cfg = getattr(runner._config, "memory", None) if runner._config else None
        agent_token_cfg = (
            getattr(runner._config, "agent_token_saving", None)
            if runner._config
            else None
        )
        compaction_cfg = (
            getattr(runner._config, "compaction", None)
            if runner._config
            else None
        )
        thinking = runner._resolve_turn_thinking(turn)
        return _AgentConfigAuxiliaries(
            thinking=thinking,
            flush_workspace_dir=str(runner._resolve_memory_source_dir(agent_id)),
            tool_result_store_dir=str(
                media_root_from_config(runner._config) / "tool-results"
            ),
            tool_result_store_session_id=session_id_for_log or session_key,
            flush_enabled=getattr(mem_cfg, "flush_enabled", False),
            flush_triggers=_coerce_flush_triggers(
                getattr(mem_cfg, "flush_triggers", None)
            ),
            flush_pre_compaction=getattr(mem_cfg, "flush_pre_compaction", False),
            flush_timeout_seconds=getattr(mem_cfg, "flush_timeout_seconds", 15.0),
            flush_background_timeout_seconds=getattr(
                mem_cfg, "flush_background_timeout_seconds", 120.0
            ),
            flush_backoff_initial_seconds=getattr(
                mem_cfg, "flush_backoff_initial_seconds", 30.0
            ),
            flush_backoff_max_seconds=getattr(
                mem_cfg, "flush_backoff_max_seconds", 300.0
            ),
            flush_archive_max_bytes=getattr(
                mem_cfg, "flush_archive_max_bytes", 800_000
            ),
            flush_compaction_requires_safe_receipt=getattr(
                mem_cfg,
                "flush_compaction_requires_safe_receipt",
                False,
            ),
            flush_compaction_safety_mode=getattr(
                mem_cfg,
                "flush_compaction_safety_mode",
                "protect",
            ),
            compaction_profile=getattr(
                compaction_cfg,
                "compaction_profile",
                "conversation",
            ),
            compaction_protected_recent_messages=getattr(
                compaction_cfg,
                "protected_recent_messages",
                0,
            ),
            tool_result_projection_max_inline_chars=getattr(
                agent_token_cfg,
                "tool_result_projection_max_inline_chars",
                60_000,
            ),
            tool_result_fresh_diagnostic_policy_enabled=getattr(
                agent_token_cfg,
                "tool_result_fresh_diagnostic_policy_enabled",
                False,
            ),
            tool_result_diagnostic_retrieval_gate_enabled=getattr(
                agent_token_cfg,
                "tool_result_diagnostic_retrieval_gate_enabled",
                False,
            ),
            tool_result_fresh_diagnostic_inline_max_chars=getattr(
                agent_token_cfg,
                "tool_result_fresh_diagnostic_inline_max_chars",
                64_000,
            ),
            tool_result_dispatch_max_chars=getattr(
                agent_token_cfg,
                "tool_result_dispatch_max_chars",
                0,
            ),
            tool_result_dispatch_turn_max_chars=getattr(
                agent_token_cfg,
                "tool_result_dispatch_turn_max_chars",
                0,
            ),
            tool_result_store_full_trace=getattr(
                agent_token_cfg,
                "tool_result_store_full_trace",
                False,
            ),
            tool_result_store_max_bytes=getattr(
                agent_token_cfg,
                "tool_result_store_max_bytes",
                8 * 1024 * 1024,
            ),
            tool_result_store_disk_budget_bytes=getattr(
                agent_token_cfg,
                "tool_result_store_disk_budget_bytes",
                256 * 1024 * 1024,
            ),
            tool_result_store_retention_seconds=getattr(
                agent_token_cfg,
                "tool_result_store_retention_seconds",
                7 * 24 * 60 * 60,
            ),
            source_diff_preservation_mode=getattr(
                runner._config,
                "source_diff_preservation_mode",
                "log",
            ),
            source_diff_candidate_mode=getattr(
                runner._config,
                "source_diff_candidate_mode",
                "log",
            ),
            runtime_state_capsule_mode=getattr(
                runner._config,
                "runtime_state_capsule_mode",
                "off",
            ),
            text_only_tool_recovery_mode=getattr(
                runner._config,
                "text_only_tool_recovery_mode",
                "off",
            ),
            finalize_evidence_gate=bool(
                getattr(
                    getattr(runner._config, "prompt", None),
                    "finalize_evidence_gate",
                    False,
                )
            ),
        )

class _TurnRunnerMemorySnapshotAdapter(MemorySnapshotPort):
    """Bind the per-agent memory warm + per-(agent_id, session_key) snapshot capture.

    The adapter wraps the runner's ``_memory_sync_managers`` lookup +
    ``warm_session`` call + ``_memory_snapshots`` dict mutation. The
    contract is narrow: this writer fires exactly once on the first
    turn when ``private_memory_allowed`` is true. The dict has other
    writers elsewhere in the runner (notably ``refresh_memory_snapshot``
    and the in-turn compaction-refresh path) that this adapter does
    NOT replace and does NOT need to coordinate with.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def warm_and_capture(
        self,
        *,
        agent_id: str,
        session_key: str,
    ) -> _MemorySnapshotResult:
        from opensquilla.engine.runtime import MemorySnapshot
        from opensquilla.session.keys import (
            allows_private_memory_prompt_injection,
        )

        runner = self._runner
        sync_manager = (
            runner._memory_sync_managers.get(agent_id)
            if runner._memory_sync_managers
            else None
        )
        if sync_manager is not None:
            await sync_manager.warm_session(session_key)

        private_memory_allowed = allows_private_memory_prompt_injection(session_key)
        snap_key = (agent_id, session_key)
        if private_memory_allowed and snap_key not in runner._memory_snapshots:
            workspace = runner._resolve_memory_source_dir(agent_id)
            runner._memory_snapshots[snap_key] = MemorySnapshot(
                memory_md=runner._load_memory_md(workspace),
                daily_notes=runner._load_daily_notes(workspace),
            )
        return _MemorySnapshotResult(
            sync_manager=sync_manager,
            private_memory_allowed=private_memory_allowed,
        )

class _TurnRunnerAgentFactoryAdapter(AgentFactoryPort):
    """Bind the typed ``Agent(...)`` constructor.

    The adapter injects the runner-singleton dependencies
    (``usage_tracker``, ``session_flush_service``) so the stage never
    sees those runtime attributes directly.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def build(
        self,
        *,
        provider: LLMProvider,
        config: AgentConfig,
        tool_definitions: list[Any],
        tool_handler: ToolHandler | None,
        session_key: str,
        turn_call_logger: TurnCallLogger | None,
        memory_sync_manager: Any | None,
        tool_context: ToolContext | None,
        turn_id: str = "",
        session_id: str | None = None,
        session_epoch: int = 0,
        agent_id: str = "",
        run_kind: str = "agent",
        provider_request_correlation: Any | None = None,
    ) -> Agent:
        from opensquilla.engine.agent import Agent

        # Embedding hosts and older test adapters may provide the historical
        # runner surface without the additive ledger dependency.  Treat that
        # exactly like an explicitly disabled sink so the upgrade remains
        # source-compatible outside the Gateway bootstrap.
        usage_event_sink = getattr(self._runner, "_usage_event_sink", None)
        usage_execution_context = None
        if usage_event_sink is not None:
            execution_id = turn_id or uuid.uuid4().hex
            usage_execution_context = UsageExecutionContext(
                execution_id=execution_id,
                agent_run_id=execution_id,
                turn_id=turn_id or None,
                session_id=session_id,
                session_epoch=max(0, int(session_epoch)),
                agent_id=agent_id,
                run_kind=run_kind or "agent",
            )
        return Agent(
            provider=provider,
            config=config,
            tool_definitions=tool_definitions,
            tool_handler=tool_handler,
            usage_tracker=self._runner._usage_tracker,
            session_key=session_key,
            turn_call_logger=turn_call_logger,
            memory_sync_manager=memory_sync_manager,
            session_flush_service=self._runner._session_flush_service,
            tool_registry=self._runner._tool_registry,
            tool_context=tool_context,
            usage_event_sink=usage_event_sink,
            usage_execution_context=usage_execution_context,
            provider_request_correlation=provider_request_correlation,
        )


# ---------------------------------------------------------------------------
# Compaction/history stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerT3UpgradeCompactionAdapter(T3UpgradeCompactionPort):
    """Bind ``TurnRunner._maybe_compact_on_t3_upgrade`` as a Protocol port.

    Forwards positional + keyword arguments verbatim. The helper
    handles its own ``asyncio.CancelledError`` re-raise and the
    log-and-record swallow for other exceptions; the adapter preserves
    that contract by not adding any try/except.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def maybe_compact(
        self,
        *,
        session_key: str,
        turn: Any,
        context_window_tokens: int,
        compaction_provider: Any | None,
        compaction_model: str | None,
        provider_request_correlation: Any | None = None,
    ) -> str:
        from opensquilla.engine.runtime import _accepts_keyword_arg

        correlation_kwargs: dict[str, Any] = {}
        if _accepts_keyword_arg(
            self._runner._maybe_compact_on_t3_upgrade,
            "provider_request_correlation",
        ):
            correlation_kwargs["provider_request_correlation"] = (
                provider_request_correlation
            )
        return await self._runner._maybe_compact_on_t3_upgrade(
            session_key,
            turn,
            context_window_tokens,
            compaction_provider=compaction_provider,
            compaction_model=compaction_model,
            **correlation_kwargs,
        )

class _TurnRunnerPreflightCompactionAdapter(PreflightCompactionPort):
    """Bind ``TurnRunner._maybe_preflight_compact`` as a Protocol port.

    Forwards positional + keyword arguments verbatim. Same exception
    contract as the T3 adapter.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def maybe_compact(
        self,
        *,
        session_key: str,
        context_window_tokens: int,
        compaction_provider: Any | None,
        compaction_model: str | None,
        provider_request_correlation: Any | None = None,
    ) -> None:
        from opensquilla.engine.runtime import _accepts_keyword_arg

        correlation_kwargs: dict[str, Any] = {}
        if _accepts_keyword_arg(
            self._runner._maybe_preflight_compact,
            "provider_request_correlation",
        ):
            correlation_kwargs["provider_request_correlation"] = (
                provider_request_correlation
            )
        await self._runner._maybe_preflight_compact(
            session_key,
            context_window_tokens,
            compaction_provider=compaction_provider,
            compaction_model=compaction_model,
            **correlation_kwargs,
        )

class _TurnRunnerHistoryLoaderAdapter(HistoryLoaderPort):
    """Bind ``TurnRunner._load_history`` as a Protocol port.

    The helper mutates ``agent._history`` via
    ``agent.set_history`` internally; the adapter preserves that.
    Exceptions propagate (no surrounding
    try/except).
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def load(
        self,
        *,
        agent: Agent,
        session_key: str,
        trim_last_user: bool,
        bound_user_message_id: str | None = None,
    ) -> str | None:
        return await self._runner._load_history(
            agent,
            session_key,
            trim_last_user=trim_last_user,
            bound_user_message_id=bound_user_message_id,
        )

class _RequestContextPrependAdapter(RequestContextPrependPort):
    """Pure shim around the module-level ``_prepend_request_context_prompt``.

    No runner reference needed — the helper is a free function. The
    lazy import keeps the harness module import-cycle-free.
    """

    def prepend(
        self,
        *,
        existing: str | None,
        prepended: str | None,
    ) -> str | None:
        from opensquilla.engine.runtime import _prepend_request_context_prompt

        return _prepend_request_context_prompt(existing, prepended)


# ---------------------------------------------------------------------------
# Stream consumer stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerAgentRunAdapter(AgentRunPort):
    """Bind ``agent.run_turn(turn_input, extra_messages=..., **kwargs)``.

    Folds ``_accepts_keyword_arg(agent.run_turn, ...)`` introspection inside
    the adapter so the stage body never imports ``inspect``. Optional keywords
    are forwarded only when the agent accepts them; otherwise the call uses the
    older invocation. The agent is supplied per-call so the stage can be
    instantiated once and reused across turns.
    """

    def run_turn(
        self,
        agent: Agent,
        *,
        turn_input: str,
        extra_messages: list[Any] | None,
        semantic_message: str | None,
        pending_input_provider: PendingInputProvider | None = None,
    ) -> AsyncIterator[AgentEvent]:
        from opensquilla.engine.runtime import _accepts_keyword_arg

        kwargs: dict[str, Any] = {}
        if _accepts_keyword_arg(agent.run_turn, "semantic_message"):
            kwargs["semantic_message"] = semantic_message
        if _accepts_keyword_arg(agent.run_turn, "pending_input_provider"):
            kwargs["pending_input_provider"] = pending_input_provider
        return agent.run_turn(
            turn_input,
            extra_messages=extra_messages,
            **kwargs,
        )

class _TurnRunnerCompactionPersistAdapter(CompactionPersistPort):
    """Bind ``SessionManager.persist_compaction_result`` + ``notify_compaction``.

    Compaction refresh: the adapter forwards the persist call verbatim and
    follows it with a completed lifecycle notification. Failed persistence
    is handled by the stream consumer stage so completed is never emitted
    before durable storage succeeds. The re-entrancy contract on
    ``persist_compaction_result`` is untouched.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def persist_and_notify(
        self,
        *,
        session_key: str,
        summary: str,
        kept_entries: list[Any],
        compaction_id: str | None = None,
    ) -> None:
        from opensquilla.engine.cache_break_monitor import notify_compaction
        from opensquilla.session.compaction_lifecycle import (
            COMPACTION_PERSISTED_EVENT,
            compaction_effect_payload,
            compaction_lifecycle_payload,
            new_compaction_id,
        )

        session_manager = self._runner._session_manager
        if session_manager is None:
            return
        persist_method = session_manager.persist_compaction_result
        params = inspect.signature(persist_method).parameters
        persist_kwargs: dict[str, Any] = {}
        if "compaction_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            persist_kwargs["compaction_id"] = compaction_id
        if "trigger_reason" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            persist_kwargs["trigger_reason"] = "agent_inline_overflow"
        async with self._runner._session_write_context(session_key):
            await persist_method(
                session_key,
                summary,
                kept_entries,
                **persist_kwargs,
            )
        compaction_id = compaction_id or new_compaction_id()
        notify_compaction(
            session_key,
            source="automatic",
            phase="agent_inline_overflow",
            status="completed",
            kept_count=len(kept_entries),
            summary_len=len(summary or ""),
            **compaction_effect_payload(status="completed"),
            **compaction_lifecycle_payload(
                compaction_id,
                COMPACTION_PERSISTED_EVENT,
            ),
        )

class _TurnRunnerMemorySnapshotRefreshAdapter(MemorySnapshotRefreshPort):
    """Refresh ``runner._memory_snapshots[(agent_id, session_key)]`` after compaction.

    Resolves the memory source dir, loads
    MEMORY.md + daily notes, writes the frozen snapshot. Respects
    ``private_memory_allowed`` -- when false, the dict is not written.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def refresh_snapshot(
        self,
        *,
        agent_id: str,
        session_key: str,
        private_memory_allowed: bool,
    ) -> None:
        from opensquilla.engine.runtime import MemorySnapshot

        runner = self._runner
        workspace = runner._resolve_memory_source_dir(agent_id)
        if private_memory_allowed:
            runner._memory_snapshots[(agent_id, session_key)] = MemorySnapshot(
                memory_md=runner._load_memory_md(workspace),
                daily_notes=runner._load_daily_notes(workspace),
            )

class _TurnRunnerSystemPromptRefreshAdapter(SystemPromptRefreshPort):
    """Rebuild + apply the cacheable system-prompt base after compaction.

    Compaction refresh: extracts the cacheable base from the
    ``(base, dynamic_suffix)`` tuple returned by ``_assemble_prompt``
    (when applicable) before invoking ``agent.refresh_system_prompt``.
    Feeding the tuple directly into ``ChatConfig.system`` would smuggle
    volatile bytes and raise ``ValidationError`` on the next turn.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def refresh_system_prompt(
        self,
        *,
        agent: Agent,
        agent_id: str,
        tool_defs: list[Any],
        session_key: str,
        bootstrap_context_mode: str | None,
    ) -> None:
        assembled = self._runner._assemble_prompt(
            agent_id,
            tool_defs,
            session_key=session_key,
            bootstrap_context_mode=bootstrap_context_mode,
        )
        refreshed_prompt = (
            assembled[0] if isinstance(assembled, tuple) else assembled
        )
        agent.refresh_system_prompt(refreshed_prompt)

class _TurnRunnerMemorySyncNotifyAdapter(MemorySyncNotifyPort):
    """Notify ``sync_manager.notify_message(byte_count)`` post-stream.

    The adapter folds the ``sync_manager is None`` guard so the stage
    body has no conditional. Byte count is the UTF-8 length of the
    effective runtime message.
    """

    def notify_message_bytes(
        self,
        sync_manager: Any | None,
        runtime_message: str,
    ) -> None:
        if sync_manager is None:
            return
        byte_count = len(runtime_message.encode("utf-8"))
        sync_manager.notify_message(byte_count)


# ---------------------------------------------------------------------------
# Attachment stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerAttachmentMessageBuilderAdapter(AttachmentMessageBuilderPort):
    """Bind ``TurnRunner._build_attachment_messages`` + media-root lookup.

    Forwards verbatim to the static helper and resolves
    ``media_root`` from the runner instance on every call. The stage
    body never sees the runner.

    Exception contract: the helper raises ``ValueError`` on
    validation failures (count cap, disallowed media type, ref without
    media_root, invalid base64, oversize). The adapter does NOT add a
    try/except — exceptions propagate to the stage which propagates
    them to the outer ``_run_turn`` terminal handler.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    def build(
        self,
        message: str,
        attachments: list[dict],
        *,
        workspace_dir: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[Any] | None:
        return self._runner._build_attachment_messages(
            message,
            attachments,
            media_root=self._runner._attachment_media_root(),
            workspace_dir=workspace_dir
            or getattr(self._runner._config, "workspace_dir", None),
            session_id=session_id,
            workspace_attachment_budget_bytes=(
                workspace_attachment_budget_from_config(self._runner._config)
            ),
        )


# ---------------------------------------------------------------------------
# Turn finalizer stage adapters
# ---------------------------------------------------------------------------

class _TurnRunnerTranscriptAppendAdapter(TranscriptAppendPort):
    """Bind ``SessionManager.append_message`` for the assistant turn persist.

    Folds two responsibilities into the adapter so the stage
    body has no ``inspect`` dependency and no ``session_manager is None``
    conditional:

    1. ``_accepts_keyword_arg(session_manager.append_message, "token_count")``
       introspection: passes ``token_count`` only when the manager
       accepts it.
    2. The ``session_manager is None`` guard: returns ``False`` (no
       append) instead of calling.

    Exceptions from ``append_message`` propagate to the stage and out
    to the outer ``_run_turn`` terminal handler -- there is
    no try/except here.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        tool_calls: list[Any] | None,
        reasoning_content: str | None,
        turn_usage: dict[str, Any] | None,
        token_count: int | None,
    ) -> TranscriptAppendResult:
        from opensquilla.engine.runtime import _accepts_keyword_arg

        session_manager = self._runner._session_manager
        if session_manager is None:
            return TranscriptAppendResult(appended=False)
        append_kwargs: dict[str, Any] = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
        }
        if reasoning_content is not None:
            append_kwargs["reasoning_content"] = reasoning_content
        if (
            turn_usage is not None
            and _accepts_keyword_arg(session_manager.append_message, "turn_usage")
        ):
            append_kwargs["turn_usage"] = turn_usage
        if _accepts_keyword_arg(session_manager.append_message, "token_count"):
            append_kwargs["token_count"] = token_count
        entry = await self._runner._append_session_message(session_key, **append_kwargs)
        raw_message_id = getattr(entry, "message_id", None)
        message_id = (
            raw_message_id
            if isinstance(raw_message_id, str) and raw_message_id
            else None
        )
        return TranscriptAppendResult(appended=True, message_id=message_id)

class _TurnRunnerTurnMemoryCaptureAdapter(TurnMemoryCapturePort):
    """Bind ``TurnRunner._capture_turn_memory`` as a Protocol port.

    Forwards verbatim. The log-and-continue try/except
    lives in the stage body, NOT the adapter -- the error-handling
    contract is visible in the stage code.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def capture_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        run_kind: str,
        no_memory_capture: bool,
    ) -> None:
        await self._runner._capture_turn_memory(
            agent_id=agent_id,
            session_key=session_key,
            runtime_message=runtime_message,
            final_text=final_text,
            input_mode=input_mode,
            tool_context=tool_context,
            input_provenance=input_provenance,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
        )

class _TurnRunnerSessionTotalsAdapter(SessionTotalsPort):
    """Roll up session token + cost + cache totals from a DoneEvent.

    Performs as a single transaction: ``get_session`` read,
    ``normalize_event_cost_source`` call, four ``next_*`` accumulators,
    ``rollup_cost_source`` call, ``Session.update`` write. Folds two
    early-return guards (``session_manager is None`` and
    ``current_session is None``) so the stage body has no conditional
    on either; the stage's outer try/except wraps the adapter call
    with log-and-continue semantics.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def rollup(
        self,
        *,
        session_key: str,
        done_event: DoneEvent,
        resolved_model: str,  # noqa: ARG002 - reserved for future model-pinning
    ) -> CostRollupResult | None:
        from opensquilla.session.cost_rollup import (
            normalize_event_cost_source,
            rollup_cost_source,
        )

        session_manager = self._runner._session_manager
        if session_manager is None:
            return None
        async with self._runner._session_write_context(session_key):
            current_session = await session_manager.get_session(session_key)
            if current_session is None:
                return None

            done_total_tokens = done_event.input_tokens + done_event.output_tokens
            event_cost_source = normalize_event_cost_source(
                done_event.cost_source,
                input_tokens=done_event.input_tokens,
                output_tokens=done_event.output_tokens,
                cache_read_tokens=done_event.cached_tokens,
                cache_write_tokens=done_event.cache_write_tokens,
                cost_usd=done_event.cost_usd,
                billed_cost_usd=done_event.billed_cost,
            )
            next_total_cost = (
                getattr(current_session, "total_cost_usd", 0.0) or 0.0
            ) + done_event.cost_usd
            next_billed_cost = (
                getattr(current_session, "billed_cost_usd", 0.0) or 0.0
            ) + done_event.billed_cost
            next_estimated_component = (
                getattr(current_session, "estimated_cost_component_usd", 0.0) or 0.0
            )
            if event_cost_source == "opensquilla_estimate":
                next_estimated_component += done_event.cost_usd
            elif event_cost_source == "mixed":
                next_estimated_component += max(
                    0.0,
                    done_event.cost_usd - done_event.billed_cost,
                )
            next_missing_entries = (
                getattr(current_session, "missing_cost_entries", 0) or 0
            )
            if event_cost_source == "unavailable":
                next_missing_entries += 1
            current_cost_source = str(
                getattr(current_session, "cost_source", "none") or "none"
            ).strip().lower()
            provider_billed_entries = int(
                current_cost_source in {"provider_billed", "mixed"}
            ) + int(event_cost_source in {"provider_billed", "mixed"})
            estimated_cost_entries = int(
                current_cost_source in {"opensquilla_estimate", "mixed"}
            ) + int(event_cost_source in {"opensquilla_estimate", "mixed"})
            next_cost_source = rollup_cost_source(
                billed_cost_usd=next_billed_cost,
                estimated_cost_component_usd=next_estimated_component,
                missing_cost_entries=next_missing_entries,
                provider_billed_entries=provider_billed_entries,
                estimated_cost_entries=estimated_cost_entries,
            )
            next_input_tokens = (
                getattr(current_session, "input_tokens", 0) or 0
            ) + done_event.input_tokens
            next_output_tokens = (
                getattr(current_session, "output_tokens", 0) or 0
            ) + done_event.output_tokens
            next_total_tokens = (
                getattr(current_session, "total_tokens", 0) or 0
            ) + done_total_tokens
            next_estimated_cost = (
                getattr(current_session, "estimated_cost_usd", 0.0) or 0.0
            ) + done_event.cost_usd
            next_cache_read = (
                getattr(current_session, "cache_read", 0) or 0
            ) + done_event.cached_tokens
            next_cache_write = (
                getattr(current_session, "cache_write", 0) or 0
            ) + done_event.cache_write_tokens
            next_model_override = done_event.model or getattr(
                current_session, "model_override", None
            )

            # Persist the last actual model into usage metadata only.
            # Writing it to session.model would pin future turns and
            # silently bypass squilla-router routing.
            await session_manager.update(
                session_key,
                input_tokens=next_input_tokens,
                output_tokens=next_output_tokens,
                total_tokens=next_total_tokens,
                total_tokens_fresh=True,
                estimated_cost_usd=next_estimated_cost,
                total_cost_usd=next_total_cost,
                billed_cost_usd=next_billed_cost,
                estimated_cost_component_usd=next_estimated_component,
                cost_source=next_cost_source,
                missing_cost_entries=next_missing_entries,
                cache_read=next_cache_read,
                cache_write=next_cache_write,
                model_override=next_model_override,
            )
        return CostRollupResult(
            input_tokens=next_input_tokens,
            output_tokens=next_output_tokens,
            total_tokens=next_total_tokens,
            estimated_cost_usd=next_estimated_cost,
            total_cost_usd=next_total_cost,
            billed_cost_usd=next_billed_cost,
            estimated_cost_component_usd=next_estimated_component,
            cost_source=next_cost_source,
            missing_cost_entries=next_missing_entries,
            cache_read=next_cache_read,
            cache_write=next_cache_write,
            model_override=next_model_override,
        )

class _TurnRunnerTurnErrorPersistAdapter(TurnErrorPersistPort):
    """Bind ``TurnRunner._persist_turn_error`` as a Protocol port.

    Forwards verbatim. The helper owns its own log-and-continue
    try/except and guards both ``session_manager is None`` and
    ``event is None`` internally, so the adapter and stage body have no
    additional guards.
    """

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def persist_error(
        self,
        *,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None:
        await self._runner._persist_turn_error(session_key, event)


class _TurnRunnerUsageTelemetryAdapter(UsageTelemetryPort):
    """Persist content-free counters for the gateway's hourly upload loop."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner

    async def record_turn(self, *, run_kind: str, done_event: DoneEvent | None) -> None:
        from opensquilla.observability.usage_telemetry import record_completed_turn

        session_manager = self._runner._session_manager
        if session_manager is None:
            return
        await record_completed_turn(
            session_manager.storage,
            config=self._runner._config,
            run_kind=run_kind,
            done_event=done_event,
        )
