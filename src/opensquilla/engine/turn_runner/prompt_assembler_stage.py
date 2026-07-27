"""Stage object for prompt assembly + pre-turn pipeline execution.

Owns the source slice that previously lived inline at the top of
``TurnRunner._run_turn`` between the provider/tools stage boundary and the
runtime-budget resolve. The harness invokes ``PromptAssemblerStage.run``
once per turn, AFTER ProviderAndToolsStage and BEFORE AgentBootstrapStage.
Side-effect contract: re-raises any exception from the prompt-assembly
helpers, the pre-turn pipeline, or the prompt-finalization helper exactly
as the inline body did. The harness catches it through the existing
CancelledError / Exception terminal handlers in ``_run_turn``.
``PromptAssemblerStage`` does NOT call any ``TurnHook`` directly —
observability emit (``turn_call_logger.write("prompt_report", ...)`` and
``turn_call_logger.write("turn_start", ...)``) is performed by THE HARNESS
upon receiving the ``StageOutcome``. The stage returns the prompt-report
dict and the model-resolve fields as part of its output; the harness
writes them.

NEVER terminates. Always returns ``StageOutcome.success(...)``. The
``StageOutcome`` shape is preserved for forward-compatibility with a
future prompt-failure early-yield branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opensquilla.provider.protocol import configured_provider_id

if TYPE_CHECKING:
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.observability.decision_log import PipelineStepRecord
    from opensquilla.observability.prompt_report import PromptReport
    from opensquilla.provider.types import ProviderRequestCorrelation
    from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# RunPipelineRequest — typed dataclass that folds the 6 inline
# ``_accepts_keyword_arg(self._run_pipeline, ...)`` introspection calls.
#
# All fields are optional because the pre-turn pipeline accepts every one of
# them as a keyword today; the stage consistently passes the value or
# ``None`` and the pipeline branches on truthiness internally.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunPipelineRequest:
    """Typed input for ``PipelineExecutionPort.run_pipeline``.

    Folds the 6 inline ``_accepts_keyword_arg(self._run_pipeline, name)``
    introspection branches into a single typed payload. The stage builds
    this once, the port consumes it once. The introspection cleanup is
    bounded to the prompt-assembler slice — ``_accepts_keyword_arg`` stays
    in ``runtime.py`` because two other call sites (``agent.run_turn`` and
    ``session_manager.append_message``) still rely on it.
    """

    runtime_message: str
    session_key: str
    provider: Any
    cloned_selector: Any
    tool_defs: list[Any]
    base_prompt: str | tuple[str, str]
    attachments: list[dict[str, Any]]
    semantic_message: str | None = None
    ingress_pipeline_steps: list[PipelineStepRecord] | None = None
    prev_assistant_text: str | None = None
    prev_assistant_usage: dict[str, Any] | None = None
    history_user_texts: list[str] | None = None
    history_has_recent_image: bool = False
    history_image_turn_count: int = 0
    vision_sticky_remaining: int = 0
    turns_since_last_image: int | None = None
    last_image_turn_text: str | None = None
    vision_candidate_turns: int = 0
    flags_text_override: str | None = None
    tool_context: ToolContext | None = None
    normalization_metadata: dict[str, Any] | None = None
    input_provenance: dict[str, Any] | str | None = None
    skill_catalog: Any | None = None
    usage_execution_context: Any | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )

# ---------------------------------------------------------------------------
# Ports — narrow Protocols so the stage is unit-testable without the full
# TurnRunner. The runtime adapters in ``harness.py`` bind these to the
# concrete TurnRunner methods (or to the module-level helper for prompt
# report).
# ---------------------------------------------------------------------------

@runtime_checkable
class PromptAssemblerPort(Protocol):
    """Wraps ``TurnRunner._assemble_prompt``.

    Returns ``str`` for the prompt-cache-stable case; returns
    ``(base, dynamic_context)`` only when daily notes, workspace files, or
    tool-context blocks need to stay outside the cacheable prefix.
    """

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
    ) -> str | tuple[str, str]: ...

@runtime_checkable
class PipelineExecutionPort(Protocol):
    """Wraps ``TurnRunner._run_pipeline``.

    The port forwards a typed ``RunPipelineRequest`` to the underlying
    helper. The adapter at the harness side unpacks the request into the
    helper's keyword arguments. The 6 inline ``_accepts_keyword_arg``
    introspection branches are eliminated in favor of always passing every
    kwarg — the helper accepts all 6 today; the introspection is dead code.
    """

    async def run_pipeline(
        self,
        request: RunPipelineRequest,
    ) -> tuple[Any, Any]: ...

@runtime_checkable
class RouterContextPort(Protocol):
    """Wraps ``TurnRunner._router_previous_assistant_context``.

    Async because the underlying helper awaits ``get_transcript`` if it is
    awaitable. Returns the pipeline-router context dict
    (``prev_assistant_text``, ``prev_assistant_usage``,
    ``history_user_texts``) or empty dict when no SessionManager is
    configured.
    """

    async def fetch_router_context(
        self,
        session_key: str,
        *,
        exclude_last_user: bool,
        bound_user_message_id: str | None = None,
    ) -> dict[str, Any]: ...

@runtime_checkable
class PromptConfigResolverPort(Protocol):
    """Wraps ``TurnRunner._resolve_prompt_config``.

    Returns the ``(final_prompt, cache_breakpoints, request_context_prompt)``
    triple from the post-pipeline TurnContext.
    """

    def resolve_prompt_config(
        self,
        turn: Any,
    ) -> tuple[str, list[Any] | None, str | None]: ...

@runtime_checkable
class PromptReportBuilderPort(Protocol):
    """Wraps ``opensquilla.observability.prompt_report.build_prompt_report``.

    Pure function port — no instance state. Bound at the harness side as a
    thin shim around the module-level helper. Stage uses it to build the
    ``PromptReport`` carried in the output (the harness later writes it
    through ``turn_call_logger``).
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
    ) -> PromptReport: ...

@runtime_checkable
class SessionIdResolverPort(Protocol):
    """Wraps ``TurnRunner._resolve_session_id_for_log``.

    Async because the underlying helper awaits SessionManager
    asynchronously. Returns the durable session_id string or ``None`` when
    not yet persisted.
    """

    async def resolve_session_id_for_log(
        self,
        session_key: str,
    ) -> str | None: ...

@runtime_checkable
class MemoryFingerprintPort(Protocol):
    """Wraps ``TurnRunner._config.memory_mode_fingerprint`` if present.

    Optional — the inline body swallows exceptions and skips the merge if
    the config does not expose the method. The port mirrors that defensive
    behavior. Returning ``None`` means "skip the merge".
    """

    def memory_mode_fingerprint(self) -> dict[str, str] | None: ...

# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptAssemblerStageInput:
    """Inputs the ``PromptAssemblerStage`` needs at the boundary it owns.

    Mirrors the locals visible to the original inline slice at the point
    ``ProviderAndToolsStage`` has finished. The 6 fields below
    (``provider``, ``cloned_selector``, ``tool_defs``,
    ``effective_tool_context``, ``tool_metadata``, ``runtime_message``) come
    from the prior two stages. Everything else is per-turn input visible at
    the call site of ``_run_turn``.
    """

    # From ProviderAndToolsStage / InputStage
    runtime_message: str
    semantic_input: str
    extra_prompt_context: dict[str, str] | None
    provider: Any
    cloned_selector: Any
    tool_defs: list[Any]
    effective_tool_context: ToolContext | None
    tool_metadata: dict[str, Any]

    # Per-turn inputs from _run_turn locals
    session_key: str
    agent_id: str
    turn_id: str
    attachments: list[dict[str, Any]]
    bootstrap_context_mode: str | None
    model: str | None
    history_has_persisted_user: bool
    persist_input: bool
    fresh_user_session: bool = False
    bound_user_message_id: str | None = None
    ingress_pipeline_steps: list[PipelineStepRecord] | None = None
    normalization_metadata: dict[str, Any] | None = None
    input_provenance: dict[str, Any] | str | None = None
    skill_catalog: Any | None = None
    usage_execution_context: Any | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )

@dataclass(frozen=True)
class PromptAssemblerStageOutput:
    """The pieces of state ``AgentBootstrapStage`` and downstream consume.

    - ``provider``: provider AFTER possible model override + selector
      fallback wrapping (NOT the input provider — the stage may re-resolve
      via ``cloned_selector.override_model(model)`` and wrap with
      ``_SelectorFallbackProvider``).
    - ``turn``: the post-pipeline pipeline ``TurnContext`` (its ``metadata``
      carries ``memory_mode_fingerprint``, ``tool_profile``, cache info,
      etc.). Downstream stages read ``turn.metadata`` and ``turn.tool_defs``.
    - ``effective_runtime_message``: the message string the agent loop
      should pass to the provider — may be the post-pipeline-rewritten
      ``turn.message`` (e.g. squilla router rewrites).
    - ``final_prompt``: the resolved system prompt string.
    - ``cache_breakpoints``: the cache-break list for the provider call,
      or ``None``.
    - ``request_context_prompt``: the dynamic context for non-cached
      prefix, or ``None``.
    - ``resolved_model``: the final model id (explicit > pipeline >
      selector).
    - ``provider_name``: the provider's name attribute or class name.
    - ``session_id_for_log``: the durable session_id for trace_context.
    - ``trace_context_session_id``: the same value, surfaced explicitly so
      the harness can ``replace(trace_context, session_id=...)``.
    - ``prompt_report``: the freshly-built ``PromptReport`` instance. The
      harness writes this through ``turn_call_logger`` if logging is
      enabled.
    - ``selector_model``: the selector's ``current_config.model`` or empty
      string (for the ``log.debug("turn_runner.model_resolved", ...)``
      payload the harness emits).
    - ``squilla_router_tier``: the value of
      ``turn.metadata.get("routed_tier")`` (same purpose).
    """

    provider: Any
    turn: Any
    effective_runtime_message: str
    final_prompt: str
    cache_breakpoints: list[Any] | None
    request_context_prompt: str | None
    resolved_model: str
    provider_name: str
    session_id_for_log: str | None
    trace_context_session_id: str | None
    prompt_report: PromptReport
    selector_model: str
    squilla_router_tier: Any = None

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class PromptAssemblerStage:
    """Assemble identity prompt + run pre-turn pipeline + finalize prompt.

    Stable boundary: runs ONCE per turn, after ``ProviderAndToolsStage`` and
    before ``AgentBootstrapStage``. Pure with respect to its inputs except
    for:

    - ``prompt_assembler.assemble_prompt`` — synchronous filesystem read for
      workspace files via the bootstrap snapshot cache; idempotent.
    - ``router_context.fetch_router_context`` — async SessionManager
      transcript read; idempotent.
    - ``pipeline_executor.run_pipeline`` — runs the pre-turn pipeline.
      Mutates the cloned selector via ``override_model`` if the squilla
      router routes a different model.
    - ``session_id_resolver.resolve_session_id_for_log`` — async
      SessionManager read.
    - ``memory_fingerprint.memory_mode_fingerprint`` — config method call,
      defensive (port returns ``None`` to mean "skip").

    Exception model: re-raises any exception from the ports. The harness
    catches it through the existing CancelledError / Exception terminal
    handlers in ``_run_turn``.

    Tool-context observability: the stage does NOT emit any ``TurnHook``
    today. The two ``turn_call_logger.write`` calls and the
    ``log.debug("turn_runner.model_resolved", ...)`` happen at the harness
    side after the stage returns — the harness has the ``trace_context``
    and the ``turn_call_logger`` instance and is the natural caller.
    """

    name = "prompt_assembler_stage"

    def __init__(
        self,
        *,
        prompt_assembler: PromptAssemblerPort,
        pipeline_executor: PipelineExecutionPort,
        router_context: RouterContextPort,
        prompt_config_resolver: PromptConfigResolverPort,
        prompt_report_builder: PromptReportBuilderPort,
        session_id_resolver: SessionIdResolverPort,
        memory_fingerprint: MemoryFingerprintPort,
    ) -> None:
        self._prompt_assembler = prompt_assembler
        self._pipeline_executor = pipeline_executor
        self._router_context = router_context
        self._prompt_config_resolver = prompt_config_resolver
        self._prompt_report_builder = prompt_report_builder
        self._session_id_resolver = session_id_resolver
        self._memory_fingerprint = memory_fingerprint

    async def run(
        self,
        inp: PromptAssemblerStageInput,
    ) -> StageOutcome[PromptAssemblerStageOutput]:
        # Local imports keep the module import-cycle-free.
        from opensquilla.engine.turn_runner.outcome import StageOutcome

        # 1. Assemble identity prompt
        prompt_metadata: dict[str, Any] = {}
        base_prompt = self._prompt_assembler.assemble_prompt(
            inp.agent_id,
            inp.tool_defs,
            session_key=inp.session_key,
            semantic_message=inp.semantic_input,
            extra_context=inp.extra_prompt_context,
            prompt_metadata=prompt_metadata,
            bootstrap_context_mode=inp.bootstrap_context_mode,
            fresh_user_session=inp.fresh_user_session,
        )

        # 2. Fetch router context (transcript-driven)
        router_context = await self._router_context.fetch_router_context(
            inp.session_key,
            exclude_last_user=(
                inp.history_has_persisted_user or inp.persist_input
            ),
            bound_user_message_id=inp.bound_user_message_id,
        )

        raw_history_image_turn_count = router_context.get("history_image_turn_count")
        history_image_turn_count = (
            raw_history_image_turn_count
            if isinstance(raw_history_image_turn_count, int)
            else 0
        )
        raw_vision_sticky_remaining = router_context.get("vision_sticky_remaining")
        vision_sticky_remaining = (
            raw_vision_sticky_remaining
            if isinstance(raw_vision_sticky_remaining, int)
            and not isinstance(raw_vision_sticky_remaining, bool)
            else 0
        )
        raw_turns_since_last_image = router_context.get("turns_since_last_image")
        turns_since_last_image = (
            raw_turns_since_last_image
            if isinstance(raw_turns_since_last_image, int)
            and not isinstance(raw_turns_since_last_image, bool)
            else None
        )
        raw_vision_candidate_turns = router_context.get("vision_candidate_turns")
        vision_candidate_turns = (
            raw_vision_candidate_turns
            if isinstance(raw_vision_candidate_turns, int)
            and not isinstance(raw_vision_candidate_turns, bool)
            else 0
        )
        raw_last_image_turn_text = router_context.get("last_image_turn_text")
        last_image_turn_text = (
            raw_last_image_turn_text
            if isinstance(raw_last_image_turn_text, str)
            else None
        )

        # 3. Run pre-turn pipeline (model routing, skills, prompt cache, etc.)
        request = RunPipelineRequest(
            runtime_message=inp.runtime_message,
            session_key=inp.session_key,
            provider=inp.provider,
            cloned_selector=inp.cloned_selector,
            tool_defs=inp.tool_defs,
            base_prompt=base_prompt,
            attachments=inp.attachments,
            semantic_message=inp.semantic_input,
            ingress_pipeline_steps=inp.ingress_pipeline_steps,
            prev_assistant_text=router_context.get("prev_assistant_text"),
            prev_assistant_usage=router_context.get("prev_assistant_usage"),
            history_user_texts=router_context.get("history_user_texts"),
            history_has_recent_image=bool(router_context.get("history_has_recent_image")),
            history_image_turn_count=history_image_turn_count,
            vision_sticky_remaining=vision_sticky_remaining,
            turns_since_last_image=turns_since_last_image,
            last_image_turn_text=last_image_turn_text,
            vision_candidate_turns=vision_candidate_turns,
            flags_text_override=inp.semantic_input,
            tool_context=inp.effective_tool_context,
            normalization_metadata=inp.normalization_metadata,
            input_provenance=inp.input_provenance,
            skill_catalog=inp.skill_catalog,
            usage_execution_context=inp.usage_execution_context,
            provider_request_correlation=inp.provider_request_correlation,
        )
        turn, provider = await self._pipeline_executor.run_pipeline(request)

        # 4. Merge prompt + tool metadata
        turn.metadata.update(prompt_metadata)
        turn.metadata.update(inp.tool_metadata)

        # 5. Memory fingerprint merge (defensive — port returns None to skip)
        fingerprint = self._memory_fingerprint.memory_mode_fingerprint()
        if fingerprint is not None:
            prompt_fingerprint = turn.metadata.get("memory_mode_fingerprint")
            if isinstance(prompt_fingerprint, dict):
                fingerprint.update(
                    {str(k): str(v) for k, v in prompt_fingerprint.items()}
                )
            turn.metadata["memory_mode_fingerprint"] = fingerprint

        # 6. Effective runtime message + selector override / fallback wrap
        effective_runtime_message = getattr(turn, "message", inp.runtime_message)
        if inp.model and inp.cloned_selector is not None:
            from opensquilla.engine.selector_override import apply_model_override

            # An explicit model overrides the routed choice, so the turn
            # actually runs inp.model; realign_routed_model keeps telemetry
            # and billing on the model that ran, not the one routing
            # recommended.
            provider = apply_model_override(
                inp.cloned_selector,
                inp.model,
                turn_metadata=turn.metadata,
                realign_routed_model=True,
            )
        if inp.cloned_selector is not None:
            # Local import to avoid pulling _SelectorFallbackProvider name
            # into the stage's module-top namespace.
            from opensquilla.engine.runtime import _SelectorFallbackProvider

            provider = _SelectorFallbackProvider(
                provider,
                inp.cloned_selector,
                turn_metadata=turn.metadata,
            )

        # 7. Resolve final prompt + cache breakpoints
        (
            final_prompt,
            cache_breakpoints,
            request_context_prompt,
        ) = self._prompt_config_resolver.resolve_prompt_config(turn)

        # 8. Resolve session_id and build prompt report
        session_id_for_log = await self._session_id_resolver.resolve_session_id_for_log(
            inp.session_key
        )
        prompt_report = self._prompt_report_builder.build_prompt_report(
            turn_id=inp.turn_id,
            session_key=inp.session_key,
            session_id=session_id_for_log,
            agent_id=inp.agent_id,
            system_prompt=final_prompt,
            tool_defs=turn.tool_defs,
            metadata=turn.metadata,
            tool_profile=turn.metadata.get("tool_profile"),
        )

        # 9. Resolve model_id: pipeline-routed > explicit param > selector current
        selector_model = ""
        if inp.cloned_selector is not None:
            try:
                selector_model = (
                    getattr(inp.cloned_selector.current_config, "model", "") or ""
                )
            except Exception:  # noqa: BLE001 - defensive
                selector_model = ""
        # ``turn.model`` is the router's requested model.  When a
        # cross-provider deployment cannot be resolved, apply_model_override
        # deliberately keeps the selector on its primary deployment and
        # records ``routed_provider_blocked``.  In that fail-closed branch the
        # selector is authoritative for execution; letting the stale routed
        # model win here would pair the primary provider with a foreign model
        # id in AgentConfig and turn-call records.
        if turn.metadata.get("routed_provider_blocked") and selector_model:
            resolved_model = selector_model
        else:
            resolved_model = getattr(turn, "model", None) or inp.model or selector_model
        # Turn-call records describe the configured deployment, not the
        # generic adapter family.  A DashScope/DeepSeek deployment runs through
        # OpenAIProvider and a MiniMax deployment may run through
        # AnthropicProvider; attributing those as ``openai`` / ``anthropic``
        # makes cross-provider execution evidence false even when routing was
        # correct.  The cloned selector is authoritative for the active link;
        # direct provider construction falls back to its additive provider_id.
        selector_provider_id = ""
        if inp.cloned_selector is not None:
            selector_provider_id = str(
                getattr(inp.cloned_selector, "active_provider_id", "") or ""
            ).strip()
            if not selector_provider_id:
                try:
                    selector_provider_id = str(
                        getattr(inp.cloned_selector.current_config, "provider", "")
                        or ""
                    ).strip()
                except Exception:  # noqa: BLE001 - telemetry fallback only
                    selector_provider_id = ""
        provider_name = (
            selector_provider_id
            or configured_provider_id(provider)
            or type(provider).__name__
        )

        return StageOutcome.success(
            PromptAssemblerStageOutput(
                provider=provider,
                turn=turn,
                effective_runtime_message=effective_runtime_message,
                final_prompt=final_prompt,
                cache_breakpoints=cache_breakpoints,
                request_context_prompt=request_context_prompt,
                resolved_model=resolved_model,
                provider_name=provider_name,
                session_id_for_log=session_id_for_log,
                trace_context_session_id=session_id_for_log,
                prompt_report=prompt_report,
                selector_model=selector_model,
                squilla_router_tier=turn.metadata.get("routed_tier"),
            )
        )
