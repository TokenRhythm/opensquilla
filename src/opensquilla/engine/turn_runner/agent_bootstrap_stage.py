"""Stage object for runtime budget resolve + AgentConfig assembly + Agent construction.

Owns the source slice that previously lived inline at the top of
``TurnRunner._run_turn`` between the prompt-assembler stage boundary and the
pre-flight compaction call. The harness invokes
``AgentBootstrapStage.run`` once per turn, AFTER PromptAssemblerStage
and BEFORE PreflightCompactionStage.
Side-effect contract: re-raises any exception from the budget resolvers,
the model-catalog lookups, the AgentConfig constructor, the memory
warm/load helpers, or the Agent constructor exactly as the inline body
did. The harness catches it through the existing CancelledError /
Exception terminal handlers in ``_run_turn``. ``AgentBootstrapStage``
does NOT call any ``TurnHook`` directly.

NEVER terminates. Always returns ``StageOutcome.success(...)``. The
``StageOutcome`` shape is preserved for forward-compatibility with a
future AgentConfig-validation early-yield branch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from opensquilla.engine.route_plan import pin_route_plan
from opensquilla.engine.runtime_recovery import (
    normalize_reasoning_prefill_recovery_mode,
    normalize_runtime_recovery_mode,
)
from opensquilla.tools.write_policy import validate_workspace_write_deny_env

if TYPE_CHECKING:
    from collections.abc import Callable

    from opensquilla.contracts.turn_execution import TurnExecutionContext
    from opensquilla.engine.agent import Agent, ToolHandler
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.engine.types import AgentConfig, ThinkingLevel
    from opensquilla.observability.turn_call_log import TurnCallLogger
    from opensquilla.provider.protocol import LLMProvider
    from opensquilla.provider.types import ModelCapabilities, ProviderRequestCorrelation
    from opensquilla.tools.types import ToolContext

_PROGRESS_WATCHDOG_MODES = frozenset({"off", "log", "warn_model", "block"})
_TOOL_LOOP_OBSERVER_MODES = frozenset({"off", "log"})
_SOURCE_DIFF_PRESERVATION_MODES = frozenset({"off", "log", "block"})
_SOURCE_DIFF_CANDIDATE_MODES = frozenset({"off", "log", "warn_model"})
_RUNTIME_STATE_CAPSULE_MODES = frozenset({"off", "log", "inject"})
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _progress_watchdog_mode_from_env() -> Literal["off", "log", "warn_model", "block"]:
    raw = os.environ.get("OPENSQUILLA_PROGRESS_WATCHDOG_MODE", "off").strip().lower()
    if raw in _PROGRESS_WATCHDOG_MODES:
        return raw  # type: ignore[return-value]
    return "off"


def _tool_loop_observer_mode_from_env() -> Literal["off", "log"]:
    raw = os.environ.get("OPENSQUILLA_TOOL_LOOP_OBSERVER_MODE", "off").strip().lower()
    if raw in _TOOL_LOOP_OBSERVER_MODES:
        return raw  # type: ignore[return-value]
    return "off"


def _runtime_recovery_mode_from_env() -> Literal["off", "log", "warn_model"]:
    return normalize_runtime_recovery_mode(os.environ.get("OPENSQUILLA_RUNTIME_RECOVERY_MODE"))


def _final_diff_contract_mode_from_env() -> Literal["off", "log", "warn_model"]:
    return normalize_runtime_recovery_mode(
        os.environ.get("OPENSQUILLA_FINAL_DIFF_CONTRACT_MODE")
    )


def _normalize_source_diff_preservation_mode(
    raw: str | None,
    *,
    default: Literal["off", "log", "block"] = "log",
) -> Literal["off", "log", "block"]:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _SOURCE_DIFF_PRESERVATION_MODES:
        return normalized  # type: ignore[return-value]
    return default


def _source_diff_preservation_mode_from_env(
    config_value: str | None = None,
) -> Literal["off", "log", "block"]:
    raw = os.environ.get("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE")
    if raw is not None:
        return _normalize_source_diff_preservation_mode(raw)
    return _normalize_source_diff_preservation_mode(config_value)


def _normalize_source_diff_candidate_mode(
    raw: str | None,
    *,
    default: Literal["off", "log", "warn_model"] = "log",
) -> Literal["off", "log", "warn_model"]:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _SOURCE_DIFF_CANDIDATE_MODES:
        return normalized  # type: ignore[return-value]
    return default


def _source_diff_candidate_mode_from_env(
    config_value: str | None = None,
) -> Literal["off", "log", "warn_model"]:
    raw = os.environ.get("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE")
    if raw is not None:
        return _normalize_source_diff_candidate_mode(raw)
    return _normalize_source_diff_candidate_mode(config_value)


def _runtime_state_capsule_mode_from_env(
    config_value: str | None = None,
) -> Literal["off", "log", "inject"]:
    raw = os.environ.get("OPENSQUILLA_RUNTIME_STATE_CAPSULE_MODE")
    if raw is None:
        raw = config_value
    if raw is None:
        return "off"
    normalized = raw.strip().lower()
    if normalized in _RUNTIME_STATE_CAPSULE_MODES:
        return normalized  # type: ignore[return-value]
    return "off"


def _post_tool_empty_recovery_mode_from_env() -> Literal["off", "log", "warn_model"]:
    return normalize_runtime_recovery_mode(
        os.environ.get("OPENSQUILLA_POST_TOOL_EMPTY_RECOVERY_MODE")
    )


def _text_only_tool_recovery_mode_from_env(
    config_value: str | None = None,
) -> Literal["off", "log", "warn_model"]:
    raw = os.environ.get("OPENSQUILLA_TEXT_ONLY_TOOL_RECOVERY_MODE")
    if raw is None:
        raw = config_value
    return normalize_runtime_recovery_mode(raw, default="off")


def _reasoning_prefill_recovery_mode_from_env() -> Literal["off", "log", "recover"]:
    return normalize_reasoning_prefill_recovery_mode(
        os.environ.get("OPENSQUILLA_REASONING_PREFILL_RECOVERY_MODE")
    )


_FINALIZE_EVIDENCE_GATE_ENV = "OPENSQUILLA_FINALIZE_EVIDENCE_GATE"
_FINALIZE_EVIDENCE_GATE_ON = frozenset({"on", "1", "true", "yes"})
_FINALIZE_EVIDENCE_GATE_OFF = frozenset({"off", "0", "false", "no"})


def _finalize_evidence_gate_from_env(config_value: bool = False) -> bool:
    """Resolve the opt-in finalize-time red-evidence gate flag.

    Default off. A non-blank ``OPENSQUILLA_FINALIZE_EVIDENCE_GATE`` overrides
    ``config_value`` (gateway ``prompt.finalize_evidence_gate``), mirroring
    ``runtime._resolve_finalize_evidence_gate`` so the loop-side gate and the
    system-prompt section can never disagree. Unrecognized env values raise
    instead of being silently ignored so a run manifest cannot record an
    override the run did not actually apply.
    """
    raw = os.environ.get(_FINALIZE_EVIDENCE_GATE_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _FINALIZE_EVIDENCE_GATE_ON:
        return True
    if raw in _FINALIZE_EVIDENCE_GATE_OFF:
        return False
    raise ValueError(
        f"{_FINALIZE_EVIDENCE_GATE_ENV} must be one of: "
        + ", ".join(sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF))
    )


_FINALIZE_EVIDENCE_STRICT_ENV = "OPENSQUILLA_FINALIZE_EVIDENCE_STRICT"


def _finalize_evidence_strict_from_env(config_value: bool = False) -> bool:
    """Resolve the opt-in strict mode of the finalize-time evidence gate.

    Default off. A non-blank ``OPENSQUILLA_FINALIZE_EVIDENCE_STRICT``
    overrides ``config_value``. Strict implies the gate itself (the loop
    activates the tracker when either flag is on). Unrecognized env values
    raise instead of being silently ignored so a run manifest cannot record
    an override the run did not actually apply.
    """
    raw = os.environ.get(_FINALIZE_EVIDENCE_STRICT_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _FINALIZE_EVIDENCE_GATE_ON:
        return True
    if raw in _FINALIZE_EVIDENCE_GATE_OFF:
        return False
    raise ValueError(
        f"{_FINALIZE_EVIDENCE_STRICT_ENV} must be one of: "
        + ", ".join(sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF))
    )


_SCRATCH_VERIFY_MIRROR_ENV = "OPENSQUILLA_SCRATCH_VERIFY_MIRROR"


def _scratch_verify_mirror_from_env(config_value: bool = False) -> bool:
    """Resolve the opt-in scratch verify-mirror flag.

    Default off. A non-blank ``OPENSQUILLA_SCRATCH_VERIFY_MIRROR`` overrides
    ``config_value``. Unrecognized env values raise instead of being silently
    ignored so a run manifest cannot record an override the run did not
    actually apply.
    """
    raw = os.environ.get(_SCRATCH_VERIFY_MIRROR_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _FINALIZE_EVIDENCE_GATE_ON:
        return True
    if raw in _FINALIZE_EVIDENCE_GATE_OFF:
        return False
    raise ValueError(
        f"{_SCRATCH_VERIFY_MIRROR_ENV} must be one of: "
        + ", ".join(sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF))
    )


_FINALIZE_VARIANT_CHALLENGE_ENV = "OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE"


def _finalize_variant_challenge_from_env(config_value: bool = False) -> bool:
    """Resolve the opt-in finalize-time variant-sweep challenge flag.

    Default off. A non-blank ``OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE``
    overrides ``config_value``. Unrecognized env values raise instead of
    being silently ignored so a run manifest cannot record an override the
    run did not actually apply.
    """
    raw = os.environ.get(_FINALIZE_VARIANT_CHALLENGE_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _FINALIZE_EVIDENCE_GATE_ON:
        return True
    if raw in _FINALIZE_EVIDENCE_GATE_OFF:
        return False
    raise ValueError(
        f"{_FINALIZE_VARIANT_CHALLENGE_ENV} must be one of: "
        + ", ".join(sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF))
    )


_SUBMIT_REVIEW_ENV = "OPENSQUILLA_SUBMIT_REVIEW"
_SUBMIT_REVIEW_ON = frozenset({"on", "1", "true", "yes"})
_SUBMIT_REVIEW_OFF = frozenset({"off", "0", "false", "no"})


def _submit_review_from_env(config_value: bool = False) -> bool:
    """Resolve the opt-in review-on-submit checkpoint flag.

    Default off. A non-blank ``OPENSQUILLA_SUBMIT_REVIEW`` overrides
    ``config_value``. Unlike the finalize-evidence gate there is no gateway aux
    mirror: the review has no system-prompt section that could disagree with the
    loop behaviour. Unrecognized env values raise instead of being silently
    ignored so an experiment manifest cannot record a lever the run did not
    actually apply.
    """
    raw = os.environ.get(_SUBMIT_REVIEW_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _SUBMIT_REVIEW_ON:
        return True
    if raw in _SUBMIT_REVIEW_OFF:
        return False
    raise ValueError(
        f"{_SUBMIT_REVIEW_ENV} must be one of: "
        + ", ".join(sorted(_SUBMIT_REVIEW_ON | _SUBMIT_REVIEW_OFF))
    )


_PATCH_HYGIENE_BLOCK_ENV = "OPENSQUILLA_PATCH_HYGIENE_BLOCK"
_PATCH_HYGIENE_BLOCK_MODES = ("off", "test_paths", "protected_paths")


def _patch_hygiene_block_from_env(
    config_value: Literal["off", "test_paths", "protected_paths"] = "off",
) -> Literal["off", "test_paths", "protected_paths"]:
    """Resolve the finalize-time patch hygiene hard-block mode.

    Default off. A non-blank ``OPENSQUILLA_PATCH_HYGIENE_BLOCK`` overrides
    ``config_value``. Unrecognized env values raise instead of being silently
    ignored so a run manifest cannot record an override the run did not
    actually apply.
    """
    raw = os.environ.get(_PATCH_HYGIENE_BLOCK_ENV, "").strip().lower()
    if not raw:
        return config_value
    if raw in _PATCH_HYGIENE_BLOCK_MODES:
        return raw  # type: ignore[return-value]
    raise ValueError(
        f"{_PATCH_HYGIENE_BLOCK_ENV} must be one of: "
        + ", ".join(_PATCH_HYGIENE_BLOCK_MODES)
    )


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _nonnegative_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


def _strict_bool_from_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _FINALIZE_EVIDENCE_GATE_ON:
        return True
    if normalized in _FINALIZE_EVIDENCE_GATE_OFF:
        return False
    raise ValueError(
        f"{name} must be one of: "
        + ", ".join(
            sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF)
        )
    )


def _name_tuple_from_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _projection_signal_hints_from_env() -> bool:
    """Parse OPENSQUILLA_PROJECTION_SIGNAL_HINTS through the runtime gate.

    Delegating keeps bootstrap and per-request resolution on one on/off
    vocabulary: an unrecognized value raises here, at bootstrap, instead of
    surviving as False and then raising mid-turn when the agent re-reads the
    env. Local import; the engine.agent module is import-cycle-safe from
    this stage only at call time.
    """
    from opensquilla.engine.agent import _projection_signal_hints_enabled

    return _projection_signal_hints_enabled(False)


# ---------------------------------------------------------------------------
# Value objects returned by the ports — typed frozen tuples that collapse
# the multi-call slice into declarative single-call shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedBudgets:
    """Frozen value returned by ``TimeoutBudgetPort.resolve_budgets``."""

    runtime_timeout: float
    max_iterations: int
    max_iterations_source: str
    iteration_timeout: float
    tool_timeout: float
    request_timeout: float
    max_provider_retries: int


@dataclass(frozen=True)
class _ResolvedCatalog:
    """Frozen resolved-model value returned by ``ModelCatalogPort.lookup``."""

    max_tokens: int
    context_window: int
    capabilities: ModelCapabilities | None
    vision_support: Literal["supported", "unsupported", "unknown"] = "unknown"
    # Raw positive global ``llm.context_window_tokens`` value, or zero when
    # unset. This is deliberately distinct from ``context_window`` because a
    # per-model override may win for the current deployment while the global
    # value still governs a later selector fallback without its own override.
    context_window_tokens_global_override: int = 0
    # Provider/model ceiling resolved without the global llm.max_tokens
    # override. Physical fallback uses it only when ``auto_max_tokens_known``
    # is true, so unknown/self-hosted models are never capped by a generic
    # default.
    auto_max_tokens: int = 0
    auto_max_tokens_known: bool = False
    temperature: float | None = None
    top_p: float | None = None
    # Explicit provider-request proof budget (chars); 0 keeps the derived path.
    provider_request_proof_max_chars: int = 0


@dataclass(frozen=True)
class _AgentConfigAuxiliaries:
    """Bag of resolved auxiliaries for AgentConfig construction.

    Carries every value the AgentConfig body reads via
    ``getattr(_mem_cfg, ...)`` / ``getattr(_agent_token_cfg, ...)`` so the
    stage body becomes a single ``AgentConfig(...)`` call site.
    """

    thinking: bool | ThinkingLevel
    flush_workspace_dir: str
    tool_result_store_dir: str
    tool_result_store_session_id: str
    # Memory-cfg-derived (defaults match the inline ``getattr`` defaults)
    flush_enabled: bool
    flush_triggers: list[str]
    flush_pre_compaction: bool
    flush_timeout_seconds: float
    flush_background_timeout_seconds: float
    flush_backoff_initial_seconds: float
    flush_backoff_max_seconds: float
    flush_archive_max_bytes: int
    flush_compaction_requires_safe_receipt: bool
    flush_compaction_safety_mode: Literal["protect", "best_effort", "block", "off"]
    compaction_profile: Literal["conversation", "coding", "research", "support"]
    compaction_protected_recent_messages: int
    compaction_total_timeout_seconds: float
    compaction_heartbeat_interval_seconds: float
    # Agent-token-cfg-derived
    tool_result_projection_max_inline_chars: int
    tool_result_fresh_diagnostic_policy_enabled: bool
    tool_result_diagnostic_retrieval_gate_enabled: bool
    tool_result_fresh_diagnostic_inline_max_chars: int
    tool_result_dispatch_max_chars: int
    tool_result_dispatch_turn_max_chars: int
    tool_result_store_full_trace: bool
    tool_result_store_max_bytes: int
    tool_result_store_disk_budget_bytes: int
    tool_result_store_retention_seconds: int
    source_diff_preservation_mode: Literal["off", "log", "block"] | None
    source_diff_candidate_mode: Literal["off", "log", "warn_model"] | None
    runtime_state_capsule_mode: Literal["off", "log", "inject"] | None
    text_only_tool_recovery_mode: Literal["off", "log", "warn_model"] | None
    # Gateway ``prompt.finalize_evidence_gate`` (env still overrides).
    finalize_evidence_gate: bool = False


@dataclass(frozen=True)
class _MemorySnapshotResult:
    """Two-field frozen value returned by ``MemorySnapshotPort.warm_and_capture``."""

    sync_manager: Any | None
    private_memory_allowed: bool


# ---------------------------------------------------------------------------
# Ports — six narrow Protocols so the stage is unit-testable without the
# full TurnRunner. The runtime adapters in ``harness.py`` bind these to the
# concrete TurnRunner methods.
# ---------------------------------------------------------------------------


@runtime_checkable
class TimeoutBudgetPort(Protocol):
    """Wraps the five ``TurnRunner._resolve_agent_*`` helpers as a
    coordinated single-call port. Returns the resolved budget tuple in
    one shot to keep the stage body declarative.

    ``effective_runtime_timeout`` honors the per-call ``timeout``
    override (``float(timeout) if timeout is not None else
    self._resolve_agent_runtime_timeout(session_key)``). The other four
    resolvers consume the per-call explicit override and the
    session/env/config fallback chain internally.
    """

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
    ) -> _ResolvedBudgets: ...


@runtime_checkable
class ModelCatalogPort(Protocol):
    """Wraps ``TurnRunner._model_catalog`` lookups defensively.

    Mirrors the inline three-call sequence with the fallback
    semantics: when ``self._model_catalog is None`` the inline body
    computes ``max_tokens=user_override or 16384`` and
    ``context_window=200_000`` and ``model_caps=None``. The adapter folds
    those defaults into the port so the stage body has no branching on
    catalog presence.

    The adapter reads ``user_max_tokens`` / ``provider_name`` /
    ``base_url`` off the runner's ``_config.llm`` chain.
    """

    def lookup(self, model_id: str, provider: str = "") -> _ResolvedCatalog: ...


@runtime_checkable
class AgentConfigBuilderPort(Protocol):
    """Wraps the ``TurnRunner`` helpers AgentConfig assembly needs.

    The inline body calls ``_resolve_turn_thinking(turn)``,
    ``_resolve_memory_source_dir(agent_id)``, and reads a handful of
    ``getattr`` values off ``_mem_cfg`` / ``_agent_token_cfg``.

    Folding them into a single port keeps the stage body free of
    runtime imports. The adapter returns a typed
    ``_AgentConfigAuxiliaries`` value that the stage feeds straight into
    ``AgentConfig(...)``.
    """

    def build_auxiliaries(
        self,
        *,
        agent_id: str,
        session_key: str,
        session_id_for_log: str | None,
        turn: Any,
    ) -> _AgentConfigAuxiliaries: ...


def _route_max_history_turns(metadata: dict[str, Any]) -> int:
    value = metadata.get("route_max_history_turns")
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _preserve_historical_images(metadata: dict[str, Any]) -> bool:
    image_route_reason = metadata.get("image_route_reason")
    return image_route_reason in {"current_turn", "gate_history"}


@runtime_checkable
class MemorySnapshotPort(Protocol):
    """Wraps the per-agent memory sync warm + the per-(agent_id, session_key)
    memory snapshot capture. Two effects, one async port.

    Inline body does:
      - ``sync_manager = self._memory_sync_managers.get(agent_id)``
      - ``await sync_manager.warm_session(session_key)`` (if present)
      - ``private_memory_allowed = allows_private_memory_prompt_injection(session_key)``
      - if allowed and snapshot missing: ``self._memory_snapshots[snap_key] = MemorySnapshot(...)``

    The port encapsulates ALL of that, including the
    ``_memory_snapshots`` dict mutation. The harness cannot move the
    mutation out without changing semantics — the snapshot is consulted
    by ``_assemble_prompt`` AND by StreamConsumerStage on CompactionEvent.
    Keeping the mutation inside the port preserves the existing
    single-writer invariant. The port returns ``(sync_manager,
    private_memory_allowed)`` so the Agent constructor receives the same
    ``sync_manager`` and the harness can read ``private_memory_allowed``
    for downstream consumers.
    """

    async def warm_and_capture(
        self,
        *,
        agent_id: str,
        session_key: str,
    ) -> _MemorySnapshotResult: ...


@runtime_checkable
class AgentFactoryPort(Protocol):
    """Wraps the typed ``Agent(...)`` constructor.

    Mirrors the call shape with the typed runtime constructor params
    (``memory_sync_manager``, ``session_flush_service``). The adapter at
    the harness side reads ``self._session_flush_service`` from the
    runner and forwards everything else from the call site.
    """

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
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        execution_context: TurnExecutionContext | None = None,
    ) -> Agent: ...


# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentBootstrapStageInput:
    """Inputs the AgentBootstrapStage needs at the boundary it owns.

    Mirrors the locals visible to the original inline slice at the point
    PromptAssemblerStage has finished. The ``provider``,
    ``cloned_selector``, ``turn``, ``final_prompt``, ``cache_breakpoints``,
    ``request_context_prompt``, ``resolved_model``, and
    ``session_id_for_log`` fields come from's
    ``PromptAssemblerStageOutput``. ``tool_handler`` comes from's
    output.
    """

    # From PromptAssemblerStage / ProviderAndToolsStage / call site
    provider: Any
    cloned_selector: Any
    turn: Any  # post-pipeline pipeline.TurnContext
    final_prompt: str
    cache_breakpoints: list[Any] | None
    request_context_prompt: str | None
    resolved_model: str
    session_id_for_log: str | None
    tool_handler: ToolHandler | None
    turn_call_logger: TurnCallLogger | None
    tool_context: ToolContext | None

    # Per-turn inputs from _run_turn locals
    session_key: str
    agent_id: str
    timeout: float | None
    max_iterations: int | None
    iteration_timeout: float | None
    tool_timeout: float | None
    request_timeout: float | None
    max_provider_retries: int | None
    length_capped_continuations: int | None
    active_provider_id: str = ""
    turn_id: str = ""
    run_kind: str = "agent"
    session_epoch: int = 0
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    execution_context: TurnExecutionContext | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True)
class AgentBootstrapStageOutput:
    """The pieces of state subsequent stages consume.

    - ``agent``: the constructed ``Agent`` instance ready for
      ``run_turn``. Subsequent stages mutate ``agent.config`` (history
      load) and ``agent._context.system_prompt`` (compaction).
    - ``agent_config``: the same ``AgentConfig`` carried on
      ``agent.config``. Surfaced separately because PreflightCompactionStage
      reads ``agent_config.context_window_tokens`` directly.
    - ``effective_runtime_timeout`` / ``effective_max_iterations`` /
      ``effective_iteration_timeout`` / ``effective_tool_timeout`` /
      ``effective_request_timeout`` / ``effective_max_provider_retries``:
      surfaced for parity assertions and downstream consumers.
    - ``model_capabilities``: the resolved ``ModelCapabilities`` (or
      ``None``). Surfaced for downstream observability.
    - ``private_memory_allowed``: the result of
      ``allows_private_memory_prompt_injection(session_key)``. Surfaced
      for parity assertions.
    - ``sync_manager``: the per-agent ``MemorySyncManager`` instance (or
      ``None``). The Agent constructor receives it; surfaced for parity
      assertions.
    """

    agent: Agent
    agent_config: AgentConfig
    effective_runtime_timeout: float
    effective_max_iterations: int
    effective_max_iterations_source: str
    effective_iteration_timeout: float
    effective_tool_timeout: float
    effective_request_timeout: float
    effective_max_provider_retries: int
    model_capabilities: ModelCapabilities | None
    private_memory_allowed: bool
    sync_manager: Any | None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class AgentBootstrapStage:
    """Resolve runtime budgets, build AgentConfig, instantiate the Agent.

    Stable boundary: runs ONCE per turn, after PromptAssemblerStage and
    before PreflightCompactionStage. Pure with respect to its inputs
    except for:

    - ``timeout_budget.resolve_budgets`` — synchronous reads of
      session/env/config; pure read, idempotent.
    - ``model_catalog.lookup`` — synchronous catalog dict lookups; pure.
    - ``agent_config_builder.build_auxiliaries`` — synchronous reads of
      ``_mem_cfg`` / ``_agent_token_cfg`` plus
      ``_resolve_memory_source_dir`` filesystem path resolution.
    - ``memory_snapshot.warm_and_capture`` — async; calls
      ``sync_manager.warm_session`` (transcript-driven preload) and
      mutates ``self._memory_snapshots`` dict.
    - ``agent_factory.build`` — pure constructor call.

    Exception model: re-raises every exception. The harness catches them
    through the existing CancelledError / Exception terminal handlers in
    ``_run_turn``.
    """

    name = "agent_bootstrap_stage"

    def __init__(
        self,
        *,
        timeout_budget: TimeoutBudgetPort,
        model_catalog: ModelCatalogPort,
        agent_config_builder: AgentConfigBuilderPort,
        memory_snapshot: MemorySnapshotPort,
        agent_factory: AgentFactoryPort,
        provider_call_observer: Callable[..., None] | None = None,
    ) -> None:
        self._timeout_budget = timeout_budget
        self._model_catalog = model_catalog
        self._agent_config_builder = agent_config_builder
        self._memory_snapshot = memory_snapshot
        self._agent_factory = agent_factory
        # Optional gateway diagnostics seam threaded onto every AgentConfig;
        # None keeps the engine fully gateway-agnostic.
        self._provider_call_observer = provider_call_observer

    async def run(
        self,
        inp: AgentBootstrapStageInput,
    ) -> StageOutcome[AgentBootstrapStageOutput]:
        # Local imports keep the module import-cycle-free.
        from opensquilla.engine.turn_runner.outcome import StageOutcome
        from opensquilla.engine.types import AgentConfig

        # 1. Resolve runtime/iteration/tool/request/retry budgets
        budgets = self._timeout_budget.resolve_budgets(
            session_key=inp.session_key,
            timeout=inp.timeout,
            max_iterations=inp.max_iterations,
            iteration_timeout=inp.iteration_timeout,
            tool_timeout=inp.tool_timeout,
            request_timeout=inp.request_timeout,
            max_provider_retries=inp.max_provider_retries,
        )

        # 2. Resolve max_tokens, context_window, capabilities from catalog.
        # Prefer the exact in-process ProviderConfig when the selector wrapper
        # exposes it; provider/model route metadata cannot distinguish two
        # TokenRhythm credentials serving the same model id.
        deployment_lookup = getattr(
            self._model_catalog,
            "lookup_deployment",
            None,
        )
        active_deployment_config = getattr(
            inp.provider,
            "active_deployment_config",
            None,
        )
        deployment = (
            active_deployment_config()
            if callable(active_deployment_config)
            else None
        )
        if (
            deployment is not None
            and callable(deployment_lookup)
            and str(getattr(deployment, "model", "") or "").strip()
            == inp.resolved_model
        ):
            catalog = deployment_lookup(
                deployment,
                include_global_overrides=True,
            )
        else:
            catalog = self._model_catalog.lookup(
                inp.resolved_model,
                inp.active_provider_id,
            )

        # Capacity diagnostics contain resolved numeric limits only. They are
        # safe for turn metadata and make catalog-vs-provider failures
        # distinguishable without exposing attachment names or contents.
        inp.turn.metadata["resolved_output_cap_tokens"] = int(catalog.max_tokens)
        inp.turn.metadata["resolved_context_window_tokens"] = int(
            catalog.context_window
        )

        # 3. Build AgentConfig auxiliaries (thinking, projection, store, mem cfg)
        aux = self._agent_config_builder.build_auxiliaries(
            agent_id=inp.agent_id,
            session_key=inp.session_key,
            session_id_for_log=inp.session_id_for_log,
            turn=inp.turn,
        )
        agent_metadata = inp.turn.metadata
        fallback_capabilities: dict[
            tuple[str, str],
            tuple[int, int, ModelCapabilities | None],
        ] = {}
        private_fallback_limits: list[
            tuple[Any, int, int, ModelCapabilities | None]
        ] = []
        private_fallback_vision_support: list[
            tuple[Any, Literal["supported", "unsupported", "unknown"]]
        ] = []
        fallback_deployment_configs = getattr(
            inp.provider,
            "fallback_deployment_configs",
            None,
        )
        if callable(fallback_deployment_configs):
            for deployment in fallback_deployment_configs():
                fallback_model = str(
                    getattr(deployment, "model", "") or ""
                ).strip()
                fallback_provider = str(
                    getattr(deployment, "provider", "") or ""
                ).strip()
                if not fallback_model or not fallback_provider:
                    continue
                fallback_catalog = (
                    deployment_lookup(
                        deployment,
                        include_global_overrides=False,
                    )
                    if callable(deployment_lookup)
                    else self._model_catalog.lookup(
                        fallback_model,
                        fallback_provider,
                    )
                )
                effective_max_tokens = (
                    fallback_catalog.auto_max_tokens
                    if fallback_catalog.auto_max_tokens_known
                    else 0
                )
                private_fallback_limits.append(
                    (
                        deployment,
                        fallback_catalog.context_window,
                        effective_max_tokens,
                        fallback_catalog.capabilities,
                    )
                )
                private_fallback_vision_support.append(
                    (deployment, fallback_catalog.vision_support)
                )
                fallback_capabilities.setdefault(
                    (fallback_provider, fallback_model),
                    (
                        fallback_catalog.context_window,
                        effective_max_tokens,
                        fallback_catalog.capabilities,
                    ),
                )
        route_provider = str(
            agent_metadata.get("routed_provider")
            or inp.active_provider_id
            or ""
        )
        fallback_sources = (
            agent_metadata.get("router_fallback_chain"),
            agent_metadata.get("selector_execution_chain"),
        )
        for raw_fallbacks in fallback_sources:
            if not isinstance(raw_fallbacks, list):
                continue
            for raw_fallback in raw_fallbacks:
                if not isinstance(raw_fallback, dict):
                    continue
                fallback_model = str(raw_fallback.get("model") or "").strip()
                if not fallback_model:
                    continue
                fallback_provider = str(
                    raw_fallback.get("provider") or route_provider
                ).strip()
                if (fallback_provider, fallback_model) in fallback_capabilities:
                    continue
                fallback_catalog = self._model_catalog.lookup(
                    fallback_model,
                    fallback_provider,
                )
                fallback_capabilities[(fallback_provider, fallback_model)] = (
                    fallback_catalog.context_window,
                    (
                        fallback_catalog.auto_max_tokens
                        if fallback_catalog.auto_max_tokens_known
                        else 0
                    ),
                    fallback_catalog.capabilities,
                )
        configure_private_fallback_limits = getattr(
            inp.provider,
            "configure_fallback_deployment_limits",
            None,
        )
        if callable(configure_private_fallback_limits):
            configure_private_fallback_limits(private_fallback_limits)
        configure_private_fallback_vision_support = getattr(
            inp.provider,
            "configure_fallback_deployment_vision_support",
            None,
        )
        if callable(configure_private_fallback_vision_support):
            configure_private_fallback_vision_support(
                private_fallback_vision_support
            )
        configure_fallback_limits = getattr(
            inp.provider,
            "configure_fallback_limits",
            None,
        )
        if callable(configure_fallback_limits):
            configure_fallback_limits(
                {
                    identity: (context_window, effective_max_tokens)
                    for identity, (
                        context_window,
                        effective_max_tokens,
                        _capabilities,
                    ) in fallback_capabilities.items()
                }
            )
        pin_route_plan(
            inp.turn,
            turn_id=inp.turn_id,
            provider=inp.active_provider_id,
            model=inp.resolved_model,
            context_window=catalog.context_window,
            capabilities=catalog.capabilities,
            effective_thinking=aux.thinking,
            fallback_capabilities=fallback_capabilities,
        )
        agent_metadata["agent_max_iterations"] = budgets.max_iterations
        agent_metadata["agent_max_iterations_source"] = budgets.max_iterations_source

        # 4. Construct AgentConfig (declarative, single call site)
        #
        # Validation-only gate: the workspace write deny env levers are read
        # at dispatch time in the tools layer (no AgentConfig fields), but a
        # typo there must stop the run at startup, not silently disable
        # enforcement mid-run.
        validate_workspace_write_deny_env()
        #
        # ``workspace_dir`` is sourced from the per-turn metadata key
        # ``bootstrap_workspace_dir`` (written by ``_run_pipeline`` from
        # the call-site's ToolContext/agent-resolved value — see
        # runtime.py initial_metadata). This makes AgentConfig.workspace_dir
        # the single authoritative source for downstream code (meta_invoke
        # handler, sub-Agent factory, etc.). Without this, the bootstrap
        # stage left workspace_dir=None, the meta_invoke fallback chain
        # collapsed to ContextVar lookups, and sub-Agents ended up using
        # the process default workspace instead of the configured one.
        agent_config = AgentConfig(
            max_iterations=budgets.max_iterations,
            system_prompt=inp.final_prompt,
            cache_breakpoints=inp.cache_breakpoints,
            request_context_prompt=inp.request_context_prompt,
            cache_mode=inp.turn.metadata.get("cache_mode", "off"),
            skills_context_prompt=inp.turn.metadata.get("skills_context_prompt"),
            model_id=inp.resolved_model,
            provider_id=inp.active_provider_id,
            workspace_dir=inp.turn.metadata.get("bootstrap_workspace_dir") or None,
            timeout=budgets.runtime_timeout,
            iteration_timeout=budgets.iteration_timeout,
            tool_timeout=budgets.tool_timeout,
            request_timeout=budgets.request_timeout,
            max_provider_retries=budgets.max_provider_retries,
            length_capped_continuations=(
                inp.length_capped_continuations
                if inp.length_capped_continuations is not None
                else AgentConfig().length_capped_continuations
            ),
            max_tokens=catalog.max_tokens,
            temperature=catalog.temperature,
            top_p=catalog.top_p,
            context_window_tokens=catalog.context_window,
            context_window_tokens_global_override=(
                catalog.context_window_tokens_global_override
            ),
            provider_request_proof_max_chars=catalog.provider_request_proof_max_chars,
            provider_request_proof_max_chars_explicit=(
                catalog.provider_request_proof_max_chars > 0
            ),
            max_history_turns=_route_max_history_turns(inp.turn.metadata),
            preserve_historical_images=_preserve_historical_images(inp.turn.metadata),
            materialize_historical_attachments=bool(
                inp.turn.metadata.get("bootstrap_workspace_dir")
            ),
            flush_enabled=aux.flush_enabled,
            flush_triggers=aux.flush_triggers,
            flush_pre_compaction=aux.flush_pre_compaction,
            flush_timeout_seconds=aux.flush_timeout_seconds,
            flush_background_timeout_seconds=aux.flush_background_timeout_seconds,
            flush_backoff_initial_seconds=aux.flush_backoff_initial_seconds,
            flush_backoff_max_seconds=aux.flush_backoff_max_seconds,
            flush_archive_max_bytes=aux.flush_archive_max_bytes,
            flush_compaction_requires_safe_receipt=(aux.flush_compaction_requires_safe_receipt),
            flush_compaction_safety_mode=aux.flush_compaction_safety_mode,
            compaction_profile=aux.compaction_profile,
            compaction_protected_recent_messages=(aux.compaction_protected_recent_messages),
            compaction_total_timeout_seconds=aux.compaction_total_timeout_seconds,
            compaction_heartbeat_interval_seconds=aux.compaction_heartbeat_interval_seconds,
            flush_workspace_dir=aux.flush_workspace_dir,
            model_capabilities=catalog.capabilities,
            model_vision_support=catalog.vision_support,
            thinking=aux.thinking,
            tool_result_projection_max_inline_chars=(aux.tool_result_projection_max_inline_chars),
            tool_result_fresh_diagnostic_policy_enabled=(
                _bool_from_env(
                    "OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_POLICY_ENABLED",
                    aux.tool_result_fresh_diagnostic_policy_enabled,
                )
            ),
            tool_result_diagnostic_retrieval_gate_enabled=(
                _bool_from_env(
                    "OPENSQUILLA_TOOL_RESULT_DIAGNOSTIC_RETRIEVAL_GATE_ENABLED",
                    aux.tool_result_diagnostic_retrieval_gate_enabled,
                )
            ),
            tool_result_fresh_diagnostic_inline_max_chars=(
                _nonnegative_int_from_env(
                    "OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_INLINE_MAX_CHARS",
                    aux.tool_result_fresh_diagnostic_inline_max_chars,
                )
            ),
            tool_result_dispatch_max_chars=aux.tool_result_dispatch_max_chars,
            tool_result_dispatch_turn_max_chars=(aux.tool_result_dispatch_turn_max_chars),
            tool_result_store_dir=aux.tool_result_store_dir,
            tool_result_store_session_id=aux.tool_result_store_session_id,
            tool_result_store_session_key=inp.session_key,
            tool_result_store_agent_id=inp.agent_id,
            tool_result_store_full_trace=aux.tool_result_store_full_trace,
            tool_result_store_max_bytes=aux.tool_result_store_max_bytes,
            tool_result_store_disk_budget_bytes=(aux.tool_result_store_disk_budget_bytes),
            tool_result_store_retention_seconds=(aux.tool_result_store_retention_seconds),
            progress_watchdog_mode=_progress_watchdog_mode_from_env(),
            progress_watchdog_repeated_tool_error_threshold=_positive_int_from_env(
                "OPENSQUILLA_PROGRESS_WATCHDOG_TOOL_ERROR_THRESHOLD",
                AgentConfig().progress_watchdog_repeated_tool_error_threshold,
            ),
            progress_watchdog_repeated_provider_failure_threshold=_positive_int_from_env(
                "OPENSQUILLA_PROGRESS_WATCHDOG_PROVIDER_FAILURE_THRESHOLD",
                AgentConfig().progress_watchdog_repeated_provider_failure_threshold,
            ),
            progress_watchdog_repeated_failure_anchor_threshold=_positive_int_from_env(
                "OPENSQUILLA_PROGRESS_WATCHDOG_FAILURE_ANCHOR_THRESHOLD",
                AgentConfig().progress_watchdog_repeated_failure_anchor_threshold,
            ),
            post_write_convergence_enabled=_bool_from_env(
                "OPENSQUILLA_POST_WRITE_CONVERGENCE",
                AgentConfig().post_write_convergence_enabled,
            ),
            post_write_convergence_warn_threshold=_positive_int_from_env(
                "OPENSQUILLA_POST_WRITE_CONVERGENCE_WARN_THRESHOLD",
                AgentConfig().post_write_convergence_warn_threshold,
            ),
            post_write_convergence_finalize_after_warning=_positive_int_from_env(
                "OPENSQUILLA_POST_WRITE_CONVERGENCE_FINALIZE_AFTER_WARNING",
                AgentConfig().post_write_convergence_finalize_after_warning,
            ),
            patch_evidence_ledger_path=(
                os.environ.get("OPENSQUILLA_PATCH_EVIDENCE_LEDGER_PATH") or None
            ),
            finalize_evidence_gate_enabled=_finalize_evidence_gate_from_env(
                aux.finalize_evidence_gate
            ),
            finalize_evidence_strict=_finalize_evidence_strict_from_env(
                AgentConfig().finalize_evidence_strict
            ),
            submit_review_enabled=_submit_review_from_env(),
            submit_review_diff_max_chars=_positive_int_from_env(
                "OPENSQUILLA_SUBMIT_REVIEW_DIFF_MAX_CHARS",
                AgentConfig().submit_review_diff_max_chars,
            ),
            patch_hygiene_block_mode=_patch_hygiene_block_from_env(
                AgentConfig().patch_hygiene_block_mode
            ),
            scratch_verify_mirror=_scratch_verify_mirror_from_env(
                AgentConfig().scratch_verify_mirror
            ),
            finalize_variant_challenge=_finalize_variant_challenge_from_env(
                AgentConfig().finalize_variant_challenge
            ),
            provider_context_block_feedback=_bool_from_env(
                "OPENSQUILLA_PROVIDER_CONTEXT_BLOCK_FEEDBACK",
                AgentConfig().provider_context_block_feedback,
            ),
            identical_request_loop_break_threshold=_nonnegative_int_from_env(
                "OPENSQUILLA_IDENTICAL_REQUEST_LOOP_BREAK",
                AgentConfig().identical_request_loop_break_threshold,
            ),
            placeholder_escalation_threshold=_nonnegative_int_from_env(
                "OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD",
                AgentConfig().placeholder_escalation_threshold,
            ),
            deadline_wrapup_margin_seconds=_nonnegative_int_from_env(
                "OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS",
                AgentConfig().deadline_wrapup_margin_seconds,
            ),
            reasoning_only_thinking_fallback=_bool_from_env(
                "OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK",
                AgentConfig().reasoning_only_thinking_fallback,
            ),
            provider_error_thinking_fallback=_strict_bool_from_env(
                "OPENSQUILLA_PROVIDER_ERROR_THINKING_FALLBACK",
                AgentConfig().provider_error_thinking_fallback,
            ),
            deadline_thinking_off_margin_seconds=_nonnegative_int_from_env(
                "OPENSQUILLA_DEADLINE_THINKING_OFF_MARGIN_SECONDS",
                AgentConfig().deadline_thinking_off_margin_seconds,
            ),
            reasoning_stream_char_cap=_nonnegative_int_from_env(
                "OPENSQUILLA_REASONING_STREAM_CHAR_CAP",
                AgentConfig().reasoning_stream_char_cap,
            ),
            final_diff_salvage=_bool_from_env(
                "OPENSQUILLA_FINAL_DIFF_SALVAGE",
                AgentConfig().final_diff_salvage,
            ),
            endgame_git_freeze_margin_seconds=_nonnegative_int_from_env(
                "OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS",
                AgentConfig().endgame_git_freeze_margin_seconds,
            ),
            max_iterations_deadline_extend_seconds=_nonnegative_int_from_env(
                "OPENSQUILLA_MAX_ITERATIONS_DEADLINE_EXTEND_SECONDS",
                AgentConfig().max_iterations_deadline_extend_seconds,
            ),
            final_diff_salvage_veto=_bool_from_env(
                "OPENSQUILLA_FINAL_DIFF_SALVAGE_VETO",
                AgentConfig().final_diff_salvage_veto,
            ),
            endgame_git_freeze_instrumentation_exempt=_bool_from_env(
                "OPENSQUILLA_ENDGAME_GIT_FREEZE_INSTRUMENTATION_EXEMPT",
                AgentConfig().endgame_git_freeze_instrumentation_exempt,
            ),
            deadline_wrapup_sticky_thinking_off=_bool_from_env(
                "OPENSQUILLA_DEADLINE_WRAPUP_STICKY_THINKING_OFF",
                AgentConfig().deadline_wrapup_sticky_thinking_off,
            ),
            endgame_fix_directive_margin_seconds=_nonnegative_int_from_env(
                "OPENSQUILLA_ENDGAME_FIX_DIRECTIVE_MARGIN_SECONDS",
                AgentConfig().endgame_fix_directive_margin_seconds,
            ),
            reasoning_only_act_now=_bool_from_env(
                "OPENSQUILLA_REASONING_ONLY_ACT_NOW",
                AgentConfig().reasoning_only_act_now,
            ),
            mid_budget_no_diff_nudge=_bool_from_env(
                "OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE",
                AgentConfig().mid_budget_no_diff_nudge,
            ),
            repeated_tool_call_recovery_threshold=_nonnegative_int_from_env(
                "OPENSQUILLA_TOOL_REPEAT_NUDGE_THRESHOLD",
                AgentConfig().repeated_tool_call_recovery_threshold,
            ),
            repeated_tool_call_recovery_extra_tools=_name_tuple_from_env(
                "OPENSQUILLA_TOOL_REPEAT_NUDGE_TOOLS",
            ),
            provider_history_dedup_enabled=_bool_from_env(
                "OPENSQUILLA_PROVIDER_HISTORY_DEDUP",
                AgentConfig().provider_history_dedup_enabled,
            ),
            provider_history_dedup_min_repeats=_positive_int_from_env(
                "OPENSQUILLA_PROVIDER_HISTORY_DEDUP_MIN_REPEATS",
                AgentConfig().provider_history_dedup_min_repeats,
            ),
            projection_signal_hints=_projection_signal_hints_from_env(),
            tool_loop_observer_mode=_tool_loop_observer_mode_from_env(),
            runtime_recovery_mode=_runtime_recovery_mode_from_env(),
            runtime_recovery_source_loop_max_nudges=_positive_int_from_env(
                "OPENSQUILLA_RUNTIME_RECOVERY_SOURCE_LOOP_MAX_NUDGES",
                AgentConfig().runtime_recovery_source_loop_max_nudges,
            ),
            final_diff_contract_mode=_final_diff_contract_mode_from_env(),
            source_diff_preservation_mode=_source_diff_preservation_mode_from_env(
                aux.source_diff_preservation_mode
            ),
            source_diff_candidate_mode=_source_diff_candidate_mode_from_env(
                aux.source_diff_candidate_mode
            ),
            runtime_state_capsule_mode=_runtime_state_capsule_mode_from_env(
                aux.runtime_state_capsule_mode
            ),
            post_tool_empty_recovery_mode=_post_tool_empty_recovery_mode_from_env(),
            text_only_tool_recovery_mode=_text_only_tool_recovery_mode_from_env(
                aux.text_only_tool_recovery_mode
            ),
            reasoning_prefill_recovery_mode=_reasoning_prefill_recovery_mode_from_env(),
            runtime_events_path=(os.environ.get("OPENSQUILLA_RUNTIME_EVENTS_PATH") or None),
            provider_call_observer=self._provider_call_observer,
            metadata=agent_metadata,
        )

        # 5. Warm session and capture memory snapshot (async, dict-mutating)
        memory = await self._memory_snapshot.warm_and_capture(
            agent_id=inp.agent_id,
            session_key=inp.session_key,
        )

        # 7. Construct the Agent from the typed runtime parameters.
        agent = self._agent_factory.build(
            provider=inp.provider,
            config=agent_config,
            tool_definitions=inp.turn.tool_defs,
            tool_handler=inp.tool_handler,
            session_key=inp.session_key,
            turn_call_logger=inp.turn_call_logger,
            memory_sync_manager=memory.sync_manager,
            tool_context=inp.tool_context,
            turn_id=inp.turn_id,
            session_id=inp.session_id_for_log,
            session_epoch=inp.session_epoch,
            agent_id=inp.agent_id,
            run_kind=inp.run_kind,
            provider_request_correlation=inp.provider_request_correlation,
            execution_context=inp.execution_context,
        )

        return StageOutcome.success(
            AgentBootstrapStageOutput(
                agent=agent,
                agent_config=agent_config,
                effective_runtime_timeout=budgets.runtime_timeout,
                effective_max_iterations=budgets.max_iterations,
                effective_max_iterations_source=budgets.max_iterations_source,
                effective_iteration_timeout=budgets.iteration_timeout,
                effective_tool_timeout=budgets.tool_timeout,
                effective_request_timeout=budgets.request_timeout,
                effective_max_provider_retries=budgets.max_provider_retries,
                model_capabilities=catalog.capabilities,
                private_memory_allowed=memory.private_memory_allowed,
                sync_manager=memory.sync_manager,
            )
        )
