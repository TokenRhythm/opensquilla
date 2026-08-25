"""Unit tests for ``AgentBootstrapStage`` driven directly (no full
TurnRunner stack).

Drives an 11-case corpus through ``AgentBootstrapStage.run`` with six
recording fakes (one per port). Two cases register raising fakes so the
propagation contract is exercised without the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    AgentBootstrapStage,
    AgentBootstrapStageInput,
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
from opensquilla.engine.turn_runner.outcome import StageOutcome
from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.types import ModelCapabilities
from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# Recording fakes (one per port)
# ---------------------------------------------------------------------------


def _default_budgets() -> _ResolvedBudgets:
    return _ResolvedBudgets(
        runtime_timeout=60.0,
        max_iterations=10,
        max_iterations_source="test budget",
        iteration_timeout=30.0,
        tool_timeout=20.0,
        request_timeout=120.0,
        max_provider_retries=3,
    )


def _default_catalog(*, capabilities: Any = None) -> _ResolvedCatalog:
    return _ResolvedCatalog(
        max_tokens=4096,
        context_window=200_000,
        capabilities=capabilities,
    )


def _default_aux(
    *,
    thinking: bool | ThinkingLevel = False,
    flush_compaction_requires_safe_receipt: bool = False,
    source_diff_preservation_mode: str | None = "log",
    source_diff_candidate_mode: str | None = "log",
    text_only_tool_recovery_mode: str | None = "off",
) -> _AgentConfigAuxiliaries:
    return _AgentConfigAuxiliaries(
        thinking=thinking,
        flush_workspace_dir="/tmp/flush",
        tool_result_store_dir="/tmp/tool-results",
        tool_result_store_session_id="session-test",
        flush_enabled=True,
        flush_triggers=["session_reset", "manual", "idle", "pre_compaction"],
        flush_pre_compaction=True,
        flush_timeout_seconds=15.0,
        flush_background_timeout_seconds=120.0,
        flush_backoff_initial_seconds=30.0,
        flush_backoff_max_seconds=300.0,
        flush_archive_max_bytes=800_000,
        flush_compaction_requires_safe_receipt=flush_compaction_requires_safe_receipt,
        flush_compaction_safety_mode="protect",
        compaction_profile="conversation",
        compaction_protected_recent_messages=0,
        compaction_total_timeout_seconds=120.0,
        compaction_heartbeat_interval_seconds=15.0,
        tool_result_projection_max_inline_chars=60_000,
        tool_result_fresh_diagnostic_policy_enabled=False,
        tool_result_diagnostic_retrieval_gate_enabled=False,
        tool_result_fresh_diagnostic_inline_max_chars=64_000,
        tool_result_dispatch_max_chars=0,
        tool_result_dispatch_turn_max_chars=0,
        tool_result_store_full_trace=False,
        tool_result_store_max_bytes=400_000,
        tool_result_store_disk_budget_bytes=4_000_000,
        tool_result_store_retention_seconds=3600,
        source_diff_preservation_mode=source_diff_preservation_mode,
        source_diff_candidate_mode=source_diff_candidate_mode,
        runtime_state_capsule_mode="off",
        text_only_tool_recovery_mode=text_only_tool_recovery_mode,
    )


@dataclass
class _RecordingTimeoutBudget:
    budgets: _ResolvedBudgets = field(default_factory=_default_budgets)
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def resolve_budgets(self, **kwargs: Any) -> _ResolvedBudgets:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording timeout budget boom")
        return self.budgets


@dataclass
class _RecordingModelCatalog:
    catalog: _ResolvedCatalog = field(default_factory=_default_catalog)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def lookup(self, model_id: str, provider: str = "") -> _ResolvedCatalog:
        self.calls.append((model_id, provider))
        return self.catalog


@dataclass
class _PerModelCatalog:
    rows: dict[tuple[str, str], _ResolvedCatalog]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def lookup(self, model_id: str, provider: str = "") -> _ResolvedCatalog:
        identity = (provider, model_id)
        self.calls.append((model_id, provider))
        return self.rows[identity]


@dataclass
class _FallbackLimitProvider:
    limits: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def configure_fallback_limits(
        self,
        limits: dict[tuple[str, str], tuple[int, int]],
    ) -> None:
        self.limits = dict(limits)


@dataclass
class _DeploymentAwareCatalog:
    deployment_calls: list[tuple[str, bool]] = field(default_factory=list)
    generic_calls: list[tuple[str, str]] = field(default_factory=list)

    def lookup(self, model_id: str, provider: str = "") -> _ResolvedCatalog:
        self.generic_calls.append((model_id, provider))
        return _ResolvedCatalog(
            max_tokens=131_072,
            context_window=1_000_000,
            capabilities=None,
        )

    def lookup_deployment(
        self,
        deployment: Any,
        *,
        include_global_overrides: bool = False,
    ) -> _ResolvedCatalog:
        self.deployment_calls.append(
            (str(getattr(deployment, "api_key", "")), include_global_overrides)
        )
        return _ResolvedCatalog(
            max_tokens=8_192,
            context_window=64_000,
            capabilities=None,
            auto_max_tokens=8_192,
            auto_max_tokens_known=True,
        )


@dataclass
class _DeploymentAwareProvider:
    active: Any

    def active_deployment_config(self) -> Any:
        return self.active

    def fallback_deployment_configs(self) -> tuple[Any, ...]:
        return ()

    def configure_fallback_deployment_limits(self, _limits: Any) -> None:
        return None


@dataclass
class _RecordingAgentConfigBuilder:
    aux: _AgentConfigAuxiliaries = field(default_factory=_default_aux)
    last_kwargs: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    def build_auxiliaries(self, **kwargs: Any) -> _AgentConfigAuxiliaries:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return self.aux


@dataclass
class _RecordingMemorySnapshot:
    result: _MemorySnapshotResult = field(
        default_factory=lambda: _MemorySnapshotResult(
            sync_manager=None, private_memory_allowed=True
        )
    )
    raises: type[BaseException] | None = None
    last_kwargs: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    async def warm_and_capture(self, **kwargs: Any) -> _MemorySnapshotResult:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self.raises is not None:
            raise self.raises("recording memory snapshot boom")
        return self.result


@dataclass
class _RecordingAgentFactory:
    last_kwargs: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    def build(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(
            provider=kwargs["provider"],
            config=kwargs["config"],
            tool_definitions=kwargs["tool_definitions"],
            tool_handler=kwargs["tool_handler"],
        )


def _make_turn(
    *,
    metadata: dict[str, Any] | None = None,
    tool_defs: list[Any] | None = None,
):
    return SimpleNamespace(
        metadata=dict(metadata or {}),
        tool_defs=list(tool_defs or []),
    )


def _make_input(
    *,
    provider=None,
    cloned_selector=None,
    turn=None,
    final_prompt="FINAL",
    cache_breakpoints=None,
    request_context_prompt=None,
    resolved_model="claude-sonnet-4.5",
    session_id_for_log="sess-1",
    tool_handler=None,
    turn_call_logger=None,
    tool_context=None,
    session_key="agent:main:s1",
    agent_id="agent:main",
    timeout=None,
    max_iterations=None,
    iteration_timeout=None,
    tool_timeout=None,
    request_timeout=None,
    max_provider_retries=None,
    length_capped_continuations=None,
    active_provider_id="",
):
    return AgentBootstrapStageInput(
        provider=provider if provider is not None else object(),
        cloned_selector=cloned_selector,
        turn=turn if turn is not None else _make_turn(),
        final_prompt=final_prompt,
        cache_breakpoints=cache_breakpoints,
        request_context_prompt=request_context_prompt,
        resolved_model=resolved_model,
        session_id_for_log=session_id_for_log,
        tool_handler=tool_handler,
        turn_call_logger=turn_call_logger,
        tool_context=tool_context,
        session_key=session_key,
        agent_id=agent_id,
        timeout=timeout,
        max_iterations=max_iterations,
        iteration_timeout=iteration_timeout,
        tool_timeout=tool_timeout,
        request_timeout=request_timeout,
        max_provider_retries=max_provider_retries,
        length_capped_continuations=length_capped_continuations,
        active_provider_id=active_provider_id,
    )


def _make_stage(
    *,
    budgets=None,
    catalog=None,
    aux=None,
    snapshot=None,
    factory=None,
):
    return AgentBootstrapStage(
        timeout_budget=budgets or _RecordingTimeoutBudget(),
        model_catalog=catalog or _RecordingModelCatalog(),
        agent_config_builder=aux or _RecordingAgentConfigBuilder(),
        memory_snapshot=snapshot or _RecordingMemorySnapshot(),
        agent_factory=factory or _RecordingAgentFactory(),
    )


# ---------------------------------------------------------------------------
# Tests — 11 cases per the design table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case01_success_all_defaults() -> None:
    stage = _make_stage()
    inp = _make_input()
    out = await stage.run(inp)
    assert isinstance(out, StageOutcome)
    o = out.output
    assert o.effective_runtime_timeout == 60.0
    assert o.effective_max_iterations == 10
    assert o.effective_max_iterations_source == "test budget"
    assert o.effective_iteration_timeout == 30.0
    assert o.effective_tool_timeout == 20.0
    assert o.effective_request_timeout == 120.0
    assert o.effective_max_provider_retries == 3
    assert o.model_capabilities is None
    assert o.private_memory_allowed is True
    assert o.sync_manager is None
    # AgentConfig was constructed and threaded into the agent
    assert o.agent_config.system_prompt == "FINAL"
    assert o.agent_config.max_tokens == 4096
    assert o.agent_config.context_window_tokens == 200_000
    assert o.agent_config.max_history_turns == 0
    assert o.agent_config.preserve_historical_images is False
    assert o.agent_config.compaction_total_timeout_seconds == 120.0
    assert o.agent_config.compaction_heartbeat_interval_seconds == 15.0
    assert o.agent_config.length_capped_continuations == 3
    assert o.agent_config.metadata["agent_max_iterations"] == 10
    assert o.agent_config.metadata["agent_max_iterations_source"] == "test budget"


@pytest.mark.asyncio
async def test_exclusive_tool_context_marks_agent_as_restricted_turn() -> None:
    stage = _make_stage()

    restricted = await stage.run(
        _make_input(
            tool_context=ToolContext(
                exclusive_tools={"document_inspect"}
            )
        )
    )
    ordinary = await stage.run(_make_input(tool_context=ToolContext()))

    assert restricted.output.agent_config.restricted_turn is True
    assert ordinary.output.agent_config.restricted_turn is False


@pytest.mark.asyncio
async def test_length_capped_continuations_threads_to_agent_config() -> None:
    stage = _make_stage()
    inp = _make_input(length_capped_continuations=3)
    out = await stage.run(inp)

    assert out.output.agent_config.length_capped_continuations == 3


@pytest.mark.asyncio
async def test_post_write_convergence_env_threads_to_agent_config(monkeypatch) -> None:
    stage = _make_stage()
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.post_write_convergence_enabled is False

    monkeypatch.setenv("OPENSQUILLA_POST_WRITE_CONVERGENCE", "1")
    monkeypatch.setenv("OPENSQUILLA_POST_WRITE_CONVERGENCE_WARN_THRESHOLD", "4")
    monkeypatch.setenv("OPENSQUILLA_POST_WRITE_CONVERGENCE_FINALIZE_AFTER_WARNING", "2")
    enabled_out = await stage.run(_make_input())

    assert enabled_out.output.agent_config.post_write_convergence_enabled is True
    assert enabled_out.output.agent_config.post_write_convergence_warn_threshold == 4
    assert (
        enabled_out.output.agent_config.post_write_convergence_finalize_after_warning
        == 2
    )


@pytest.mark.asyncio
async def test_tool_repeat_nudge_threshold_env_zero_disables_recovery(monkeypatch) -> None:
    stage = _make_stage()
    monkeypatch.setenv("OPENSQUILLA_TOOL_REPEAT_NUDGE_THRESHOLD", "0")
    out = await stage.run(_make_input())

    assert out.output.agent_config.repeated_tool_call_recovery_threshold == 0


@pytest.mark.asyncio
async def test_placeholder_escalation_and_wrapup_env_thread_to_agent_config(
    monkeypatch,
) -> None:
    stage = _make_stage()
    monkeypatch.delenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", raising=False)
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.placeholder_escalation_threshold == 0
    assert default_out.output.agent_config.deadline_wrapup_margin_seconds == 0

    monkeypatch.setenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", "2")
    monkeypatch.setenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", "360")
    enabled_out = await stage.run(_make_input())

    assert enabled_out.output.agent_config.placeholder_escalation_threshold == 2
    assert enabled_out.output.agent_config.deadline_wrapup_margin_seconds == 360


@pytest.mark.asyncio
async def test_final_diff_salvage_and_endgame_freeze_env_thread_to_agent_config(
    monkeypatch,
) -> None:
    stage = _make_stage()
    monkeypatch.delenv("OPENSQUILLA_FINAL_DIFF_SALVAGE", raising=False)
    monkeypatch.delenv("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", raising=False)
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.final_diff_salvage is False
    assert default_out.output.agent_config.endgame_git_freeze_margin_seconds == 0

    monkeypatch.setenv("OPENSQUILLA_FINAL_DIFF_SALVAGE", "1")
    monkeypatch.setenv("OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS", "300")
    enabled_out = await stage.run(_make_input())

    assert enabled_out.output.agent_config.final_diff_salvage is True
    assert enabled_out.output.agent_config.endgame_git_freeze_margin_seconds == 300


@pytest.mark.asyncio
async def test_reasoning_cap_and_mid_budget_nudge_env_thread_to_agent_config(
    monkeypatch,
) -> None:
    stage = _make_stage()
    monkeypatch.delenv("OPENSQUILLA_REASONING_STREAM_CHAR_CAP", raising=False)
    monkeypatch.delenv("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", raising=False)
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.reasoning_stream_char_cap == 0
    assert default_out.output.agent_config.mid_budget_no_diff_nudge is False

    monkeypatch.setenv("OPENSQUILLA_REASONING_STREAM_CHAR_CAP", "20000")
    monkeypatch.setenv("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", "1")
    enabled_out = await stage.run(_make_input())
    assert enabled_out.output.agent_config.reasoning_stream_char_cap == 20000
    assert enabled_out.output.agent_config.mid_budget_no_diff_nudge is True

    # Garbage values fall back to the defaults instead of raising.
    monkeypatch.setenv("OPENSQUILLA_REASONING_STREAM_CHAR_CAP", "banana")
    monkeypatch.setenv("OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE", "banana")
    garbage_out = await stage.run(_make_input())
    assert garbage_out.output.agent_config.reasoning_stream_char_cap == 0
    assert garbage_out.output.agent_config.mid_budget_no_diff_nudge is False


@pytest.mark.asyncio
async def test_source_diff_preservation_config_threads_to_agent_config(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", raising=False)
    builder = _RecordingAgentConfigBuilder(
        aux=_default_aux(source_diff_preservation_mode="block")
    )
    stage = _make_stage(aux=builder)
    out = await stage.run(_make_input())

    assert out.output.agent_config.source_diff_preservation_mode == "block"


@pytest.mark.asyncio
async def test_source_diff_preservation_env_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", "off")
    builder = _RecordingAgentConfigBuilder(
        aux=_default_aux(source_diff_preservation_mode="block")
    )
    stage = _make_stage(aux=builder)
    out = await stage.run(_make_input())

    assert out.output.agent_config.source_diff_preservation_mode == "off"


@pytest.mark.asyncio
async def test_source_diff_candidate_config_threads_to_agent_config(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", raising=False)
    builder = _RecordingAgentConfigBuilder(
        aux=_default_aux(source_diff_candidate_mode="warn_model")
    )
    stage = _make_stage(aux=builder)
    out = await stage.run(_make_input())

    assert out.output.agent_config.source_diff_candidate_mode == "warn_model"


@pytest.mark.asyncio
async def test_source_diff_candidate_env_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", "off")
    builder = _RecordingAgentConfigBuilder(
        aux=_default_aux(source_diff_candidate_mode="warn_model")
    )
    stage = _make_stage(aux=builder)
    out = await stage.run(_make_input())

    assert out.output.agent_config.source_diff_candidate_mode == "off"


@pytest.mark.asyncio
async def test_route_history_limit_metadata_threads_to_agent_config() -> None:
    stage = _make_stage()
    inp = _make_input(turn=_make_turn(metadata={"route_max_history_turns": 1}))
    out = await stage.run(inp)
    assert out.output.agent_config.max_history_turns == 1


@pytest.mark.asyncio
async def test_image_route_metadata_allows_historical_image_replay() -> None:
    stage = _make_stage()
    inp = _make_input(turn=_make_turn(metadata={"image_route_reason": "gate_history"}))
    out = await stage.run(inp)
    assert out.output.agent_config.preserve_historical_images is True


@pytest.mark.asyncio
async def test_lookup_receives_active_provider_name() -> None:
    catalog = _RecordingModelCatalog()
    stage = _make_stage(catalog=catalog)
    inp = _make_input(active_provider_id="ollama", resolved_model="qwen3:4b")
    await stage.run(inp)
    assert catalog.calls[-1] == ("qwen3:4b", "ollama")


@pytest.mark.asyncio
async def test_routed_primary_uses_exact_private_deployment_lookup() -> None:
    catalog = _DeploymentAwareCatalog()
    deployment = SimpleNamespace(
        provider="tokenrhythm",
        model="shared/model",
        api_key="synthetic-routed-key-b",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )
    provider = _DeploymentAwareProvider(active=deployment)
    stage = _make_stage(catalog=catalog)

    out = await stage.run(
        _make_input(
            provider=provider,
            resolved_model="shared/model",
            active_provider_id="tokenrhythm",
        )
    )

    assert out.output.agent_config.max_tokens == 8_192
    assert out.output.agent_config.context_window_tokens == 64_000
    assert catalog.deployment_calls == [("synthetic-routed-key-b", True)]
    assert catalog.generic_calls == []


@pytest.mark.asyncio
async def test_bootstrap_installs_known_fallback_limits_on_provider_wrapper() -> None:
    catalog = _PerModelCatalog(
        rows={
            ("provider-a", "primary/model"): _ResolvedCatalog(
                max_tokens=131_072,
                context_window=1_000_000,
                capabilities=None,
                auto_max_tokens=131_072,
                auto_max_tokens_known=True,
            ),
            ("provider-b", "fallback/model"): _ResolvedCatalog(
                max_tokens=131_072,
                context_window=32_000,
                capabilities=None,
                auto_max_tokens=8_192,
                auto_max_tokens_known=True,
            ),
            ("provider-b", "unknown/model"): _ResolvedCatalog(
                max_tokens=131_072,
                context_window=200_000,
                capabilities=None,
                auto_max_tokens=16_384,
                auto_max_tokens_known=False,
            ),
        }
    )
    provider = _FallbackLimitProvider()
    turn = _make_turn(
        metadata={
            "routed_tier": "c2",
            "routed_provider": "provider-a",
            "routed_model": "primary/model",
            "selector_execution_chain": [
                {"provider": "provider-b", "model": "fallback/model"},
                {"provider": "provider-b", "model": "unknown/model"},
            ],
        }
    )
    stage = _make_stage(catalog=catalog)

    await stage.run(
        _make_input(
            provider=provider,
            turn=turn,
            resolved_model="primary/model",
            active_provider_id="provider-a",
        )
    )

    assert provider.limits == {
        ("provider-b", "fallback/model"): (32_000, 8_192),
        ("provider-b", "unknown/model"): (200_000, 0),
    }
    assert turn.metadata["route_plan"]["fallback_chain"][0]["capabilities"][
        "effective_max_tokens"
    ] == 8_192


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_supports_tools", [True, False])
async def test_fallback_capability_does_not_downgrade_active_model_verification(
    fallback_supports_tools: bool,
) -> None:
    catalog = _PerModelCatalog(
        rows={
            ("provider-a", "primary/model"): _ResolvedCatalog(
                max_tokens=16_384,
                context_window=128_000,
                capabilities=ModelCapabilities(supports_tools=True),
                tools_capability_verified=True,
            ),
            ("provider-b", "fallback/model"): _ResolvedCatalog(
                max_tokens=8_192,
                context_window=32_000,
                capabilities=ModelCapabilities(
                    supports_tools=fallback_supports_tools,
                ),
                tools_capability_verified=False,
            ),
        }
    )
    provider = _FallbackLimitProvider()
    turn = _make_turn(
        metadata={
            "routed_tier": "c2",
            "routed_provider": "provider-a",
            "routed_model": "primary/model",
            "selector_execution_chain": [
                {"provider": "provider-b", "model": "fallback/model"},
            ],
        }
    )

    out = await _make_stage(catalog=catalog).run(
        _make_input(
            provider=provider,
            turn=turn,
            resolved_model="primary/model",
            active_provider_id="provider-a",
        )
    )

    assert out.output.agent_config.model_tools_capability_verified is True
    fallback_caps = turn.metadata["route_plan"]["fallback_chain"][0]["capabilities"]
    assert fallback_caps["supports_tools"] is fallback_supports_tools


@pytest.mark.asyncio
async def test_private_fallback_does_not_downgrade_fixed_glm_5_2_verification() -> None:
    active = SimpleNamespace(
        provider="tokenrhythm",
        model="glm-5.2",
        api_key="synthetic-primary-key",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )
    fallback = SimpleNamespace(
        provider="tokenrhythm",
        model="legacy-no-tools",
        api_key="synthetic-fallback-key",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )

    class _Catalog:
        def lookup(self, _model_id: str, _provider: str = "") -> _ResolvedCatalog:
            raise AssertionError("exact deployment lookup must be used")

        def lookup_deployment(
            self,
            deployment: Any,
            *,
            include_global_overrides: bool = False,
        ) -> _ResolvedCatalog:
            del include_global_overrides
            if deployment is active:
                return _ResolvedCatalog(
                    max_tokens=16_384,
                    context_window=200_000,
                    capabilities=ModelCapabilities(supports_tools=True),
                    tools_capability_verified=True,
                )
            assert deployment is fallback
            return _ResolvedCatalog(
                max_tokens=8_192,
                context_window=32_000,
                capabilities=ModelCapabilities(supports_tools=False),
                tools_capability_verified=True,
            )

    class _Provider:
        private_limits: list[tuple[Any, int, int, ModelCapabilities | None]] = []

        def active_deployment_config(self) -> Any:
            return active

        def fallback_deployment_configs(self) -> tuple[Any, ...]:
            return (fallback,)

        def configure_fallback_deployment_limits(
            self,
            limits: list[tuple[Any, int, int, ModelCapabilities | None]],
        ) -> None:
            self.private_limits = limits

        def configure_fallback_deployment_vision_support(
            self,
            _entries: list[tuple[Any, str]],
        ) -> None:
            return None

    provider = _Provider()
    out = await _make_stage(catalog=_Catalog()).run(
        _make_input(
            provider=provider,
            resolved_model="glm-5.2",
            active_provider_id="tokenrhythm",
        )
    )

    assert out.output.agent_config.model_tools_capability_verified is True
    assert provider.private_limits[0][0] is fallback
    assert provider.private_limits[0][3] == ModelCapabilities(supports_tools=False)


@pytest.mark.asyncio
async def test_unverified_ensemble_aggregator_overrides_inherited_tool_denial() -> None:
    inherited_catalog = _RecordingModelCatalog(
        catalog=_ResolvedCatalog(
            max_tokens=16_384,
            context_window=200_000,
            capabilities=ModelCapabilities(supports_tools=False),
            tools_capability_verified=True,
        )
    )
    provider = SimpleNamespace(
        artifact_tool_executor_capabilities=ModelCapabilities(supports_tools=True),
        artifact_tools_capability_verified=False,
    )

    out = await _make_stage(catalog=inherited_catalog).run(
        _make_input(
            provider=provider,
            resolved_model="inherited-no-tools",
            active_provider_id="tokenrhythm",
        )
    )

    assert out.output.model_capabilities == ModelCapabilities(supports_tools=True)
    assert out.output.agent_config.model_capabilities == ModelCapabilities(
        supports_tools=True
    )
    assert out.output.agent_config.model_tools_capability_verified is False


@pytest.mark.asyncio
async def test_case02_per_call_timeout_threaded() -> None:
    budgets = _RecordingTimeoutBudget()
    stage = _make_stage(budgets=budgets)
    inp = _make_input(timeout=42.0)
    await stage.run(inp)
    assert budgets.calls[0]["timeout"] == 42.0


@pytest.mark.asyncio
async def test_case03_per_call_iteration_timeout_threaded() -> None:
    budgets = _RecordingTimeoutBudget()
    stage = _make_stage(budgets=budgets)
    inp = _make_input(iteration_timeout=15.0)
    await stage.run(inp)
    assert budgets.calls[0]["iteration_timeout"] == 15.0


@pytest.mark.asyncio
async def test_case04_session_max_iterations_threaded() -> None:
    budgets = _RecordingTimeoutBudget(
        budgets=replace(_default_budgets(), max_iterations=5)
    )
    stage = _make_stage(budgets=budgets)
    inp = _make_input(max_iterations=5)
    out = await stage.run(inp)
    assert out.output.effective_max_iterations == 5
    assert out.output.agent_config.metadata["agent_max_iterations"] == 5


@pytest.mark.asyncio
async def test_case05_no_model_catalog_fallback() -> None:
    """Catalog fallback when adapter returns 8192/200_000/None."""
    catalog = _RecordingModelCatalog(
        catalog=_ResolvedCatalog(
            max_tokens=8192, context_window=200_000, capabilities=None
        )
    )
    stage = _make_stage(catalog=catalog)
    inp = _make_input()
    out = await stage.run(inp)
    assert out.output.agent_config.max_tokens == 8192
    assert out.output.agent_config.context_window_tokens == 200_000
    assert out.output.model_capabilities is None
    assert out.output.agent_config.metadata["resolved_output_cap_tokens"] == 8192
    assert out.output.agent_config.metadata["resolved_context_window_tokens"] == 200_000


@pytest.mark.asyncio
async def test_catalog_sampling_controls_thread_to_agent_config() -> None:
    catalog = _RecordingModelCatalog(
        catalog=_ResolvedCatalog(
            max_tokens=8192,
            context_window=200_000,
            capabilities=None,
            temperature=1.0,
            top_p=0.95,
        )
    )
    stage = _make_stage(catalog=catalog)

    out = await stage.run(_make_input())

    assert out.output.agent_config.temperature == 1.0
    assert out.output.agent_config.top_p == 0.95


@pytest.mark.asyncio
async def test_global_context_window_override_threads_to_agent_config() -> None:
    catalog = _RecordingModelCatalog(
        catalog=_ResolvedCatalog(
            max_tokens=2_048,
            context_window=8_192,
            context_window_tokens_global_override=8_192,
            capabilities=None,
        )
    )
    stage = _make_stage(catalog=catalog)

    out = await stage.run(_make_input())

    assert out.output.agent_config.context_window_tokens == 8_192
    assert out.output.agent_config.context_window_tokens_global_override == 8_192


@pytest.mark.asyncio
async def test_case06_model_with_capabilities_and_projection_limit() -> None:
    caps = SimpleNamespace(supports_reasoning=True)
    catalog = _RecordingModelCatalog(
        catalog=_ResolvedCatalog(
            max_tokens=8192, context_window=200_000, capabilities=caps
        )
    )
    aux_builder = _RecordingAgentConfigBuilder(
        aux=replace(
            _default_aux(thinking=True),
            tool_result_projection_max_inline_chars=1234,
            tool_result_fresh_diagnostic_policy_enabled=True,
            tool_result_diagnostic_retrieval_gate_enabled=True,
            tool_result_fresh_diagnostic_inline_max_chars=12_345,
        )
    )
    factory = _RecordingAgentFactory()
    stage = _make_stage(catalog=catalog, aux=aux_builder, factory=factory)
    inp = _make_input(cloned_selector=SimpleNamespace())
    out = await stage.run(inp)
    assert out.output.model_capabilities is caps
    assert out.output.agent_config.thinking is True
    assert out.output.agent_config.tool_result_projection_max_inline_chars == 1234
    assert out.output.agent_config.tool_result_fresh_diagnostic_policy_enabled is True
    assert out.output.agent_config.tool_result_diagnostic_retrieval_gate_enabled is True
    assert out.output.agent_config.tool_result_fresh_diagnostic_inline_max_chars == 12_345


@pytest.mark.asyncio
async def test_fresh_diagnostic_env_overrides_agent_token_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_POLICY_ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_DIAGNOSTIC_RETRIEVAL_GATE_ENABLED", "1")
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_INLINE_MAX_CHARS", "2048")
    aux_builder = _RecordingAgentConfigBuilder(
        aux=replace(
            _default_aux(),
            tool_result_fresh_diagnostic_policy_enabled=False,
            tool_result_diagnostic_retrieval_gate_enabled=False,
            tool_result_fresh_diagnostic_inline_max_chars=64_000,
        )
    )
    stage = _make_stage(aux=aux_builder)

    out = await stage.run(_make_input())

    assert out.output.agent_config.tool_result_fresh_diagnostic_policy_enabled is True
    assert out.output.agent_config.tool_result_diagnostic_retrieval_gate_enabled is True
    assert out.output.agent_config.tool_result_fresh_diagnostic_inline_max_chars == 2048


@pytest.mark.asyncio
async def test_text_only_tool_recovery_env_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_TEXT_ONLY_TOOL_RECOVERY_MODE", "warn_model")
    stage = _make_stage(
        aux=_RecordingAgentConfigBuilder(
            aux=_default_aux(text_only_tool_recovery_mode="off")
        )
    )

    out = await stage.run(_make_input())

    assert out.output.agent_config.text_only_tool_recovery_mode == "warn_model"


@pytest.mark.asyncio
async def test_finalize_evidence_gate_config_value_propagates(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", raising=False)
    stage = _make_stage(
        aux=_RecordingAgentConfigBuilder(
            aux=replace(_default_aux(), finalize_evidence_gate=True)
        )
    )

    out = await stage.run(_make_input())

    assert out.output.agent_config.finalize_evidence_gate_enabled is True


@pytest.mark.asyncio
async def test_finalize_evidence_gate_env_off_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_FINALIZE_EVIDENCE_GATE", "off")
    stage = _make_stage(
        aux=_RecordingAgentConfigBuilder(
            aux=replace(_default_aux(), finalize_evidence_gate=True)
        )
    )

    out = await stage.run(_make_input())

    assert out.output.agent_config.finalize_evidence_gate_enabled is False


@pytest.mark.asyncio
async def test_case06b_dispatch_tool_result_budget_propagates() -> None:
    aux_builder = _RecordingAgentConfigBuilder(
        aux=replace(
            _default_aux(),
            tool_result_dispatch_max_chars=120_000,
            tool_result_dispatch_turn_max_chars=300_000,
        )
    )
    stage = _make_stage(aux=aux_builder)
    out = await stage.run(_make_input())

    assert out.output.agent_config.tool_result_dispatch_max_chars == 120_000
    assert out.output.agent_config.tool_result_dispatch_turn_max_chars == 300_000


@pytest.mark.asyncio
async def test_case08_sync_manager_warm() -> None:
    sync_manager = SimpleNamespace(label="memsync")
    snap = _RecordingMemorySnapshot(
        result=_MemorySnapshotResult(
            sync_manager=sync_manager, private_memory_allowed=True
        )
    )
    factory = _RecordingAgentFactory()
    stage = _make_stage(snapshot=snap, factory=factory)
    inp = _make_input()
    out = await stage.run(inp)
    assert snap.calls == 1
    assert snap.last_kwargs == {"agent_id": "agent:main", "session_key": "agent:main:s1"}
    assert out.output.sync_manager is sync_manager
    assert factory.last_kwargs["memory_sync_manager"] is sync_manager


@pytest.mark.asyncio
async def test_case09_private_memory_disabled() -> None:
    snap = _RecordingMemorySnapshot(
        result=_MemorySnapshotResult(
            sync_manager=None, private_memory_allowed=False
        )
    )
    stage = _make_stage(snapshot=snap)
    inp = _make_input()
    out = await stage.run(inp)
    assert out.output.private_memory_allowed is False


@pytest.mark.asyncio
async def test_case10_snapshot_already_exists() -> None:
    """Stage returns whatever the port reports — port handles existence check."""
    snap = _RecordingMemorySnapshot(
        result=_MemorySnapshotResult(
            sync_manager=None, private_memory_allowed=True
        )
    )
    stage = _make_stage(snapshot=snap)
    inp = _make_input()
    out = await stage.run(inp)
    assert snap.calls == 1
    assert out.output.private_memory_allowed is True


@pytest.mark.asyncio
async def test_case11_max_iterations_zero_threads_as_unlimited() -> None:
    budgets = _RecordingTimeoutBudget(
        budgets=replace(
            _default_budgets(),
            max_iterations=0,
            max_iterations_source="explicit argument",
        )
    )
    stage = _make_stage(budgets=budgets)
    inp = _make_input(max_iterations=0)
    out = await stage.run(inp)
    assert out.output.effective_max_iterations == 0
    assert out.output.effective_max_iterations_source == "explicit argument"
    assert out.output.agent_config.max_iterations == 0
    assert out.output.agent_config.metadata["agent_max_iterations"] == 0
    assert (
        out.output.agent_config.metadata["agent_max_iterations_source"]
        == "explicit argument"
    )


@pytest.mark.asyncio
async def test_memory_snapshot_failure_propagates() -> None:
    """Synthetic raising MemorySnapshotPort fake for the warm failure path."""
    snap = _RecordingMemorySnapshot(raises=RuntimeError)
    stage = _make_stage(snapshot=snap)
    inp = _make_input()
    with pytest.raises(RuntimeError, match="recording memory snapshot boom"):
        await stage.run(inp)


@pytest.mark.asyncio
async def test_agent_factory_receives_typed_inputs() -> None:
    """Sanity check that AgentFactoryPort.build receives all the typed kwargs."""
    factory = _RecordingAgentFactory()
    handler = SimpleNamespace(name="handler")
    logger = SimpleNamespace(name="logger")
    tool_context = SimpleNamespace(name="tool-context")
    stage = _make_stage(factory=factory)
    turn = _make_turn(metadata={"cache_mode": "automatic"}, tool_defs=[1, 2, 3])
    inp = _make_input(
        provider=SimpleNamespace(provider_name="p"),
        turn=turn,
        tool_handler=handler,
        turn_call_logger=logger,
        tool_context=tool_context,
    )
    await stage.run(inp)
    kw = factory.last_kwargs
    assert kw["tool_handler"] is handler
    assert kw["turn_call_logger"] is logger
    assert kw["tool_context"] is tool_context
    assert kw["session_key"] == "agent:main:s1"
    assert kw["tool_definitions"] == [1, 2, 3]
    assert kw["config"].cache_mode == "automatic"


@pytest.mark.asyncio
async def test_turn_metadata_threaded_into_agent_config() -> None:
    """``cache_mode``, ``skills_context_prompt``, and full metadata flow through."""
    turn = _make_turn(
        metadata={
            "cache_mode": "automatic",
            "skills_context_prompt": "SKILLS",
            "routed_tier": "premium",
        }
    )
    factory = _RecordingAgentFactory()
    stage = _make_stage(factory=factory)
    inp = _make_input(turn=turn)
    out = await stage.run(inp)
    assert out.output.agent_config.cache_mode == "automatic"
    assert out.output.agent_config.skills_context_prompt == "SKILLS"
    assert out.output.agent_config.metadata is turn.metadata
    assert out.output.agent_config.metadata.get("routed_tier") == "premium"
    assert turn.metadata["agent_max_iterations"] == 10
    assert turn.metadata["agent_max_iterations_source"] == "test budget"


def test_stage_name_constant() -> None:
    assert AgentBootstrapStage.name == "agent_bootstrap_stage"


def test_ports_runtime_checkable() -> None:
    assert isinstance(_RecordingTimeoutBudget(), TimeoutBudgetPort)
    assert isinstance(_RecordingModelCatalog(), ModelCatalogPort)
    assert isinstance(_RecordingAgentConfigBuilder(), AgentConfigBuilderPort)
    assert isinstance(_RecordingMemorySnapshot(), MemorySnapshotPort)
    assert isinstance(_RecordingAgentFactory(), AgentFactoryPort)


def test_value_objects_frozen() -> None:
    budgets = _default_budgets()
    with pytest.raises(Exception):  # noqa: BLE001 - dataclass FrozenInstanceError
        budgets.runtime_timeout = 1.0  # type: ignore[misc]

    catalog = _default_catalog()
    with pytest.raises(Exception):  # noqa: BLE001
        catalog.max_tokens = 1  # type: ignore[misc]

    aux = _default_aux()
    with pytest.raises(Exception):  # noqa: BLE001
        aux.flush_enabled = False  # type: ignore[misc]

    result = _MemorySnapshotResult(sync_manager=None, private_memory_allowed=True)
    with pytest.raises(Exception):  # noqa: BLE001
        result.sync_manager = None  # type: ignore[misc]


# replace lint suppress
_ = replace
