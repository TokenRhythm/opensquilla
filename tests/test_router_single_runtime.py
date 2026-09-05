from __future__ import annotations

import asyncio
import gc
import hashlib
import inspect
import threading
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _ROUTER_SINGLE_FROZEN_CATALOG,
    TurnRunner,
    _router_dynamic_cache_affinity_policy,
    _RouterDynamicCacheAffinityCollectionContext,
    _RouterDynamicCacheAffinityPolicy,
    _RouterDynamicCacheAffinityReceiptBatch,
    _RouterDynamicCacheReroutePlan,
    _RouterDynamicCacheRerouteResult,
    _RouterSingleDirectProvider,
    _SelectorFallbackProvider,
)
from opensquilla.engine.turn_runner.harness import (
    _TurnRunnerModelCatalogAdapter,
    _TurnRunnerPipelineExecutionAdapter,
)
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    PromptAssemblerStage,
    PromptAssemblerStageInput,
)
from opensquilla.engine.types import DoneEvent as EngineDone
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import (
    ChatConfig,
    Message,
    ProviderFailureKind,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    ErrorEvent as ProviderError,
)
from opensquilla.provider.cache_affinity import (
    CacheDomainGuard,
    build_cache_affinity_receipt,
    build_credential_namespace_token,
)
from opensquilla.provider.deployment import ProviderDeploymentResolution
from opensquilla.provider.ensemble import resolve_router_single_route
from opensquilla.provider.ranking_router import (
    DynamicRankingError,
    RankedModel,
    SingleModelRankingDecision,
    TaskAnalysisResult,
    ranking_config_snapshot,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider.types import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider.types import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider.types import (
    ToolUseStartEvent as ProviderToolUseStart,
)


class _NoChatProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        raise AssertionError("routing gate must not start generation")

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._cfg = config or ProviderConfig(
            provider="openrouter",
            model="openai/gpt-5.5",
            api_key="synthetic",
        )

    @property
    def current_config(self) -> ProviderConfig:
        return self._cfg

    @property
    def active_provider_id(self) -> str:
        return self._cfg.provider

    def override_model(self, model: str) -> None:
        self._cfg = replace(self._cfg, model=model)

    def override_provider_config(self, config: ProviderConfig) -> None:
        self._cfg = config

    def resolve(self) -> _NoChatProvider:
        return _NoChatProvider()


def _router_single_config() -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "router_dynamic",
        },
    )


def _fixed_four_tier_v2_config(*, mock_seed: int = 7) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=True, rollout_phase="full"),
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
            "four_tier_mapping": {"mock_seed": mock_seed},
        },
    )


_REGISTERED_MODEL_MANIFEST_HASH = "sha256:" + ("a" * 64)


def _registered_fixed_four_tier_v2_config(
    tmp_path: Any,
    *,
    model_set_id: str = "router-runtime-a1",
) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=True, rollout_phase="full"),
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
            "four_tier_mapping": {
                "classifier": {
                    "backend": "registered_model",
                    "artifact_root": str(tmp_path / "router-artifacts"),
                    "metadata_db": str(tmp_path / "router-metadata.sqlite3"),
                    "model_set_id": model_set_id,
                    "expected_manifest_hash": _REGISTERED_MODEL_MANIFEST_HASH,
                }
            },
        },
    )


def _patch_fake_registered_model_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    from opensquilla.engine.routing import registered_model as registered_model_module
    from opensquilla.engine.routing.fixed_four_tier_v2 import ClassifierPrediction

    instances: list[Any] = []

    class FakeRegisteredModelClassifier:
        backend = "registered_model"
        feature_schema_version = "lightgbm_380.v1"
        feature_vector_dim = 380
        feature_vector_status = "materialized"

        def __init__(self, **kwargs: Any) -> None:
            self.options = dict(kwargs)
            self.identity = {
                "schema_version": "local_runner_identity.v2",
                "model_set_id": kwargs["model_set_id"],
                "model_manifest_hash": kwargs["expected_manifest_hash"],
                "artifact_closure_hash": "sha256:" + ("b" * 64),
                "runner_digest": "sha256:" + ("c" * 64),
                "environment_digest": "sha256:" + ("d" * 64),
                "model_type": "lightgbm",
                "execution_mode": "native_embedded",
                "registry_status": "VALIDATED",
                "input_schema_version": self.feature_schema_version,
            }
            self.version = f"{kwargs['model_set_id']}@fake"
            self.calls: list[tuple[dict[str, Any], tuple[str, ...] | None]] = []
            self.close_calls = 0
            instances.append(self)

        def predict(
            self,
            snapshot: Any,
            allowed_tiers: Any = None,
        ) -> ClassifierPrediction:
            normalized_tiers = tuple(allowed_tiers) if allowed_tiers is not None else None
            self.calls.append((dict(snapshot), normalized_tiers))
            if normalized_tiers is None:
                return ClassifierPrediction(
                    label="continue",
                    probabilities={"continue": 0.9, "redo": 0.05, "new_task": 0.05},
                    confidence=0.9,
                    version=self.version,
                )
            return ClassifierPrediction(
                label="c1",
                probabilities={"c0": 0.05, "c1": 0.85, "c2": 0.05, "c3": 0.05},
                confidence=0.85,
                version=self.version,
            )

        def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(
        registered_model_module,
        "RegisteredModelClassifier",
        FakeRegisteredModelClassifier,
    )
    return instances


class _FixedRouteSessionManager:
    def __init__(self, session_id: str = "fixed-session") -> None:
        self.session_id = session_id
        self.state: Any | None = None
        self.decisions: dict[str, Any] = {}
        self.claims: dict[tuple[str, str], Any] = {}
        self.transcript: list[Any] = []
        self.settlements: list[dict[str, Any]] = []
        self.response_bindings: dict[str, dict[str, Any]] = {}

    async def get_session(self, session_key: str) -> Any:
        del session_key
        return SimpleNamespace(session_id=self.session_id, epoch=0)

    async def get_fixed_four_tier_state(self, session_id: str) -> Any | None:
        assert session_id == self.session_id
        return self.state

    async def get_transcript(self, session_key: str) -> list[Any]:
        del session_key
        return list(self.transcript)

    async def merge_message_turn_context(
        self,
        session_key: str,
        message_id: str,
        turn_context_patch: dict[str, Any],
    ) -> bool:
        del session_key
        self.response_bindings[message_id] = {
            **self.response_bindings.get(message_id, {}),
            **turn_context_patch,
        }
        return True

    async def reconcile_stale_fixed_four_tier_request(
        self,
        *,
        session_id: str,
        request_id: str,
        now_ms: int | None = None,
    ) -> Any | None:
        assert session_id == self.session_id
        claim = self.claims.get((session_id, request_id))
        if (
            claim is not None
            and claim.status in {"claimed", "materialized"}
            and now_ms is not None
            and claim.lease_expires_at_ms <= now_ms
        ):
            claim.status = "failed"
            claim.error_code = "execution_lease_expired"
        return claim

    async def claim_fixed_four_tier_request(self, claim: Any) -> tuple[bool, Any]:
        key = (claim.session_id, claim.request_id)
        existing = self.claims.get(key)
        if existing is not None:
            return False, existing
        self.claims[key] = claim
        return True, claim

    async def settle_fixed_four_tier_request_claim(self, **kwargs: Any) -> bool:
        for claim in self.claims.values():
            if claim.claim_id == kwargs["claim_id"]:
                claim.status = kwargs["execution_status"]
                claim.error_code = kwargs.get("error_code")
                return True
        return False

    async def get_fixed_four_tier_decision_by_request(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> Any | None:
        assert session_id == self.session_id
        return next(
            (
                decision
                for decision in self.decisions.values()
                if getattr(decision, "request_id", None) == request_id
            ),
            None,
        )

    async def get_fixed_four_tier_decision_by_route(self, route_id: str) -> Any | None:
        return self.decisions.get(route_id)

    async def get_fixed_four_tier_decision_by_input_message(
        self,
        *,
        session_id: str,
        input_message_id: str,
    ) -> Any | None:
        del input_message_id
        assert session_id == self.session_id
        return None

    async def stage_fixed_four_tier_decision(self, record: Any) -> Any:
        self.decisions[record.route_id] = record
        claim = self.claims[(record.session_id, record.request_id)]
        claim.status = "materialized"
        claim.route_id = record.route_id
        return record

    async def commit_fixed_four_tier_decision(
        self,
        *,
        route_id: str,
        state: Any,
        expected_version: int | None,
        route_trace: dict[str, Any],
        updated_at_ms: int,
    ) -> Any:
        del expected_version, route_trace, updated_at_ms
        assert route_id in self.decisions
        self.state = state
        return state

    async def settle_fixed_four_tier_decision(self, **kwargs: Any) -> bool:
        assert kwargs["route_id"] in self.decisions
        self.settlements.append(dict(kwargs))
        record = self.decisions[kwargs["route_id"]]
        record.execution_status = kwargs["execution_status"]
        if kwargs.get("route_trace") is not None:
            record.route_trace = kwargs["route_trace"]
        await self.settle_fixed_four_tier_request_claim(
            claim_id=record.claim_id,
            execution_status=kwargs["execution_status"],
            error_code=kwargs.get("error_code"),
        )
        return True

    async def get_usage_event_ids_for_turn(self, **kwargs: Any) -> list[str]:
        del kwargs
        return []


async def test_explicit_model_returns_before_router_single_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    called = False

    async def fail_if_called(**kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("explicit model must skip router_single Analyzer")

    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_if_called)
    for method_name in (
        "_ensure_router_dynamic_cache_compaction_listener",
        "_resolve_router_dynamic_session_epoch",
        "_router_single_cache_continuity_snapshot",
        "_register_router_dynamic_cache_sidecar",
        "_stage_router_dynamic_cache_affinity_batch",
    ):
        monkeypatch.setattr(
            runner,
            method_name,
            lambda *args, _name=method_name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"explicit model touched cache affinity: {_name}")
            ),
        )
    original = _NoChatProvider()
    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:explicit",
        original,
        _Selector(),
        [],
        "system",
        [],
        explicit_model="openai/gpt-5.6",
    )

    assert called is False
    assert provider is not original
    assert isinstance(provider, _NoChatProvider)
    assert "router_single_decision" not in turn.metadata
    assert "ensemble_enabled" not in turn.metadata


class _StagePromptAssembler:
    def assemble_prompt(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "system"


class _StageRouterContext:
    async def fetch_router_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {}


class _StagePromptConfigResolver:
    def resolve_prompt_config(self, turn: Any) -> tuple[str, None, None]:
        del turn
        return "system", None, None


class _StagePromptReportBuilder:
    def build_prompt_report(self, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)


class _StageSessionIdResolver:
    async def resolve_session_id_for_log(self, session_key: str) -> str:
        del session_key
        return "session-id"


class _StageMemoryFingerprint:
    def memory_mode_fingerprint(self) -> None:
        return None


async def test_prompt_stage_explicit_model_wins_after_router_single_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    analyzer_called = False

    async def fail_if_called(**kwargs: Any) -> Any:
        nonlocal analyzer_called
        analyzer_called = True
        raise AssertionError("explicit model must skip router_single Analyzer")

    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_if_called)
    selector = _Selector()
    original = _NoChatProvider()
    stage = PromptAssemblerStage(
        prompt_assembler=_StagePromptAssembler(),
        pipeline_executor=_TurnRunnerPipelineExecutionAdapter(runner),
        router_context=_StageRouterContext(),
        prompt_config_resolver=_StagePromptConfigResolver(),
        prompt_report_builder=_StagePromptReportBuilder(),
        session_id_resolver=_StageSessionIdResolver(),
        memory_fingerprint=_StageMemoryFingerprint(),
    )
    explicit_model = "openai/gpt-5.6"

    outcome = await stage.run(
        PromptAssemblerStageInput(
            runtime_message="hello",
            semantic_input="hello",
            extra_prompt_context=None,
            provider=original,
            cloned_selector=selector,
            tool_defs=[],
            effective_tool_context=None,
            tool_metadata={},
            session_key="agent:main:explicit-stage",
            agent_id="main",
            turn_id="turn-explicit-stage",
            attachments=[],
            bootstrap_context_mode=None,
            model=explicit_model,
            history_has_persisted_user=False,
            persist_input=False,
        )
    )

    result = outcome.output
    assert analyzer_called is False
    assert selector.current_config.model == explicit_model
    assert result.resolved_model == explicit_model
    assert result.turn.metadata["executed_model"] == explicit_model
    assert "router_single_decision" not in result.turn.metadata
    assert result.provider.primary is not original


async def test_router_single_early_return_never_enters_fusion_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    direct = object()
    calls: list[dict[str, Any]] = []

    async def resolve_direct(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return direct

    monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_direct)
    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:single",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
    )

    assert provider is direct
    assert len(calls) == 1
    assert calls[0]["ensemble_cfg"].mode == "single"
    assert "ensemble_decision_id" not in turn.metadata
    assert "ensemble_enabled" not in turn.metadata


async def test_fixed_four_tier_v2_uses_only_its_isolated_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    direct = object()
    fixed_calls: list[dict[str, Any]] = []

    async def resolve_fixed(**kwargs: Any) -> Any:
        fixed_calls.append(kwargs)
        return direct

    async def fail_legacy(**kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("fixed_four_tier_v2 touched the legacy Analyzer route")

    monkeypatch.setattr(runner, "_resolve_fixed_four_tier_v2_provider", resolve_fixed)
    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_legacy)

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:fixed-four-tier",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
    )

    assert provider is direct
    assert len(fixed_calls) == 1
    assert fixed_calls[0]["ensemble_cfg"].selection_mode == "four_tier_mapping"
    assert turn.metadata["fixed_four_tier_v2_legacy_router_skipped"] is True
    assert "ensemble_decision_id" not in turn.metadata
    assert "ensemble_enabled" not in turn.metadata


async def test_fixed_four_tier_pipeline_forwards_only_trusted_route_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    captured_metadata: list[dict[str, Any]] = []

    async def resolve_fixed(**kwargs: Any) -> Any:
        captured_metadata.append(dict(kwargs["turn"].metadata))
        return object()

    monkeypatch.setattr(runner, "_resolve_fixed_four_tier_v2_provider", resolve_fixed)
    trusted = {
        "fixed_four_tier_v2_control_event": "redo",
        "fixed_four_tier_v2_redo_parent_session_key": "agent:main:parent",
        "fixed_four_tier_v2_redo_parent_session_id": "parent-id",
        "fixed_four_tier_v2_redo_of_message_id": "parent-input",
        "fixed_four_tier_v2_redo_child_task_start_input_message_id": "child-start",
        "untrusted_extra": "must-not-cross",
    }

    turn, _ = await runner._run_pipeline(
        "hello",
        "agent:main:trusted-fixed-route",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
        trusted_route_metadata=trusted,
    )

    assert len(captured_metadata) == 1
    for key, value in trusted.items():
        if key not in {
            "fixed_four_tier_v2_redo_parent_session_key",
            "untrusted_extra",
        }:
            assert captured_metadata[0][key] == value
    assert "fixed_four_tier_v2_redo_parent_session_key" not in captured_metadata[0]
    assert "untrusted_extra" not in captured_metadata[0]
    assert "untrusted_extra" not in turn.metadata


async def test_legacy_pipeline_ignores_trusted_fixed_route_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    captured_metadata: list[dict[str, Any]] = []

    async def resolve_legacy(**kwargs: Any) -> Any:
        captured_metadata.append(dict(kwargs["turn"].metadata))
        return object()

    monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_legacy)
    turn, _ = await runner._run_pipeline(
        "hello",
        "agent:main:legacy-route-metadata",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
        trusted_route_metadata={
            "fixed_four_tier_v2_control_event": "redo",
            "fixed_four_tier_v2_redo_parent_session_id": "parent-id",
        },
    )

    assert len(captured_metadata) == 1
    assert "fixed_four_tier_v2_control_event" not in captured_metadata[0]
    assert "fixed_four_tier_v2_redo_parent_session_id" not in turn.metadata


async def test_router_dynamic_never_calls_fixed_four_tier_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    direct = object()

    async def resolve_legacy(**kwargs: Any) -> Any:
        del kwargs
        return direct

    async def fail_fixed(**kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("router_dynamic touched fixed_four_tier_v2")

    monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_legacy)
    monkeypatch.setattr(runner, "_resolve_fixed_four_tier_v2_provider", fail_fixed)

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:router-dynamic-control",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
    )

    assert provider is direct
    assert "fixed_four_tier_v2_legacy_router_skipped" not in turn.metadata


async def test_switching_away_from_fixed_four_tier_releases_cached_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    cached_router = runner._fixed_four_tier_v2_router_for_config(runner._config.llm_ensemble)
    close_threads: list[str] = []

    def record_close() -> None:
        close_threads.append(threading.current_thread().name)

    monkeypatch.setattr(cached_router, "close", record_close)
    runner._config = _router_single_config()

    async def resolve_legacy(**kwargs: Any) -> Any:
        del kwargs
        return object()

    monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_legacy)
    try:
        await runner._run_pipeline(
            "hello",
            "agent:main:fixed-mode-retired",
            _NoChatProvider(),
            _Selector(),
            [],
            "system",
            [],
        )

        assert close_threads == ["opensquilla-fixed-four-tier_0"]
        assert runner._fixed_four_tier_v2_routers == {}
    finally:
        await runner.aclose()


async def test_switching_away_release_timeout_does_not_block_non_fixed_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_FIXED_FOUR_TIER_V2_RELEASE_TIMEOUT_SECONDS", 0.05)
    warnings: list[tuple[str, dict[str, Any]]] = []

    class RecordingLog:
        def warning(self, event: str, **kwargs: Any) -> None:
            warnings.append((event, kwargs))

    monkeypatch.setattr(runtime_module, "log", RecordingLog())
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    native_started = threading.Event()
    native_release = threading.Event()
    events: list[str] = []

    class CachedRouter:
        def close(self) -> None:
            events.append("router-close")

    runner._fixed_four_tier_v2_routers["cached"] = CachedRouter()
    runner._fixed_four_tier_v2_router_cache_populated = True

    def hung_native_call() -> None:
        events.append("native-start")
        native_started.set()
        assert native_release.wait(timeout=3.0)
        events.append("native-end")

    native_waiter = asyncio.create_task(runner._run_fixed_four_tier_v2_job(hung_native_call))
    try:
        for _ in range(100):
            if native_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert native_started.is_set()

        runner._config = _router_single_config()

        async def resolve_legacy(**kwargs: Any) -> Any:
            del kwargs
            return object()

        monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_legacy)
        loop = asyncio.get_running_loop()
        switched_at = loop.time()
        await runner._run_pipeline(
            "hello",
            "agent:main:fixed-release-timeout",
            _NoChatProvider(),
            _Selector(),
            [],
            "system",
            [],
        )

        assert loop.time() - switched_at < 0.5
        assert events == ["native-start"]
        assert [event for event, _ in warnings] == ["fixed_four_tier_v2.release_timed_out"]

        # The same pending release was already bounded once; later non-fixed
        # turns do not each inherit another timeout-sized delay.
        await asyncio.wait_for(
            runner._release_fixed_four_tier_v2_routers(),
            timeout=0.02,
        )

        native_release.set()
        await native_waiter
        await runner.aclose()

        assert events == ["native-start", "native-end", "router-close"]
        assert [event for event, _ in warnings].count("fixed_four_tier_v2.release_timed_out") == 1
    finally:
        native_release.set()
        if not native_waiter.done():
            await native_waiter
        await runner.aclose()


async def test_explicit_model_cannot_bypass_fixed_four_tier_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    fixed_calls: list[dict[str, Any]] = []
    direct = object()

    async def resolve_fixed(**kwargs: Any) -> Any:
        fixed_calls.append(kwargs)
        return direct

    async def fail_legacy(**kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("fixed mode touched the legacy routing classifier")

    async def fail_prompt_cache(turn: Any) -> Any:
        del turn
        raise AssertionError("fixed mode applied a pre-route legacy prompt cache policy")

    monkeypatch.setattr(runner, "_resolve_fixed_four_tier_v2_provider", resolve_fixed)
    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_legacy)
    monkeypatch.setattr("opensquilla.engine.steps.apply_prompt_cache", fail_prompt_cache)

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:fixed-explicit",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
        explicit_model="openai/gpt-5.6",
    )

    assert provider is direct
    assert len(fixed_calls) == 1
    # resolve_model may write a temporary turn.model, but the fixed resolver
    # receives the untouched selector and remains the only model authority.
    assert fixed_calls[0]["cloned_selector"].current_config.model == "openai/gpt-5.5"
    assert turn.metadata["fixed_four_tier_v2_prompt_cache_skipped"] is True


def test_fusion_gate_remains_the_original_unqualified_block() -> None:
    source = inspect.getsource(TurnRunner._run_pipeline)
    original_gate = 'if provider is not None and getattr(ensemble_cfg, "enabled", False):'

    assert source.count(original_gate) == 1
    assert source.index("if router_single_mode:") < source.index(original_gate)
    assert "if provider is not None and router_single_mode" not in source


def test_router_single_does_not_read_or_write_b5_last_route_memory() -> None:
    single_source = inspect.getsource(TurnRunner._resolve_router_single_provider)
    pipeline_source = inspect.getsource(TurnRunner._run_pipeline)

    assert "_previous_router_dynamic_route" not in single_source
    assert "router_dynamic_last_route" not in single_source
    # The pre-existing B5 branch retains its continuity lookup and commit path.
    assert "_previous_router_dynamic_route" in pipeline_source
    assert "router_dynamic_pending_route_plan" in pipeline_source


def test_fixed_four_tier_resolver_has_no_legacy_analyzer_or_fusion_dependency() -> None:
    source = inspect.getsource(TurnRunner._resolve_fixed_four_tier_v2_provider)

    assert "_resolve_router_single_provider" not in source
    assert "resolve_router_single_route" not in source
    assert "analyze_task" not in source
    assert "build_ensemble_provider_from_config" not in source
    assert "fallbacks=[]" in source


def test_fixed_four_tier_router_never_defaults_a_missing_classifier_to_random() -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    incomplete = SimpleNamespace(
        four_tier_mapping=SimpleNamespace(
            default_new_task_tier="c1",
            intent_min_confidence=0.5,
            tier_min_confidence=0.5,
            min_margin=0.05,
        )
    )

    with pytest.raises(RuntimeError, match="classifier configuration is unavailable"):
        runner._fixed_four_tier_v2_router_for_config(incomplete)

    assert runner._fixed_four_tier_v2_routers == {}


def test_registered_four_tier_config_shares_one_classifier_between_both_heads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    instances = _patch_fake_registered_model_classifier(monkeypatch)
    config = _registered_fixed_four_tier_v2_config(tmp_path)
    runner = TurnRunner(provider_selector=None, config=config)

    router = runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble)

    assert len(instances) == 1
    assert router._intent_classifier is instances[0]
    assert router._tier_classifier is instances[0]
    assert instances[0].options == {
        "artifact_root": str(tmp_path / "router-artifacts"),
        "metadata_db": str(tmp_path / "router-metadata.sqlite3"),
        "model_set_id": "router-runtime-a1",
        "expected_manifest_hash": _REGISTERED_MODEL_MANIFEST_HASH,
        "allow_candidate": False,
    }
    assert runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble) is router
    assert len(instances) == 1


def test_registered_four_tier_config_replacement_closes_old_shared_runner_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    instances = _patch_fake_registered_model_classifier(monkeypatch)
    first_config = _registered_fixed_four_tier_v2_config(
        tmp_path,
        model_set_id="router-runtime-a1",
    )
    second_config = _registered_fixed_four_tier_v2_config(
        tmp_path,
        model_set_id="router-runtime-a2",
    )
    runner = TurnRunner(provider_selector=None, config=first_config)

    first_router = runner._fixed_four_tier_v2_router_for_config(first_config.llm_ensemble)
    second_router = runner._fixed_four_tier_v2_router_for_config(second_config.llm_ensemble)

    assert first_router is not second_router
    assert len(instances) == 2
    assert instances[0].close_calls == 1
    assert instances[1].close_calls == 0
    assert list(runner._fixed_four_tier_v2_routers.values()) == [second_router]
    assert runner._fixed_four_tier_v2_router_for_config(second_config.llm_ensemble) is second_router
    assert len(instances) == 2
    assert instances[0].close_calls == 1
    assert instances[1].close_calls == 0


async def test_fixed_four_tier_private_executor_drains_cancelled_work_before_close() -> None:
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    started = threading.Event()
    release = threading.Event()
    events: list[tuple[str, str]] = []

    class CachedRouter:
        def close(self) -> None:
            events.append(("router-close", threading.current_thread().name))

    runner._fixed_four_tier_v2_routers["cached"] = CachedRouter()
    runner._fixed_four_tier_v2_router_cache_populated = True

    def blocked_native_call() -> str:
        events.append(("first-start", threading.current_thread().name))
        started.set()
        assert release.wait(timeout=2.0)
        events.append(("first-end", threading.current_thread().name))
        return "first"

    def queued_native_call() -> str:
        events.append(("second", threading.current_thread().name))
        return "second"

    first_waiter = asyncio.create_task(runner._run_fixed_four_tier_v2_job(blocked_native_call))
    second_waiter: asyncio.Task[Any] | None = None
    close_waiter: asyncio.Task[Any] | None = None
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        first_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_waiter

        second_waiter = asyncio.create_task(runner._run_fixed_four_tier_v2_job(queued_native_call))
        await asyncio.sleep(0)
        close_waiter = asyncio.create_task(runner.aclose())
        await asyncio.sleep(0.05)

        assert [event for event, _ in events] == ["first-start"]
        assert not close_waiter.done()

        release.set()
        assert await second_waiter == "second"
        await close_waiter
        await runner.close()

        assert [event for event, _ in events] == [
            "first-start",
            "first-end",
            "second",
            "router-close",
        ]
        assert {thread for _, thread in events} == {"opensquilla-fixed-four-tier_0"}
        assert runner._fixed_four_tier_v2_executor._max_workers == 1
        assert runner._fixed_four_tier_v2_executor._shutdown is True
        with pytest.raises(RuntimeError, match="runtime is closed"):
            await runner._run_fixed_four_tier_v2_job(lambda: None)
    finally:
        release.set()
        if second_waiter is not None and not second_waiter.done():
            await second_waiter
        if close_waiter is not None and not close_waiter.done():
            await close_waiter
        await runner.aclose()


async def test_fixed_four_tier_private_executor_bounds_and_cancels_queued_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_FIXED_FOUR_TIER_V2_MAX_OUTSTANDING_JOBS", 2)
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    started = threading.Event()
    release = threading.Event()
    executed: list[str] = []

    def blocked_native_call() -> str:
        executed.append(threading.current_thread().name)
        started.set()
        assert release.wait(timeout=3.0)
        return "running"

    running_waiter = asyncio.create_task(runner._run_fixed_four_tier_v2_job(blocked_native_call))
    queued_waiter: asyncio.Task[Any] | None = None
    final_waiter: asyncio.Task[Any] | None = None
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        queued_waiter = asyncio.create_task(
            runner._run_fixed_four_tier_v2_job(lambda: executed.append("stale"))
        )
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="queue is full"):
            await runner._run_fixed_four_tier_v2_job(lambda: executed.append("overflow"))

        queued_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_waiter

        # Repeated disconnected requests reclaim their queue slot immediately;
        # none can accumulate behind the one intentionally blocked native call.
        for index in range(25):
            disconnected = asyncio.create_task(
                runner._run_fixed_four_tier_v2_job(
                    lambda index=index: executed.append(f"disconnected-{index}")
                )
            )
            await asyncio.sleep(0)
            disconnected.cancel()
            with pytest.raises(asyncio.CancelledError):
                await disconnected

        assert len(runner._fixed_four_tier_v2_pending_futures) == 1
        final_waiter = asyncio.create_task(
            runner._run_fixed_four_tier_v2_job(lambda: executed.append("final"))
        )
        await asyncio.sleep(0)
        release.set()

        assert await running_waiter == "running"
        assert await final_waiter is None
        assert executed == ["opensquilla-fixed-four-tier_0", "final"]
    finally:
        release.set()
        if not running_waiter.done():
            await running_waiter
        if final_waiter is not None and not final_waiter.done():
            await final_waiter
        await runner.aclose()


async def test_fixed_four_tier_close_timeout_leaves_running_model_owned_until_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_FIXED_FOUR_TIER_V2_CLOSE_TIMEOUT_SECONDS", 0.05)
    warnings: list[tuple[str, dict[str, Any]]] = []

    class RecordingLog:
        def warning(self, event: str, **kwargs: Any) -> None:
            warnings.append((event, kwargs))

    monkeypatch.setattr(runtime_module, "log", RecordingLog())
    runner = TurnRunner(provider_selector=None, config=_fixed_four_tier_v2_config())
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    class CachedRouter:
        def close(self) -> None:
            events.append("router-close")

    runner._fixed_four_tier_v2_routers["cached"] = CachedRouter()
    runner._fixed_four_tier_v2_router_cache_populated = True

    def hung_native_call() -> str:
        events.append("native-start")
        started.set()
        assert release.wait(timeout=3.0)
        events.append("native-end")
        return "done"

    running_waiter = asyncio.create_task(runner._run_fixed_four_tier_v2_job(hung_native_call))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        loop = asyncio.get_running_loop()
        close_started = loop.time()
        await runner.aclose()
        close_elapsed = loop.time() - close_started

        assert close_elapsed < 0.5
        assert events == ["native-start"]
        assert runner._fixed_four_tier_v2_executor._shutdown is True
        assert [event for event, _ in warnings] == ["fixed_four_tier_v2.runtime_close_timed_out"]

        release.set()
        assert await running_waiter == "done"
        await runner.aclose()

        assert events == ["native-start", "native-end", "router-close"]
        assert [event for event, _ in warnings].count(
            "fixed_four_tier_v2.runtime_close_timed_out"
        ) == 1
    finally:
        release.set()
        if not running_waiter.done():
            await running_waiter
        await runner.aclose()


async def test_registered_router_construction_failure_rolls_back_classifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from opensquilla.engine.routing import fixed_four_tier_v2 as fixed_module

    instances = _patch_fake_registered_model_classifier(monkeypatch)
    config = _registered_fixed_four_tier_v2_config(tmp_path)
    runner = TurnRunner(provider_selector=None, config=config)

    class BrokenRouter:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("router construction failed")

    monkeypatch.setattr(fixed_module, "FixedFourTierV2Router", BrokenRouter)
    try:
        with pytest.raises(RuntimeError, match="router construction failed"):
            runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble)

        assert len(instances) == 1
        assert instances[0].close_calls == 1
        assert runner._fixed_four_tier_v2_routers == {}
    finally:
        await runner.aclose()


async def test_fixed_four_tier_close_failures_are_logged_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import runtime as runtime_module

    warnings: list[tuple[str, dict[str, Any]]] = []

    class RecordingLog:
        def warning(self, event: str, **kwargs: Any) -> None:
            warnings.append((event, kwargs))

    class BrokenCloseRouter:
        def close(self) -> None:
            raise RuntimeError("close failed")

    config = _fixed_four_tier_v2_config()
    runner = TurnRunner(provider_selector=None, config=config)
    runner._fixed_four_tier_v2_routers["old"] = BrokenCloseRouter()
    runner._fixed_four_tier_v2_router_cache_populated = True
    monkeypatch.setattr(runtime_module, "log", RecordingLog())

    replacement = runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble)
    monkeypatch.setattr(replacement, "close", BrokenCloseRouter().close)

    await runner.aclose()
    await runner.close()

    assert runner._fixed_four_tier_v2_routers == {}
    assert [kwargs["phase"] for event, kwargs in warnings if event.endswith("close_failed")] == [
        "config_replaced",
        "turn_runner_close",
    ]


def _ranked_model(
    model_id: str = "openai/gpt-5.5",
    *,
    capability: float = 0.9,
) -> RankedModel:
    return RankedModel(
        provider="openrouter",
        model_id=model_id,
        version="test",
        source="test",
        registry_facts={
            "provider": "openrouter",
            "model_id": model_id,
            "status": "enabled",
            "roles": ["proposer"],
            "supports_tools": True,
            "modalities": ["text"],
            "context_window": 200_000,
            "credential_available": True,
        },
        static_profile={"capability": capability},
        online_profile={},
        thinking=None,
    )


class _Catalog:
    def __init__(self) -> None:
        self.output = {
            "openai/gpt-5.5": 4_096,
            "anthropic/claude-sonnet-4.5": 8_192,
            "qwen/qwen3.7-flash": 8_192,
            "deepseek/deepseek-v4-flash": 8_192,
            "deepseek/deepseek-v4-pro": 8_192,
            "z-ai/glm-5.3": 8_192,
        }
        self.context = {
            "openai/gpt-5.5": 128_000,
            "anthropic/claude-sonnet-4.5": 200_000,
            "qwen/qwen3.7-flash": 128_000,
            "deepseek/deepseek-v4-flash": 128_000,
            "deepseek/deepseek-v4-pro": 128_000,
            "z-ai/glm-5.3": 128_000,
        }

    def resolve_max_tokens(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> int:
        del provider
        return user_override if user_override > 0 else self.output[model_id]

    def resolve_max_tokens_with_source(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> tuple[int, str]:
        return (
            self.resolve_max_tokens(model_id, user_override, provider),
            "override" if user_override > 0 else "catalog",
        )

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        del provider
        return self.context[model_id]

    def resolve_context_window_with_source(
        self,
        model_id: str,
        provider: str = "",
    ) -> tuple[int, str]:
        return self.resolve_context_window(model_id, provider), "catalog"

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "",
        base_url: str = "",
    ) -> Any:
        from opensquilla.provider import ModelCapabilities

        del model_id, provider_name, base_url
        return ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            supports_vision=True,
        )


async def test_registered_four_tier_resolver_requires_route_history_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierRoutingError

    instances = _patch_fake_registered_model_classifier(monkeypatch)
    config = _registered_fixed_four_tier_v2_config(tmp_path)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.list_recent_fixed_four_tier_decisions = None
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    turn = TurnContext(
        message="route this request",
        raw_message="route this request",
        session_key="agent:main:registered-route-history-required",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "registered-route-history-required"},
    )

    with pytest.raises(FixedFourTierRoutingError) as exc_info:
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="input-registered-route-history-required",
        )

    assert exc_info.value.reason == "route_history_storage_unavailable"
    assert instances == []
    assert manager.claims == {}


async def test_fixed_four_tier_resolver_materializes_exactly_one_model() -> None:
    from opensquilla.engine.pipeline import TurnContext

    config = _fixed_four_tier_v2_config(mock_seed=19)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="input-fixed-1",
            role="user",
            content="build a small parser",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    turn = TurnContext(
        message="build a small parser",
        raw_message="build a small parser",
        session_key="agent:main:fixed-materialize",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "request-fixed-1"},
    )

    provider = await runner._resolve_fixed_four_tier_v2_provider(
        turn=turn,
        provider=turn.provider,
        cloned_selector=selector,
        turn_config=config,
        ensemble_cfg=config.llm_ensemble,
        turn_absolute_deadline=None,
        bound_user_message_id="input-fixed-1",
    )

    fixed_models = {
        "qwen/qwen3.7-flash",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.3",
    }
    assert isinstance(provider, _SelectorFallbackProvider)
    assert selector.current_config.model in fixed_models
    assert turn.model == selector.current_config.model
    assert turn.metadata["fixed_four_tier_v2_selected_provider"] == "openrouter"
    assert turn.metadata["fixed_four_tier_v2_selected_model"] == turn.model
    assert turn.metadata["baseline_model"] == inherited.model
    assert turn.metadata["router_fallback_chain"] == []
    assert turn.metadata["route_max_history_turns"] == 0
    assert turn.metadata["fixed_four_tier_v2_context_action"] == "reset"
    trace = turn.metadata["fixed_four_tier_v2_decision"]
    assert trace["mode"] == "four_tier_mapping"
    assert trace["request_id"] == "request-fixed-1"
    assert trace["execution_id"] != trace["request_id"]
    assert trace["claim_id"]
    assert trace["intent"]["run_status"] == "not_run"
    assert trace["intent"]["prediction"] is None
    assert trace["intent"]["probabilities"] is None
    assert trace["tier"]["run_status"] == "ran"
    assert trace["model"] == turn.model


async def _persist_fixed_redo_parent_route(
    manager: Any,
    *,
    parent: Any,
    task_start_input_message_id: str,
    input_message_id: str,
    response_id: str,
    task_turn_index: int,
) -> Any:
    from opensquilla.engine.routing.fixed_four_tier_v2 import (
        FixedFourTierTaskState,
        FixedFourTierV2Router,
        RoutingRequest,
    )
    from opensquilla.session.models import (
        FixedFourTierDecisionRecord,
        FixedFourTierRequestClaim,
        FixedFourTierState,
    )

    now_ms = time.time_ns() // 1_000_000
    route_id = f"parent-route-{task_turn_index}"
    request_id = f"parent-request-{task_turn_index}"
    execution_id = f"parent-execution-{task_turn_index}"
    claim = FixedFourTierRequestClaim(
        claim_id=f"parent-claim-{task_turn_index}",
        session_id=parent.session_id,
        session_key=parent.session_key,
        request_id=request_id,
        execution_id=execution_id,
        input_message_id=input_message_id,
        claimed_at_ms=now_ms,
        updated_at_ms=now_ms,
        lease_expires_at_ms=now_ms + 300_000,
    )
    acquired, _ = await manager.claim_fixed_four_tier_request(claim)
    assert acquired is True
    prior_state = (
        FixedFourTierTaskState(
            task_id="parent-task",
            tier="c2",
            turn_count=task_turn_index,
            version=0,
            task_start_input_message_id=task_start_input_message_id,
        )
        if task_turn_index > 0
        else None
    )
    core_decision, next_state = FixedFourTierV2Router(
        mock_seed=41,
        route_id_factory=lambda: route_id,
        task_id_factory=lambda: "parent-task",
        clock_ms=lambda: now_ms,
    ).decide(
        RoutingRequest(
            session_id=parent.session_id,
            request_id=request_id,
            message="repeat exactly",
            input_message_id=input_message_id,
            control_event="redo" if prior_state is not None else "new_task",
        ),
        prior_state,
    )
    route_trace = core_decision.trace(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
    )
    route_trace.update(
        {
            "session_id": parent.session_id,
            "session_epoch": parent.epoch,
            "claim_id": claim.claim_id,
            "execution_id": execution_id,
            "session_key_hash": hashlib.sha256(parent.session_key.encode("utf-8")).hexdigest(),
            "input_message_id": input_message_id,
            "redo_parent_route_id": None,
            "task_start_input_message_id": next_state.task_start_input_message_id,
            "state_version_before": None,
            "state_version_after": None,
            "execution_status": "pending",
            "response_id": None,
            "state_committed": False,
            "reasoning": "max",
            "deployment_version": "0731",
            "preflight": {"status": "pending"},
        }
    )
    record = FixedFourTierDecisionRecord(
        route_id=core_decision.route_id,
        session_id=parent.session_id,
        session_key=parent.session_key,
        claim_id=claim.claim_id,
        request_id=request_id,
        execution_id=execution_id,
        input_message_id=input_message_id,
        task_id=core_decision.task_id,
        decided_at_ms=core_decision.decided_at_ms,
        updated_at_ms=now_ms,
        intent=core_decision.intent.trace(),
        tier=core_decision.tier.trace(),
        previous_tier=core_decision.previous_tier,
        final_tier=core_decision.final_tier,
        task_turn_index=core_decision.task_turn_index,
        task_start_input_message_id=next_state.task_start_input_message_id,
        context_action=core_decision.context_action,
        selected_provider="openrouter",
        selected_model="deepseek/deepseek-v4-flash",
        reasoning="max",
        deployment_version="0731",
        config_version=core_decision.schema_version,
        route_trace=route_trace,
    )
    await manager.stage_fixed_four_tier_decision(record)
    committed_trace = {
        **route_trace,
        "state_committed": True,
        "state_version_after": 1,
        "preflight": {"status": "passed"},
    }
    await manager.commit_fixed_four_tier_decision(
        route_id=route_id,
        state=FixedFourTierState(
            session_id=parent.session_id,
            session_key=parent.session_key,
            version=1,
            task_id=core_decision.task_id,
            tier=core_decision.final_tier,
            task_turn_count=task_turn_index + 1,
            task_start_input_message_id=task_start_input_message_id,
            last_request_id=request_id,
            last_route_id=route_id,
            updated_at_ms=now_ms,
        ),
        expected_version=None,
        route_trace=committed_trace,
        updated_at_ms=now_ms,
    )
    assert await manager.settle_fixed_four_tier_decision(
        route_id=route_id,
        execution_status="succeeded",
        preflight_status="passed",
        response_id=response_id,
        route_trace={
            **committed_trace,
            "execution_status": "succeeded",
            "response_id": response_id,
        },
        updated_at_ms=now_ms + 1,
    )
    return await manager.get_fixed_four_tier_decision_by_route(route_id)


@pytest.mark.parametrize(
    ("task_turn_index", "canonical_incomplete"),
    [(0, False), (1, False), (1, True)],
)
async def test_fixed_four_tier_redo_maps_parent_boundary_to_exact_child_row(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    task_turn_index: int,
    canonical_incomplete: bool,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierRoutingError
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.models import SessionSummary
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage(str(tmp_path / f"redo-{task_turn_index}-{canonical_incomplete}.db"))
    runner: TurnRunner | None = None
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        parent = await manager.create(f"agent:main:redo-parent-{task_turn_index}")
        task_start = await manager.append_message(
            parent.session_key,
            "user",
            "first task request",
        )
        if task_turn_index == 1:
            await manager.append_message(parent.session_key, "assistant", "first final answer")
            target_input = await manager.append_message(
                parent.session_key,
                "user",
                "second task request",
            )
        else:
            target_input = task_start
        original_response = await manager.append_message(
            parent.session_key,
            "assistant",
            "original answer to regenerate",
        )
        parent_route = await _persist_fixed_redo_parent_route(
            manager,
            parent=parent,
            task_start_input_message_id=task_start.message_id,
            input_message_id=target_input.message_id,
            response_id=original_response.message_id,
            task_turn_index=task_turn_index,
        )
        assert parent_route is not None
        if not canonical_incomplete:
            list_recent_routes = manager.list_recent_fixed_four_tier_decisions

            async def list_with_same_millisecond_peer(**kwargs: Any) -> list[Any]:
                assert kwargs["before_ms"] == parent_route.decided_at_ms - 1
                assert kwargs["limit"] == 4
                records = list(await list_recent_routes(**kwargs))
                records.extend(
                    SimpleNamespace(
                        route_id=f"same-millisecond-route-{index}",
                        decided_at_ms=parent_route.decided_at_ms,
                        final_tier="c3",
                        tier={
                            "probabilities": {
                                "c0": 0.0,
                                "c1": 0.0,
                                "c2": 0.0,
                                "c3": 1.0,
                            }
                        },
                    )
                    for index in range(5)
                )
                return records

            monkeypatch.setattr(
                manager,
                "list_recent_fixed_four_tier_decisions",
                list_with_same_millisecond_peer,
            )
        if task_turn_index == 1:
            parent_entries = await manager.get_transcript(parent.session_key)
            removed_entries = parent_entries[:2]
            kept_entries = parent_entries[2:]
            parent.compaction_count = 1
            await storage.rewrite_compacted_session(
                node=parent,
                summary=SessionSummary(
                    session_id=parent.session_id,
                    session_key=parent.session_key,
                    compaction_id="redo-parent-compaction",
                    summary_text="archived first task turn",
                    removed_count=len(removed_entries),
                    kept_count=len(kept_entries),
                    covered_through_id=max(entry.id or 0 for entry in removed_entries),
                ),
                entries=kept_entries,
                archived_entries=removed_entries,
            )
            assert task_start.message_id not in {
                entry.message_id for entry in await manager.get_transcript(parent.session_key)
            }
            if canonical_incomplete:
                await storage.conn.execute(
                    "DELETE FROM session_summaries WHERE session_id = ?",
                    (parent.session_id,),
                )
                await storage.conn.commit()
            assert (
                await manager.is_canonical_transcript_complete(parent.session_key)
            ) is not canonical_incomplete

        child_key = f"agent:main:redo-child-{task_turn_index}"
        plan = await manager.prepare_prefix_branch(
            parent.session_key,
            child_key,
            fork_before_message_id=target_input.message_id,
        )
        await storage.upsert_session(plan.node)
        for entry in plan.initial_transcript_entries:
            await storage.append_transcript_entry(entry)
        replacement = await manager.append_message(
            child_key,
            "user",
            str(target_input.content),
        )
        if task_turn_index == 0:
            expected_child_task_start = replacement.message_id
        else:
            source_to_child = dict(plan.source_to_child_message_ids)
            expected_child_task_start = source_to_child[task_start.message_id]
            assert expected_child_task_start != task_start.message_id

        instances = _patch_fake_registered_model_classifier(monkeypatch)
        config = _registered_fixed_four_tier_v2_config(
            tmp_path,
            model_set_id=f"router-redo-{task_turn_index}-a1",
        )
        inherited = ProviderConfig(
            provider="openrouter",
            model="openai/gpt-5.5",
            api_key="synthetic",
        )
        selector = _Selector(inherited)
        runner = TurnRunner(
            provider_selector=selector,
            session_manager=manager,
            config=config,
            model_catalog=_Catalog(),
        )
        turn = TurnContext(
            message=str(target_input.content),
            raw_message=str(target_input.content),
            session_key=child_key,
            config=config,
            provider=_NoChatProvider(),
            model=inherited.model,
            tool_defs=[],
            system_prompt="system",
            attachments=[],
            metadata={
                "fixed_four_tier_v2_request_id": f"redo-request-{task_turn_index}",
                "fixed_four_tier_v2_control_event": "redo",
                "fixed_four_tier_v2_redo_parent_session_id": parent.session_id,
                "fixed_four_tier_v2_redo_of_message_id": target_input.message_id,
                "fixed_four_tier_v2_redo_child_task_start_input_message_id": (
                    expected_child_task_start
                ),
            },
        )

        if canonical_incomplete:
            with pytest.raises(FixedFourTierRoutingError) as exc_info:
                await runner._resolve_fixed_four_tier_v2_provider(
                    turn=turn,
                    provider=turn.provider,
                    cloned_selector=selector,
                    turn_config=config,
                    ensemble_cfg=config.llm_ensemble,
                    turn_absolute_deadline=None,
                    bound_user_message_id=replacement.message_id,
                )
            assert exc_info.value.reason == "redo_parent_canonical_transcript_incomplete"
            assert await manager.get_fixed_four_tier_state(plan.node.session_id) is None
            assert instances == []
            return

        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id=replacement.message_id,
        )

        child_state = await manager.get_fixed_four_tier_state(plan.node.session_id)
        assert child_state is not None
        assert child_state.task_start_input_message_id == expected_child_task_start
        trace = turn.metadata["fixed_four_tier_v2_decision"]
        assert trace["task_start_input_message_id"] == expected_child_task_start
        assert trace["feature_input"]["transcript_refs"] == {
            "input_message_id": target_input.message_id,
            "task_start_input_message_id": task_start.message_id,
        }
        assert trace["history_turns_to_keep"] == task_turn_index
        assert trace["context_action"] == "keep"
        assert len(instances) == 1
        assert len(instances[0].calls) == 1
        redo_router_input = instances[0].calls[0][0]["router_input"]
        assert len(redo_router_input["route_history"]) == 1
        assert (
            redo_router_input["route_history"][-1]["tier_id"]
            == str(parent_route.final_tier).upper()
        )
    finally:
        if runner is not None:
            await runner.aclose()
        await storage.close()


async def test_fixed_four_tier_context_failure_never_switches_tier() -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.routing.fixed_four_tier_v2 import (
        FixedFourTierRoutingError,
    )

    config = _fixed_four_tier_v2_config(mock_seed=23)
    config.llm.context_window_tokens = 8
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="input-fixed-context",
            role="user",
            content="this request is intentionally much longer than eight tokens",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    turn = TurnContext(
        message="this request is intentionally much longer than eight tokens",
        raw_message="this request is intentionally much longer than eight tokens",
        session_key="agent:main:fixed-context-failure",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "request-fixed-context"},
    )

    with pytest.raises(FixedFourTierRoutingError) as exc_info:
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="input-fixed-context",
        )

    assert exc_info.value.reason == "context_length_exceeded"
    assert selector.current_config == inherited
    trace = turn.metadata["fixed_four_tier_v2_decision"]
    assert trace["preflight"]["status"] == "failed"
    assert trace["execution_status"] == "failed"
    assert next(iter(manager.claims.values())).status == "failed"


async def test_fixed_four_tier_rejects_request_controlled_redo_provenance() -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierRoutingError

    config = _fixed_four_tier_v2_config(mock_seed=29)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="forged-redo-input",
            role="user",
            content="ordinary request",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    turn = TurnContext(
        message="ordinary request",
        raw_message="ordinary request",
        session_key="agent:main:forged-redo",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={
            "fixed_four_tier_v2_request_id": "forged-redo-request",
            "input_provenance": {
                "action": "redo",
                "fixed_four_tier_v2_redo_parent_session_id": "known-parent",
                "fixed_four_tier_v2_redo_of_message_id": "known-message",
            },
        },
    )

    with pytest.raises(FixedFourTierRoutingError) as exc_info:
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="forged-redo-input",
        )

    assert exc_info.value.reason == "redo_provenance_untrusted"
    assert manager.claims == {}


async def test_fixed_four_tier_classifier_snapshot_excludes_envelopes_and_queued_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext

    config = _fixed_four_tier_v2_config(mock_seed=31)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    secret_base64 = "c2VjcmV0LWJpbmFyeS1wYXlsb2Fk"
    manager.transcript = [
        SimpleNamespace(
            message_id="history-1",
            role="user",
            content=(
                '{"text":"[2026-08-26T10:30+08:00 Wed Asia/Shanghai]\\n'
                'visible history","attachments":[{"type":"image/png","data":"'
                + secret_base64
                + '"}]}'
            ),
            turn_usage=None,
        ),
        SimpleNamespace(
            message_id="assistant-artifact",
            role="assistant",
            content='{"text":"visible answer","artifacts":[{"bytes":"binary"}]}',
            turn_usage=None,
        ),
        SimpleNamespace(
            message_id="input-current",
            role="user",
            content="current request",
            turn_usage=None,
        ),
        SimpleNamespace(
            message_id="input-future",
            role="user",
            content="QUEUED_FUTURE_SECRET",
            turn_usage=None,
        ),
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    real_router = runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble)
    captured: list[Any] = []

    class CaptureRouter:
        def decide(self, request: Any, state: Any) -> Any:
            captured.append(request)
            return real_router.decide(request, state)

    monkeypatch.setattr(
        runner,
        "_fixed_four_tier_v2_router_for_config",
        lambda _ensemble: CaptureRouter(),
    )
    turn = TurnContext(
        message="current request",
        raw_message="current request",
        session_key="agent:main:fixed-feature-snapshot",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[
            {
                "mime": "application/pdf",
                "name": "customer-private-name.pdf",
                "content": "ATTACHMENT_PRIVATE_CONTENT",
            },
            {
                "mime_type": "image/png",
                "data": "ATTACHMENT_PRIVATE_BASE64",
            },
        ],
        metadata={"fixed_four_tier_v2_request_id": "request-feature-snapshot"},
    )

    await runner._resolve_fixed_four_tier_v2_provider(
        turn=turn,
        provider=turn.provider,
        cloned_selector=selector,
        turn_config=config,
        ensemble_cfg=config.llm_ensemble,
        turn_absolute_deadline=None,
        bound_user_message_id="input-current",
    )

    assert len(captured) == 1
    assert captured[0].user_history == ("visible history",)
    assert captured[0].previous_assistant_text is None
    assert captured[0].attachment_count == 2
    assert captured[0].attachment_modalities == ("document", "image")
    feature_input = turn.metadata["fixed_four_tier_v2_decision"]["feature_input"]
    assert feature_input["attachment_count"] == 2
    assert feature_input["attachment_modalities"] == ["document", "image"]
    assert feature_input["missing"]["attachment_metadata"] is False
    trace_text = repr(turn.metadata["fixed_four_tier_v2_decision"])
    assert secret_base64 not in trace_text
    assert "QUEUED_FUTURE_SECRET" not in trace_text
    assert "binary" not in trace_text
    assert "customer-private-name.pdf" not in trace_text
    assert "ATTACHMENT_PRIVATE_CONTENT" not in trace_text
    assert "ATTACHMENT_PRIVATE_BASE64" not in trace_text


async def test_registered_four_tier_real_resolver_builds_complete_router_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from opensquilla.engine.pipeline import TurnContext

    instances = _patch_fake_registered_model_classifier(monkeypatch)
    config = _registered_fixed_four_tier_v2_config(tmp_path)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    previous_usage = {
        "input_tokens": 101,
        "output_tokens": 202,
        "reasoning_tokens": 33,
        "cached_tokens": 44,
        "cache_write_tokens": 55,
        "cost_usd": 0.125,
    }
    previous_route = SimpleNamespace(
        route_id="previous-route",
        request_id="previous-request",
        execution_status="failed",
        error_code="provider_error",
        response_id="previous-response",
        route_trace={"attempt_ids": ["previous-attempt"]},
        final_tier="c1",
        tier={"probabilities": {"c0": 0.0, "c1": 1.0, "c2": 0.0, "c3": 0.0}},
    )
    manager.decisions[previous_route.route_id] = previous_route
    manager.state = SimpleNamespace(
        schema_version=1,
        task_id="active-task",
        tier="c1",
        task_turn_count=1,
        version=1,
        task_start_input_message_id="task-anchor",
        last_request_id=previous_route.request_id,
        last_route_id=previous_route.route_id,
    )
    history_texts = (
        "old task one",
        "old task two",
        "old task three",
        "old task four",
        "active task anchor",
    )
    manager.transcript = [
        *[
            SimpleNamespace(
                message_id=f"history-{index}",
                role="user",
                content=text,
                turn_usage=None,
            )
            for index, text in enumerate(history_texts[:-1], start=1)
        ],
        SimpleNamespace(
            message_id="task-anchor",
            role="user",
            content=history_texts[-1],
            turn_usage=None,
        ),
        SimpleNamespace(
            message_id="previous-response",
            role="assistant",
            content="previous assistant answer",
            turn_usage=previous_usage,
        ),
        SimpleNamespace(
            message_id="input-current",
            role="user",
            content="current original request",
            turn_usage=None,
        ),
    ]
    recent_routes = [
        SimpleNamespace(
            final_tier="c3",
            tier={"probabilities": {"c0": 0.0, "c1": 0.0, "c2": 0.0, "c3": 1.0}},
        ),
        previous_route,
    ]
    route_history_calls: list[dict[str, Any]] = []

    async def list_recent_fixed_four_tier_decisions(**kwargs: Any) -> list[Any]:
        route_history_calls.append(dict(kwargs))
        return recent_routes

    manager.list_recent_fixed_four_tier_decisions = list_recent_fixed_four_tier_decisions
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    attachment = {
        "mime_type": "application/pdf",
        "parse_status": "parsed",
        "summary": "safe attachment summary",
        "token_count": 321,
        "name": "private.pdf",
        "content": "PRIVATE ATTACHMENT CONTENT",
    }
    turn = TurnContext(
        message="decorated current request",
        raw_message="current original request",
        session_key="agent:main:registered-router-input",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[
            ToolDefinition(
                name="zeta_tool",
                description="zeta",
                input_schema=ToolInputSchema(),
            ),
            ToolDefinition(
                name="alpha_tool",
                description="alpha",
                input_schema=ToolInputSchema(),
            ),
        ],
        system_prompt="system",
        attachments=[attachment],
        metadata={
            "fixed_four_tier_v2_request_id": "request-registered-router-input",
            "channel_kind": "discord",
        },
        surface_kind="cli",
    )

    await runner._resolve_fixed_four_tier_v2_provider(
        turn=turn,
        provider=turn.provider,
        cloned_selector=selector,
        turn_config=config,
        ensemble_cfg=config.llm_ensemble,
        turn_absolute_deadline=None,
        bound_user_message_id="input-current",
    )

    assert len(instances) == 1
    assert len(instances[0].calls) == 1
    snapshot, allowed_tiers = instances[0].calls[0]
    assert allowed_tiers is None
    router_input = snapshot["router_input"]
    assert router_input["current_request"] == "current original request"
    assert router_input["history_user"] == list(history_texts[-4:])
    assert router_input["task_anchor"] == "active task anchor"
    assert router_input["previous_answer"] == "previous assistant answer"
    assert router_input["previous_usage"] == previous_usage
    assert router_input["previous_outcome"] == "failure"
    assert router_input["active_route_tier"] == "C1"
    assert router_input["route_history"] == [
        {"tier_id": "C3", "difficulty": 3.0, "margin": 1.0},
        {"tier_id": "C1", "difficulty": 1.0, "margin": 1.0},
    ]
    assert router_input["context"] == {
        "turn_index": 2,
        "context_tokens_est": (
            len("current original request")
            + sum(len(value) for value in history_texts)
            + len("previous assistant answer")
        )
        // 4,
        "entrypoint": "cli",
        "platform": "discord",
    }
    assert router_input["tool_state"] == {"available_tools": ["alpha_tool", "zeta_tool"]}
    assert router_input["attachments"] == [
        {
            "type": "document",
            "mime_type": "application/pdf",
            "media_type": None,
            "parse_status": "parsed",
            "status": None,
            "summary": "safe attachment summary",
            "truncated": None,
            "token_count": 321,
        }
    ]
    assert route_history_calls[0]["session_id"] == manager.session_id
    assert route_history_calls[0]["session_epoch"] == 0
    assert route_history_calls[0]["limit"] == 5
    assert route_history_calls[0]["since_ms"] < route_history_calls[0]["before_ms"]
    trace = turn.metadata["fixed_four_tier_v2_decision"]
    assert trace["classifier_backend"] == "registered_model"
    assert trace["classifier_identity"] == instances[0].identity

    await runner.aclose()


async def test_registered_router_input_falls_back_to_latest_unbound_assistant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Enabling the mode mid-session must not erase observable prior context."""

    from opensquilla.engine.pipeline import TurnContext

    instances = _patch_fake_registered_model_classifier(monkeypatch)
    config = _registered_fixed_four_tier_v2_config(tmp_path)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    previous_usage = {
        "input_tokens": 11,
        "output_tokens": 22,
        "reasoning_tokens": 3,
        "cached_tokens": 4,
        "cache_write_tokens": 5,
        "cost_usd": 0.01,
    }
    manager.transcript = [
        SimpleNamespace(
            message_id="legacy-user",
            role="user",
            content="question before fixed routing was enabled",
            turn_usage=None,
            turn_context=None,
            tool_calls=None,
        ),
        SimpleNamespace(
            message_id="legacy-assistant",
            role="assistant",
            content="I need one detail before proceeding.",
            turn_usage=previous_usage,
            turn_context={"agent_loop_stop_reason": "end_turn"},
            tool_calls=[{"type": "tool_use", "name": "ask_user"}],
        ),
        SimpleNamespace(
            message_id="current-input",
            role="user",
            content="the missing detail is X",
            turn_usage=None,
            turn_context=None,
            tool_calls=None,
        ),
    ]

    route_history_calls: list[dict[str, Any]] = []

    async def list_recent_fixed_four_tier_decisions(**kwargs: Any) -> list[Any]:
        route_history_calls.append(dict(kwargs))
        return []

    manager.list_recent_fixed_four_tier_decisions = list_recent_fixed_four_tier_decisions
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    turn = TurnContext(
        message="the missing detail is X",
        raw_message="the missing detail is X",
        session_key="agent:main:registered-mid-session",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "registered-mid-session"},
    )

    try:
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="current-input",
        )

        assert len(instances) == 1
        assert len(instances[0].calls) == 1
        router_input = instances[0].calls[0][0]["router_input"]
        assert router_input["history_user"] == ["question before fixed routing was enabled"]
        assert router_input["previous_answer"] == "I need one detail before proceeding."
        assert router_input["previous_usage"] == previous_usage
        assert router_input["previous_outcome"] == "clarification"
        assert router_input["active_route_tier"] is None
        assert router_input["route_history"] == []
        assert len(route_history_calls) == 1
    finally:
        await runner.aclose()


async def test_fixed_four_tier_missing_current_feature_anchor_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierRoutingError

    config = _fixed_four_tier_v2_config(mock_seed=37)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="different-input",
            role="user",
            content="different",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    monkeypatch.setattr(
        runner,
        "_fixed_four_tier_v2_router_for_config",
        lambda _ensemble: (_ for _ in ()).throw(
            AssertionError("missing input anchor reached the classifier")
        ),
    )
    turn = TurnContext(
        message="current",
        raw_message="current",
        session_key="agent:main:fixed-missing-anchor",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "request-missing-anchor"},
    )

    with pytest.raises(FixedFourTierRoutingError) as exc_info:
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="missing-input",
        )

    assert exc_info.value.reason == "current_feature_boundary_unavailable"
    assert manager.claims == {}


async def test_fixed_four_tier_previous_response_is_bound_by_route_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext

    config = _fixed_four_tier_v2_config(mock_seed=41)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.state = SimpleNamespace(
        schema_version=1,
        task_id="task-existing",
        tier="c1",
        task_turn_count=1,
        version=1,
        task_start_input_message_id="task-start",
        last_route_id="route-previous",
    )
    manager.decisions["route-previous"] = SimpleNamespace(
        route_id="route-previous",
        task_id="task-existing",
        final_tier="c1",
        task_turn_index=0,
        task_start_input_message_id="task-start",
        input_message_id="task-start",
        response_id="response-final",
        execution_status="succeeded",
        error_code=None,
        route_trace={"attempt_ids": ["attempt-1"]},
    )
    manager.transcript = [
        SimpleNamespace(
            message_id="task-start",
            role="user",
            content="[2026-08-26T10:30+08:00 Wed Asia/Shanghai]\nstart task",
            turn_usage=None,
        ),
        SimpleNamespace(
            message_id="response-final",
            role="assistant",
            content='{"text":"exact final answer","artifacts":[{"secret":"omit-me"}]}',
            turn_usage={"input_tokens": 10, "output_tokens": 20},
        ),
        SimpleNamespace(
            message_id="assistant-intermediate",
            role="assistant",
            content="WRONG_INTERMEDIATE_ASSISTANT",
            turn_usage={"input_tokens": 999},
        ),
        SimpleNamespace(
            message_id="input-current",
            role="user",
            content="continue",
            turn_usage=None,
        ),
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    captured: list[Any] = []

    class CaptureAndStopRouter:
        def decide(self, request: Any, state: Any) -> Any:
            del state
            captured.append(request)
            raise RuntimeError("capture complete")

    monkeypatch.setattr(
        runner,
        "_fixed_four_tier_v2_router_for_config",
        lambda _ensemble: CaptureAndStopRouter(),
    )
    turn = TurnContext(
        message="continue",
        raw_message="continue",
        session_key="agent:main:fixed-previous-response",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "request-continue"},
    )

    with pytest.raises(RuntimeError, match="capture complete"):
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="input-current",
        )

    assert captured[0].user_history == ("start task",)
    assert captured[0].previous_assistant_text == "exact final answer"
    assert captured[0].previous_assistant_usage["input_tokens"] == 10
    assert captured[0].previous_assistant_usage["route_id"] == "route-previous"
    assert next(iter(manager.claims.values())).status == "failed"


async def test_fixed_four_tier_classifier_cancellation_terminalizes_request_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext

    config = _fixed_four_tier_v2_config(mock_seed=43)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="input-cancel",
            role="user",
            content="cancel",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )

    class CancelRouter:
        def decide(self, request: Any, state: Any) -> Any:
            del request, state
            raise asyncio.CancelledError

    monkeypatch.setattr(
        runner,
        "_fixed_four_tier_v2_router_for_config",
        lambda _ensemble: CancelRouter(),
    )
    turn = TurnContext(
        message="cancel",
        raw_message="cancel",
        session_key="agent:main:fixed-classifier-cancel",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": "request-cancel"},
    )

    with pytest.raises(asyncio.CancelledError):
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="input-cancel",
        )

    claim = next(iter(manager.claims.values()))
    assert claim.status == "cancelled"
    assert claim.error_code == "CancelledError"


@pytest.mark.parametrize(
    "failure_site",
    ["router_factory", "decision_trace", "state_commit_conflict"],
)
async def test_fixed_four_tier_all_post_claim_failures_terminalize_claim(
    failure_site: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.session.storage import FixedFourTierStateConflictError

    config = _fixed_four_tier_v2_config(mock_seed=47)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    selector = _Selector(inherited)
    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="input-post-claim-failure",
            role="user",
            content="route me",
            turn_usage=None,
        )
    ]
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_Catalog(),
    )
    real_router = runner._fixed_four_tier_v2_router_for_config(config.llm_ensemble)
    if failure_site == "router_factory":
        monkeypatch.setattr(
            runner,
            "_fixed_four_tier_v2_router_for_config",
            lambda _ensemble: (_ for _ in ()).throw(RuntimeError("factory failed")),
        )
    elif failure_site == "decision_trace":

        class BrokenTraceRouter:
            def decide(self, request: Any, state: Any) -> Any:
                decision, next_state = real_router.decide(request, state)

                class BrokenDecision:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(decision, name)

                    def trace(self, **kwargs: Any) -> Any:
                        del kwargs
                        raise RuntimeError("trace failed")

                return BrokenDecision(), next_state

        monkeypatch.setattr(
            runner,
            "_fixed_four_tier_v2_router_for_config",
            lambda _ensemble: BrokenTraceRouter(),
        )
    else:

        async def fail_commit(**kwargs: Any) -> Any:
            del kwargs
            raise FixedFourTierStateConflictError("synthetic state race")

        monkeypatch.setattr(manager, "commit_fixed_four_tier_decision", fail_commit)

    turn = TurnContext(
        message="route me",
        raw_message="route me",
        session_key="agent:main:fixed-post-claim-failure",
        config=config,
        provider=_NoChatProvider(),
        model=inherited.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"fixed_four_tier_v2_request_id": f"request-{failure_site}"},
    )

    with pytest.raises(Exception):
        await runner._resolve_fixed_four_tier_v2_provider(
            turn=turn,
            provider=turn.provider,
            cloned_selector=selector,
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
            bound_user_message_id="input-post-claim-failure",
        )

    assert len(manager.claims) == 1
    claim = next(iter(manager.claims.values()))
    assert claim.status == "failed"
    if failure_site == "state_commit_conflict":
        assert claim.error_code == "task_state_conflict"


@pytest.mark.parametrize(
    ("history_start_message_id", "bound_user_message_id", "expected_reason"),
    [
        ("missing-start", "input-current", "task_history_boundary_unavailable"),
        ("task-start", "missing-current", "current_history_boundary_unavailable"),
    ],
)
async def test_fixed_four_tier_history_reload_missing_anchor_fails_closed(
    history_start_message_id: str,
    bound_user_message_id: str,
    expected_reason: str,
) -> None:
    from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierRoutingError

    manager = _FixedRouteSessionManager()
    manager.transcript = [
        SimpleNamespace(
            message_id="task-start",
            role="user",
            content="start",
            tool_calls=None,
            reasoning_content=None,
        ),
        SimpleNamespace(
            message_id="input-current",
            role="user",
            content="current",
            tool_calls=None,
            reasoning_content=None,
        ),
    ]
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=_fixed_four_tier_v2_config(),
    )
    agent = SimpleNamespace(config=SimpleNamespace())

    with pytest.raises(FixedFourTierRoutingError) as exc_info:
        await runner._load_history(
            agent,
            "agent:main:fixed-history-anchor",
            bound_user_message_id=bound_user_message_id,
            suppress_compaction_context=True,
            history_start_message_id=history_start_message_id,
        )

    assert exc_info.value.reason == expected_reason


def _resolver_inputs() -> tuple[Any, ProviderConfig, dict[str, Any]]:
    frozen = ranking_config_snapshot()
    ensemble = SimpleNamespace(
        selection_mode="router_dynamic",
        prepared_ranking_config=lambda: frozen,
        ranking_user_profile_enabled=False,
        candidates=[],
        model_options=[],
        ranking_thinking_assignment_enabled=False,
    )
    config = SimpleNamespace(
        llm_ensemble=ensemble,
        llm=SimpleNamespace(
            max_tokens=0,
            context_window_tokens=0,
            temperature=None,
        ),
        squilla_router=SimpleNamespace(tiers={}),
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    inputs = {
        "decision_id": "decision-1",
        "ranking_config": frozen,
        "task_analysis": TaskAnalysisResult(
            profile={},
            source="llm_provider",
            schema_valid=True,
            confidence=1.0,
        ),
        "request_context": {
            "conversation": {},
            "tool_state": {},
            "workspace_state": {},
            "intermediate_outputs": {},
            "last_route": {},
            "routing_budget": {
                "estimated_input_tokens": 1,
                "tool_log_tokens": 0,
                "direct_output_tokens": 16_384,
            },
            "input_modalities": ["text"],
            "attachment_refs": [],
            "snapshot_hash": "a" * 64,
        },
    }
    return config, inherited, inputs


def _patch_single_resolver_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], ProviderConfig]:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module

    models = [
        _ranked_model("openai/gpt-5.5", capability=0.8),
        _ranked_model("anthropic/claude-sonnet-4.5", capability=0.9),
    ]
    selected = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.5",
        api_key="synthetic",
    )
    rank_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        ranking_module,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: snapshot,
    )
    monkeypatch.setattr(
        ranking_module,
        "build_model_registry_snapshot",
        lambda **kwargs: {
            "snapshot_version": "test",
            "models": [{"registry_facts": dict(model.registry_facts)} for model in models],
        },
    )

    def rank_single(**kwargs: Any) -> SingleModelRankingDecision:
        rank_calls.append(kwargs)
        selected_facts = kwargs["registry_snapshot"]["models"][1]["registry_facts"]
        selected_model = replace(models[1], registry_facts=dict(selected_facts))
        return SingleModelRankingDecision(
            model=selected_model,
            effective_tier=2,
            trace={
                "strategy": "router_dynamic",
                "execution_mode": "router_single",
                "selected_model": selected_model.identity,
                "selected_P": [selected_model.identity],
            },
            thinking_assignment={},
            thinking_assignment_details={},
        )

    monkeypatch.setattr(ranking_module, "rank_single_model", rank_single)
    monkeypatch.setattr(
        ranking_module,
        "dynamic_output_token_budgets",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("single route read fusion budgets")),
    )
    monkeypatch.setattr(
        ranking_module,
        "build_request_context",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("single route built candidate/A context")
        ),
    )
    monkeypatch.setattr(ensemble_module, "_member_from_ref", lambda *a, **k: object())
    monkeypatch.setattr(
        ensemble_module,
        "_member_model_capabilities",
        lambda member: SimpleNamespace(supports_vision=False),
    )

    def deployment(
        ref: Any,
        inherited: ProviderConfig,
        **kwargs: Any,
    ) -> ProviderDeploymentResolution:
        del inherited, kwargs
        deployment_config = replace(selected, model=ref.model)
        return ProviderDeploymentResolution(
            provider=deployment_config.provider,
            model=deployment_config.model,
            ready=True,
            provider_config=deployment_config,
        )

    monkeypatch.setattr(ensemble_module, "_resolve_member_deployment", deployment)
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )
    return rank_calls, selected


def _health_row(*, state: str = "healthy", half_open: bool = False) -> dict[str, Any]:
    return {
        "schema": "opensquilla.provider-health-runtime-facts/v1",
        "provider": "openrouter",
        "model": "model",
        "upstream": "",
        "state": state,
        "fresh": True,
        "half_open_inflight": half_open,
        "eligible": state == "healthy" and not half_open,
    }


def test_single_resolver_freezes_candidate_catalog_budgets_without_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, selected = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    catalog = _Catalog()
    ledger = SimpleNamespace(runtime_facts=lambda *a, **k: _health_row())

    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=True,
        provider_health_ledger=ledger,
        model_catalog=catalog,
    )

    facts = [row["registry_facts"] for row in rank_calls[0]["registry_snapshot"]["models"]]
    assert route.provider_config == selected
    assert [row["runtime_direct_output_tokens"] for row in facts] == [4_096, 8_192]
    assert [row["context_window"] for row in facts] == [128_000, 200_000]
    assert route.direct_output_tokens == 8_192
    assert route.context_window_tokens == 200_000
    assert route.trace["selected_P"] == ["openrouter:anthropic/claude-sonnet-4.5"]
    assert "selected_A" not in route.trace
    assert "aggregator" not in route.trace


def test_single_resolver_without_cache_policy_never_calls_affinity_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("absent cache policy must be a strict no-op")

    monkeypatch.setattr(
        "opensquilla.provider.ensemble._cache_affinity_private_inputs",
        fail_if_called,
    )

    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=False,
        provider_health_ledger=SimpleNamespace(runtime_facts=lambda *args, **kwargs: _health_row()),
        model_catalog=_Catalog(),
    )

    assert route.provider_config.model == "anthropic/claude-sonnet-4.5"


def test_single_resolver_fresh_health_failure_is_fail_closed_without_reselect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    health_calls = 0

    def runtime_facts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal health_calls
        del args, kwargs
        health_calls += 1
        return _health_row(state="benched") if health_calls == 3 else _health_row()

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            provider_health_ledger=SimpleNamespace(runtime_facts=runtime_facts),
            model_catalog=_Catalog(),
        )

    assert raised.value.reason == "router_single_selected_deployment_unhealthy"
    assert len(rank_calls) == 1
    assert health_calls == 3


def test_agent_bootstrap_consumes_frozen_single_budget_after_catalog_mutation() -> None:
    catalog = _Catalog()
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
        model_catalog=catalog,
    )
    _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "openrouter",
            "model": "openai/gpt-5.5",
            "max_tokens": 4_096,
            "context_window": 128_000,
        }
    )
    catalog.output["openai/gpt-5.5"] = 99_999
    catalog.context["openai/gpt-5.5"] = 999_999

    resolved = _TurnRunnerModelCatalogAdapter(runner).lookup("openai/gpt-5.5", "openrouter")

    assert resolved.max_tokens == 4_096
    assert resolved.context_window == 128_000
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None


class _HealthLedger:
    def __init__(self, admissions: list[bool] | None = None) -> None:
        self.admissions = list(admissions or [True])
        self.begin_calls: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.successes: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []

    def begin_attempt(
        self,
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        allowed = self.admissions.pop(0) if self.admissions else False
        row = {"provider": provider, "model": model, **kwargs}
        self.begin_calls.append(row)
        return {
            "allowed": allowed,
            "reason": "half_open_busy" if not allowed else "",
            "lease_token": f"lease-{len(self.begin_calls)}" if allowed else None,
            "started_at": time.monotonic(),
        }

    def record_failure(
        self,
        provider: str,
        model: str,
        kind: ProviderFailureKind,
        **kwargs: Any,
    ) -> None:
        self.failures.append({"provider": provider, "model": model, "kind": kind, **kwargs})

    def record_success(self, provider: str, model: str, **kwargs: Any) -> None:
        self.successes.append({"provider": provider, "model": model, **kwargs})

    def cancel_attempt(self, provider: str, model: str, **kwargs: Any) -> None:
        self.cancels.append({"provider": provider, "model": model, **kwargs})


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _real_health_ledger(clock: _FakeClock) -> ProviderHealthLedger:
    return ProviderHealthLedger(
        failure_threshold=1,
        cooldown_s=1.0,
        max_cooldown_s=2.0,
        clock=clock,
    )


def _real_health_facts(ledger: ProviderHealthLedger) -> dict[str, Any]:
    cfg = _direct_config()
    return ledger.runtime_facts(
        cfg.provider,
        cfg.model,
        upstream="anthropic",
    )


def _bench_real_health(ledger: ProviderHealthLedger) -> None:
    cfg = _direct_config()
    ledger.record_failure(
        cfg.provider,
        cfg.model,
        ProviderFailureKind.RATE_LIMITED,
        upstream="anthropic",
    )


def _direct_config() -> ProviderConfig:
    model = "anthropic/claude-sonnet-4.5"
    return ProviderConfig(
        provider="openrouter",
        model=model,
        api_key="synthetic",
        provider_routing={model: "anthropic"},
    )


class _BlockingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _BlockingStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _BlockingProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.streams: list[_BlockingStream] = []

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        stream = _BlockingStream()
        self.streams.append(stream)
        return stream


async def test_direct_deadline_records_timeout_failure_and_bounds_stubborn_close() -> None:
    ledger = _HealthLedger([True])
    raw = _BlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.03,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    provider._STREAM_CLOSE_TIMEOUT_SECONDS = 0.03
    started = time.monotonic()

    events = [event async for event in provider.chat([], config=ChatConfig(timeout=30.0))]
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert raw.calls == 1
    assert len(events) == 1
    assert events[0].code == "router_single_absolute_deadline"
    assert events[0].request_started is True
    assert events[0].physical_request_count == 1
    assert len(ledger.failures) == 1
    assert ledger.failures[0]["kind"] is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert ledger.failures[0]["upstream"] == "anthropic"
    assert ledger.failures[0]["lease_token"] == "lease-1"
    assert ledger.cancels == []
    raw.streams[0].close_release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_direct_real_health_ledger_rejects_bench_then_settles_probe_success() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    raw = _TimedDoneProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    rejected = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert raw.calls == []
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert _real_health_facts(ledger)["state"] == "benched"

    clock.advance(1.1)
    completed = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _real_health_facts(ledger)

    assert completed[-1].kind == "done"
    assert len(raw.calls) == 1
    assert facts["upstream"] == "anthropic"
    assert facts["state"] == "healthy"
    assert facts["half_open_inflight"] is False
    assert facts["recent_successes"] == 1


async def test_direct_real_health_ledger_timeout_rebenches_half_open_probe() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    clock.advance(1.1)
    raw = _BlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.03,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    provider._STREAM_CLOSE_TIMEOUT_SECONDS = 0.03

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _real_health_facts(ledger)

    assert raw.calls == 1
    assert events[-1].request_started is True
    assert facts["upstream"] == "anthropic"
    assert facts["state"] == "benched"
    assert facts["half_open_inflight"] is False
    assert facts["last_failure_kind"] == "transport_transient"
    raw.streams[0].close_release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_half_open_atomic_admission_allows_only_one_physical_request() -> None:
    ledger = _HealthLedger([True, False])
    raw = _BlockingProvider()
    first = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 1.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    second = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 1.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first_task = asyncio.create_task(_collect(first.chat([], config=ChatConfig(timeout=30.0))))
    while not raw.streams:
        await asyncio.sleep(0)
    await raw.streams[0].started.wait()

    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))
    raw.streams[0].close_release.set()
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert raw.calls == 1
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert second.retry_failed_call_safe is False
    assert [call["never_strand_exempt"] for call in ledger.begin_calls] == [
        False,
        False,
    ]
    assert all(call["upstream"] == "anthropic" for call in ledger.begin_calls)
    assert len(ledger.cancels) == 1


class _AtomicHalfOpenLedger(_HealthLedger):
    def __init__(self) -> None:
        super().__init__([])
        self.active = False

    def begin_attempt(
        self,
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        allowed = not self.active
        if allowed:
            self.active = True
        self.begin_calls.append({"provider": provider, "model": model, **kwargs})
        return {
            "allowed": allowed,
            "reason": "half_open_busy" if not allowed else "",
            "lease_token": "half-open-lease" if allowed else None,
            "started_at": time.monotonic(),
        }

    def cancel_attempt(self, provider: str, model: str, **kwargs: Any) -> None:
        super().cancel_attempt(provider, model, **kwargs)
        self.active = False


class _CloseGateStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _CloseGateStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _CloseGateProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _CloseGateStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_half_open_lease_is_held_until_cancelled_stream_closes() -> None:
    ledger = _AtomicHalfOpenLedger()
    raw = _CloseGateProvider()
    first = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    second = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first_task = asyncio.create_task(_collect(first.chat([], config=ChatConfig(timeout=30.0))))
    await asyncio.wait_for(raw.stream.started.wait(), timeout=0.5)

    first_task.cancel()
    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert ledger.active is True
    assert raw.calls == 1
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0

    raw.stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert ledger.active is False
    assert len(ledger.cancels) == 1


async def test_direct_real_health_ledger_cancel_releases_probe_after_close() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    clock.advance(1.1)
    raw = _CloseGateProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    task = asyncio.create_task(_collect(provider.chat([], config=ChatConfig(timeout=30.0))))
    await asyncio.wait_for(raw.stream.started.wait(), timeout=0.5)

    task.cancel()
    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    during_close = _real_health_facts(ledger)
    assert during_close["state"] == "half_open"
    assert during_close["half_open_inflight"] is True

    raw.stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    after_close = _real_health_facts(ledger)
    assert after_close["state"] == "half_open"
    assert after_close["half_open_inflight"] is False


class _TimedDoneProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ChatConfig]] = []

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages
        assert config is not None
        self.calls.append((tools, config))
        call_number = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if call_number == 1:
                await asyncio.sleep(0.04)
            yield ProviderDone(
                stop_reason="stop",
                input_tokens=10,
                output_tokens=1,
            )

        return stream()


async def test_direct_tool_loop_calls_share_one_absolute_deadline() -> None:
    deadline = time.monotonic() + 0.15
    ledger = _HealthLedger([True, True])
    raw = _TimedDoneProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=deadline,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    tools = [object()]

    first = await _collect(provider.chat([], tools=tools, config=ChatConfig(timeout=30.0)))
    second = await _collect(provider.chat([], tools=tools, config=ChatConfig(timeout=30.0)))

    assert first[-1].kind == "done"
    assert second[-1].kind == "done"
    assert len(raw.calls) == 2
    assert raw.calls[0][0] is tools
    assert raw.calls[1][0] is tools
    first_timeout = float(raw.calls[0][1].timeout)
    second_timeout = float(raw.calls[1][1].timeout)
    assert 0 < second_timeout < first_timeout - 0.02

    await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
    rejected = await _collect(provider.chat([], tools=tools, config=ChatConfig(timeout=30.0)))
    assert len(raw.calls) == 2
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert provider.retry_failed_call_safe is False


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


class _SequenceProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[ChatConfig] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools
        assert config is not None
        self.calls.append(config)
        index = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if index == 1:
                yield ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="reasoning only",
                )
                return
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1)

        return stream()

    async def list_models(self) -> list[Any]:
        return []


class _OneModelSelector:
    active_provider_id = "openrouter"
    current_config = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )

    def __init__(self) -> None:
        self.fallback_calls = 0

    def has_fallback(self) -> bool:
        return False

    def remaining_chain(self) -> list[ProviderConfig]:
        return [self.current_config]

    def next_fallback_after_failure(self, error: Exception) -> Any:
        self.fallback_calls += 1
        raise IndexError("single route has no fallback")


async def test_managed_single_uses_provider_native_max_and_cannot_downgrade() -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"thinking": "high"}),
    )
    turn = SimpleNamespace(
        metadata={
            "thinking_requested": True,
            "thinking_level": "max",
            "_router_single_managed_provider_thinking_level": "max",
        }
    )
    resolved = runner._resolve_turn_thinking(turn)
    assert resolved is ThinkingLevel.MAX

    raw = _SequenceProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        _OneModelSelector.current_config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=True,
    )
    wrapped = _SelectorFallbackProvider(direct, _OneModelSelector())
    agent = Agent(
        provider=wrapped,
        config=AgentConfig(
            thinking=resolved,
            reasoning_only_thinking_fallback=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert wrapped._routed_thinking_policy_blocks_fallback() is True
    assert len(raw.calls) == 1
    assert [config.thinking_level for config in raw.calls] == [
        ThinkingLevel.MAX,
    ]
    assert all(config.thinking is True for config in raw.calls)


class _ToolLoopProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[ChatConfig] = []
        self.tools_by_call: list[list[Any] | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages
        assert config is not None
        self.calls.append(config)
        self.tools_by_call.append(tools)
        call_number = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if call_number == 1:
                yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
                yield ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "again"},
                )
                yield ProviderDone(
                    stop_reason="tool_use",
                    input_tokens=10,
                    output_tokens=1,
                )
                return
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1)

        return stream()


async def test_managed_single_tool_loop_keeps_model_tools_thinking_and_usage() -> None:
    raw = _ToolLoopProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        _OneModelSelector.current_config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=True,
    )
    selector = _OneModelSelector()
    wrapped = _SelectorFallbackProvider(direct, selector)
    tool_definition = ToolDefinition(
        name="echo",
        description="Echo.",
        input_schema=ToolInputSchema(),
    )

    async def echo_tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=wrapped,
        config=AgentConfig(thinking=ThinkingLevel.MAX),
        tool_definitions=[tool_definition],
        tool_handler=echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert len(raw.calls) == 2
    assert [config.thinking_level for config in raw.calls] == [
        ThinkingLevel.MAX,
        ThinkingLevel.MAX,
    ]
    assert all(config.thinking is True for config in raw.calls)
    assert raw.tools_by_call == [[tool_definition], [tool_definition]]
    assert selector.fallback_calls == 0
    assert done.input_tokens == 21
    assert done.output_tokens == 2
    assert not any(event.kind == "ensemble_progress" for event in events)


def test_single_realigns_routed_model_and_clears_stale_savings() -> None:
    metadata = {
        "routed_model": "old/model",
        "savings_pct": 25.0,
        "savings_max_price_per_m": 10.0,
        "savings_routed_price_per_m": 1.0,
    }
    wrapped = _SelectorFallbackProvider(
        _NoChatProvider(),
        _OneModelSelector(),
        turn_metadata=metadata,
    )

    wrapped._realign_routed_model_after_fallback()

    assert metadata["routed_model"] == "openai/gpt-5.5"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "openai/gpt-5.5"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0
    assert metadata["savings_routed_price_per_m"] == 0.0


async def test_selector_wrapper_generation_guard_blocks_stale_ensemble_dispatch() -> None:
    raw = _NoChatProvider()
    raw._router_dynamic_cache_dispatch_generation_guard = lambda: False
    wrapped = _SelectorFallbackProvider(raw, _Selector())

    events = await _collect(wrapped.chat([], config=ChatConfig()))

    assert raw.calls == 0
    assert len(events) == 1
    assert events[0].code == "router_dynamic_cache_generation_changed"
    assert events[0].physical_request_count == 0


async def test_selector_wrapper_rechecks_generation_after_lazy_usage_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RawProvider:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1

            async def stream() -> AsyncIterator[Any]:
                yield ProviderDone(provider="openrouter", model="openai/gpt-5.5")

            return stream()

    setup_entered = asyncio.Event()
    release_setup = asyncio.Event()

    def delayed_account_provider_stream(
        stream_factory: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        del kwargs

        async def stream() -> AsyncIterator[Any]:
            setup_entered.set()
            await release_setup.wait()
            async for event in stream_factory():
                yield event

        return stream()

    monkeypatch.setattr(
        "opensquilla.engine.runtime.account_provider_stream",
        delayed_account_provider_stream,
    )
    generation = 0
    raw = RawProvider()
    raw._router_dynamic_cache_dispatch_generation_guard = lambda: generation == 0
    wrapped = _SelectorFallbackProvider(raw, _Selector())

    collect_task = asyncio.create_task(_collect(wrapped.chat([], config=ChatConfig())))
    await asyncio.wait_for(setup_entered.wait(), timeout=1.0)
    generation = 1
    release_setup.set()
    events = await asyncio.wait_for(collect_task, timeout=1.0)

    assert raw.calls == 0
    assert len(events) == 1
    assert events[0].code == "router_dynamic_cache_generation_changed"
    assert events[0].request_started is False
    assert events[0].physical_request_count == 0
    assert not any(getattr(event, "kind", "") == "router_decision" for event in events)


async def test_selector_fallback_rechecks_generation_after_lazy_usage_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FallbackProvider:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1

            async def stream() -> AsyncIterator[Any]:
                yield ProviderDone(provider="openrouter", model="fallback/model")

            return stream()

    class FallbackSelector:
        def __init__(self, fallback: FallbackProvider) -> None:
            self._fallback = fallback
            self.current_config = ProviderConfig(
                provider="openrouter",
                model="primary/model",
                api_key="synthetic",
            )

        @property
        def active_provider_id(self) -> str:
            return self.current_config.provider

        def next_fallback_after_failure(self, error: Exception) -> FallbackProvider:
            del error
            self.current_config = replace(self.current_config, model="fallback/model")
            return self._fallback

    setup_entered = asyncio.Event()
    release_setup = asyncio.Event()
    account_call_count = 0

    def delayed_fallback_account_provider_stream(
        stream_factory: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        nonlocal account_call_count
        del kwargs
        account_call_count += 1
        call_number = account_call_count

        async def stream() -> AsyncIterator[Any]:
            if call_number == 2:
                setup_entered.set()
                await release_setup.wait()
            async for event in stream_factory():
                yield event

        return stream()

    monkeypatch.setattr(
        "opensquilla.engine.runtime.account_provider_stream",
        delayed_fallback_account_provider_stream,
    )
    generation = 0
    primary = _ErrorProvider("429")
    primary._router_dynamic_cache_dispatch_generation_guard = lambda: generation == 0
    fallback = FallbackProvider()
    wrapped = _SelectorFallbackProvider(primary, FallbackSelector(fallback))

    collect_task = asyncio.create_task(_collect(wrapped.chat([], config=ChatConfig())))
    await asyncio.wait_for(setup_entered.wait(), timeout=1.0)
    generation = 1
    release_setup.set()
    events = await asyncio.wait_for(collect_task, timeout=1.0)

    assert account_call_count == 2
    assert fallback.calls == 0
    assert len(events) == 1
    assert events[0].code == "router_dynamic_cache_generation_changed"
    assert events[0].physical_request_count == 0


class _ErrorProvider:
    provider_name = "openai"

    def __init__(self, code: str) -> None:
        self.code = code

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config

        async def stream() -> AsyncIterator[Any]:
            yield ProviderError(message="provider rejected request", code=self.code)

        return stream()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("429", ProviderFailureKind.RATE_LIMITED),
        ("401", ProviderFailureKind.AUTH_INVALID),
        ("402", ProviderFailureKind.INSUFFICIENT_CREDITS),
    ],
)
async def test_single_pool_failure_uses_configured_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: ProviderFailureKind,
) -> None:
    import opensquilla.gateway.llm_runtime as llm_runtime

    reports: list[tuple[str, str, ProviderFailureKind]] = []
    pool = SimpleNamespace(
        report_failure=lambda provider, session, kind, retry_after_seconds=None: reports.append(
            (provider, session, kind)
        )
    )
    monkeypatch.setattr(llm_runtime, "profile_credential_pools", lambda: pool)
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    session_key = "agent:main:pool"
    seeded = await _seed_single_affinity_state(
        runner,
        policy,
        session_key=session_key,
        decision_id=f"pool-{code}",
    )
    generation_before_failure = runner._router_dynamic_cache_generation(session_key)
    metadata = {
        "_router_single_provider_finalized": True,
        "routed_provider_applied": "openrouter",
        "credential_pool": {
            "provider": "openrouter",
            "session_key": session_key,
        },
    }
    purges: list[str] = []
    wrapped = _SelectorFallbackProvider(
        _ErrorProvider(code),
        _OneModelSelector(),
        turn_metadata=metadata,
        cache_affinity_credential_failure_callback=lambda: (
            purges.append(code),
            runner._invalidate_router_dynamic_cache_affinity(
                session_key=session_key,
                reason="credential_pool_failure",
            ),
        ),
    )

    await _collect(wrapped.chat([], config=ChatConfig()))

    assert reports == [("openrouter", "agent:main:pool", expected)]
    assert purges == [code]
    assert runner._router_dynamic_cache_generation(session_key) > (generation_before_failure)
    assert not runner._router_single_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=seeded.session_epoch,
        policy=policy,
    )[0]


def test_single_resolver_requires_authoritative_catalog_before_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            provider_health_ledger=None,
            model_catalog=None,
        )

    assert raised.value.reason == "router_single_model_catalog_unavailable"
    assert rank_calls == []


async def test_missing_catalog_fails_before_analyzer_admission_or_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.admission as admission_module
    import opensquilla.provider.ranking_router as ranking_module

    analyzer_calls = 0
    admission_calls = 0

    async def analyzer_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal analyzer_calls
        del args, kwargs
        analyzer_calls += 1
        raise AssertionError("missing catalog must fail before Analyzer")

    def admission_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal admission_calls
        del args, kwargs
        admission_calls += 1
        raise AssertionError("missing catalog must fail before admission")

    monkeypatch.setattr(ranking_module, "analyze_task_with_provider", analyzer_spy)
    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_fallback_chain",
        analyzer_spy,
    )
    monkeypatch.setattr(
        admission_module,
        "get_shared_provider_admission_controller",
        admission_spy,
    )
    config = _router_single_config()
    runner = TurnRunner(
        provider_selector=None,
        config=config,
        model_catalog=None,
    )
    provider = _NoChatProvider()
    turn = SimpleNamespace(
        metadata={},
        semantic_message="hello",
        attachments=[],
        session_key="agent:main:missing-catalog",
    )

    with pytest.raises(DynamicRankingError) as raised:
        await runner._resolve_router_single_provider(
            turn=turn,
            provider=provider,
            cloned_selector=_Selector(),
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
        )

    assert raised.value.reason == "router_single_model_catalog_unavailable"
    assert analyzer_calls == 0
    assert admission_calls == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("inherited_provider", "inherited_model", "inherited_replay", "expected"),
    [
        ("OPENROUTER", "anthropic/claude-sonnet-4.5", True, True),
        ("openrouter", "anthropic/claude-sonnet-4.5", False, False),
        ("openrouter", "openai/gpt-5.5", True, False),
        ("anthropic", "anthropic/claude-sonnet-4.5", True, False),
    ],
)
def test_single_replays_private_provider_state_only_for_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    inherited_provider: str,
    inherited_model: str,
    inherited_replay: bool,
    expected: bool,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module

    _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inherited = replace(
        inherited,
        provider=inherited_provider,
        model=inherited_model,
        replay_provider_state=inherited_replay,
    )
    replay_arguments: list[bool | None] = []

    def resolve_selected(*args: Any, **kwargs: Any) -> ProviderDeploymentResolution:
        del args
        replay = kwargs.get("replay_provider_state")
        replay_arguments.append(replay)
        selected = ProviderConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.5",
            api_key="synthetic",
            replay_provider_state=bool(replay),
        )
        return ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        )

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        resolve_selected,
    )
    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=False,
        model_catalog=_Catalog(),
    )

    assert replay_arguments == [expected]
    assert route.provider_config.replay_provider_state is expected


def test_single_freezes_capabilities_from_selected_endpoint_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    from opensquilla.provider import ModelCapabilities

    _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    endpoint = "https://selected-endpoint.invalid/v1"
    selected = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.5",
        api_key="secret-never-project",
        base_url=endpoint,
        replay_provider_state=False,
    )
    capability = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=False,
        supports_streaming=True,
        reasoning_format="openrouter",
    )
    capability_calls: list[tuple[str, str, str]] = []

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            capability_calls.append((model_id, provider_name, base_url))
            return capability

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )
    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=False,
        model_catalog=EndpointCatalog(),
    )

    assert route.model_capabilities is capability
    assert capability_calls == [(selected.model, selected.provider, endpoint)]
    assert endpoint not in repr(route)
    assert endpoint not in repr(route.trace)
    assert selected.api_key not in repr(route)


@pytest.mark.parametrize(
    ("requires_tools", "input_modalities", "capabilities", "expected_reason"),
    [
        (
            True,
            ["text"],
            {"supports_tools": False, "supports_vision": True},
            "router_single_selected_model_tools_unavailable",
        ),
        (
            False,
            ["text", "image"],
            {"supports_tools": True, "supports_vision": False},
            "router_single_selected_model_vision_unavailable",
        ),
    ],
)
def test_selected_endpoint_capability_mismatch_fails_before_generation(
    monkeypatch: pytest.MonkeyPatch,
    requires_tools: bool,
    input_modalities: list[str],
    capabilities: dict[str, bool],
    expected_reason: str,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, selected = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inputs["request_context"] = {
        **inputs["request_context"],
        "input_modalities": input_modalities,
    }

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(**capabilities)

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=requires_tools,
            model_catalog=EndpointCatalog(),
        )

    assert raised.value.reason == expected_reason
    assert len(rank_calls) == 1


def test_anthropic_capabilities_become_authoritative_when_migration_gate_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.model_catalog as model_catalog_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inputs["request_context"] = {
        **inputs["request_context"],
        "input_modalities": ["text", "image"],
    }
    selected = ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="synthetic",
    )
    monkeypatch.setattr(
        model_catalog_module,
        "CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA",
        True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(supports_vision=False)

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            model_catalog=EndpointCatalog(),
        )

    assert raised.value.reason == "router_single_selected_model_vision_unavailable"
    assert len(rank_calls) == 1


@pytest.mark.parametrize(
    (
        "provider_id",
        "model_id",
        "supports_reasoning",
        "reasoning_format",
        "expect_failure",
    ),
    [
        ("openrouter", "anthropic/claude-sonnet-4.5", False, "none", True),
        ("anthropic", "claude-sonnet-4-6", False, "none", False),
        ("openai_codex", "gpt-5.5-codex", False, "none", False),
        ("deepseek", "deepseek-v4-pro", False, "none", True),
        ("deepseek", "deepseek-v4-pro", False, "deepseek", False),
        ("openrouter", "openai/gpt-5.5", True, "none", True),
        ("openrouter", "openai/gpt-5.5", True, "unknown", True),
        ("openrouter", "deepseek/deepseek-r1", True, "deepseek", False),
        ("ollama", "llama3", False, "none", True),
        ("openai_responses", "gpt-5.5", False, "none", True),
    ],
)
def test_managed_thinking_uses_selected_adapter_execution_contract(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    model_id: str,
    supports_reasoning: bool,
    reasoning_format: str,
    expect_failure: bool,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    if provider_id == "anthropic":
        inputs["request_context"] = {
            **inputs["request_context"],
            "input_modalities": ["text", "image"],
        }
    original_rank = ranking_module.rank_single_model

    def managed_rank(**kwargs: Any) -> SingleModelRankingDecision:
        decision = original_rank(**kwargs)
        selected_model = replace(
            decision.model,
            provider=provider_id,
            model_id=model_id,
            registry_facts={
                **decision.model.registry_facts,
                "provider": provider_id,
                "model_id": model_id,
            },
            thinking="high",
            requested_thinking_level="high",
            effective_thinking_level="high",
            thinking_policy_version="router-single-test/v1",
        )
        return replace(
            decision,
            model=selected_model,
            trace={
                **decision.trace,
                "selected_model": selected_model.identity,
                "selected_P": [selected_model.identity],
            },
        )

    monkeypatch.setattr(ranking_module, "rank_single_model", managed_rank)
    selected = ProviderConfig(
        provider=provider_id,
        model=model_id,
        api_key="synthetic",
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            resolved_model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del resolved_model_id, provider_name, base_url
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                reasoning_format=reasoning_format,
            )

    if expect_failure:
        with pytest.raises(DynamicRankingError) as raised:
            resolve_router_single_route(
                config=config,
                inherited_provider_config=inherited,
                turn_metadata={},
                ranking_inputs=inputs,
                requires_tools=False,
                model_catalog=EndpointCatalog(),
            )
        assert raised.value.reason == "router_single_selected_model_reasoning_unavailable"
    else:
        route = resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            model_catalog=EndpointCatalog(),
        )
        assert route.provider_config == selected
        assert route.model_capabilities.supports_reasoning is supports_reasoning
    assert len(rank_calls) == 1


def test_frozen_single_capabilities_bypass_mutated_live_catalog() -> None:
    from opensquilla.provider import ModelCapabilities

    capability = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=False,
        supports_streaming=True,
        reasoning_format="deepseek",
    )

    class ExplodingCatalog:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"live catalog access after freeze: {name}")

    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
        model_catalog=ExplodingCatalog(),
    )
    token = _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-r1",
            "max_tokens": 8_192,
            "context_window": 128_000,
            "capabilities": capability,
        }
    )
    try:
        resolved = _TurnRunnerModelCatalogAdapter(runner).lookup(
            "deepseek/deepseek-r1",
            "openrouter",
        )
    finally:
        _ROUTER_SINGLE_FROZEN_CATALOG.reset(token)

    assert resolved.max_tokens == 8_192
    assert resolved.context_window == 128_000
    assert resolved.capabilities is capability


def test_single_allowlist_candidates_still_receive_rank_time_health_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    health_calls: list[str] = []

    monkeypatch.setattr(
        ensemble_module,
        "_apply_router_dynamic_registry_allowlist",
        lambda *args, **kwargs: {
            "candidate_scope": "configured",
            "expected_identities": [],
        },
    )

    def runtime_facts(
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        health_calls.append(model)
        row = _health_row(state="benched" if model == "openai/gpt-5.5" else "healthy")
        row.update({"provider": provider, "model": model})
        return row

    resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs={**inputs, "registry_allowlist": {"enabled": True}},
        requires_tools=False,
        provider_health_ledger=SimpleNamespace(runtime_facts=runtime_facts),
        model_catalog=_Catalog(),
    )

    ranked_rows = rank_calls[0]["registry_snapshot"]["models"]
    first_facts = ranked_rows[0]["registry_facts"]
    second_facts = ranked_rows[1]["registry_facts"]
    assert health_calls == [
        "openai/gpt-5.5",
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-sonnet-4.5",
    ]
    assert first_facts["runtime_health"]["state"] == "benched"
    assert first_facts["runtime_hard_filter_reasons_by_role"]["proposer"] == [
        "runtime_deployment_benched"
    ]
    assert second_facts["runtime_health"]["state"] == "healthy"


def _direct_config_for(model: str) -> ProviderConfig:
    return ProviderConfig(
        provider="openrouter",
        model=model,
        api_key="synthetic",
        provider_routing={model: "anthropic"},
    )


def _facts_for(
    ledger: ProviderHealthLedger,
    config: ProviderConfig,
) -> dict[str, Any]:
    return ledger.runtime_facts(
        config.provider,
        config.model,
        upstream="anthropic",
    )


class _DoneWithoutCloseStream:
    def __init__(self) -> None:
        self.sent = False

    def __aiter__(self) -> _DoneWithoutCloseStream:
        return self

    async def __anext__(self) -> Any:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1)


class _DoneWithoutCloseProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return _DoneWithoutCloseStream()


async def test_direct_terminal_without_aclose_is_a_valid_stream_boundary() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/done-without-close")
    raw = _DoneWithoutCloseProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert [event.kind for event in events] == ["done"]
    assert raw.calls == 1
    assert facts["state"] == "healthy"
    assert facts["recent_successes"] == 1


class _TerminalThenBlockingStream:
    def __init__(self) -> None:
        self.sent = False
        self.second_next_called = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _TerminalThenBlockingStream:
        return self

    async def __anext__(self) -> Any:
        if not self.sent:
            self.sent = True
            return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1)
        self.second_next_called.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _TerminalThenBlockingProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _TerminalThenBlockingStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_direct_stops_at_terminal_and_withholds_it_until_close_returns() -> None:
    ledger = _HealthLedger([True])
    raw = _TerminalThenBlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config_for("test/terminal-close"),
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    task = asyncio.create_task(_collect(provider.chat([], config=ChatConfig(timeout=30.0))))

    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    assert task.done() is False
    assert raw.stream.second_next_called.is_set() is False
    assert ledger.successes == []
    raw.stream.close_release.set()
    events = await asyncio.wait_for(task, timeout=0.5)

    assert [event.kind for event in events] == ["done"]
    assert len(ledger.successes) == 1


async def test_cleanup_gate_blocks_past_health_cooldown_until_close_finishes() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/pending-cleanup")
    first_raw = _BlockingProvider()
    first = _RouterSingleDirectProvider(
        first_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.02,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first._STREAM_CLOSE_TIMEOUT_SECONDS = 0.02

    first_events = await _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    assert first_events[-1].request_started is True
    assert _facts_for(ledger, config)["state"] == "benched"

    clock.advance(1.1)
    eligible = _facts_for(ledger, config)
    assert eligible["state"] == "half_open"
    assert eligible["eligible"] is True

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert second_raw.calls == []
    assert rejected[0].code == "router_single_cleanup_pending"
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0

    first_raw.streams[0].close_release.set()
    for _ in range(4):
        await asyncio.sleep(0)
    completed = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert completed[-1].kind == "done"
    assert len(second_raw.calls) == 1
    assert _facts_for(ledger, config)["state"] == "healthy"


class _FailingCloseStream:
    def __init__(self) -> None:
        self.close_calls = 0

    def __aiter__(self) -> _FailingCloseStream:
        return self

    async def __anext__(self) -> Any:
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("synthetic close failure")


class _FailingCloseProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _FailingCloseStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_failed_required_close_sticky_poison_blocks_later_dispatch() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/poisoned-cleanup")
    raw = _FailingCloseProvider()
    first = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.02,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    await _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    assert raw.stream.close_calls == 1
    clock.advance(10.0)

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert second_raw.calls == []
    assert rejected[0].code == "router_single_cleanup_poisoned"
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0


class _LazyNeverStartedStream:
    def __init__(self) -> None:
        self.next_calls = 0
        self.close_calls = 0

    def __aiter__(self) -> _LazyNeverStartedStream:
        return self

    async def __anext__(self) -> Any:
        self.next_calls += 1
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        await asyncio.Event().wait()


class _LazyNeverStartedProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _LazyNeverStartedStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_deadline_before_first_anext_does_not_poison_cleanup_gate() -> None:
    ledger = _HealthLedger([True, True])
    config = _direct_config_for("test/pre-anext-deadline")
    raw = _LazyNeverStartedProvider()
    first = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 30.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first._STREAM_CLOSE_TIMEOUT_SECONDS = 0.01
    remaining = iter([1.0, 1.0, 0.0])
    first._remaining_seconds = lambda: next(remaining)  # type: ignore[method-assign]

    rejected = await _collect(first.chat([], config=ChatConfig(timeout=30.0)))

    assert raw.calls == 1
    assert raw.stream.next_calls == 0
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert len(ledger.cancels) == 1

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    completed = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert completed[-1].kind == "done"
    assert len(second_raw.calls) == 1


class _ErrorEventProvider:
    provider_name = "openrouter"

    def __init__(self, event: ProviderError) -> None:
        self.event = event
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1

        async def stream() -> AsyncIterator[Any]:
            yield self.event

        return stream()


@pytest.mark.parametrize("request_started", [False, True])
async def test_provider_error_zero_request_cancels_probe_but_started_error_benches(
    request_started: bool,
) -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for(f"test/error-started-{request_started}")
    ledger.record_failure(
        config.provider,
        config.model,
        ProviderFailureKind.RATE_LIMITED,
        upstream="anthropic",
    )
    clock.advance(1.1)
    event = ProviderError(
        message="synthetic provider error",
        code="429",
        request_started=request_started,
        physical_request_count=1 if request_started else 0,
    )
    raw = _ErrorEventProvider(event)
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    observed = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert observed == [event]
    assert facts["half_open_inflight"] is False
    assert facts["state"] == ("benched" if request_started else "half_open")


async def test_fixed_route_zero_request_error_never_claims_executed_identity() -> None:
    config = _direct_config_for("test/fixed-zero-request")
    event = ProviderError(
        message="local credential resolution failed",
        code="auth_unavailable",
        request_started=False,
        physical_request_count=0,
    )
    metadata: dict[str, Any] = {
        "fixed_four_tier_v2_decision": {
            "dispatch": {
                "physical_request_started": False,
                "physical_request_count": 0,
            }
        }
    }
    provider = _RouterSingleDirectProvider(
        _ErrorEventProvider(event),
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        turn_metadata=metadata,
        deployment_version="frozen-revision",
    )

    observed = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert observed == [event]
    dispatch = metadata["fixed_four_tier_v2_decision"]["dispatch"]
    assert dispatch["physical_request_started"] is False
    assert dispatch["physical_request_count"] == 0
    assert dispatch["execution_evidence"] == "provider_zero_request_error"
    assert dispatch["executed_provider"] is None
    assert dispatch["executed_model"] is None
    assert "executed_provider" not in metadata
    assert "executed_model" not in metadata


async def test_fixed_route_physical_audit_accumulates_and_never_regresses() -> None:
    config = _direct_config_for("test/fixed-multi-call")

    class SequencedProvider:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
            del messages, tools, config
            self.calls += 1
            call = self.calls

            async def stream() -> AsyncIterator[Any]:
                if call <= 2:
                    yield ProviderDone(
                        stop_reason="stop",
                        provider="openrouter",
                        model="test/fixed-multi-call",
                    )
                else:
                    yield ProviderError(
                        message="local preflight rejected the third call",
                        code="local_rejection",
                        request_started=False,
                        physical_request_count=0,
                    )

            return stream()

    metadata: dict[str, Any] = {
        "fixed_four_tier_v2_decision": {
            "dispatch": {
                "physical_request_started": False,
                "physical_request_count": 0,
            }
        }
    }
    provider = _RouterSingleDirectProvider(
        SequencedProvider(),
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        turn_metadata=metadata,
        deployment_version="frozen-revision",
    )

    assert (await _collect(provider.chat([], config=ChatConfig(timeout=30.0))))[-1].kind == ("done")
    assert (await _collect(provider.chat([], config=ChatConfig(timeout=30.0))))[-1].kind == ("done")
    assert (await _collect(provider.chat([], config=ChatConfig(timeout=30.0))))[-1].kind == (
        "error"
    )

    dispatch = metadata["fixed_four_tier_v2_decision"]["dispatch"]
    assert dispatch["physical_request_started"] is True
    assert dispatch["physical_request_count"] == 2
    assert dispatch["last_call_request_started"] is False
    assert dispatch["last_call_physical_request_count"] == 0
    assert dispatch["executed_provider"] == "openrouter"
    assert dispatch["executed_model"] == "test/fixed-multi-call"


async def test_fixed_route_terminal_usage_has_mutually_exclusive_billing_buckets() -> None:
    manager = _FixedRouteSessionManager()
    claim = SimpleNamespace(
        claim_id="claim-usage",
        status="materialized",
        error_code=None,
    )
    manager.claims[(manager.session_id, "request-usage")] = claim
    manager.decisions["route-usage"] = SimpleNamespace(
        route_id="route-usage",
        claim_id=claim.claim_id,
        execution_status="pending",
        route_trace={},
    )
    turn = SimpleNamespace(
        metadata={
            "fixed_four_tier_v2_decision_id": "route-usage",
            "fixed_four_tier_v2_decision": {
                "session_id": manager.session_id,
                "request_id": "request-usage",
                "execution_id": "execution-usage",
                "dispatch": {
                    "physical_request_started": True,
                    "physical_request_count": 1,
                    "executed_provider": "openrouter",
                    "executed_model": "deepseek/deepseek-v4-flash",
                },
            },
        }
    )
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    done = EngineDone(
        input_tokens=100,
        output_tokens=30,
        reasoning_tokens=7,
        cached_tokens=20,
        cache_write_tokens=5,
        cost_usd=0.02,
        billed_cost=0.015,
        cost_source="provider_billed",
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        requested_provider="openrouter",
        requested_model="deepseek/deepseek-v4-flash",
    )
    done.provider_usage = {
        "provider_reported_cost": 0.015,
        "response_ids": ["upstream-response"],
        "api_key": "must-not-persist",
        "request": {"prompt": "must-not-persist-either"},
    }

    await runner._settle_fixed_four_tier_v2_route(
        turn,
        execution_status="succeeded",
        response_id="response-usage",
        done_event=done,
    )

    usage = turn.metadata["fixed_four_tier_v2_decision"]["provider_usage"]
    assert usage["input_tokens"] == 100
    assert usage["cache_read_tokens"] == 20
    assert usage["cache_write_tokens"] == 5
    assert usage["normalized_billing_buckets"] == {
        "normal_input_tokens": 75,
        "cache_read_tokens": 20,
        "cache_write_tokens": 5,
        "output_tokens": 30,
        "reasoning_tokens_detail": 7,
        "input_tokens_total": 100,
        "input_buckets_reconcile": True,
        "normalization_anomaly": False,
    }
    assert usage["provider_native_usage"] == {
        "provider_reported_cost": 0.015,
        "response_ids": ["upstream-response"],
    }
    assert "must-not-persist" not in repr(usage)
    assert manager.settlements[-1]["response_id"] == "response-usage"


async def test_fixed_route_terminal_usage_preserves_anomalous_raw_cache_counters() -> None:
    manager = _FixedRouteSessionManager()
    claim = SimpleNamespace(
        claim_id="claim-usage-anomaly",
        status="materialized",
        error_code=None,
    )
    manager.claims[(manager.session_id, "request-usage-anomaly")] = claim
    manager.decisions["route-usage-anomaly"] = SimpleNamespace(
        route_id="route-usage-anomaly",
        claim_id=claim.claim_id,
        execution_status="pending",
        route_trace={},
    )
    turn = SimpleNamespace(
        metadata={
            "fixed_four_tier_v2_decision_id": "route-usage-anomaly",
            "fixed_four_tier_v2_decision": {
                "session_id": manager.session_id,
                "request_id": "request-usage-anomaly",
                "execution_id": "execution-usage-anomaly",
                "dispatch": {
                    "physical_request_started": True,
                    "physical_request_count": 1,
                    "executed_provider": "openrouter",
                    "executed_model": "deepseek/deepseek-v4-flash",
                },
            },
        }
    )
    runner = TurnRunner(provider_selector=None, session_manager=manager)

    await runner._settle_fixed_four_tier_v2_route(
        turn,
        execution_status="succeeded",
        done_event=EngineDone(
            input_tokens=100,
            output_tokens=30,
            cached_tokens=120,
            cache_write_tokens=5,
            provider="openrouter",
            model="deepseek/deepseek-v4-flash",
        ),
    )

    usage = turn.metadata["fixed_four_tier_v2_decision"]["provider_usage"]
    assert usage["cache_read_tokens"] == 120
    assert usage["cache_write_tokens"] == 5
    assert usage["normalized_billing_buckets"] == {
        "normal_input_tokens": 0,
        "cache_read_tokens": 100,
        "cache_write_tokens": 0,
        "output_tokens": 30,
        "reasoning_tokens_detail": 0,
        "input_tokens_total": 100,
        "input_buckets_reconcile": False,
        "normalization_anomaly": True,
    }


async def test_required_fixed_route_terminal_settlement_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FixedRouteSessionManager()
    calls = 0

    async def fail_settlement(**kwargs: Any) -> bool:
        nonlocal calls
        del kwargs
        calls += 1
        raise RuntimeError("synthetic storage outage")

    monkeypatch.setattr(manager, "settle_fixed_four_tier_decision", fail_settlement)
    turn = SimpleNamespace(
        metadata={
            "fixed_four_tier_v2_decision_id": "route-required-settlement",
            "fixed_four_tier_v2_decision": {
                "session_id": manager.session_id,
                "request_id": "request-required-settlement",
                "execution_id": "execution-required-settlement",
                "dispatch": {
                    "physical_request_started": False,
                    "physical_request_count": 0,
                },
            },
        }
    )
    runner = TurnRunner(provider_selector=None, session_manager=manager)

    with pytest.raises(
        RuntimeError,
        match="terminal audit persistence failed",
    ):
        await runner._settle_fixed_four_tier_v2_route(
            turn,
            execution_status="succeeded",
            response_id="response-required-settlement",
            required=True,
        )

    assert calls == 3
    pending = turn.metadata["fixed_four_tier_v2_terminal_settlement_pending"]
    assert {
        key: pending[key] for key in ("route_id", "execution_status", "response_id", "error_code")
    } == {
        "route_id": "route-required-settlement",
        "execution_status": "succeeded",
        "response_id": "response-required-settlement",
        "error_code": None,
    }
    assert isinstance(pending["recorded_at_ms"], int)


class _MissingTerminalStream:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.close_called = False

    def __aiter__(self) -> _MissingTerminalStream:
        return self

    async def __anext__(self) -> Any:
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_called = True


class _MissingTerminalProvider:
    provider_name = "openrouter"

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.stream = _MissingTerminalStream(error)

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_empty_stream_records_transport_failure_in_real_ledger() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/empty-stream")
    raw = _MissingTerminalProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert events == []
    assert raw.stream.close_called is True
    assert facts["state"] == "benched"
    assert facts["last_failure_kind"] == "transport_transient"


async def test_iterator_exception_records_transport_failure_in_real_ledger() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/iterator-exception")
    raw = _MissingTerminalProvider(RuntimeError("synthetic iterator failure"))
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    with pytest.raises(RuntimeError, match="synthetic iterator failure"):
        await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert raw.stream.close_called is True
    assert facts["state"] == "benched"
    assert facts["last_failure_kind"] == "transport_transient"


def test_b5_pipeline_does_not_touch_router_single_contextvar() -> None:
    sentinel = {
        "provider": "sentinel",
        "model": "sentinel",
        "max_tokens": 1,
        "context_window": 1,
    }
    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": False, "mode": "multiple"},
    )
    runner = TurnRunner(provider_selector=None, config=config)

    async def run() -> None:
        token = _ROUTER_SINGLE_FROZEN_CATALOG.set(sentinel)
        try:
            await runner._run_pipeline(
                "hello",
                "agent:main:b5-contextvar",
                _NoChatProvider(),
                _Selector(),
                [],
                "system",
                [],
            )
            assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is sentinel
        finally:
            _ROUTER_SINGLE_FROZEN_CATALOG.reset(token)

    asyncio.run(run())


def test_real_router_single_analyzer_provider_accepts_frozen_call_contract() -> None:
    config = _router_single_config()
    runner = TurnRunner(provider_selector=None, config=config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )

    provider = runner._router_dynamic_task_analyzer_provider(
        inherited,
        session_key="agent:main:analyzer-signature",
        ranking_config=ranking_config_snapshot(),
        analyzer_route={
            "provider": "openrouter",
            "model": "openai/gpt-5.5",
            "upstream_provider": "openai",
        },
        allow_canary_route=True,
    )

    assert provider is not None


async def test_run_turn_scopes_private_freeze_after_router_event_and_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.types import RouterDecisionEvent
    from opensquilla.provider import ModelCapabilities
    from opensquilla.tools.types import CallerKind, ToolContext

    selected = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-r1",
        api_key="private-api-key-never-project",
        base_url="https://selected-private-endpoint.invalid/v1",
        provider_routing={"deepseek/deepseek-r1": "deepseek"},
        replay_provider_state=False,
    )
    capabilities = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=True,
        supports_streaming=True,
        reasoning_format="deepseek",
    )

    class RunSelector(_Selector):
        def clone(self) -> RunSelector:
            return RunSelector(self._cfg)

    class RunProvider:
        provider_name = "openrouter"

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text="ok")
                yield ProviderDone(
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=1,
                )

            return stream()

        async def list_models(self) -> list[Any]:
            return []

    class ExplodingLiveCatalog:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"live catalog accessed after freeze: {name}")

    raw = RunProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        selected,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={
            "provider": selected.provider,
            "model": selected.model,
            "max_tokens": 6_144,
            "context_window": 131_072,
            "capabilities": capabilities,
        },
        enforces_routed_thinking_policy=False,
    )
    finalized_observability: list[bool] = []

    def unexpected_reroute() -> Any:
        raise AssertionError("unchanged generation must not reroute")

    direct._router_dynamic_cache_reroute_plan = _RouterDynamicCacheReroutePlan(
        session_key="agent:main:single-freeze-scope",
        selection_generation=0,
        reroute_without_affinity=unexpected_reroute,
        finalize_observability=lambda: finalized_observability.append(True),
    )

    async def routed_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[TurnContext, Any]:
        del provider, cloned_selector, kwargs
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=direct,
                model=selected.model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "_router_single_provider_finalized": True,
                    "_router_single_frozen_catalog": {
                        "provider": selected.provider,
                        "model": selected.model,
                        "max_tokens": 6_144,
                        "context_window": 131_072,
                    },
                    "routed_tier": "c2",
                    "routed_model": selected.model,
                    "routing_source": "router_single",
                    "routing_confidence": 0.91,
                    "executed_provider": selected.provider,
                    "executed_model": selected.model,
                },
            ),
            direct,
        )

    monkeypatch.setattr(TurnRunner, "_run_pipeline", routed_pipeline)
    logger_writes: list[str] = []

    class _RecordingTurnCallLogger:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def write(self, kind: str, payload: Any) -> None:
            del payload
            logger_writes.append(kind)

    monkeypatch.setattr(
        "opensquilla.engine.runtime.TurnCallLogger",
        _RecordingTurnCallLogger,
    )
    runner = TurnRunner(
        provider_selector=RunSelector(selected),
        config=GatewayConfig(
            squilla_router=SquillaRouterConfig(enabled=False),
            llm={"max_tokens": 0, "context_window_tokens": 0},
        ),
        model_catalog=ExplodingLiveCatalog(),
        diagnostics_state=SimpleNamespace(
            raw_turn_call_enabled=lambda: True,
        ),
    )
    frozen_during_bootstrap: list[dict[str, Any] | None] = []
    resolved_capabilities: list[Any] = []
    original_bootstrap = runner._agent_bootstrap_stage.run

    async def observe_bootstrap(inp: Any) -> Any:
        frozen = _ROUTER_SINGLE_FROZEN_CATALOG.get()
        frozen_during_bootstrap.append(dict(frozen) if frozen is not None else None)
        outcome = await original_bootstrap(inp)
        resolved_capabilities.append(outcome.output.model_capabilities)
        return outcome

    monkeypatch.setattr(runner._agent_bootstrap_stage, "run", observe_bootstrap)
    stream = runner.run(
        "hello",
        "agent:main:single-freeze-scope",
        tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
        history_has_persisted_user=False,
        no_memory_capture=True,
    )

    router_event = await anext(stream)
    assert isinstance(router_event, RouterDecisionEvent)
    assert router_event.context_window == 131_072
    # The private freeze is not installed across this externally visible yield.
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None

    # Same-task unrelated consumption cannot clear the pending single freeze,
    # because the authoritative payload still lives on the direct wrapper.
    unrelated_token = _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "other",
            "model": "other-model",
            "max_tokens": 1,
            "context_window": 1,
            "capabilities": None,
        }
    )
    try:
        unrelated = TurnRunner(
            provider_selector=None,
            config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
            model_catalog=ExplodingLiveCatalog(),
        )
        unrelated_result = _TurnRunnerModelCatalogAdapter(unrelated).lookup(
            "other-model",
            "other",
        )
        assert unrelated_result.max_tokens == 1
    finally:
        _ROUTER_SINGLE_FROZEN_CATALOG.reset(unrelated_token)

    remaining_events = [event async for event in stream]

    assert frozen_during_bootstrap == [
        {
            "provider": selected.provider,
            "model": selected.model,
            "max_tokens": 6_144,
            "context_window": 131_072,
            "capabilities": capabilities,
        }
    ]
    assert resolved_capabilities == [capabilities]
    assert finalized_observability == [True]
    assert logger_writes.count("prompt_report") == 1
    assert logger_writes.count("turn_start") == 1
    assert logger_writes.count("agent_runtime_budget") == 1
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None
    assert any(getattr(event, "kind", "") == "done" for event in remaining_events)
    projected = repr([router_event, *remaining_events]) + repr(frozen_during_bootstrap)
    assert selected.base_url not in projected
    assert selected.api_key not in projected


async def test_compaction_generation_drift_reroutes_before_history_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.types import RouterDecisionEvent
    from opensquilla.provider import ModelCapabilities
    from opensquilla.tools.types import CallerKind, ToolContext

    session_key = "agent:main:compaction-reroute"
    initial = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic-initial",
    )
    final = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.5",
        api_key="synthetic-final",
    )
    capabilities = ModelCapabilities(
        supports_tools=True,
        supports_streaming=True,
    )

    class RunSelector(_Selector):
        def clone(self) -> RunSelector:
            return RunSelector(self._cfg)

    class FinalProvider:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text="final")
                yield ProviderDone(
                    provider=final.provider,
                    model=final.model,
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=1,
                )

            return stream()

    old_raw = _NoChatProvider()
    final_raw = FinalProvider()
    initial_selector = _Selector(initial)
    initial_direct = _RouterSingleDirectProvider(
        old_raw,
        initial,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={
            "provider": initial.provider,
            "model": initial.model,
            "max_tokens": 4_096,
            "context_window": 128_000,
            "capabilities": capabilities,
        },
        enforces_routed_thinking_policy=False,
    )
    initial_provider = _SelectorFallbackProvider(
        initial_direct,
        initial_selector,
    )
    turn_holder: dict[str, Any] = {}
    analyzer_calls = 0

    def reroute() -> _RouterDynamicCacheRerouteResult:
        turn = turn_holder["turn"]
        selector = turn_holder["selector"]
        selector.override_provider_config(final)
        turn.model = final.model
        turn.metadata.update(
            {
                "routed_model": final.model,
                "executed_provider": final.provider,
                "executed_model": final.model,
                "resolved_model": final.model,
                "alias_resolution_chain": [final.model],
                "provider_after_rewrite": final.provider,
                "router_single_selected_provider": final.provider,
                "router_single_selected_model": final.model,
                "_router_single_frozen_catalog": {
                    "provider": final.provider,
                    "model": final.model,
                    "max_tokens": 8_192,
                    "context_window": 200_000,
                },
            }
        )
        final_selector = _Selector(final)
        final_direct = _RouterSingleDirectProvider(
            final_raw,
            final,
            health_ledger=None,
            absolute_deadline=None,
            frozen_catalog={
                "provider": final.provider,
                "model": final.model,
                "max_tokens": 8_192,
                "context_window": 200_000,
                "capabilities": capabilities,
            },
            enforces_routed_thinking_policy=False,
        )
        return _RouterDynamicCacheRerouteResult(
            provider=_SelectorFallbackProvider(final_direct, final_selector),
            resolved_model=final.model,
            provider_name=final.provider,
            active_provider_id=final.provider,
        )

    initial_provider._router_dynamic_cache_reroute_plan = _RouterDynamicCacheReroutePlan(
        session_key=session_key,
        selection_generation=0,
        reroute_without_affinity=reroute,
    )

    async def routed_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[TurnContext, Any]:
        nonlocal analyzer_calls
        del provider, kwargs
        analyzer_calls += 1
        turn = TurnContext(
            message=message,
            session_key=session_key,
            config=self._config,
            provider=initial_provider,
            model=initial.model,
            tool_defs=tool_defs,
            system_prompt=base_prompt,
            attachments=attachments,
            metadata={
                "_router_single_provider_finalized": True,
                "_router_single_frozen_catalog": {
                    "provider": initial.provider,
                    "model": initial.model,
                    "max_tokens": 4_096,
                    "context_window": 128_000,
                },
                "routed_tier": "c2",
                "routed_model": initial.model,
                "routing_source": "router_single",
                "routing_confidence": 0.9,
                "executed_provider": initial.provider,
                "executed_model": initial.model,
            },
        )
        turn_holder.update(turn=turn, selector=cloned_selector)
        return turn, initial_provider

    monkeypatch.setattr(TurnRunner, "_run_pipeline", routed_pipeline)
    logger_instances: list[Any] = []

    class RecordingLogger:
        def __init__(self, **kwargs: Any) -> None:
            self.provider = kwargs["provider"]
            self.model = kwargs["model"]
            self.writes: list[tuple[str, Any]] = []
            logger_instances.append(self)

        def write(self, kind: str, payload: Any) -> None:
            self.writes.append((kind, payload))

    monkeypatch.setattr(
        "opensquilla.engine.runtime.TurnCallLogger",
        RecordingLogger,
    )
    runner = TurnRunner(
        provider_selector=RunSelector(initial),
        config=GatewayConfig(
            squilla_router=SquillaRouterConfig(enabled=False),
            llm={"max_tokens": 0, "context_window_tokens": 0},
        ),
        model_catalog=_Catalog(),
        diagnostics_state=SimpleNamespace(
            raw_turn_call_enabled=lambda: True,
        ),
    )
    runner._router_dynamic_cache_affinity_generation = {session_key: 1}
    bootstrap_models: list[str] = []
    original_bootstrap = runner._agent_bootstrap_stage.run

    async def observe_bootstrap(inp: Any) -> Any:
        bootstrap_models.append(inp.resolved_model)
        return await original_bootstrap(inp)

    monkeypatch.setattr(runner._agent_bootstrap_stage, "run", observe_bootstrap)
    events = [
        event
        async for event in runner.run(
            "hello",
            session_key,
            tool_context=ToolContext(
                is_owner=True,
                caller_kind=CallerKind.CLI,
            ),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert analyzer_calls == 1
    assert bootstrap_models == [initial.model, final.model]
    assert old_raw.calls == 0
    assert final_raw.calls == 1
    assert len(router_events) == 1
    assert router_events[0].model == final.model
    assert turn_holder["turn"].metadata["executed_model"] == final.model
    written_loggers = [logger for logger in logger_instances if logger.writes]
    assert len(written_loggers) == 1
    assert written_loggers[0].provider == final.provider
    assert written_loggers[0].model == final.model
    prompt_payload = next(
        payload for kind, payload in written_loggers[0].writes if kind == "prompt_report"
    )
    assert prompt_payload["resolved_model"] == final.model
    assert prompt_payload["provider_after_rewrite"] == final.provider


async def test_single_compaction_reroute_disables_scoring_but_collects_fresh_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    import opensquilla.provider.selector as selector_module
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.provider import ModelCapabilities

    session_key = "agent:main:single-compaction-collection"
    selected = _affinity_direct_config("anthropic/single-compaction-collection")
    policy_override = {
        "session": {
            "kv_cache_affinity": {
                "strategy": "bonus",
                "topologies": ["single"],
                "ttl_seconds": 300,
                "age_decay": "linear",
                "bonus_by_evidence": {
                    "read_hit": 0.05,
                    "write_only": 0.025,
                },
            }
        }
    }
    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm={
            "provider": selected.provider,
            "model": selected.model,
            "api_key": selected.api_key,
            "base_url": selected.base_url,
            "max_tokens": 4_096,
            "context_window_tokens": 128_000,
        },
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "router_dynamic",
            "ranking_config_override": policy_override,
            "ranking_thinking_assignment_enabled": False,
            "latency_class": "experiment",
        },
    )
    runner = TurnRunner(
        provider_selector=_Selector(selected),
        config=config,
        model_catalog=_Catalog(),
        session_manager=object(),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.session_services.get_session_epoch",
        lambda manager, key: 0,
    )
    monkeypatch.setattr(
        runner,
        "_router_dynamic_task_analyzer_provider",
        lambda *args, **kwargs: None,
    )
    task_analysis = TaskAnalysisResult(
        profile={
            "capability_dist": {"reasoning": 1.0},
            "domain_dist": {"software_engineering": 1.0},
            "tier_dist": {"3": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "context": "short",
                "modality": ["text"],
                "risk": "medium",
            },
            "optional_constraints": {},
            "session_intent": {"type": "continue", "confidence": 1.0},
        },
        source="single_compaction_collection_test",
        schema_valid=True,
        confidence=1.0,
    )

    async def analyze_task(**kwargs: Any) -> TaskAnalysisResult:
        del kwargs
        return task_analysis

    monkeypatch.setattr(ranking_module, "analyze_task_with_provider", analyze_task)
    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_fallback_chain",
        analyze_task,
    )
    ranking_inputs_seen: list[dict[str, Any]] = []
    credential_token = _affinity_credential_token(selected)

    def resolve_route(**kwargs: Any) -> Any:
        inputs = kwargs["ranking_inputs"]
        assert isinstance(inputs, dict)
        ranking_inputs_seen.append(dict(inputs))
        return SimpleNamespace(
            provider_config=selected,
            effective_tier=2,
            trace={
                "strategy": "router_dynamic",
                "execution_mode": "router_single",
                "selected_model": f"{selected.provider}:{selected.model}",
                "selected_P": [f"{selected.provider}:{selected.model}"],
            },
            direct_output_tokens=4_096,
            context_window_tokens=128_000,
            model_capabilities=ModelCapabilities(
                supports_tools=True,
                supports_streaming=True,
            ),
            thinking=None,
            requested_thinking_level=None,
            effective_thinking_level=None,
            thinking_fallback_reason="",
            thinking_policy_version="",
            actual_model_aliases=(selected.model,),
            credential_namespace_token=credential_token,
        )

    monkeypatch.setattr(ensemble_module, "resolve_router_single_route", resolve_route)
    physical_providers: list[_AffinityDoneProvider] = []

    def resolve_physical(selector: Any) -> _AffinityDoneProvider:
        del selector
        physical = _AffinityDoneProvider(
            [
                ProviderDone(
                    provider=selected.provider,
                    model=selected.model,
                    cached_tokens=31,
                    cache_write_tokens=0,
                )
            ]
        )
        physical_providers.append(physical)
        return physical

    monkeypatch.setattr(selector_module.ModelSelector, "resolve", resolve_physical)
    turn = TurnContext(
        message="continue",
        session_key=session_key,
        config=config,
        provider=_NoChatProvider(),
        model=selected.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={
            "routed_tier": "c2",
            "routing_confidence": 0.9,
        },
    )

    initial_provider = await runner._resolve_router_single_provider(
        turn=turn,
        provider=turn.provider,
        cloned_selector=_Selector(selected),
        turn_config=config,
        ensemble_cfg=config.llm_ensemble,
        turn_absolute_deadline=None,
    )
    reroute_plan = getattr(
        initial_provider,
        "_router_dynamic_cache_reroute_plan",
        None,
    )
    assert isinstance(reroute_plan, _RouterDynamicCacheReroutePlan)
    runner._invalidate_router_dynamic_cache_affinity(
        session_key=session_key,
        reason="compaction",
    )

    rerouted = reroute_plan.reroute_without_affinity()

    assert len(ranking_inputs_seen) == 2
    assert "cache_affinity_policy" in ranking_inputs_seen[0]
    assert ranking_inputs_seen[1]["cache_affinity_collection_enabled"] is True
    assert "cache_affinity_policy" not in ranking_inputs_seen[1]
    assert "cache_affinity_receipts" not in ranking_inputs_seen[1]
    final_direct = rerouted.provider._provider
    assert isinstance(final_direct, _RouterSingleDirectProvider)
    assert final_direct._cache_affinity_credential_namespace_token is credential_token
    assert physical_providers[0].calls == 0
    events = await _collect(rerouted.provider.chat([], config=ChatConfig(timeout=30.0)))
    assert [event.kind for event in events] == ["done"]
    assert physical_providers[1].calls == 1
    assert runner._commit_pending_router_dynamic_cache_affinity(turn, EngineDone())
    available, receipts, _ = runner._router_single_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=0,
        policy=_router_dynamic_cache_affinity_policy(
            config.llm_ensemble.prepared_ranking_config(),
            topology="single",
        ),
    )
    assert available is True
    assert len(receipts) == 1
    assert receipts[0].cached_tokens == 31


async def test_multiple_compaction_generation_drift_rebuilds_final_plan_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.types import RouterDecisionEvent
    from opensquilla.tools.types import CallerKind, ToolContext

    session_key = "agent:main:multiple-compaction-reroute"
    initial_model = "synthetic/aggregator-affinity"
    final_model = "synthetic/aggregator-neutral"
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic-multiple-compaction",
    )
    policy_override = {
        "session": {
            "kv_cache_affinity": {
                "strategy": "bonus",
                "topologies": ["multiple"],
                "ttl_seconds": 300,
                "age_decay": "linear",
                "bonus_by_evidence": {
                    "read_hit": 0.05,
                    "write_only": 0.025,
                },
            }
        }
    }

    class RunSelector(_Selector):
        def clone(self) -> RunSelector:
            return RunSelector(self._cfg)

    class SyntheticEnsembleProvider:
        provider_name = "openrouter"
        profile_name = "router_dynamic/c2"

        def __init__(
            self,
            *,
            aggregator_model: str,
            decision_id: str,
            turn_metadata: dict[str, Any],
        ) -> None:
            self.aggregator_model = aggregator_model
            self.turn_metadata = turn_metadata
            self.calls = 0
            self.pending_plan_matched_at_dispatch = False
            self.selection_plan = {
                "strategy": "router_dynamic",
                "selection_mode": "router_dynamic",
                "decision_id": decision_id,
                "ranking_version": "multiple-compaction-test-v1",
                "registry_snapshot_version": "multiple-compaction-test-v1",
                "registry_snapshot_hash": f"hash-{aggregator_model}",
                "selected_P": [
                    "openrouter:synthetic/proposer-a",
                    "openrouter:synthetic/proposer-b",
                ],
                "selected_A": f"openrouter:{aggregator_model}",
                "effective_tier": 2,
                "session": {
                    "intent": "continue",
                    "cache_continuity_available": aggregator_model == initial_model,
                },
            }

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            self.pending_plan_matched_at_dispatch = (
                self.turn_metadata.get("router_dynamic_pending_route_plan") is self.selection_plan
            )

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text=f"answer from {self.aggregator_model}")
                yield ProviderDone(
                    provider="openrouter",
                    model=self.aggregator_model,
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=1,
                )

            return stream()

    built: list[SyntheticEnsembleProvider] = []
    built_ranking_inputs: list[dict[str, Any]] = []

    def build_provider(**kwargs: Any) -> SyntheticEnsembleProvider:
        inputs = kwargs.get("ranking_inputs")
        assert isinstance(inputs, dict)
        built_ranking_inputs.append(dict(inputs))
        decision_id = str(inputs["decision_id"])
        affinity_active = "cache_affinity_policy" in inputs
        provider = SyntheticEnsembleProvider(
            aggregator_model=(initial_model if affinity_active else final_model),
            decision_id=decision_id,
            turn_metadata=kwargs["turn_metadata"],
        )
        built.append(provider)
        return provider

    monkeypatch.setattr(
        ensemble_module,
        "build_ensemble_provider_from_config",
        build_provider,
    )

    task_analysis = TaskAnalysisResult(
        profile={
            "capability_dist": {"reasoning": 1.0},
            "domain_dist": {"software_engineering": 1.0},
            "tier_dist": {"3": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "context": "short",
                "modality": ["text"],
                "risk": "medium",
            },
            "optional_constraints": {},
            "session_intent": {"type": "continue", "confidence": 1.0},
        },
        source="multiple_compaction_test",
        schema_valid=True,
        confidence=1.0,
    )

    async def analyze_task(**kwargs: Any) -> TaskAnalysisResult:
        del kwargs
        return task_analysis

    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_provider",
        analyze_task,
    )
    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_fallback_chain",
        analyze_task,
    )

    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm={
            "provider": inherited.provider,
            "model": inherited.model,
            "api_key": inherited.api_key,
            "max_tokens": 4_096,
            "context_window_tokens": 128_000,
        },
        llm_ensemble={
            "enabled": True,
            "mode": "multiple",
            "selection_mode": "router_dynamic",
            "shuffle_candidates": False,
            "ranking_thinking_assignment_enabled": False,
            "ranking_config_override": policy_override,
            "aggregator_recovery_mode": "experiment",
            "latency_class": "experiment",
        },
    )
    runner = TurnRunner(
        provider_selector=RunSelector(inherited),
        config=config,
        model_catalog=_Catalog(),
    )
    runner._session_manager = object()
    monkeypatch.setattr(
        "opensquilla.gateway.session_services.get_session_epoch",
        lambda manager, key: 0,
    )
    selector = RunSelector(inherited)
    turn, initial_provider = await runner._run_pipeline(
        "continue",
        session_key,
        _NoChatProvider(),
        selector,
        [],
        "system",
        [],
        usage_execution_context=SimpleNamespace(
            turn_id="turn-multiple-compaction",
        ),
    )
    assert isinstance(turn, TurnContext)
    assert len(built) == 1
    assert initial_provider is built[0]
    assert built[0].aggregator_model == initial_model
    initial_decision_id = turn.metadata["ensemble_decision_id"]
    runner._session_manager = None
    turn.metadata.update(
        {
            "routed_tier": "c2",
            "routed_model": inherited.model,
            "routing_source": "router_dynamic",
            "routing_confidence": 0.9,
        }
    )

    # The route froze generation zero. Compaction invalidates continuity before
    # history/dispatch, so the retained Analyzer result must rebuild once with
    # all affinity inputs removed.
    runner._router_dynamic_cache_affinity_generation = {session_key: 1}
    reroute_plan = getattr(
        initial_provider,
        "_router_dynamic_cache_reroute_plan",
        None,
    )
    assert isinstance(reroute_plan, _RouterDynamicCacheReroutePlan)
    assert reroute_plan.selection_generation == 0
    assert runner._router_dynamic_cache_generation(session_key) == 1

    async def prebuilt_pipeline(
        self: TurnRunner,
        message: str,
        requested_session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[TurnContext, Any]:
        del (
            self,
            message,
            requested_session_key,
            provider,
            cloned_selector,
            tool_defs,
            base_prompt,
            attachments,
            kwargs,
        )
        return turn, initial_provider

    monkeypatch.setattr(TurnRunner, "_run_pipeline", prebuilt_pipeline)
    events = [
        event
        async for event in runner.run(
            "continue",
            session_key,
            tool_context=ToolContext(
                is_owner=True,
                caller_kind=CallerKind.CLI,
            ),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    assert len(built) == 2
    assert "cache_affinity_policy" in built_ranking_inputs[0]
    assert built_ranking_inputs[1]["cache_affinity_collection_enabled"] is True
    assert "cache_affinity_policy" not in built_ranking_inputs[1]
    assert "cache_affinity_receipts" not in built_ranking_inputs[1]
    final_provider = built[1]
    assert built[0].calls == 0
    assert final_provider.calls == 1
    assert final_provider.aggregator_model == final_model
    assert final_provider.selection_plan["decision_id"] == initial_decision_id
    final_projection = turn.metadata["router_dynamic_decision"]
    assert final_projection["decision_id"] == initial_decision_id
    assert final_projection["selected_P"] == (final_provider.selection_plan["selected_P"])
    assert final_projection["selected_A"] == (final_provider.selection_plan["selected_A"])
    assert final_provider.pending_plan_matched_at_dispatch is True
    assert "router_dynamic_pending_route_plan" not in turn.metadata
    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 1
    assert router_events[0].model == inherited.model


class _AffinityDoneProvider:
    provider_name = "openrouter"
    _provider_routing_strict = True

    def __init__(self, events: list[ProviderDone]) -> None:
        self._events = list(events)
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        event = self._events[self.calls]
        self.calls += 1

        async def stream() -> AsyncIterator[Any]:
            yield event

        return stream()


def _affinity_direct_config(model: str = "anthropic/affinity-model") -> ProviderConfig:
    return ProviderConfig(
        provider="openrouter",
        model=model,
        api_key="synthetic-affinity-secret",
        base_url="https://openrouter.ai/api/v1",
        org_id="synthetic-tenant",
        provider_routing={model: "anthropic"},
        _provider_routing_strict_override=True,
    )


def _affinity_credential_token(config: ProviderConfig) -> object:
    token = build_credential_namespace_token(
        provider=config.provider,
        resolved_secret=config.api_key,
        org_id=config.org_id,
    )
    assert token is not None
    return token


def _affinity_context(
    *,
    decision_id: str = "decision-affinity",
    provider_instance_token: str = "provider-affinity",
    generation: int = 0,
    session_key: str = "agent:main:affinity",
    session_epoch: int = 7,
) -> _RouterDynamicCacheAffinityCollectionContext:
    return _RouterDynamicCacheAffinityCollectionContext(
        turn_id="turn-affinity",
        decision_id=decision_id,
        provider_instance_token=provider_instance_token,
        provider_instance_generation=0,
        session_key=session_key,
        session_epoch=session_epoch,
        selection_generation=generation,
        topology="single",
    )


async def _seed_single_affinity_state(
    runner: TurnRunner,
    policy: _RouterDynamicCacheAffinityPolicy,
    *,
    session_key: str,
    decision_id: str,
    session_epoch: int = 7,
) -> _RouterDynamicCacheAffinityCollectionContext:
    config = _affinity_direct_config(f"anthropic/{decision_id}")
    context = _affinity_context(
        decision_id=decision_id,
        provider_instance_token=f"provider-{decision_id}",
        session_key=session_key,
        session_epoch=session_epoch,
    )
    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        _AffinityDoneProvider(
            [
                ProviderDone(
                    provider=config.provider,
                    model=config.model,
                    cached_tokens=17,
                    cache_write_tokens=0,
                )
            ]
        ),
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=context,
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_generation_getter=lambda: 0,
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )
    await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    assert len(batches) == 1
    key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(key, batches[0])
    turn = SimpleNamespace(
        session_key=session_key,
        metadata={"router_single_decision_id": decision_id},
    )
    assert runner._commit_pending_router_dynamic_cache_affinity(
        turn,
        EngineDone(),
    )
    return context


async def _seed_multiple_affinity_state(
    runner: TurnRunner,
    policy: _RouterDynamicCacheAffinityPolicy,
    *,
    session_key: str,
    decision_id: str,
    session_epoch: int = 7,
) -> _RouterDynamicCacheAffinityCollectionContext:
    context = _RouterDynamicCacheAffinityCollectionContext(
        turn_id=f"turn-{decision_id}",
        decision_id=decision_id,
        provider_instance_token=f"provider-{decision_id}",
        provider_instance_generation=0,
        session_key=session_key,
        session_epoch=session_epoch,
        selection_generation=0,
        topology="multiple",
    )
    receipt = build_cache_affinity_receipt(
        physical_attempt_id=f"attempt-{decision_id}",
        role="proposer",
        topology="multiple",
        execution_slot="proposer:0:0",
        requested_identity="openrouter:anthropic/model",
        actual_identity="openrouter:anthropic/model",
        cache_domain_guard=CacheDomainGuard(b"p" * 32),
        cached_tokens=17,
        cache_write_tokens=0,
        observed_at_monotonic=time.monotonic(),
    )
    assert receipt is not None
    key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _RouterDynamicCacheAffinityReceiptBatch(
            turn_id=context.turn_id,
            decision_id=context.decision_id,
            provider_instance_token=context.provider_instance_token,
            provider_instance_generation=0,
            chat_call_id=f"chat-{decision_id}",
            chat_call_sequence=1,
            runtime_generation=0,
            topology="multiple",
            receipts=(receipt,),
        ),
    )
    turn = SimpleNamespace(
        session_key=session_key,
        metadata={"ensemble_decision_id": decision_id},
    )
    assert runner._commit_pending_router_dynamic_cache_affinity(turn, EngineDone())
    return context


async def test_direct_affinity_uses_raw_done_and_latest_chat_overwrites_receipt() -> None:
    config = _affinity_direct_config()
    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=23,
                cache_write_tokens=0,
            ),
            ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=0,
                cache_write_tokens=0,
            ),
        ]
    )
    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_generation_getter=lambda: 0,
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )

    first = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    second = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert [event.kind for event in first] == ["done"]
    assert [event.kind for event in second] == ["done"]
    assert [batch.chat_call_sequence for batch in batches] == [1, 2]
    assert len(batches[0].receipts) == 1
    receipt = batches[0].receipts[0]
    assert receipt.role == "single"
    assert receipt.execution_slot == "0"
    assert receipt.requested_identity == f"{config.provider}:{config.model}"
    assert receipt.actual_identity == receipt.requested_identity
    assert receipt.evidence_kind == "read_hit"
    assert receipt.cached_tokens == 23
    assert batches[1].receipts == ()
    assert "synthetic-affinity-secret" not in repr(batches)


async def test_direct_affinity_consumes_frozen_credential_token_without_reopening_secret() -> None:
    config = _affinity_direct_config("anthropic/frozen-credential-model")
    credential_token = _affinity_credential_token(config)

    class SecretForbiddenConfig:
        provider = config.provider
        model = config.model
        base_url = config.base_url
        org_id = config.org_id
        provider_routing = config.provider_routing

        @property
        def api_key(self) -> str:
            raise AssertionError("physical receipt collection reopened the credential secret")

    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        _AffinityDoneProvider(
            [
                ProviderDone(
                    provider=config.provider,
                    model=config.model,
                    cached_tokens=29,
                    cache_write_tokens=0,
                )
            ]
        ),
        SecretForbiddenConfig(),
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_generation_getter=lambda: 0,
        cache_affinity_credential_namespace_token=credential_token,
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert [event.kind for event in events] == ["done"]
    assert len(batches) == 1
    assert len(batches[0].receipts) == 1
    assert batches[0].receipts[0].cached_tokens == 29


async def test_direct_affinity_ttl_starts_at_done_and_survives_optional_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.runtime as runtime_module

    config = _affinity_direct_config("anthropic/terminal-clock-model")
    clock = [10.0]

    class TerminalThenAdvancingCloseStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self) -> TerminalThenAdvancingCloseStream:
            return self

        async def __anext__(self) -> Any:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=19,
                cache_write_tokens=0,
            )

        async def aclose(self) -> None:
            clock[0] = 99.0
            raise RuntimeError("synthetic optional close failure")

    class TerminalThenAdvancingCloseProvider:
        provider_name = "openrouter"
        _provider_routing_strict = True

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> TerminalThenAdvancingCloseStream:
            del messages, tools, config
            return TerminalThenAdvancingCloseStream()

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock[0])
    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        TerminalThenAdvancingCloseProvider(),
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_generation_getter=lambda: 0,
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert [event.kind for event in events] == ["done"]
    assert clock[0] == 99.0
    assert len(batches) == 1
    assert len(batches[0].receipts) == 1
    assert batches[0].receipts[0].observed_at_monotonic == 10.0


async def test_direct_affinity_rejects_unattested_identity_and_missing_terminal() -> None:
    config = _affinity_direct_config("anthropic/identity-model")
    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider="openrouter",
                model="anthropic/different-model",
                cached_tokens=11,
                cache_write_tokens=0,
            )
        ]
    )
    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_actual_model_aliases=(
            config.model,
            "anthropic/identity-model-20260801",
        ),
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )

    await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert len(batches) == 1
    assert batches[0].receipts == ()

    missing_batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    missing = _RouterSingleDirectProvider(
        _MissingTerminalProvider(),
        _affinity_direct_config("anthropic/missing-terminal-affinity"),
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=missing_batches.append,
    )
    await _collect(missing.chat([], config=ChatConfig(timeout=30.0)))
    assert missing_batches == []


async def test_direct_affinity_accepts_frozen_serving_alias_and_canonicalizes_receipt() -> None:
    config = _affinity_direct_config("anthropic/alias-model")
    serving_alias = "anthropic/alias-model-20260801"
    batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        _AffinityDoneProvider(
            [
                ProviderDone(
                    provider=config.provider,
                    model=serving_alias,
                    cached_tokens=13,
                    cache_write_tokens=0,
                )
            ]
        ),
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=batches.append,
        cache_affinity_actual_model_aliases=(config.model, serving_alias),
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert [event.kind for event in events] == ["done"]
    assert len(batches) == 1
    assert len(batches[0].receipts) == 1
    receipt = batches[0].receipts[0]
    assert receipt.requested_identity == f"{config.provider}:{config.model}"
    assert receipt.actual_identity == receipt.requested_identity


async def test_direct_affinity_sink_failure_never_fails_successful_turn() -> None:
    config = _affinity_direct_config("anthropic/sink-failure-model")
    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=0,
                cache_write_tokens=19,
            )
        ]
    )

    def broken_sink(batch: _RouterDynamicCacheAffinityReceiptBatch) -> None:
        del batch
        raise RuntimeError("synthetic evidence sink failure")

    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(),
        cache_affinity_receipt_sink=broken_sink,
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    assert [event.kind for event in events] == ["done"]


async def test_direct_affinity_generation_guard_blocks_stale_physical_dispatch() -> None:
    config = _affinity_direct_config("anthropic/stale-generation-model")
    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=1,
            )
        ]
    )
    generation = 1
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=_affinity_context(generation=0),
        cache_affinity_receipt_sink=lambda batch: None,
        cache_affinity_generation_getter=lambda: generation,
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert raw.calls == 0
    assert len(events) == 1
    assert events[0].code == "router_dynamic_cache_generation_changed"
    assert events[0].request_started is False
    assert events[0].physical_request_count == 0


async def test_single_affinity_sidecar_commits_only_latest_successful_batch() -> None:
    config = _affinity_direct_config("anthropic/sidecar-model")
    context = _affinity_context()
    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider=config.provider,
                model=config.model,
                cached_tokens=31,
                cache_write_tokens=0,
            )
        ]
    )
    captured: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=context,
        cache_affinity_receipt_sink=captured.append,
        cache_affinity_generation_getter=lambda: 0,
        cache_affinity_credential_namespace_token=(_affinity_credential_token(config)),
    )
    await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    assert len(captured) == 1

    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    assert runner._router_dynamic_cache_affinity is None
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=2,
        source={"enabled": True},
    )
    sidecar_key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        sidecar_key,
        captured[0],
    )
    turn = SimpleNamespace(
        session_key=context.session_key,
        metadata={"router_single_decision_id": context.decision_id},
    )
    assert runner._commit_pending_router_dynamic_cache_affinity(
        turn,
        EngineDone(),
    )
    available, receipts, generation = runner._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
        now=captured[0].receipts[0].observed_at_monotonic + 1.0,
    )
    assert available is True
    assert receipts == captured[0].receipts
    assert generation == 0

    next_context = _affinity_context(
        decision_id="decision-affinity-empty",
        provider_instance_token="provider-affinity-empty",
    )
    empty_key = runner._register_router_dynamic_cache_sidecar(
        context=next_context,
        policy=policy,
    )
    empty_batch = _RouterDynamicCacheAffinityReceiptBatch(
        turn_id=next_context.turn_id,
        decision_id=next_context.decision_id,
        provider_instance_token=next_context.provider_instance_token,
        provider_instance_generation=0,
        chat_call_id="chat-empty",
        chat_call_sequence=1,
        runtime_generation=0,
        topology="single",
        receipts=(),
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(empty_key, empty_batch)
    empty_turn = SimpleNamespace(
        session_key=next_context.session_key,
        metadata={"router_single_decision_id": next_context.decision_id},
    )
    assert not runner._commit_pending_router_dynamic_cache_affinity(
        empty_turn,
        EngineDone(),
    )
    assert not runner._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]


async def test_cache_affinity_state_is_not_shared_with_a_new_runner() -> None:
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=2,
        source={"strategy": "bonus"},
    )
    original = TurnRunner(provider_selector=None, config=_router_single_config())
    context = await _seed_single_affinity_state(
        original,
        policy,
        session_key="agent:main:process-restart",
        decision_id="process-restart-seed",
    )
    assert original._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]

    replacement = TurnRunner(provider_selector=None, config=_router_single_config())

    assert replacement._router_dynamic_cache_affinity is None
    assert not replacement._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]


async def test_single_affinity_failed_turn_and_missing_batch_clear_previous() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    context = await _seed_single_affinity_state(
        runner,
        policy,
        session_key="agent:main:preserve",
        decision_id="preserve-seed",
    )

    missing_context = _affinity_context(
        decision_id="preserve-missing",
        provider_instance_token="provider-preserve-missing",
        session_key=context.session_key,
    )
    runner._register_router_dynamic_cache_sidecar(
        context=missing_context,
        policy=policy,
    )
    missing_turn = SimpleNamespace(
        session_key=context.session_key,
        metadata={"router_single_decision_id": missing_context.decision_id},
    )
    assert not runner._commit_pending_router_dynamic_cache_affinity(
        missing_turn,
        EngineDone(),
    )
    assert not runner._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]

    await _seed_single_affinity_state(
        runner,
        policy,
        session_key=context.session_key,
        decision_id="clear-reseed",
    )
    failed_context = _affinity_context(
        decision_id="clear-failed",
        provider_instance_token="provider-clear-failed",
        session_key=context.session_key,
    )
    runner._register_router_dynamic_cache_sidecar(
        context=failed_context,
        policy=policy,
    )
    failed_turn = SimpleNamespace(
        session_key=context.session_key,
        metadata={"router_single_decision_id": failed_context.decision_id},
    )
    runner._discard_pending_router_dynamic_cache_affinity(
        failed_turn,
        session_key=context.session_key,
    )
    assert not runner._router_single_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]


@pytest.mark.parametrize("topology", ["single", "multiple"])
@pytest.mark.parametrize("session_epoch_available", [True, False])
async def test_affinity_analyzer_failure_before_materialization_clears_previous(
    monkeypatch: pytest.MonkeyPatch,
    topology: str,
    session_epoch_available: bool,
) -> None:
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.engine.pipeline import TurnContext

    selected = _affinity_direct_config(f"anthropic/pre-materialize-{topology}")
    session_key = f"agent:main:pre-materialize-{topology}"
    session_epoch = 7
    policy_override = {
        "session": {
            "kv_cache_affinity": {
                "strategy": "bonus",
                "topologies": [topology],
                "ttl_seconds": 300,
                "age_decay": "linear",
                "bonus_by_evidence": {
                    "read_hit": 0.05,
                    "write_only": 0.025,
                },
            }
        }
    }
    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm={
            "provider": selected.provider,
            "model": selected.model,
            "api_key": selected.api_key,
            "base_url": selected.base_url,
            "max_tokens": 4_096,
            "context_window_tokens": 128_000,
        },
        llm_ensemble={
            "enabled": True,
            "mode": topology,
            "selection_mode": "router_dynamic",
            "ranking_config_override": policy_override,
            "ranking_thinking_assignment_enabled": False,
            "latency_class": "experiment",
        },
    )
    runner = TurnRunner(
        provider_selector=_Selector(selected),
        config=config,
        model_catalog=_Catalog(),
        session_manager=object(),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.session_services.get_session_epoch",
        lambda manager, key: session_epoch if session_epoch_available else None,
    )
    monkeypatch.setattr(
        runner,
        "_router_dynamic_task_analyzer_provider",
        lambda *args, **kwargs: None,
    )

    async def fail_analyzer(**kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError("synthetic analyzer failure before provider materialization")

    monkeypatch.setattr(ranking_module, "analyze_task_with_provider", fail_analyzer)
    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_fallback_chain",
        fail_analyzer,
    )
    policy = _router_dynamic_cache_affinity_policy(
        config.llm_ensemble.prepared_ranking_config(),
        topology=topology,
    )
    if topology == "single":
        await _seed_single_affinity_state(
            runner,
            policy,
            session_key=session_key,
            decision_id="pre-materialize-seed-single",
            session_epoch=session_epoch,
        )
        turn = TurnContext(
            message="continue",
            session_key=session_key,
            config=config,
            provider=_NoChatProvider(),
            model=selected.model,
            tool_defs=[],
            system_prompt="system",
            attachments=[],
            metadata={"routed_tier": "c2", "routing_confidence": 0.9},
        )
        with pytest.raises(RuntimeError, match="synthetic analyzer failure"):
            await runner._resolve_router_single_provider(
                turn=turn,
                provider=turn.provider,
                cloned_selector=_Selector(selected),
                turn_config=config,
                ensemble_cfg=config.llm_ensemble,
                turn_absolute_deadline=None,
            )
        assert bool(turn.metadata.get("router_single_decision_id")) is (session_epoch_available)
        cleanup_turn: object | None = turn
    else:
        await _seed_multiple_affinity_state(
            runner,
            policy,
            session_key=session_key,
            decision_id="pre-materialize-seed-multiple",
            session_epoch=session_epoch,
        )
        turn, _ = await runner._run_pipeline(
            "continue",
            session_key,
            _NoChatProvider(),
            _Selector(selected),
            [],
            "system",
            [],
        )
        assert turn.metadata["ensemble_wrap_skipped_reason"] == (
            "router_dynamic_ranking_unavailable"
        )
        cleanup_turn = turn

    sidecars = runner._router_dynamic_cache_affinity_sidecars
    assert sidecars is not None
    if session_epoch_available:
        assert any(key[0] == session_key for key in sidecars)
        runner._discard_pending_router_dynamic_cache_affinity(
            cleanup_turn,
            session_key=session_key,
        )
    else:
        assert not any(key[0] == session_key for key in sidecars)
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=session_epoch,
        policy=policy,
    )[0]


@pytest.mark.parametrize("topology", ["single", "multiple"])
async def test_successful_selector_fallback_clears_old_and_earlier_chat_affinity(
    topology: str,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology=topology,
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    session_key = f"agent:main:fallback-clears-{topology}"
    session_epoch = 23
    role = "single" if topology == "single" else "proposer"
    seed_context = _RouterDynamicCacheAffinityCollectionContext(
        turn_id=f"turn-seed-{topology}",
        decision_id=f"decision-seed-{topology}",
        provider_instance_token=f"provider-seed-{topology}",
        provider_instance_generation=0,
        session_key=session_key,
        session_epoch=session_epoch,
        selection_generation=0,
        topology=topology,
    )
    seed_receipt = build_cache_affinity_receipt(
        physical_attempt_id=f"attempt-seed-{topology}",
        role=role,
        topology=topology,
        execution_slot="0" if topology == "single" else "0:0",
        requested_identity="openrouter:seed/model",
        actual_identity="openrouter:seed/model",
        cache_domain_guard=CacheDomainGuard(b"f" * 32),
        cached_tokens=17,
        cache_write_tokens=0,
        observed_at_monotonic=time.monotonic(),
    )
    assert seed_receipt is not None
    seed_key = runner._register_router_dynamic_cache_sidecar(
        context=seed_context,
        policy=policy,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        seed_key,
        _RouterDynamicCacheAffinityReceiptBatch(
            turn_id=seed_context.turn_id,
            decision_id=seed_context.decision_id,
            provider_instance_token=seed_context.provider_instance_token,
            provider_instance_generation=0,
            chat_call_id=f"chat-seed-{topology}",
            chat_call_sequence=1,
            runtime_generation=0,
            topology=topology,
            receipts=(seed_receipt,),
        ),
    )
    seed_metadata_key = (
        "router_single_decision_id" if topology == "single" else "ensemble_decision_id"
    )
    assert runner._commit_pending_router_dynamic_cache_affinity(
        SimpleNamespace(
            session_key=session_key,
            metadata={seed_metadata_key: seed_context.decision_id},
        ),
        EngineDone(),
    )
    assert runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=session_epoch,
        policy=policy,
    )[0]

    # A cancelled/failed turn is authoritative negative evidence for the
    # selected topology even though it never reaches Engine Done.
    cancelled_context = replace(
        seed_context,
        turn_id=f"turn-cancelled-{topology}",
        decision_id=f"decision-cancelled-{topology}",
        provider_instance_token=f"provider-cancelled-{topology}",
    )
    runner._register_router_dynamic_cache_sidecar(
        context=cancelled_context,
        policy=policy,
    )
    runner._discard_pending_router_dynamic_cache_affinity(
        SimpleNamespace(
            session_key=session_key,
            metadata={
                seed_metadata_key: cancelled_context.decision_id,
                "router_fallback_hops": 1,
            },
        ),
        session_key=session_key,
    )
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=session_epoch,
        policy=policy,
    )[0]

    fallback_context = replace(
        seed_context,
        turn_id=f"turn-fallback-{topology}",
        decision_id=f"decision-fallback-{topology}",
        provider_instance_token=f"provider-fallback-{topology}",
    )
    fallback_sidecar_key = runner._register_router_dynamic_cache_sidecar(
        context=fallback_context,
        policy=policy,
    )
    first_chat_receipt = build_cache_affinity_receipt(
        physical_attempt_id=f"attempt-first-chat-{topology}",
        role=role,
        topology=topology,
        execution_slot="0" if topology == "single" else "0:0",
        requested_identity="openrouter:first-chat/model",
        actual_identity="openrouter:first-chat/model",
        cache_domain_guard=CacheDomainGuard(b"g" * 32),
        cached_tokens=29,
        cache_write_tokens=0,
        observed_at_monotonic=time.monotonic(),
    )
    assert first_chat_receipt is not None
    assert runner._stage_router_dynamic_cache_affinity_batch(
        fallback_sidecar_key,
        _RouterDynamicCacheAffinityReceiptBatch(
            turn_id=fallback_context.turn_id,
            decision_id=fallback_context.decision_id,
            provider_instance_token=fallback_context.provider_instance_token,
            provider_instance_generation=0,
            chat_call_id=f"chat-first-{topology}",
            chat_call_sequence=1,
            runtime_generation=0,
            topology=topology,
            receipts=(first_chat_receipt,),
        ),
    )

    class SuccessfulFallbackProvider:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text="fallback success")
                yield ProviderDone(
                    provider="openrouter",
                    model="fallback/model",
                )

            return stream()

    class SuccessfulFallbackSelector:
        def __init__(self, fallback: SuccessfulFallbackProvider) -> None:
            self._fallback = fallback
            self.current_config = ProviderConfig(
                provider="openrouter",
                model="seed/model",
                api_key="synthetic",
            )

        @property
        def active_provider_id(self) -> str:
            return self.current_config.provider

        def next_fallback_after_failure(
            self,
            error: Exception,
        ) -> SuccessfulFallbackProvider:
            del error
            self.current_config = replace(
                self.current_config,
                model="fallback/model",
            )
            return self._fallback

    metadata = {seed_metadata_key: fallback_context.decision_id}
    fallback = SuccessfulFallbackProvider()
    wrapped = _SelectorFallbackProvider(
        _ErrorProvider("429"),
        SuccessfulFallbackSelector(fallback),
        turn_metadata=metadata,
    )

    events = await _collect(wrapped.chat([], config=ChatConfig()))

    assert fallback.calls == 1
    assert any(getattr(event, "kind", "") == "done" for event in events)
    assert metadata["router_fallback_hops"] == 1
    assert not runner._commit_pending_router_dynamic_cache_affinity(
        SimpleNamespace(session_key=session_key, metadata=metadata),
        EngineDone(),
    )
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=session_epoch,
        policy=policy,
    )[0]


async def test_single_affinity_read_enforces_current_effective_lru_capacity() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    wide_policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=3,
        source={"strategy": "bonus"},
    )
    for index in range(3):
        await _seed_single_affinity_state(
            runner,
            wide_policy,
            session_key=f"agent:main:lru-{index}",
            decision_id=f"lru-{index}",
        )

    narrow_policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=1,
        source={"strategy": "bonus"},
    )
    assert runner._router_single_cache_continuity_snapshot(
        session_key="agent:main:lru-2",
        session_epoch=7,
        policy=narrow_policy,
    )[0]
    assert runner._router_dynamic_cache_affinity is not None
    assert len(runner._router_dynamic_cache_affinity) == 1


async def test_affinity_control_state_is_bounded_and_keeps_live_dispatch_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    runner._session_manager = object()
    epochs: dict[str, int] = {}
    monkeypatch.setattr(
        "opensquilla.gateway.session_services.get_session_epoch",
        lambda manager, key: epochs.get(key),
    )
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=2,
        source={"strategy": "bonus"},
    )
    runner._ensure_router_dynamic_cache_affinity_state()

    for index in range(12):
        session_key = f"agent:main:control-{index}"
        epochs[session_key] = index
        assert await runner._resolve_router_dynamic_session_epoch(session_key) == index
        runner._router_dynamic_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=index,
            policy=policy,
        )

    assert runner._router_dynamic_cache_affinity_generation is not None
    assert runner._router_dynamic_cache_affinity_epoch_by_key is not None
    assert len(runner._router_dynamic_cache_affinity_generation) <= 2
    assert len(runner._router_dynamic_cache_affinity_epoch_by_key) <= 2

    active_session = "agent:main:control-active"
    epochs[active_session] = 23
    assert await runner._resolve_router_dynamic_session_epoch(active_session) == 23
    _, _, selected_generation = runner._router_dynamic_cache_continuity_snapshot(
        session_key=active_session,
        session_epoch=23,
        policy=policy,
    )
    context = _affinity_context(
        decision_id="control-active",
        provider_instance_token="provider-control-active",
        generation=selected_generation,
        session_key=active_session,
        session_epoch=23,
    )
    runner._register_router_dynamic_cache_sidecar(context=context, policy=policy)
    runner._invalidate_router_dynamic_cache_affinity(
        session_key=active_session,
        reason="test_live_fence",
    )
    invalidated_generation = runner._router_dynamic_cache_generation(active_session)
    assert invalidated_generation != selected_generation

    for index in range(20):
        runner._invalidate_router_dynamic_cache_affinity(
            session_key=f"agent:main:invalidated-{index}",
            reason="test_control_capacity",
        )

    assert runner._router_dynamic_cache_generation(active_session) == invalidated_generation
    assert active_session in runner._router_dynamic_cache_affinity_generation
    assert len(runner._router_dynamic_cache_affinity_generation) <= 2
    assert len(runner._router_dynamic_cache_affinity_epoch_by_key) <= 2

    raw = _AffinityDoneProvider(
        [
            ProviderDone(
                provider="openrouter",
                model="anthropic/control-active",
                cached_tokens=1,
            )
        ]
    )
    direct = _RouterSingleDirectProvider(
        raw,
        _affinity_direct_config("anthropic/control-active"),
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=context,
        cache_affinity_receipt_sink=lambda batch: None,
        cache_affinity_generation_getter=(
            lambda: runner._router_dynamic_cache_generation(active_session)
        ),
    )
    events = await _collect(direct.chat([], config=ChatConfig(timeout=30.0)))
    assert raw.calls == 0
    assert len(events) == 1
    assert events[0].code == "router_dynamic_cache_generation_changed"

    runner._discard_pending_router_dynamic_cache_affinity(
        SimpleNamespace(
            session_key=active_session,
            metadata={"router_single_decision_id": context.decision_id},
        ),
        session_key=active_session,
    )
    for index in range(12, 18):
        session_key = f"agent:main:control-{index}"
        epochs[session_key] = index
        assert await runner._resolve_router_dynamic_session_epoch(session_key) == index
        runner._router_dynamic_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=index,
            policy=policy,
        )

    assert active_session not in runner._router_dynamic_cache_affinity_generation
    assert active_session not in runner._router_dynamic_cache_affinity_epoch_by_key
    assert len(runner._router_dynamic_cache_affinity_generation) <= 2
    assert len(runner._router_dynamic_cache_affinity_epoch_by_key) <= 2


def test_affinity_compaction_listener_is_singleton_and_removed_after_hot_rebuild() -> None:
    import opensquilla.engine.cache_break_monitor as monitor_module

    gc.collect()
    baseline = len(monitor_module._compaction_listeners)
    for index in range(4):
        runner = TurnRunner(provider_selector=None, config=_router_single_config())
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=2,
        )
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=2,
        )
        assert len(monitor_module._compaction_listeners) == baseline + 1
        runner_ref = weakref.ref(runner)
        del runner
        gc.collect()
        assert runner_ref() is None, index
        assert len(monitor_module._compaction_listeners) == baseline


def test_affinity_session_delete_listener_is_singleton_and_removed_after_hot_rebuild() -> None:
    class DeleteAwareStorage:
        def __init__(self) -> None:
            self.listeners: list[Any] = []

        def add_session_delete_listener(self, listener: Any) -> Any:
            self.listeners.append(listener)

            def remove() -> None:
                if listener in self.listeners:
                    self.listeners.remove(listener)

            return remove

    storage = DeleteAwareStorage()
    manager = SimpleNamespace(storage=storage)
    for index in range(4):
        runner = TurnRunner(
            provider_selector=None,
            config=_router_single_config(),
            session_manager=manager,
        )
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=2,
        )
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=2,
        )
        assert len(storage.listeners) == 1
        runner_ref = weakref.ref(runner)
        del runner
        gc.collect()
        assert runner_ref() is None, index
        assert storage.listeners == []


async def test_single_affinity_epoch_is_fail_closed_and_independent() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())

    assert await runner._resolve_router_dynamic_session_epoch("agent:main:none") is None
    assert runner._router_dynamic_cache_affinity_epoch_by_key is None
    assert runner._usage_session_epoch_by_key == {}

    runner._ensure_router_dynamic_cache_affinity_state()
    assert runner._router_dynamic_cache_affinity_epoch_by_key == {}


async def test_session_delete_purges_sidecar_epoch_and_same_key_recreate_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    session_key = "agent:main:delete-recreate"
    await _seed_single_affinity_state(
        runner,
        policy,
        session_key=session_key,
        decision_id="delete-recreate-seed",
        session_epoch=0,
    )
    assert runner._router_dynamic_cache_affinity_epoch_by_key is not None
    runner._router_dynamic_cache_affinity_epoch_by_key[session_key] = 0
    generation_before_delete = runner._router_dynamic_cache_generation(session_key)
    pending_context = _affinity_context(
        decision_id="delete-recreate-pending",
        provider_instance_token="provider-delete-recreate-pending",
        generation=generation_before_delete,
        session_key=session_key,
        session_epoch=0,
    )
    pending_key = runner._register_router_dynamic_cache_sidecar(
        context=pending_context,
        policy=policy,
    )

    runner._invalidate_router_dynamic_cache_affinity(
        session_key=session_key,
        reason="session_deleted",
    )

    assert runner._router_dynamic_cache_generation(session_key) > generation_before_delete
    assert runner._router_dynamic_cache_affinity_sidecars is not None
    assert pending_key not in runner._router_dynamic_cache_affinity_sidecars
    assert session_key not in runner._router_dynamic_cache_affinity_epoch_by_key
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=0,
        policy=policy,
    )[0]

    runner._session_manager = object()
    monkeypatch.setattr(
        "opensquilla.gateway.session_services.get_session_epoch",
        lambda manager, key: 0,
    )
    assert await runner._resolve_router_dynamic_session_epoch(session_key) == 0
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=0,
        policy=policy,
    )[0]


async def test_durable_storage_delete_notifies_runtime_affinity_owner() -> None:
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage()
    await storage.connect()
    manager = SessionManager(storage)
    runner = TurnRunner(
        provider_selector=None,
        config=_router_single_config(),
        session_manager=manager,
    )
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    session_key = "agent:main:durable-delete-recreate"
    try:
        await manager.create(session_key)
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=policy.route_cache_max_entries,
        )
        seeded = await _seed_single_affinity_state(
            runner,
            policy,
            session_key=session_key,
            decision_id="durable-delete-seed",
            session_epoch=0,
        )
        manager.set_cached_epoch(session_key, 0)
        assert runner._router_dynamic_cache_affinity_epoch_by_key is not None
        runner._router_dynamic_cache_affinity_epoch_by_key[session_key] = 0
        generation_before_delete = runner._router_dynamic_cache_generation(session_key)
        pending_context = _affinity_context(
            decision_id="durable-delete-pending",
            provider_instance_token="provider-durable-delete-pending",
            generation=generation_before_delete,
            session_key=session_key,
            session_epoch=0,
        )
        pending_key = runner._register_router_dynamic_cache_sidecar(
            context=pending_context,
            policy=policy,
        )

        await storage.delete_session(session_key)

        assert manager.get_cached_epoch(session_key) is None
        assert runner._router_dynamic_cache_generation(session_key) > generation_before_delete
        assert runner._router_dynamic_cache_affinity_sidecars is not None
        assert pending_key not in runner._router_dynamic_cache_affinity_sidecars
        assert session_key not in runner._router_dynamic_cache_affinity_epoch_by_key
        assert not runner._router_single_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=seeded.session_epoch,
            policy=policy,
        )[0]

        recreated = await manager.create(session_key)
        assert recreated.epoch == 0
        assert await runner._resolve_router_dynamic_session_epoch(session_key) == 0
        assert not runner._router_single_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=0,
            policy=policy,
        )[0]
    finally:
        await storage.close()


@pytest.mark.parametrize("delete_path", ["prune", "cap"])
async def test_manager_maintenance_delete_invalidates_runtime_affinity(
    delete_path: str,
) -> None:
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage()
    await storage.connect()
    manager = SessionManager(storage)
    runner = TurnRunner(
        provider_selector=None,
        config=_router_single_config(),
        session_manager=manager,
    )
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="single",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    session_key = f"agent:main:maintenance-{delete_path}"
    try:
        node = await manager.create(session_key)
        await storage.upsert_session(node.model_copy(update={"updated_at": 1}))
        if delete_path == "cap":
            await manager.create(f"agent:main:maintenance-{delete_path}-keeper")
        runner._ensure_router_dynamic_cache_compaction_listener(
            route_cache_max_entries=policy.route_cache_max_entries,
        )
        seeded = await _seed_single_affinity_state(
            runner,
            policy,
            session_key=session_key,
            decision_id=f"maintenance-{delete_path}-seed",
            session_epoch=0,
        )
        manager.set_cached_epoch(session_key, 0)
        assert runner._router_dynamic_cache_affinity_epoch_by_key is not None
        runner._router_dynamic_cache_affinity_epoch_by_key[session_key] = 0
        generation_before_delete = runner._router_dynamic_cache_generation(session_key)

        if delete_path == "prune":
            assert await manager.prune_stale(max_age_ms=1) == 1
        else:
            assert await manager.cap_entries(max_entries=1) == 1

        assert await storage.get_session(session_key) is None
        assert manager.get_cached_epoch(session_key) is None
        assert runner._router_dynamic_cache_generation(session_key) > generation_before_delete
        assert not runner._router_single_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=seeded.session_epoch,
            policy=policy,
        )[0]

        recreated = await manager.create(session_key)
        assert recreated.epoch == 0
        assert await runner._resolve_router_dynamic_session_epoch(session_key) == 0
        assert not runner._router_single_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=0,
            policy=policy,
        )[0]
    finally:
        await storage.close()


def test_runtime_affinity_outer_thinking_projection_matches_agent_chat_config() -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(
            llm={"thinking": None},
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    enabled_turn = SimpleNamespace(
        semantic_message="reason carefully",
        metadata={"thinking_requested": True, "thinking_level": "low"},
    )
    disabled_turn = SimpleNamespace(
        semantic_message="answer",
        metadata={},
    )

    assert runner._router_dynamic_outer_thinking_projection(enabled_turn) == {
        "thinking_enabled": True,
        "effective_thinking_level": "low",
        "thinking_budget_tokens": AgentConfig(thinking=ThinkingLevel.LOW).resolve_thinking(
            prompt="reason carefully"
        )[1],
    }
    assert runner._router_dynamic_outer_thinking_projection(disabled_turn) == {
        "thinking_enabled": False,
        "effective_thinking_level": "off",
        "thinking_budget_tokens": 0,
    }


def test_runtime_affinity_policy_uses_authoritative_validator_and_absent_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ranking_router as ranking_router

    baseline = ranking_config_snapshot()

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("absent affinity must not call the policy helper")

    monkeypatch.setattr(
        ranking_router,
        "router_dynamic_cache_affinity_policy",
        fail_if_called,
    )
    assert (
        _router_dynamic_cache_affinity_policy(
            baseline,
            topology="single",
        )
        is None
    )

    calls: list[str] = []
    enabled = ranking_config_snapshot(
        override={
            "session": {
                "kv_cache_affinity": {
                    "strategy": "bonus",
                    "topologies": ["single"],
                    "ttl_seconds": 45,
                    "age_decay": "linear",
                    "bonus_by_evidence": {
                        "read_hit": 0.01,
                        "write_only": 0.005,
                    },
                }
            }
        }
    )

    def authoritative(
        config: Any,
        *,
        topology: str,
    ) -> dict[str, Any]:
        assert config is enabled
        calls.append(topology)
        return dict(enabled["session"]["kv_cache_affinity"])

    monkeypatch.setattr(
        ranking_router,
        "router_dynamic_cache_affinity_policy",
        authoritative,
    )
    policy = _router_dynamic_cache_affinity_policy(enabled, topology="single")

    assert calls == ["single"]
    assert policy is not None
    assert policy.ttl_seconds == 45.0
    assert policy.route_cache_max_entries == enabled["session"]["route_cache_max_entries"]


def _multiple_affinity_receipt(
    attempt_id: str,
    *,
    cached_tokens: int = 1,
) -> Any:
    receipt = build_cache_affinity_receipt(
        physical_attempt_id=attempt_id,
        role="proposer",
        topology="multiple",
        execution_slot="proposer:0:0",
        requested_identity="openrouter:anthropic/model",
        actual_identity="openrouter:anthropic/model",
        cache_domain_guard=CacheDomainGuard(b"m" * 32),
        cached_tokens=cached_tokens,
        cache_write_tokens=0,
        observed_at_monotonic=10.0 + cached_tokens,
    )
    assert receipt is not None
    return receipt


def _multiple_affinity_context(
    *,
    decision_id: str,
    provider_token: str,
    generation: int = 0,
) -> _RouterDynamicCacheAffinityCollectionContext:
    return _RouterDynamicCacheAffinityCollectionContext(
        turn_id="turn-multiple",
        decision_id=decision_id,
        provider_instance_token=provider_token,
        provider_instance_generation=0,
        session_key="agent:main:multiple-affinity",
        session_epoch=3,
        selection_generation=generation,
        topology="multiple",
    )


def _multiple_affinity_batch(
    context: _RouterDynamicCacheAffinityCollectionContext,
    *,
    provider_token: str,
    provider_generation: int,
    chat_sequence: int,
    receipts: tuple[Any, ...],
) -> _RouterDynamicCacheAffinityReceiptBatch:
    return _RouterDynamicCacheAffinityReceiptBatch(
        turn_id=context.turn_id,
        decision_id=context.decision_id,
        provider_instance_token=provider_token,
        provider_instance_generation=provider_generation,
        chat_call_id=f"chat-{provider_generation}-{chat_sequence}",
        chat_call_sequence=chat_sequence,
        runtime_generation=context.selection_generation,
        topology="multiple",
        receipts=receipts,
    )


def test_multiple_affinity_retry_generation_dominates_old_provider_batches() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="multiple",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    context = _multiple_affinity_context(
        decision_id="multiple-retry",
        provider_token="provider-generation-0",
    )
    key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    first_receipt = _multiple_affinity_receipt("attempt-first")
    final_receipt = _multiple_affinity_receipt(
        "attempt-final",
        cached_tokens=7,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _multiple_affinity_batch(
            context,
            provider_token="provider-generation-0",
            provider_generation=0,
            chat_sequence=0,
            receipts=(first_receipt,),
        ),
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _multiple_affinity_batch(
            context,
            provider_token="provider-generation-1",
            provider_generation=1,
            chat_sequence=1,
            receipts=(final_receipt,),
        ),
    )
    # A late callback from the retired provider cannot win even with a newer
    # apparent chat sequence, and the active generation's token is immutable.
    assert not runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _multiple_affinity_batch(
            context,
            provider_token="provider-generation-0",
            provider_generation=0,
            chat_sequence=2,
            receipts=(first_receipt,),
        ),
    )
    assert not runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _multiple_affinity_batch(
            context,
            provider_token="wrong-token-generation-1",
            provider_generation=1,
            chat_sequence=2,
            receipts=(first_receipt,),
        ),
    )
    turn = SimpleNamespace(
        session_key=context.session_key,
        metadata={"ensemble_decision_id": context.decision_id},
    )
    assert runner._commit_pending_router_dynamic_cache_affinity(
        turn,
        EngineDone(),
    )
    available, receipts, _ = runner._router_dynamic_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
        now=18.0,
    )
    assert available is True
    assert receipts == (final_receipt,)


def test_multiple_final_malformed_receipt_becomes_empty_and_clears_old_hit() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    policy = _RouterDynamicCacheAffinityPolicy(
        topology="multiple",
        ttl_seconds=60.0,
        route_cache_max_entries=4,
        source={"strategy": "bonus"},
    )
    context = _multiple_affinity_context(
        decision_id="multiple-malformed-final",
        provider_token="provider-generation-0",
    )
    key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    assert runner._stage_router_dynamic_cache_affinity_batch(
        key,
        _multiple_affinity_batch(
            context,
            provider_token="provider-generation-0",
            provider_generation=0,
            chat_sequence=0,
            receipts=(_multiple_affinity_receipt("attempt-old"),),
        ),
    )
    malformed = SimpleNamespace(
        physical_attempt_id="attempt-malformed",
        role="proposer",
        execution_slot="proposer:0:0",
        requested_identity="openrouter:anthropic/model",
        actual_identity="openrouter:anthropic/model",
        cache_domain_guard=None,
        cached_tokens=9,
        cache_write_tokens=0,
        observed_at_monotonic=20.0,
    )
    raw_final_batch = SimpleNamespace(
        turn_id=context.turn_id,
        decision_id=context.decision_id,
        provider_instance_token="provider-generation-1",
        provider_instance_generation=1,
        chat_sequence=1,
        chat_call_id="chat-final-malformed",
        topology="multiple",
        receipts=(malformed,),
    )
    normalized = runner._normalize_multiple_cache_affinity_batch(
        context=context,
        batch=raw_final_batch,
    )
    assert normalized is not None
    assert normalized.receipts == ()
    assert runner._stage_router_dynamic_cache_affinity_batch(key, normalized)
    turn = SimpleNamespace(
        session_key=context.session_key,
        metadata={"ensemble_decision_id": context.decision_id},
    )
    assert not runner._commit_pending_router_dynamic_cache_affinity(
        turn,
        EngineDone(),
    )
    assert not runner._router_dynamic_cache_continuity_snapshot(
        session_key=context.session_key,
        session_epoch=context.session_epoch,
        policy=policy,
    )[0]


def test_multiple_frozen_serving_alias_is_canonicalized_and_unknown_alias_is_empty() -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    context = _multiple_affinity_context(
        decision_id="multiple-serving-alias",
        provider_token="provider-alias",
    )
    requested_model = "anthropic/alias-model"
    serving_alias = "anthropic/alias-model-20260801"

    def raw_batch(actual_model: str, *, chat_sequence: int) -> Any:
        receipt = SimpleNamespace(
            physical_attempt_id=f"attempt-alias-{chat_sequence}",
            role="proposer",
            execution_slot="proposer:0:0",
            requested_provider="openrouter",
            requested_model=requested_model,
            actual_provider="openrouter",
            actual_model=actual_model,
            actual_model_aliases=(requested_model, serving_alias),
            cache_domain_guard=CacheDomainGuard(b"a" * 32),
            cached_tokens=9,
            cache_write_tokens=0,
            observed_at_monotonic=20.0,
        )
        return SimpleNamespace(
            turn_id=context.turn_id,
            decision_id=context.decision_id,
            provider_instance_token="provider-alias",
            provider_instance_generation=0,
            chat_sequence=chat_sequence,
            chat_call_id=f"chat-alias-{chat_sequence}",
            topology="multiple",
            receipts=(receipt,),
        )

    accepted = runner._normalize_multiple_cache_affinity_batch(
        context=context,
        batch=raw_batch(serving_alias, chat_sequence=0),
    )
    rejected = runner._normalize_multiple_cache_affinity_batch(
        context=context,
        batch=raw_batch("anthropic/unknown-serving-model", chat_sequence=1),
    )

    assert accepted is not None
    assert len(accepted.receipts) == 1
    assert accepted.receipts[0].requested_identity == (f"openrouter:{requested_model}")
    assert accepted.receipts[0].actual_identity == accepted.receipts[0].requested_identity
    assert rejected is not None
    assert rejected.receipts == ()


@pytest.mark.parametrize("feature_enabled", [False, True])
async def test_single_physical_receipt_commit_changes_next_real_ranking_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    feature_enabled: bool,
) -> None:
    import opensquilla.engine.runtime as runtime_module
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.provider.cache_affinity import (
        build_credential_namespace_token,
    )

    cached_model = "openai/gpt-5.5"
    baseline_model = "anthropic/claude-sonnet-4.5"
    session_key = f"agent:main:affinity-e2e-{feature_enabled}"
    session_epoch = 41
    decision_id = "affinity-e2e-seed"
    policy_override = {
        "session": {
            "kv_cache_affinity": {
                "strategy": "bonus",
                "topologies": ["single"],
                "ttl_seconds": 300,
                "age_decay": "linear",
                "bonus_by_evidence": {
                    "read_hit": 0.05,
                    "write_only": 0.025,
                },
            }
        }
    }
    ranking_config = ranking_config_snapshot(override=policy_override if feature_enabled else None)
    policy = (
        _router_dynamic_cache_affinity_policy(
            ranking_config,
            topology="single",
        )
        if feature_enabled
        else None
    )
    cached_config = _affinity_direct_config(cached_model)
    baseline_config = replace(
        cached_config,
        model=baseline_model,
        provider_routing={
            cached_model: "anthropic",
            baseline_model: "anthropic",
        },
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "true")

    def registry_model(model_id: str, capability: float) -> dict[str, Any]:
        return {
            "source": "affinity_e2e_registry",
            "runtime": {"thinking": "off"},
            "registry_facts": {
                "model_id": model_id,
                "version": f"{model_id}-20260818",
                "provider": "openrouter",
                "vendor": "synthetic",
                "family": model_id,
                "is_open_source": False,
                "is_chinese_model": False,
                "status": "enabled",
                "roles": ["proposer"],
                "context_window": 200_000,
                "effective_context_bucket": "extra_long",
                "modalities": ["text"],
                "tools": [],
                "price": {
                    "input_per_million": 1.0,
                    "output_per_million": 1.0,
                },
                "latency_p50_ms": 1_000,
                "latency_p95_ms": 2_000,
                "quota": "available",
                "rate_limit": "available",
                "health": "healthy",
                "credential_available": True,
            },
            "static_profile": {
                "capability_dist_prior": {
                    "reasoning": capability,
                    "code_generation": capability,
                    "format_following": capability,
                },
                "domain_dist_prior": {"software_engineering": capability},
                "tier_dist_prior": {
                    "1": capability,
                    "2": capability,
                    "3": capability,
                    "4": capability,
                },
                "role_fit_prior": {
                    "proposer": capability,
                    "aggregator": capability,
                },
            },
            "online_profile": {
                "error_rates": {
                    "hallucination": max(0.0, 1.0 - capability),
                    "omission": max(0.0, 0.9 - capability),
                }
            },
        }

    monkeypatch.setattr(
        ranking_module,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: snapshot,
    )
    monkeypatch.setattr(
        ranking_module,
        "build_model_registry_snapshot",
        lambda **kwargs: {
            "schema_version": "affinity-e2e",
            "snapshot_version": "affinity-e2e-v1",
            "models": [
                registry_model(baseline_model, 0.9),
                registry_model(cached_model, 0.899),
            ],
        },
    )
    monkeypatch.setattr(ensemble_module, "_member_from_ref", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        ensemble_module,
        "_member_model_capabilities",
        lambda member: SimpleNamespace(supports_vision=False),
    )

    def resolution_for(model_id: str) -> ProviderDeploymentResolution:
        config = cached_config if model_id == cached_model else baseline_config
        return ProviderDeploymentResolution(
            provider=config.provider,
            model=model_id,
            ready=True,
            provider_config=config,
        )

    def resolve_member(ref: Any, inherited: Any, **kwargs: Any) -> Any:
        del inherited, kwargs
        return resolution_for(str(ref.model))

    def resolve_deployment(
        config: Any,
        provider_id: str,
        model: str,
        **kwargs: Any,
    ) -> ProviderDeploymentResolution:
        del config, provider_id, kwargs
        return resolution_for(model)

    def resolve_cache_identity(
        config: Any,
        provider_id: str,
        model: str,
        **kwargs: Any,
    ) -> tuple[ProviderDeploymentResolution, object | None]:
        del config, provider_id, kwargs
        resolution = resolution_for(model)
        provider_config = resolution.provider_config
        assert provider_config is not None
        return (
            resolution,
            build_credential_namespace_token(
                provider=provider_config.provider,
                resolved_secret=provider_config.api_key,
                org_id=provider_config.org_id,
            ),
        )

    monkeypatch.setattr(ensemble_module, "_resolve_member_deployment", resolve_member)
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        resolve_deployment,
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment_cache_identity",
        resolve_cache_identity,
    )

    if not feature_enabled:

        def unexpected_runtime_affinity(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("feature-absent physical turn touched cache affinity")

        def unexpected_ranking_affinity(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("feature-absent ranking touched cache affinity")

        monkeypatch.setattr(
            runtime_module,
            "_router_dynamic_cache_domain_guard",
            unexpected_runtime_affinity,
        )
        monkeypatch.setattr(
            ensemble_module,
            "_cache_affinity_private_inputs",
            unexpected_ranking_affinity,
        )

    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    captured_batches: list[_RouterDynamicCacheAffinityReceiptBatch] = []
    affinity_context: _RouterDynamicCacheAffinityCollectionContext | None = None
    affinity_sink: Any = None
    if policy is not None:
        affinity_context = _affinity_context(
            decision_id=decision_id,
            provider_instance_token="affinity-e2e-provider",
            generation=0,
            session_key=session_key,
            session_epoch=session_epoch,
        )
        sidecar_key = runner._register_router_dynamic_cache_sidecar(
            context=affinity_context,
            policy=policy,
        )

        def stage_batch(batch: _RouterDynamicCacheAffinityReceiptBatch) -> None:
            captured_batches.append(batch)
            assert runner._stage_router_dynamic_cache_affinity_batch(
                sidecar_key,
                batch,
            )

        affinity_sink = stage_batch

    physical = _RouterSingleDirectProvider(
        _AffinityDoneProvider(
            [
                ProviderDone(
                    provider=cached_config.provider,
                    model=cached_model,
                    input_tokens=1_000,
                    cached_tokens=1_000,
                    cache_write_tokens=0,
                )
            ]
        ),
        cached_config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
        cache_affinity_context=affinity_context,
        cache_affinity_receipt_sink=affinity_sink,
        cache_affinity_generation_getter=(
            lambda: runner._router_dynamic_cache_generation(session_key)
        ),
        cache_affinity_credential_namespace_token=(
            _affinity_credential_token(cached_config) if policy is not None else None
        ),
    )
    physical_events = await _collect(physical.chat([], config=ChatConfig(timeout=30.0)))
    assert [event.kind for event in physical_events] == ["done"]

    if policy is None:
        assert captured_batches == []
        assert runner._router_dynamic_cache_affinity is None
        cache_available = False
        receipts: tuple[Any, ...] = ()
    else:
        assert len(captured_batches) == 1
        seed_turn = SimpleNamespace(
            session_key=session_key,
            metadata={"router_single_decision_id": decision_id},
        )
        assert runner._commit_pending_router_dynamic_cache_affinity(
            seed_turn,
            EngineDone(),
        )
        cache_available, receipts, _ = runner._router_single_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=session_epoch,
            policy=policy,
        )
        assert cache_available is True
        assert len(receipts) == 1
        assert receipts[0].requested_identity == f"openrouter:{cached_model}"

    from opensquilla.provider.ranking_router import (
        build_single_model_request_context,
    )

    request_context = build_single_model_request_context(
        message="continue the previous task",
        turn_metadata={"input_tokens": 1_000},
        attachments=[],
        output_tokens=4_096,
        ranking_config=ranking_config,
    )
    task_analysis = TaskAnalysisResult(
        profile={
            "capability_dist": {"reasoning": 0.6, "code_generation": 0.4},
            "domain_dist": {"software_engineering": 1.0},
            "tier_dist": {"3": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "context": "short",
                "modality": ["text"],
                "risk": "medium",
            },
            "optional_constraints": {"format": "patch"},
            "session_intent": {"type": "continue", "confidence": 1.0},
        },
        source="affinity_e2e",
        schema_valid=True,
        confidence=1.0,
    )
    ensemble_config = SimpleNamespace(
        selection_mode="router_dynamic",
        prepared_ranking_config=lambda: ranking_config,
        ranking_user_profile_enabled=False,
        candidates=[],
        model_options=[],
        ranking_thinking_assignment_enabled=False,
    )
    resolver_config = SimpleNamespace(
        llm_ensemble=ensemble_config,
        llm=SimpleNamespace(
            max_tokens=0,
            context_window_tokens=0,
            temperature=None,
        ),
        squilla_router=SimpleNamespace(tiers={}),
    )
    ranking_inputs: dict[str, Any] = {
        "decision_id": "affinity-e2e-next",
        "task_analysis": task_analysis,
        "request_context": request_context,
        "ranking_config": ranking_config,
    }
    if policy is not None:
        ranking_inputs.update(
            {
                "cache_continuity_available": cache_available,
                "cache_affinity_policy": policy.source,
                "cache_affinity_receipts": receipts,
                "cache_affinity_session_epoch": session_epoch,
                "cache_affinity_now_monotonic": time.monotonic(),
                "cache_affinity_outer_thinking_projection": {
                    "thinking_enabled": False,
                    "effective_thinking_level": "off",
                    "thinking_budget_tokens": 0,
                },
            }
        )
    route = resolve_router_single_route(
        config=resolver_config,
        inherited_provider_config=cached_config,
        turn_metadata={
            "routed_tier": "c2",
            "routing_confidence": 0.9,
        },
        ranking_inputs=ranking_inputs,
        requires_tools=False,
        session_key=session_key,
        model_catalog=_Catalog(),
    )

    expected_model = cached_model if feature_enabled else baseline_model
    assert route.provider_config.model == expected_model
    scores = {row["identity"]: row for row in route.trace["model_scores"]}
    cached_score = scores[f"openrouter:{cached_model}"]
    if feature_enabled:
        assert cached_score["cache_affinity"]["score_adjustment"] > 0.0
        assert route.trace["selection_policy"] == "cache_adjusted_base_score_top1"
    else:
        assert "cache_affinity" not in cached_score


async def test_multiple_physical_receipt_commit_flips_next_real_aggregator_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.provider.cache_affinity import (
        build_credential_namespace_token,
    )
    from opensquilla.provider.ensemble import (
        build_ensemble_provider_from_config,
    )
    from opensquilla.provider.types import ProviderMessageCountProjection

    proposer_models = ("synthetic/proposer-a", "synthetic/proposer-b")
    cached_aggregator = "synthetic/aggregator-cached"
    baseline_aggregator = "synthetic/aggregator-baseline"
    all_models = (*proposer_models, cached_aggregator, baseline_aggregator)
    session_key = "agent:main:affinity-multiple-e2e"
    session_epoch = 53
    decision_id = "affinity-multiple-seed"
    policy_override = {
        "session": {
            "kv_cache_affinity": {
                "strategy": "bonus",
                "topologies": ["multiple"],
                "ttl_seconds": 300,
                "age_decay": "linear",
                "bonus_by_evidence": {
                    "read_hit": 0.05,
                    "write_only": 0.025,
                },
            }
        }
    }
    ranking_config = ranking_config_snapshot(override=policy_override)
    policy = _router_dynamic_cache_affinity_policy(
        ranking_config,
        topology="multiple",
    )
    assert policy is not None
    inherited = ProviderConfig(
        provider="openrouter",
        model=proposer_models[0],
        api_key="synthetic-multiple-affinity-secret",
        base_url="https://openrouter.ai/api/v1",
        org_id="synthetic-tenant",
        provider_routing={model: "anthropic" for model in all_models},
        _provider_routing_strict_override=True,
    )
    model_configs = {model: replace(inherited, model=model) for model in all_models}
    ranking_phase = {"name": "seed"}

    def registry_model(
        model_id: str,
        *,
        role: str,
        capability: float,
    ) -> dict[str, Any]:
        return {
            "source": "affinity_multiple_e2e_registry",
            "runtime": {"thinking": "off"},
            "registry_facts": {
                "model_id": model_id,
                "version": f"{model_id}-20260818",
                "provider": "openrouter",
                "vendor": "synthetic",
                "family": model_id,
                "is_open_source": False,
                "is_chinese_model": False,
                "status": "enabled",
                "roles": [role],
                "context_window": 200_000,
                "effective_context_bucket": "extra_long",
                "modalities": ["text"],
                "tools": [],
                "price": {
                    "input_per_million": 1.0,
                    "output_per_million": 1.0,
                },
                "latency_p50_ms": 1_000,
                "latency_p95_ms": 2_000,
                "quota": "available",
                "rate_limit": "available",
                "health": "healthy",
                "credential_available": True,
            },
            "static_profile": {
                "capability_dist_prior": {
                    "reasoning": capability,
                    "code_generation": capability,
                    "format_following": capability,
                },
                "domain_dist_prior": {
                    "software_engineering": capability,
                },
                "tier_dist_prior": {
                    "1": capability,
                    "2": capability,
                    "3": capability,
                    "4": capability,
                },
                "role_fit_prior": {role: capability},
            },
            "online_profile": {
                "error_rates": {
                    "hallucination": max(0.0, 1.0 - capability),
                    "omission": max(0.0, 0.9 - capability),
                }
            },
        }

    def registry_snapshot(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        seed = ranking_phase["name"] == "seed"
        return {
            "schema_version": "affinity-multiple-e2e",
            "snapshot_version": f"affinity-multiple-{ranking_phase['name']}",
            "models": [
                registry_model(
                    proposer_models[0],
                    role="proposer",
                    capability=0.95,
                ),
                registry_model(
                    proposer_models[1],
                    role="proposer",
                    capability=0.94,
                ),
                registry_model(
                    baseline_aggregator,
                    role="aggregator",
                    capability=0.90,
                ),
                registry_model(
                    cached_aggregator,
                    role="aggregator",
                    capability=0.901 if seed else 0.899,
                ),
            ],
        }

    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "true")
    monkeypatch.setattr(
        ranking_module,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: snapshot,
    )
    monkeypatch.setattr(
        ranking_module,
        "build_model_registry_snapshot",
        registry_snapshot,
    )

    def resolution_for(model_id: str) -> ProviderDeploymentResolution:
        config = model_configs[model_id]
        return ProviderDeploymentResolution(
            provider=config.provider,
            model=model_id,
            ready=True,
            provider_config=config,
        )

    def resolve_member(ref: Any, inherited_config: Any, **kwargs: Any) -> Any:
        del inherited_config, kwargs
        return resolution_for(str(ref.model))

    def resolve_cache_identity(
        config: Any,
        provider_id: str,
        model: str,
        **kwargs: Any,
    ) -> tuple[ProviderDeploymentResolution, object]:
        del config, provider_id, kwargs
        resolution = resolution_for(model)
        provider_config = resolution.provider_config
        assert provider_config is not None
        return (
            resolution,
            build_credential_namespace_token(
                provider=provider_config.provider,
                resolved_secret=provider_config.api_key,
                org_id=provider_config.org_id,
            ),
        )

    monkeypatch.setattr(
        ensemble_module,
        "_resolve_member_deployment",
        resolve_member,
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment_cache_identity",
        resolve_cache_identity,
    )

    class SyntheticMemberProvider:
        def __init__(self, provider_config: ProviderConfig) -> None:
            self._config = provider_config
            self.provider_name = provider_config.provider

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text=f"answer from {self._config.model}")
                yield ProviderDone(
                    provider=self._config.provider,
                    model=self._config.model,
                    stop_reason="stop",
                    input_tokens=1_000,
                    output_tokens=10,
                    cached_tokens=(1_000 if self._config.model == cached_aggregator else 0),
                    cache_write_tokens=0,
                )

            return stream()

        def project_message_count(
            self,
            messages: list[Any],
            config: Any = None,
            *,
            additional_messages: int = 0,
        ) -> ProviderMessageCountProjection:
            system_messages = int(bool(config is not None and config.system))
            return ProviderMessageCountProjection(
                actual_wire_messages=(len(messages) + system_messages + additional_messages),
                logical_messages=len(messages) + additional_messages,
                system_messages=system_messages,
                tool_result_messages=0,
                additional_messages=additional_messages,
                provider_kind=self._config.provider,
                model=self._config.model,
            )

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        ensemble_module,
        "_build_provider",
        lambda provider_config: SyntheticMemberProvider(provider_config),
    )

    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": proposer_models[0],
            "api_key": "synthetic-multiple-affinity-secret",
            "base_url": "https://openrouter.ai/api/v1",
            "max_tokens": 16_384,
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "shuffle_candidates": False,
            "ranking_thinking_assignment_enabled": False,
            "ranking_config_override": policy_override,
            "aggregator_recovery_mode": "experiment",
            "all_failed_policy": "error",
        },
    )
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    context = _RouterDynamicCacheAffinityCollectionContext(
        turn_id="turn-affinity-multiple-e2e",
        decision_id=decision_id,
        provider_instance_token="provider-affinity-multiple-e2e",
        provider_instance_generation=0,
        session_key=session_key,
        session_epoch=session_epoch,
        selection_generation=0,
        topology="multiple",
    )
    sidecar_key = runner._register_router_dynamic_cache_sidecar(
        context=context,
        policy=policy,
    )
    physical_batches: list[Any] = []
    task_analysis = TaskAnalysisResult(
        profile={
            "capability_dist": {
                "reasoning": 0.6,
                "code_generation": 0.4,
            },
            "domain_dist": {"software_engineering": 1.0},
            "tier_dist": {"3": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "context": "short",
                "modality": ["text"],
                "risk": "medium",
            },
            "optional_constraints": {"format": "comparison"},
            "session_intent": {"type": "continue", "confidence": 1.0},
        },
        source="frozen_replay",
        schema_valid=True,
        confidence=1.0,
    )

    def stage_physical_batch(batch: object) -> None:
        physical_batches.append(batch)
        normalized = runner._normalize_multiple_cache_affinity_batch(
            context=context,
            batch=batch,
        )
        assert normalized is not None
        assert runner._stage_router_dynamic_cache_affinity_batch(
            sidecar_key,
            normalized,
        )

    def ranking_inputs(
        *,
        current_decision_id: str,
        receipts: tuple[Any, ...],
    ) -> dict[str, Any]:
        return {
            "decision_id": current_decision_id,
            "ranking_config": ranking_config,
            "task_analysis": task_analysis,
            "cache_continuity_available": True,
            "cache_affinity_policy": policy.source,
            "cache_affinity_receipts": receipts,
            "cache_affinity_session_epoch": session_epoch,
            "cache_affinity_now_monotonic": time.monotonic(),
            "cache_affinity_outer_thinking_projection": {
                "thinking_enabled": False,
                "effective_thinking_level": "off",
                "thinking_budget_tokens": 0,
            },
        }

    seed_provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={
            "routed_tier": "c2",
            "routing_confidence": 0.9,
            "router_dynamic_task_text": "compare two technical systems",
        },
        ranking_inputs=ranking_inputs(
            current_decision_id=decision_id,
            receipts=(),
        ),
        _cache_affinity_receipt_callback=stage_physical_batch,
        _cache_affinity_turn_id=context.turn_id,
        _cache_affinity_provider_instance_token=(context.provider_instance_token),
        _cache_affinity_session_epoch=session_epoch,
    )
    assert seed_provider.selection_plan["selected_A"] == (f"openrouter:{cached_aggregator}")
    seed_events = await _collect(
        seed_provider.chat(
            [Message(role="user", content="continue")],
            config=ChatConfig(
                timeout=30.0,
                thinking=False,
                thinking_budget_tokens=0,
            ),
        )
    )
    assert any(event.kind == "done" for event in seed_events)
    assert len(physical_batches) == 1
    assert len(physical_batches[0].receipts) == 1
    assert runner._commit_pending_router_dynamic_cache_affinity(
        SimpleNamespace(
            session_key=session_key,
            metadata={"ensemble_decision_id": decision_id},
        ),
        EngineDone(),
    )
    available, receipts, _ = runner._router_dynamic_cache_continuity_snapshot(
        session_key=session_key,
        session_epoch=session_epoch,
        policy=policy,
    )
    assert available is True
    assert len(receipts) == 1
    assert receipts[0].role == "aggregator"
    assert receipts[0].requested_identity == (f"openrouter:{cached_aggregator}")
    expected_guard = ensemble_module._cache_affinity_guard_for_resolution(
        resolution_for(cached_aggregator),
        build_credential_namespace_token(
            provider=inherited.provider,
            resolved_secret=inherited.api_key,
            org_id=inherited.org_id,
        ),
        role="aggregator",
        topology="multiple",
        session_epoch=session_epoch,
        upstream="anthropic",
        thinking_enabled=False,
        effective_thinking_level="off",
        thinking_budget_tokens=0,
    )
    assert receipts[0].cache_domain_guard == expected_guard

    ranking_phase["name"] = "next"
    common_turn_metadata = {
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "router_dynamic_task_text": "continue the comparison",
    }
    control = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata=common_turn_metadata,
        ranking_inputs=ranking_inputs(
            current_decision_id="affinity-multiple-control",
            receipts=(),
        ),
    )
    affinity = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata=common_turn_metadata,
        ranking_inputs=ranking_inputs(
            current_decision_id="affinity-multiple-next",
            receipts=receipts,
        ),
    )

    assert control.selection_plan["selected_A"] == (f"openrouter:{baseline_aggregator}")
    assert affinity.selection_plan["cache_affinity_inputs"], affinity.selection_plan.get(
        "cache_affinity_unavailable_reasons"
    )
    assert affinity.selection_plan["selected_A"] == (f"openrouter:{cached_aggregator}")
    affinity_scores = {
        row["identity"]: row for row in affinity.selection_plan["aggregator"]["scores"]
    }
    cached_row = affinity_scores[f"openrouter:{cached_aggregator}"]
    assert cached_row["cache_affinity"]["score_adjustment"] > 0.0
