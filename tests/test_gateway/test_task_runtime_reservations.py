"""Two-phase TaskRuntime admission contracts.

Ingress persists the user message, task ledger row, and idempotency receipt in
one SQLite transaction.  The runtime therefore needs an inert reservation
phase that can be aborted when that transaction fails, followed by activation
only after the transaction commits.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmEnsembleCandidateConfig
from opensquilla.gateway.model_routing import capture_model_routing_config
from opensquilla.gateway.project_workspace_runtime import AcceptedRunModeOverride
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.session_model_routing import accepted_model_routing_audit
from opensquilla.gateway.task_runtime import (
    PendingOverflowPolicy,
    TaskQueueFullError,
    TaskRuntime,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


@dataclass
class _TrackingStorage:
    records: dict[str, AgentTaskRecord] = field(default_factory=dict)
    create_calls: list[str] = field(default_factory=list)
    update_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.create_calls.append(record.task_id)
        self.records[record.task_id] = record

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        self.update_calls.append((task_id, fields))
        record = self.records.get(task_id)
        if record is None:
            return
        for name, value in fields.items():
            setattr(record, name, value)

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_: Any) -> list[AgentTaskRecord]:
        return list(self.records.values())

    def accept(self, record: AgentTaskRecord) -> None:
        """Stand in for the ingress transaction that owns task persistence."""

        self.records[record.task_id] = record


@dataclass(frozen=True)
class _PersistenceResult:
    replayed: bool = False


def _envelope(session_key: str = "agent-1::reservation") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="reservation-test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "synthetic-test"},
    )


async def _noop_turn_handler(_run: Any) -> None:
    return


@pytest.mark.asyncio
async def test_reserve_is_inert_until_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _TrackingStorage()
    handler_started = asyncio.Event()
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    lifecycle_events: list[Any] = []

    async def handler(_run: Any) -> None:
        handler_started.set()

    async def emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, name, payload))

    async def lifecycle(event: Any) -> None:
        lifecycle_events.append(event)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        event_emitter=emit,
        lifecycle_listener=lifecycle,
    )

    def forbid_create_task(*_args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        raise AssertionError("reserve must not create an asyncio task")

    # Patch only around reserve/abort: activation is explicitly the phase that
    # may start the asyncio task.
    monkeypatch.setattr(asyncio, "create_task", forbid_create_task)
    reservation = await runtime.reserve(_envelope(), "hello")

    assert reservation.task_record.status == AgentTaskStatus.QUEUED
    assert reservation.task_record.session_key == "agent-1::reservation"
    assert storage.records == {}
    assert storage.create_calls == []
    assert storage.update_calls == []
    assert emitted == []
    assert lifecycle_events == []
    assert not handler_started.is_set()
    assert reservation.task_record.task_id not in runtime._tasks
    assert runtime._pending_by_session == {}
    assert runtime._running_by_session == {}
    assert runtime._last_envelope_by_session == {}

    await runtime.abort_reservation(reservation)
    assert emitted == []
    assert lifecycle_events == []
    assert runtime._tasks == {}


@pytest.mark.asyncio
async def test_transferable_explicit_intent_release_survives_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = TaskRuntime(storage=_TrackingStorage(), turn_handler=_noop_turn_handler)
    session_key = "agent-1::cancelled-intent-release"
    lease = await runtime.acquire_explicit_ingress_intent(session_key)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    original_release = runtime._release_explicit_ingress_intent

    async def delayed_release(key: str, state: Any) -> None:
        release_started.set()
        await allow_release.wait()
        await original_release(key, state)

    monkeypatch.setattr(runtime, "_release_explicit_ingress_intent", delayed_release)
    caller = asyncio.create_task(lease.release())
    await asyncio.wait_for(release_started.wait(), timeout=1.0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert await runtime.has_explicit_ingress_intent(session_key) is True

    allow_release.set()
    assert lease._release_task is not None
    await asyncio.wait_for(lease._release_task, timeout=1.0)

    assert await runtime.has_explicit_ingress_intent(session_key) is False
    assert session_key not in runtime._ingress_intent_states


@pytest.mark.asyncio
async def test_reserve_preserves_task_id_without_capturing_accepted_config() -> None:
    storage = _TrackingStorage()
    config_captures: list[dict[str, str]] = []

    async def handler(_run: Any) -> None:
        raise AssertionError("an aborted reservation must never execute")

    def capture_config() -> dict[str, str]:
        snapshot = {"strategy": "router"}
        config_captures.append(snapshot)
        return snapshot

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        accepted_config_provider=capture_config,
    )

    reservation = await runtime.reserve(
        _envelope(),
        "reserved",
        task_id="turn-preallocated",
    )

    assert reservation.task_id == "turn-preallocated"
    assert reservation.task_record.task_id == "turn-preallocated"
    assert reservation.runtime_task.accepted_config is None
    assert config_captures == []

    await runtime.abort_reservation(reservation)
    assert config_captures == []


@pytest.mark.asyncio
async def test_activate_captures_accepted_config_once() -> None:
    storage = _TrackingStorage()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    seen_configs: list[Any] = []
    config_captures: list[dict[str, str]] = []

    async def handler(run: Any) -> None:
        seen_configs.append(run.accepted_config)
        handler_started.set()
        await release_handler.wait()

    def capture_config() -> dict[str, str]:
        snapshot = {"strategy": f"router-{len(config_captures) + 1}"}
        config_captures.append(snapshot)
        return snapshot

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        accepted_config_provider=capture_config,
    )
    reservation = await runtime.reserve(_envelope(), "accepted")

    assert config_captures == []
    storage.accept(reservation.task_record)

    first = await runtime.activate(reservation)
    second = await runtime.activate(reservation)
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    assert first == second
    assert config_captures == [{"strategy": "router-1"}]
    assert seen_configs == [{"strategy": "router-1"}]

    release_handler.set()
    assert (await runtime.wait(first.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_activate_passes_session_and_run_kind_to_contextual_config_provider() -> None:
    storage = _TrackingStorage()
    calls: list[tuple[str, str]] = []
    captured = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=False, rollout_phase="observe"),
        llm_ensemble=SimpleNamespace(enabled=False, selection_mode=""),
        session_mode="direct",
        session_routing_revision=11,
        session_routing_source="session_override",
    )

    async def provider(*, session_key: str, run_kind: str) -> Any:
        calls.append((session_key, run_kind))
        return captured

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_noop_turn_handler,
        accepted_config_provider=provider,
    )
    reservation = await runtime.reserve(
        _envelope("agent-1::accepted-routing"),
        "accepted",
        run_kind="channel_turn",
    )
    storage.accept(reservation.task_record)

    handle = await runtime.activate(reservation)
    await runtime.wait(handle.task_id, timeout=1.0)

    assert calls == [("agent-1::accepted-routing", "channel_turn")]
    assert reservation.runtime_task.accepted_config is captured
    details = storage.records[handle.task_id].details
    assert details is not None
    assert details["accepted_model_routing"] == {
        "scope": "session",
        "session_mode": "direct",
        "session_revision": 11,
        "source": "session_override",
        "effective_mode": "direct",
        "router_enabled": False,
        "ensemble_enabled": False,
        "rollout_phase": "observe",
        "selection_mode": "",
        "run_kind": "channel_turn",
    }


@pytest.mark.asyncio
async def test_freeze_acceptance_attaches_audit_before_commit_without_storage_update() -> None:
    storage = _TrackingStorage()
    captured = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=False, rollout_phase="observe"),
        llm_ensemble=SimpleNamespace(enabled=False, selection_mode=""),
        session_mode="direct",
        session_routing_revision=17,
        session_routing_source="session_override",
    )

    async def provider(*, session_key: str, run_kind: str) -> Any:
        assert session_key == "agent-1::precommit-routing-audit"
        assert run_kind == "session_turn"
        return captured

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_noop_turn_handler,
        accepted_config_provider=provider,
    )
    reservation = await runtime.reserve(
        _envelope("agent-1::precommit-routing-audit"),
        "accepted",
        run_kind="session_turn",
    )

    await runtime.freeze_acceptance(reservation)

    assert storage.records == {}
    assert storage.update_calls == []
    assert reservation.task_record.details is not None
    audit = reservation.task_record.details["accepted_model_routing"]
    assert audit["effective_mode"] == "direct"
    assert audit["session_revision"] == 17

    # Simulate the process dying immediately after durable acceptance: the
    # record itself already carries recovery authority, before activation or
    # any best-effort update can run.
    await storage.create_agent_task(reservation.task_record)
    committed = storage.records[reservation.task_id]
    assert committed.details is not None
    assert committed.details["accepted_model_routing"] == audit
    assert storage.update_calls == []

    await runtime.abort_reservation(reservation)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["router", "ensemble"])
async def test_durable_routing_recovery_restores_exact_accepted_details(mode: str) -> None:
    storage = _TrackingStorage()
    accepted_live = GatewayConfig()
    accepted_live.squilla_router.tiers["c0"]["model"] = "accepted-router-model"
    if mode == "ensemble":
        accepted_live.llm_ensemble.selection_mode = "custom_b5"
        accepted_live.llm_ensemble.enabled = True
        accepted_live.llm_ensemble.candidates = [
            LlmEnsembleCandidateConfig(
                provider="openai",
                model="accepted-proposer-one",
                role="primary",
            ),
            LlmEnsembleCandidateConfig(
                provider="anthropic",
                model="accepted-proposer-two",
                role="contrast",
            ),
            LlmEnsembleCandidateConfig(
                provider="openai",
                model="accepted-aggregator",
                role="aggregator",
            ),
        ]
        accepted_live.llm_ensemble = type(accepted_live.llm_ensemble).model_validate(
            accepted_live.llm_ensemble.model_dump(mode="python")
        )

    accepted = capture_model_routing_config(
        accepted_live,
        session_mode=mode,
        session_routing_revision=23,
        session_routing_source="session",
    )
    audit = accepted_model_routing_audit(accepted, run_kind="web_turn")
    assert audit is not None
    assert "config_snapshot" in audit

    def reject_current_config(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("new durable records must not read current routing details")

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_noop_turn_handler,
        accepted_config_provider=reject_current_config,
    )
    reservation = await runtime.reserve(
        _envelope(f"agent-1::durable-{mode}"),
        "accepted",
        run_kind="web_turn",
    )
    reservation.task_record.details = {"accepted_model_routing": audit}

    await runtime._restore_durable_accepted_model_routing(  # noqa: SLF001
        reservation,
        reservation.task_record,
    )

    restored = reservation.runtime_task.accepted_config
    assert restored.squilla_router.model_dump(mode="json") == (
        accepted.squilla_router.model_dump(mode="json")
    )
    assert restored.llm_ensemble.model_dump(mode="json") == (
        accepted.llm_ensemble.model_dump(mode="json")
    )
    assert restored.session_mode == mode
    assert restored.session_routing_revision == 23
    await runtime.abort_reservation(reservation)


@pytest.mark.asyncio
async def test_legacy_durable_routing_audit_uses_compatible_current_details() -> None:
    storage = _TrackingStorage()
    accepted_live = GatewayConfig()
    accepted = capture_model_routing_config(
        accepted_live,
        session_mode="router",
        session_routing_revision=5,
        session_routing_source="session",
    )
    audit = accepted_model_routing_audit(accepted, run_kind="web_turn")
    assert audit is not None
    audit.pop("config_snapshot")

    current_live = GatewayConfig()
    current_live.squilla_router.tiers["c0"]["model"] = "current-compatible-model"
    current = capture_model_routing_config(current_live)
    provider_calls: list[tuple[str, str]] = []

    def current_config_provider(*, session_key: str, run_kind: str) -> Any:
        provider_calls.append((session_key, run_kind))
        return current

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_noop_turn_handler,
        accepted_config_provider=current_config_provider,
    )
    reservation = await runtime.reserve(
        _envelope("agent-1::legacy-durable-routing"),
        "accepted",
        run_kind="web_turn",
    )
    reservation.task_record.details = {"accepted_model_routing": audit}

    await runtime._restore_durable_accepted_model_routing(  # noqa: SLF001
        reservation,
        reservation.task_record,
    )

    restored = reservation.runtime_task.accepted_config
    assert provider_calls == [
        ("agent-1::legacy-durable-routing", "recovery_model_routing_base")
    ]
    assert restored.session_mode == "router"
    assert restored.session_routing_revision == 5
    assert restored.squilla_router.tiers["c0"]["model"] == "current-compatible-model"
    await runtime.abort_reservation(reservation)


@pytest.mark.asyncio
async def test_accepted_config_resolution_does_not_hold_runtime_state_lock() -> None:
    storage = _TrackingStorage()
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(*, session_key: str, run_kind: str) -> dict[str, str]:
        del session_key, run_kind
        provider_started.set()
        await release_provider.wait()
        return {"strategy": "direct"}

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_noop_turn_handler,
        accepted_config_provider=provider,
    )
    accepted = await runtime.reserve(_envelope("agent-1::lock-free"), "accepted")
    storage.accept(accepted.task_record)

    activating = asyncio.create_task(runtime.activate(accepted))
    await asyncio.wait_for(provider_started.wait(), timeout=1.0)

    # A second reservation touches the same runtime state lock.  It must not
    # wait for the session/storage resolver used by the first accepted task.
    second = await asyncio.wait_for(
        runtime.reserve(_envelope("agent-1::other-session"), "next"),
        timeout=1.0,
    )
    await runtime.abort_reservation(second)

    release_provider.set()
    handle = await asyncio.wait_for(activating, timeout=1.0)
    await runtime.wait(handle.task_id, timeout=1.0)


@pytest.mark.asyncio
async def test_activate_starts_precommitted_task_without_creating_ledger_row() -> None:
    storage = _TrackingStorage()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    emitted_names: list[str] = []

    async def handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    async def emit(_session_key: str, name: str, _payload: dict[str, Any]) -> None:
        emitted_names.append(name)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, event_emitter=emit)
    reservation = await runtime.reserve(_envelope(), "accepted")

    assert not handler_started.is_set()
    assert emitted_names == []
    storage.accept(reservation.task_record)

    handle = await runtime.activate(reservation)
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    assert handle.task_id == reservation.task_record.task_id
    assert handle.session_key == reservation.task_record.session_key
    assert storage.create_calls == []
    assert handle.task_id in runtime._tasks
    assert "task.queued" in emitted_names

    release_handler.set()
    record = await runtime.wait(handle.task_id, timeout=1.0)
    assert record.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_try_collect_atomically_mutates_only_after_persist_and_skips_replay() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()
    seen_runs: list[Any] = []

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()
        seen_runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::atomic-collect")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    candidate_handle = await runtime.enqueue(
        envelope,
        "first",
        attachments=[{"name": "first.txt"}],
        mode="collect",
        semantic_message="semantic first",
        persisted_user_message_id="message-first",
        message_count=2,
    )
    candidate = runtime._tasks[candidate_handle.task_id]
    persist_observations: list[tuple[str, dict[str, Any]]] = []

    async def persist(
        handle: Any,
        details: dict[str, Any],
    ) -> _PersistenceResult:
        assert handle.task_id == candidate_handle.task_id
        persist_observations.append((candidate.message, details))
        return _PersistenceResult()

    collected = await runtime.try_collect_atomically(
        envelope=envelope,
        message="second",
        attachments=[{"name": "second.txt"}],
        run_kind="default",
        no_memory_capture=False,
        semantic_message="semantic second",
        persisted_user_message_id="message-second",
        message_count=3,
        persist=persist,
    )

    assert collected is not None
    assert collected[0] == candidate_handle
    assert collected[1] == _PersistenceResult()
    assert persist_observations == [
        (
            "first",
            {
                "source_name": "reservation-test",
                "input_provenance": {"kind": "synthetic-test"},
                "metadata": {},
                "collected": True,
                "message_count": 5,
                "no_memory_capture": False,
                "persisted_user_message_id": "message-first",
                "persisted_user_message_ids": ["message-first", "message-second"],
                "fresh_user_session": False,
            },
        )
    ]
    assert candidate.message == "first\nsecond"
    assert candidate.attachments == [{"name": "first.txt"}, {"name": "second.txt"}]
    assert candidate.semantic_message == "semantic first\n\nsemantic second"
    assert candidate.message_count == 5
    assert candidate.persisted_user_message_id == "message-first"
    assert candidate.persisted_user_message_ids == [
        "message-first",
        "message-second",
    ]

    async def replay_persist(
        _handle: Any,
        _details: dict[str, Any],
    ) -> _PersistenceResult:
        return _PersistenceResult(replayed=True)

    replay = await runtime.try_collect_atomically(
        envelope=envelope,
        message="second",
        run_kind="default",
        no_memory_capture=False,
        persist=replay_persist,
    )

    assert replay is not None
    assert replay[1].replayed is True
    assert candidate.message == "first\nsecond"

    release_running.set()
    assert (await runtime.wait(running.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )
    assert (await runtime.wait(candidate_handle.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )
    assert [run.message for run in seen_runs] == ["running", "first\nsecond"]
    collected_run = seen_runs[-1]
    assert collected_run.attachments == [
        {"name": "first.txt"},
        {"name": "second.txt"},
    ]
    assert collected_run.semantic_message == "semantic first\n\nsemantic second"
    assert collected_run.persisted_user_message_id == "message-first"
    assert collected_run.persisted_user_message_ids == (
        "message-first",
        "message-second",
    )


@pytest.mark.parametrize(
    ("pending_mode", "later_mode"),
    [("full", "safe"), ("safe", "full")],
)
@pytest.mark.asyncio
async def test_try_collect_atomically_rejects_different_accepted_mode_capabilities(
    pending_mode: str,
    later_mode: str,
) -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope(f"agent-1::collect-{pending_mode}-to-{later_mode}")
    running = await runtime.enqueue(_envelope("agent-1::collect-blocker"), "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    pending_override = AcceptedRunModeOverride(
        run_mode=RunMode(pending_mode),
        run_mode_source="user",
        source="request",
    )
    later_override = AcceptedRunModeOverride(
        run_mode=RunMode(later_mode),
        run_mode_source="user",
        source="request",
    )
    candidate_handle = await runtime.enqueue(
        envelope,
        "first",
        mode="collect",
        accepted_run_mode_override=pending_override,
    )
    candidate = runtime._tasks[candidate_handle.task_id]
    persist_calls = 0

    async def persist(
        _handle: Any,
        _details: dict[str, Any],
    ) -> _PersistenceResult:
        nonlocal persist_calls
        persist_calls += 1
        return _PersistenceResult()

    collected = await runtime.try_collect_atomically(
        envelope=envelope,
        message="second",
        run_kind="default",
        no_memory_capture=False,
        accepted_run_mode_override=later_override,
        persist=persist,
    )

    assert collected is None
    assert persist_calls == 0
    assert candidate.message == "first"
    assert candidate.accepted_run_mode_override == pending_override

    replacement = await runtime.reserve(
        envelope,
        "second",
        mode="collect",
        accepted_run_mode_override=later_override,
    )
    try:
        assert replacement.task_id != candidate_handle.task_id
        assert replacement.runtime_task.accepted_run_mode_override == later_override
    finally:
        await runtime.abort_reservation(replacement)
        release_running.set()
        await runtime.wait(running.task_id, timeout=1.0)
        await runtime.wait(candidate_handle.task_id, timeout=1.0)


@pytest.mark.parametrize("mode", [None, "safe", "full"])
@pytest.mark.asyncio
async def test_try_collect_atomically_accepts_identical_mode_capabilities(
    mode: str | None,
) -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope(f"agent-1::collect-compatible-{mode or 'none'}")
    running = await runtime.enqueue(_envelope("agent-1::compatible-blocker"), "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    accepted_override = (
        AcceptedRunModeOverride(
            run_mode=RunMode(mode),
            run_mode_source="user",
            source="request",
        )
        if mode is not None
        else None
    )
    candidate_handle = await runtime.enqueue(
        envelope,
        "first",
        mode="collect",
        accepted_run_mode_override=accepted_override,
    )
    candidate = runtime._tasks[candidate_handle.task_id]

    async def persist(
        _handle: Any,
        _details: dict[str, Any],
    ) -> _PersistenceResult:
        return _PersistenceResult()

    collected = await runtime.try_collect_atomically(
        envelope=envelope,
        message="second",
        run_kind="default",
        no_memory_capture=False,
        accepted_run_mode_override=accepted_override,
        persist=persist,
    )

    assert collected is not None
    assert collected[0] == candidate_handle
    assert candidate.message == "first\nsecond"
    assert candidate.accepted_run_mode_override == accepted_override

    release_running.set()
    await runtime.wait(running.task_id, timeout=1.0)
    await runtime.wait(candidate_handle.task_id, timeout=1.0)


@pytest.mark.asyncio
async def test_try_collect_atomically_leaves_candidate_unchanged_on_persist_error() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::atomic-collect-failure")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    candidate_handle = await runtime.enqueue(envelope, "first", mode="collect")
    candidate = runtime._tasks[candidate_handle.task_id]

    async def fail_persist(_handle: Any, _details: dict[str, Any]) -> None:
        raise RuntimeError("synthetic persistence failure")

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        await runtime.try_collect_atomically(
            envelope=envelope,
            message="second",
            run_kind="default",
            no_memory_capture=True,
            persist=fail_persist,
        )

    assert candidate.message == "first"
    assert candidate.no_memory_capture is False

    release_running.set()
    await runtime.wait(running.task_id, timeout=1.0)
    await runtime.wait(candidate_handle.task_id, timeout=1.0)


@pytest.mark.asyncio
async def test_slow_collect_claim_does_not_hold_runtime_state_for_other_session() -> None:
    storage = _TrackingStorage()
    a_started = asyncio.Event()
    release_a = asyncio.Event()
    b_started = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "a-running":
            a_started.set()
            await release_a.wait()
        elif run.message == "b-running":
            b_started.set()
            await asyncio.Event().wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=3)
    envelope_a = _envelope("agent-1::claim-a")
    envelope_b = _envelope("agent-1::claim-b")
    running_a = await runtime.enqueue(envelope_a, "a-running")
    await asyncio.wait_for(a_started.wait(), timeout=1.0)
    candidate = await runtime.enqueue(envelope_a, "a-collected", mode="collect")
    running_b = await runtime.enqueue(envelope_b, "b-running")
    await asyncio.wait_for(b_started.wait(), timeout=1.0)

    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def persist(_handle: Any, _details: dict[str, Any]) -> _PersistenceResult:
        persist_started.set()
        await release_persist.wait()
        return _PersistenceResult()

    collection = asyncio.create_task(
        runtime.try_collect_atomically(
            envelope=envelope_a,
            message="a-second",
            run_kind="default",
            no_memory_capture=False,
            persist=persist,
        )
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1.0)

    reservation_b = await asyncio.wait_for(
        runtime.reserve(envelope_b, "b-reserved"),
        timeout=0.2,
    )
    assert await asyncio.wait_for(runtime.cancel(task_id=running_b.task_id), 0.2) == 1
    assert (await runtime.wait(running_b.task_id, timeout=0.2)).status == (
        AgentTaskStatus.CANCELLED
    )
    assert not collection.done()
    await runtime.abort_reservation(reservation_b)

    release_persist.set()
    assert await asyncio.wait_for(collection, timeout=1.0) is not None
    release_a.set()
    await runtime.wait(running_a.task_id, timeout=1.0)
    await runtime.wait(candidate.task_id, timeout=1.0)


@pytest.mark.asyncio
async def test_collect_cancellation_settles_persist_and_runtime_apply() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::collect-cancel")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    handle = await runtime.enqueue(
        envelope,
        "first",
        mode="collect",
        persisted_user_message_id="message-first",
    )
    candidate = runtime._tasks[handle.task_id]
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def persist(_handle: Any, _details: dict[str, Any]) -> _PersistenceResult:
        persist_started.set()
        await release_persist.wait()
        return _PersistenceResult()

    collection = asyncio.create_task(
        runtime.try_collect_atomically(
            envelope=envelope,
            message="second",
            run_kind="default",
            no_memory_capture=False,
            persisted_user_message_id="message-second",
            persist=persist,
        )
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1.0)
    collection.cancel()
    await asyncio.sleep(0)
    assert not collection.done()

    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(collection, timeout=1.0)
    assert candidate.message == "first\nsecond"
    assert candidate.persisted_user_message_id == "message-first"
    assert candidate.persisted_user_message_ids == [
        "message-first",
        "message-second",
    ]

    release_running.set()
    await runtime.wait(running.task_id, timeout=1.0)
    await runtime.wait(handle.task_id, timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_collect_claim_releases_claimed_slot() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()
    replacement_started = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()
        elif run.message == "replacement":
            replacement_started.set()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::collect-slot-cancel")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    handle = await runtime.enqueue(envelope, "first", mode="collect")
    candidate = runtime._tasks[handle.task_id]
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def persist(_handle: Any, _details: dict[str, Any]) -> _PersistenceResult:
        persist_started.set()
        await release_persist.wait()
        return _PersistenceResult()

    collection = asyncio.create_task(
        runtime.try_collect_atomically(
            envelope=envelope,
            message="second",
            run_kind="default",
            no_memory_capture=False,
            persist=persist,
        )
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1.0)
    release_running.set()
    await runtime.wait(running.task_id, timeout=1.0)
    for _ in range(100):
        if candidate.acquired_slot:
            break
        await asyncio.sleep(0.01)
    assert candidate.acquired_slot is True

    assert await runtime.cancel(task_id=handle.task_id) == 1
    release_persist.set()
    assert await asyncio.wait_for(collection, timeout=1.0) is not None
    cancelled = await runtime.wait(handle.task_id, timeout=1.0)

    assert cancelled.status == AgentTaskStatus.CANCELLED
    assert runtime._global_in_flight == 0
    replacement = await runtime.enqueue(
        _envelope("agent-1::collect-slot-replacement"),
        "replacement",
    )
    await asyncio.wait_for(replacement_started.wait(), timeout=1.0)
    assert (await runtime.wait(replacement.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_direct_collect_admission_serializes_miss_through_activation() -> None:
    @dataclass
    class BlockingCreateStorage(_TrackingStorage):
        block_next: bool = False
        create_started: asyncio.Event = field(default_factory=asyncio.Event)
        release_create: asyncio.Event = field(default_factory=asyncio.Event)

        async def create_agent_task(self, record: AgentTaskRecord) -> None:
            if self.block_next:
                self.block_next = False
                self.create_started.set()
                await self.release_create.wait()
            await super().create_agent_task(record)

    storage = BlockingCreateStorage()
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "blocker":
            blocker_started.set()
            await release_blocker.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::collect-admission")
    blocker = await runtime.enqueue(envelope, "blocker")
    await asyncio.wait_for(blocker_started.wait(), timeout=1.0)
    storage.block_next = True

    first = asyncio.create_task(runtime.enqueue(envelope, "first", mode="collect"))
    await asyncio.wait_for(storage.create_started.wait(), timeout=1.0)
    second = asyncio.create_task(runtime.enqueue(envelope, "second", mode="collect"))
    await asyncio.sleep(0)
    assert not second.done()

    storage.release_create.set()
    first_handle, second_handle = await asyncio.gather(first, second)
    assert first_handle.task_id == second_handle.task_id
    assert runtime._tasks[first_handle.task_id].message == "first\nsecond"
    assert len(storage.records) == 2

    release_blocker.set()
    await runtime.wait(blocker.task_id, timeout=1.0)
    await runtime.wait(first_handle.task_id, timeout=1.0)
    assert runtime._collect_admission_locks == {}


@pytest.mark.asyncio
async def test_collect_admission_reclaims_registry_after_concurrent_unique_sessions() -> None:
    runtime = TaskRuntime(storage=_TrackingStorage(), turn_handler=_noop_turn_handler)
    active_by_session: dict[str, int] = {}
    max_active_by_session: dict[str, int] = {}
    start = asyncio.Event()

    async def borrow(session_key: str) -> None:
        await start.wait()
        async with runtime.collect_admission(session_key):
            active = active_by_session.get(session_key, 0) + 1
            active_by_session[session_key] = active
            max_active_by_session[session_key] = max(
                max_active_by_session.get(session_key, 0),
                active,
            )
            await asyncio.sleep(0)
            active_by_session[session_key] = active - 1

    session_keys = [f"agent-1::admission-registry-{index}" for index in range(100)]
    borrowers = [
        asyncio.create_task(borrow(session_key))
        for session_key in session_keys
        for _ in range(4)
    ]
    start.set()
    await asyncio.gather(*borrowers)

    assert max_active_by_session == dict.fromkeys(session_keys, 1)
    assert runtime._collect_admission_locks == {}


@pytest.mark.asyncio
async def test_collect_admission_reclaims_registry_after_waiter_cancellation() -> None:
    runtime = TaskRuntime(storage=_TrackingStorage(), turn_handler=_noop_turn_handler)
    session_key = "agent-1::admission-cancelled-waiters"
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold() -> None:
        async with runtime.collect_admission(session_key):
            holder_entered.set()
            await release_holder.wait()

    async def wait() -> None:
        async with runtime.collect_admission(session_key):
            return

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holder_entered.wait(), timeout=1.0)
    waiters = [asyncio.create_task(wait()) for _ in range(50)]
    await asyncio.sleep(0)

    for waiter in waiters[::2]:
        waiter.cancel()
    await asyncio.gather(*waiters[::2], return_exceptions=True)
    release_holder.set()
    await asyncio.wait_for(
        asyncio.gather(holder, *waiters[1::2]),
        timeout=1.0,
    )

    assert runtime._collect_admission_locks == {}


@pytest.mark.asyncio
async def test_direct_enqueue_cancellation_after_task_commit_still_activates() -> None:
    @dataclass
    class CommitThenPauseStorage(_TrackingStorage):
        committed: asyncio.Event = field(default_factory=asyncio.Event)
        release_create: asyncio.Event = field(default_factory=asyncio.Event)

        async def create_agent_task(self, record: AgentTaskRecord) -> None:
            await super().create_agent_task(record)
            self.committed.set()
            await self.release_create.wait()

    storage = CommitThenPauseStorage()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    sending = asyncio.create_task(runtime.enqueue(_envelope(), "committed"))
    await asyncio.wait_for(storage.committed.wait(), timeout=1.0)
    sending.cancel()
    storage.release_create.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sending, timeout=1.0)
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    assert runtime._reservations_by_session == {}
    assert len(storage.records) == 1
    task_id = next(iter(storage.records))
    assert task_id in runtime._tasks

    release_handler.set()
    assert (await runtime.wait(task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_activate_settles_overflow_victim_when_terminal_persistence_fails() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=1,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )
    envelope = _envelope("agent-1::overflow-persistence")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    victim_handle = await runtime.enqueue(envelope, "victim")
    victim = runtime._tasks[victim_handle.task_id]

    original_update = storage.update_agent_task

    async def fail_victim_terminal(task_id: str, **fields: Any) -> None:
        if (
            task_id == victim_handle.task_id
            and fields.get("status") == AgentTaskStatus.CANCELLED
        ):
            raise RuntimeError("synthetic terminal persistence failure")
        await original_update(task_id, **fields)

    storage.update_agent_task = fail_victim_terminal  # type: ignore[method-assign]
    replacement = await runtime.reserve(envelope, "replacement")
    storage.accept(replacement.task_record)

    handle = await runtime.activate(replacement)

    assert handle.task_id == replacement.task_id
    assert replacement.activated is True
    assert replacement.task_id in runtime._tasks
    assert victim_handle.task_id not in runtime._tasks
    assert victim.done.is_set()

    release_running.set()
    await runtime.wait(running.task_id, timeout=1.0)
    assert (await runtime.wait(replacement.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_activate_does_not_drop_reserved_victim_after_it_starts_running() -> None:
    storage = _TrackingStorage()
    running_started = asyncio.Event()
    release_running = asyncio.Event()
    victim_started = asyncio.Event()
    release_victim = asyncio.Event()
    victim_cancelled = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await release_running.wait()
            return
        if run.message == "victim":
            victim_started.set()
            try:
                await release_victim.wait()
            except asyncio.CancelledError:
                victim_cancelled.set()
                raise

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=1,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )
    envelope = _envelope("agent-1::overflow-victim-started")
    running = await runtime.enqueue(envelope, "running")
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    victim_handle = await runtime.enqueue(envelope, "victim")

    # Reserve the replacement while the victim is still queued, then let the
    # victim acquire the only execution slot before the durable acceptance is
    # activated. Running work must no longer be eligible for DROP_OLDEST.
    replacement = await runtime.reserve(envelope, "replacement")
    storage.accept(replacement.task_record)
    release_running.set()
    assert (await runtime.wait(running.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )
    await asyncio.wait_for(victim_started.wait(), timeout=1.0)
    assert (await runtime.status(victim_handle.task_id)).status == AgentTaskStatus.RUNNING

    replacement_handle = await runtime.activate(replacement)

    await asyncio.sleep(0)
    assert victim_cancelled.is_set() is False
    assert (await runtime.status(victim_handle.task_id)).status == AgentTaskStatus.RUNNING
    assert replacement.runtime_task.overflow_dropped is False

    release_victim.set()
    assert (await runtime.wait(victim_handle.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )
    assert (await runtime.wait(replacement_handle.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_abort_releases_reserved_queue_capacity_and_all_runtime_state() -> None:
    storage = _TrackingStorage()

    async def handler(_run: Any) -> None:
        return None

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_pending_per_session=1,
    )
    envelope = _envelope("agent-1::abort-capacity")

    first = await runtime.reserve(envelope, "first")
    with pytest.raises(TaskQueueFullError):
        await runtime.reserve(envelope, "queue is reserved")

    await runtime.abort_reservation(first)
    replacement = await runtime.reserve(envelope, "replacement")

    assert replacement.task_record.task_id != first.task_record.task_id
    assert storage.records == {}
    assert storage.create_calls == []
    assert runtime._tasks == {}
    assert runtime._pending_by_session == {}
    assert runtime._running_by_session == {}
    assert runtime._last_envelope_by_session == {}

    await runtime.abort_reservation(replacement)
    # A second reserve proves abort released the admission token, not merely
    # hid it from the active-task dictionaries.
    final = await runtime.reserve(envelope, "capacity reusable")
    await runtime.abort_reservation(final)
    assert runtime._reservations_by_session == {}
    assert runtime._reserved_overflow_victims == set()
    assert runtime._agent_active_sessions == {}
    assert runtime._agent_session_rr == {}


@pytest.mark.asyncio
async def test_queue_full_is_rejected_before_reserve_side_effects() -> None:
    storage = _TrackingStorage()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    emitted_names: list[str] = []

    async def handler(run: Any) -> None:
        if run.message == "running":
            first_started.set()
            await release_first.wait()

    async def emit(_session_key: str, name: str, _payload: dict[str, Any]) -> None:
        emitted_names.append(name)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        event_emitter=emit,
        max_concurrency=1,
        max_pending_per_session=1,
    )
    envelope = _envelope("agent-1::full-before-reserve")

    running_reservation = await runtime.reserve(envelope, "running")
    storage.accept(running_reservation.task_record)
    running = await runtime.activate(running_reservation)
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    pending_reservation = await runtime.reserve(envelope, "pending")
    storage.accept(pending_reservation.task_record)
    pending = await runtime.activate(pending_reservation)
    event_snapshot = list(emitted_names)
    record_ids = set(storage.records)
    active_ids = set(runtime._tasks)

    with pytest.raises(TaskQueueFullError):
        await runtime.reserve(envelope, "rejected")

    assert emitted_names == event_snapshot
    assert set(storage.records) == record_ids
    assert set(runtime._tasks) == active_ids
    assert storage.create_calls == []
    assert runtime._reservations_by_session == {}

    release_first.set()
    assert (await runtime.wait(running.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )
    assert (await runtime.wait(pending.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_aborted_interrupt_reservation_does_not_cancel_existing_task() -> None:
    storage = _TrackingStorage()
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    old_cancelled = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message != "old turn":
            return
        old_started.set()
        try:
            await release_old.wait()
        except asyncio.CancelledError:
            old_cancelled.set()
            raise

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    envelope = _envelope("agent-1::interrupt-abort")
    old = await runtime.enqueue(envelope, "old turn")
    await asyncio.wait_for(old_started.wait(), timeout=1.0)

    reservation = await runtime.reserve(envelope, "replacement", mode="interrupt")
    await asyncio.sleep(0)

    assert not old_cancelled.is_set()
    assert storage.records[old.task_id].status == AgentTaskStatus.RUNNING
    assert reservation.task_record.task_id not in storage.records

    await runtime.abort_reservation(reservation)
    await asyncio.sleep(0)
    assert not old_cancelled.is_set()
    assert storage.records[old.task_id].status == AgentTaskStatus.RUNNING

    release_old.set()
    record = await runtime.wait(old.task_id, timeout=1.0)
    assert record.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.parametrize("mode", ["followup", "interrupt", "steer"])
@pytest.mark.asyncio
async def test_every_direct_enqueue_mode_waits_for_session_admission(
    mode: str,
) -> None:
    storage = _TrackingStorage()
    session_key = f"agent-1::admission-{mode}"

    async def handler(_run: Any) -> None:
        return

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    async with runtime.collect_admission(session_key):
        enqueueing = asyncio.create_task(
            runtime.enqueue(
                _envelope(session_key),
                "held behind admission",
                mode=mode,
            )
        )
        await asyncio.sleep(0)

        assert enqueueing.done() is False
        assert storage.create_calls == []
        assert session_key not in runtime._reservations_by_session

    handle = await asyncio.wait_for(enqueueing, timeout=1.0)
    assert (await runtime.wait(handle.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_quiesce_sessions_cancels_running_and_queued_and_waits_for_driver_tail() -> None:
    storage = _TrackingStorage()
    session_key = "agent-1::quiesce-driver-tail"
    running_started = asyncio.Event()
    terminal_tail_started = asyncio.Event()
    release_terminal_tail = asyncio.Event()
    quiesce_entered = asyncio.Event()
    release_quiesce = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "running":
            running_started.set()
            await asyncio.Event().wait()

    async def terminal_listener(_event: Any) -> None:
        terminal_tail_started.set()
        await release_terminal_tail.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        terminal_listener=terminal_listener,
        max_concurrency=1,
    )
    envelope = _envelope(session_key)
    envelope.metadata["parent_session_key"] = "agent-1::parent"
    running = await runtime.enqueue(
        envelope,
        "running",
        run_kind="subagent",
    )
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    queued = await runtime.enqueue(envelope, "queued")

    async def quiesce() -> None:
        async with runtime.quiesce_sessions([session_key]):
            quiesce_entered.set()
            await release_quiesce.wait()

    quiescing = asyncio.create_task(quiesce())
    await asyncio.sleep(0)
    assert quiescing.done() is False
    await asyncio.wait_for(terminal_tail_started.wait(), timeout=1.0)

    assert (await runtime.wait(running.task_id, timeout=1.0)).status == (
        AgentTaskStatus.CANCELLED
    )
    assert quiesce_entered.is_set() is False
    assert quiescing.done() is False

    release_terminal_tail.set()
    await asyncio.wait_for(quiesce_entered.wait(), timeout=1.0)
    assert runtime._tasks == {}
    assert runtime._reservations_by_session == {}
    assert runtime._driver_tasks_by_session == {}
    assert storage.records[queued.task_id].status == AgentTaskStatus.CANCELLED

    blocked_enqueue = asyncio.create_task(
        runtime.enqueue(envelope, "after quiesce")
    )
    await asyncio.sleep(0)
    assert blocked_enqueue.done() is False

    release_quiesce.set()
    await asyncio.wait_for(quiescing, timeout=1.0)
    after = await asyncio.wait_for(blocked_enqueue, timeout=1.0)
    assert (await runtime.wait(after.task_id, timeout=1.0)).status == (
        AgentTaskStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_quiesce_sessions_retries_a_committed_reservation_before_activation() -> None:
    storage = _TrackingStorage()
    session_key = "agent-1::quiesce-commit-before-activate"
    quiesce_entered = asyncio.Event()
    release_quiesce = asyncio.Event()

    async def handler(_run: Any) -> None:
        await asyncio.Event().wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    envelope = _envelope(session_key)
    reservation = await runtime.reserve(envelope, "committed")
    storage.accept(reservation.task_record)

    async def quiesce() -> None:
        async with runtime.quiesce_sessions([session_key]):
            quiesce_entered.set()
            await release_quiesce.wait()

    async def activate() -> Any:
        async with runtime.collect_admission(session_key):
            return await runtime.activate(reservation)

    async with runtime.collect_admission(session_key):
        quiescing = asyncio.create_task(quiesce())
        await asyncio.sleep(0)
        activating = asyncio.create_task(activate())
        await asyncio.sleep(0)

    entered_wait = asyncio.create_task(quiesce_entered.wait())
    try:
        done, _pending = await asyncio.wait(
            {activating, entered_wait},
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert activating in done
        assert entered_wait not in done

        handle = activating.result()
        await asyncio.wait_for(quiesce_entered.wait(), timeout=1.0)
        assert storage.records[handle.task_id].status == AgentTaskStatus.CANCELLED
        assert runtime._tasks == {}
        assert runtime._reservations_by_session == {}
        assert runtime._driver_tasks_by_session == {}
    finally:
        release_quiesce.set()
        if not activating.done():
            await asyncio.wait_for(activating, timeout=1.0)
        await asyncio.wait_for(quiescing, timeout=1.0)
        entered_wait.cancel()


@pytest.mark.asyncio
async def test_quiesce_sessions_cancels_driver_that_wins_execution_lock_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _TrackingStorage()
    session_key = "agent-1::quiesce-execution-acquire-gap"
    driver_started = asyncio.Event()
    release_driver = asyncio.Event()

    async def handler(_run: Any) -> None:
        driver_started.set()
        await release_driver.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    reservation = await runtime.reserve(_envelope(session_key), "gap winner")
    storage.accept(reservation.task_record)
    original_drain = runtime._cancel_and_drain_session_drivers
    injected = False

    async def inject_after_initial_drain(*args: Any, **kwargs: Any) -> None:
        nonlocal injected
        await original_drain(*args, **kwargs)
        if not injected:
            injected = True
            await runtime.activate(reservation)
            await asyncio.wait_for(driver_started.wait(), timeout=1.0)

    monkeypatch.setattr(
        runtime,
        "_cancel_and_drain_session_drivers",
        inject_after_initial_drain,
    )

    async def quiesce() -> None:
        async with runtime.quiesce_sessions([session_key]):
            return

    try:
        await asyncio.wait_for(quiesce(), timeout=1.0)
        assert storage.records[reservation.task_id].status == (
            AgentTaskStatus.CANCELLED
        )
        assert runtime._driver_tasks_by_session == {}
    finally:
        release_driver.set()
        with contextlib.suppress(Exception):
            await runtime.shutdown(cancel=True, timeout=1.0)


@pytest.mark.asyncio
async def test_execution_fence_acquire_releases_lock_when_cancelled_after_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = TaskRuntime(
        storage=_TrackingStorage(),
        turn_handler=_noop_turn_handler,
    )
    execution_lock = asyncio.Lock()

    async def cancel_after_acquire(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0)
        assert execution_lock.locked() is True
        raise asyncio.CancelledError

    monkeypatch.setattr(
        runtime,
        "_cancel_and_drain_session_drivers",
        cancel_after_acquire,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._acquire_execution_lock_while_quiescing(
            execution_lock,
            ("agent-1::cancelled-fence",),
            frozenset({"agent-1::cancelled-fence"}),
        )

    assert execution_lock.locked() is False


@pytest.mark.asyncio
async def test_execution_fence_cancellation_cleans_driver_change_waiter() -> None:
    runtime = TaskRuntime(
        storage=_TrackingStorage(),
        turn_handler=_noop_turn_handler,
    )
    execution_lock = asyncio.Lock()
    await execution_lock.acquire()
    waiting_event = runtime._driver_state_changed
    acquiring = asyncio.create_task(
        runtime._acquire_execution_lock_while_quiescing(
            execution_lock,
            ("agent-1::cancelled-waiter",),
            frozenset({"agent-1::cancelled-waiter"}),
        )
    )
    try:
        for _ in range(20):
            if waiting_event._waiters:
                break
            await asyncio.sleep(0)
        assert len(waiting_event._waiters) == 1

        acquiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquiring

        assert len(waiting_event._waiters) == 0
        assert execution_lock.locked() is True
    finally:
        execution_lock.release()


@pytest.mark.asyncio
async def test_concurrent_execution_fences_cannot_erase_driver_change_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = TaskRuntime(
        storage=_TrackingStorage(),
        turn_handler=_noop_turn_handler,
    )
    first_key = "agent-1::first-concurrent-quiescer"
    second_key = "agent-1::second-concurrent-quiescer"
    first_lock = asyncio.Lock()
    second_lock = asyncio.Lock()
    await first_lock.acquire()
    await second_lock.acquire()
    first_snapshot_taken = asyncio.Event()
    release_first_snapshot = asyncio.Event()
    second_snapshot_taken = asyncio.Event()
    first_redrained = asyncio.Event()
    first_drain_calls = 0

    async def controlled_drain(
        keys: tuple[str, ...],
        _key_set: frozenset[str],
    ) -> None:
        nonlocal first_drain_calls
        if keys == (first_key,):
            first_drain_calls += 1
            if first_drain_calls == 1:
                first_snapshot_taken.set()
                await release_first_snapshot.wait()
            else:
                first_redrained.set()
            return
        second_snapshot_taken.set()

    monkeypatch.setattr(
        runtime,
        "_cancel_and_drain_session_drivers",
        controlled_drain,
    )
    first = asyncio.create_task(
        runtime._acquire_execution_lock_while_quiescing(
            first_lock,
            (first_key,),
            frozenset({first_key}),
        )
    )
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_snapshot_taken.wait(), timeout=1.0)
        runtime._signal_driver_state_changed()
        second = asyncio.create_task(
            runtime._acquire_execution_lock_while_quiescing(
                second_lock,
                (second_key,),
                frozenset({second_key}),
            )
        )
        await asyncio.wait_for(second_snapshot_taken.wait(), timeout=1.0)
        release_first_snapshot.set()

        await asyncio.wait_for(first_redrained.wait(), timeout=1.0)
    finally:
        first_lock.release()
        second_lock.release()
        for task in (first, second):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
