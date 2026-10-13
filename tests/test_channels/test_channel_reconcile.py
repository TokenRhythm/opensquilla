"""Live reconcile: make running adapters match the config without a restart.

Adding, editing, or removing a channel used to demand a full gateway
restart; reconcile diffs the entries per name and starts, rebuilds, or
stops exactly the affected adapters. Webhook-mode adapters stay
restart-gated (their HTTP routes are bound at boot), and a bad entry's
blast radius is that entry — never the gateway.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.channels.manager as manager_module
from opensquilla.channels.manager import ChannelManager
from opensquilla.gateway.boot import ServiceContainer, _bind_channel_service_readiness
from opensquilla.gateway.routing import RouteEnvelope
from opensquilla.gateway.task_runtime import TaskDependencyError, TaskRuntime


class _FakeAdapter:
    transport_name = "websocket"

    def __init__(self, entry: SimpleNamespace) -> None:
        self.entry = entry
        self.token = getattr(entry, "token", "")
        self.started = False
        self.stopped = False
        self.fail_start = getattr(entry, "fail_start", False)

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("bad credentials")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def receive(self):  # pragma: no cover - dispatch loop parks here
        import asyncio

        await asyncio.Event().wait()

    async def health_check(self):  # pragma: no cover - not exercised
        from opensquilla.channels.types import ChannelHealth

        return ChannelHealth(connected=self.started)


class _FakeWebhookAdapter(_FakeAdapter):
    transport_name = "webhook"

    def create_webhook_route(self):  # pragma: no cover - existence is the signal
        raise AssertionError("reconcile must never collect webhook routes")


def _entry(
    name: str,
    *,
    token: str = "t1",
    enabled: bool = True,
    type: str = "fake",  # noqa: A002 - mirrors the config entry field name
    **extra,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        type=type,
        enabled=enabled,
        token=token,
        dm_access="pairing",
        allowed_senders=(),
        agent_id="main",
        group_session_scope="per_sender",
        busy_input_mode="followup",
        debounce_window_s=0.0,
        **extra,
    )


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChannelManager:
    def _build(entry: SimpleNamespace):
        if getattr(entry, "webhook", False):
            return _FakeWebhookAdapter(entry)
        if entry.type == "unknown":
            return None
        return _FakeAdapter(entry)

    monkeypatch.setattr(manager_module, "build_managed_channel", _build)
    mgr = ChannelManager.from_config(
        [],
        turn_runner=object(),
        session_manager=object(),
        config=SimpleNamespace(state_dir=str(tmp_path)),
    )
    return mgr


async def _teardown(mgr: ChannelManager) -> None:
    for name in list(mgr._channels):
        await mgr.stop_channel(name)
    if mgr._delivery_store is not None:
        (await mgr._delivery_store.close())


def _bind_readiness(manager: ChannelManager):
    services = ServiceContainer(config=SimpleNamespace(), optional_generation=1)
    assert _bind_channel_service_readiness(manager, services, config_revision=7)
    runtime = object.__new__(TaskRuntime)
    runtime._service_snapshot = lambda: services.optional_services

    def admit(name: str) -> None:
        runtime._ensure_required_services(RouteEnvelope(
            source_kind="channel", source_name=name, agent_id="main", session_key="test",
            required_services=(f"channel:{name}",),
        ))

    return services, admit


async def test_hot_add_publishes_readiness_before_dispatch(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, admit = _bind_readiness(manager)
    dispatched = asyncio.Event()

    async def dispatch(**kwargs):
        admit(kwargs["session_prefix"])
        dispatched.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_module, "run_channel_dispatch", dispatch)
    try:
        assert await manager.reconcile([_entry("hot")]) == {"hot": "started"}
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        assert services.optional_services["channel:hot"] == {
            "status": "ready", "generation": 1, "config_revision": 7, "owner_generation": 1,
        }
    finally:
        await _teardown(manager)


async def test_healthy_channel_does_not_wait_for_other_optional_startup(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, admit = _bind_readiness(manager)
    release = asyncio.Event()
    dispatched = asyncio.Event()

    class SlowAdapter(_FakeAdapter):
        async def start(self):
            await release.wait()
            await super().start()

    async def dispatch(**kwargs):
        admit(kwargs["session_prefix"])
        if kwargs["session_prefix"] == "fast":
            dispatched.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_module, "run_channel_dispatch", dispatch)
    for name, adapter in (("fast", _FakeAdapter), ("slow", SlowAdapter)):
        entry = _entry(name)
        manager._install_adapter(entry, adapter(entry))
    start = asyncio.create_task(manager.start_all())
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        assert not start.done()
        assert services.optional_services["channel:slow"]["status"] == "starting"
        admit("fast")
        with pytest.raises(TaskDependencyError, match="dependency_starting"):
            admit("slow")
    finally:
        release.set()
        await start
        await _teardown(manager)


async def test_readiness_tracks_failed_start_restart_stop_and_hot_remove(
    manager: ChannelManager,
) -> None:
    services, admit = _bind_readiness(manager)
    try:
        assert await manager.reconcile([_entry("hot", fail_start=True)]) == {"hot": "failed"}
        assert services.optional_services["channel:hot"]["status"] == "degraded"
        with pytest.raises(TaskDependencyError, match="dependency_unavailable"):
            admit("hot")
        manager.get("hot").fail_start = False
        await manager.restart_channel("hot")
        admit("hot")
        await manager.stop_channel("hot")
        assert services.optional_services["channel:hot"]["status"] == "disabled"
        with pytest.raises(TaskDependencyError, match="dependency_unavailable"):
            admit("hot")
        await manager.restart_channel("hot")
        admit("hot")
        assert await manager.reconcile([]) == {"hot": "removed"}
        assert "channel:hot" not in services.optional_services
        assert not manager._stopping_channels
    finally:
        await _teardown(manager)


async def test_stopping_dispatch_cannot_republish_ready_during_drain(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, admit = _bind_readiness(manager)
    draining = asyncio.Event()
    release = asyncio.Event()

    async def drain(name):
        draining.set()
        await release.wait()

    await manager.reconcile([_entry("hot")])
    monkeypatch.setattr(manager._delivery_store, "drain_channel", drain)
    stop = asyncio.create_task(manager.stop_channel("hot"))
    try:
        await asyncio.wait_for(draining.wait(), timeout=1)
        manager._set_dispatch_state("hot", "running")
        assert services.optional_services["channel:hot"]["status"] == "stopping"
        with pytest.raises(TaskDependencyError, match="dependency_starting"):
            admit("hot")
    finally:
        release.set()
        await stop
        await _teardown(manager)


async def test_dispatch_failure_clears_readiness_until_retry_or_manual_restart(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, admit = _bind_readiness(manager)
    statuses = []
    publish = manager._service_status_callback

    def record(name, status):
        statuses.append(status)
        publish(name, status)

    async def dispatch(**kwargs):
        admit(kwargs["session_prefix"])
        raise RuntimeError("synthetic dispatch failure")

    manager.set_service_status_callback(record)
    manager._max_retries = 1
    manager._retry_backoff_initial = 0
    manager._max_restart_cycles = 0
    monkeypatch.setattr(manager_module, "run_channel_dispatch", dispatch)
    try:
        await manager.reconcile([_entry("hot")])
        await asyncio.wait_for(manager._tasks["hot"], timeout=1)
        assert statuses.count("degraded") >= 2
        first_failure = statuses.index("degraded")
        assert "ready" in statuses[first_failure + 1:]
        assert manager._dispatch_states["hot"] == "dead"
        assert services.optional_services["channel:hot"]["status"] == "degraded"
        with pytest.raises(TaskDependencyError, match="dependency_unavailable"):
            admit("hot")
    finally:
        await _teardown(manager)


async def test_channel_readiness_generation_fences_late_start_and_old_cleanup(
    manager: ChannelManager,
) -> None:
    services, admit = _bind_readiness(manager)
    manager._publish_service_status("hot", "ready")
    services.optional_generation += 1
    manager._publish_service_status("hot", "stopping")
    manager._publish_service_status("hot", "ready")
    assert services.optional_services["channel:hot"]["status"] == "stopping"
    replacement = ChannelManager({}, None, None)
    assert _bind_channel_service_readiness(replacement, services, config_revision=8)
    replacement._publish_service_status("hot", "ready")
    manager._publish_service_status("hot", None)
    manager._publish_service_status("hot", "degraded")
    admit("hot")
    assert services.optional_services["channel:hot"]["generation"] == 2
    await _teardown(manager)


async def test_add_starts_a_new_channel_live(manager: ChannelManager) -> None:
    results = await manager.reconcile([_entry("tg-main")])

    assert results == {"tg-main": "started"}
    adapter = manager.get("tg-main")
    assert adapter is not None and adapter.started
    assert manager._channel_types["tg-main"] == "fake"
    assert "tg-main" in manager._transport_leases
    assert "tg-main" in manager._tasks
    await _teardown(manager)


async def test_unchanged_entry_is_left_untouched(manager: ChannelManager) -> None:
    entry = _entry("tg-main")
    await manager.reconcile([entry])
    first = manager.get("tg-main")

    results = await manager.reconcile([entry])

    assert results == {"tg-main": "unchanged"}
    assert manager.get("tg-main") is first
    assert first.stopped is False
    await _teardown(manager)


async def test_changed_entry_rebuilds_with_the_new_config(manager: ChannelManager) -> None:
    await manager.reconcile([_entry("tg-main", token="old")])
    old = manager.get("tg-main")

    results = await manager.reconcile([_entry("tg-main", token="new")])

    assert results == {"tg-main": "rebuilt"}
    new = manager.get("tg-main")
    assert new is not old
    assert old.stopped is True
    # The rebuilt adapter runs the NEW config — the whole point.
    assert new.token == "new" and new.started
    await _teardown(manager)


async def test_removed_and_disabled_entries_stop_live(manager: ChannelManager) -> None:
    await manager.reconcile([_entry("a"), _entry("b")])
    adapter_a = manager.get("a")
    adapter_b = manager.get("b")

    results = await manager.reconcile([_entry("b", enabled=False)])

    assert results == {"a": "removed", "b": "removed"}
    assert manager.get("a") is None and manager.get("b") is None
    assert adapter_a.stopped and adapter_b.stopped
    assert "a" not in manager._transport_leases
    assert "a" not in manager._channel_types
    await _teardown(manager)


async def test_webhook_entries_stay_restart_gated(manager: ChannelManager) -> None:
    results = await manager.reconcile([_entry("hooked", webhook=True)])

    assert results == {"hooked": "pending_restart"}
    # Nothing was installed live: no adapter, no lease, no dispatch task.
    assert manager.get("hooked") is None
    assert "hooked" not in manager._transport_leases
    await _teardown(manager)


async def test_running_webhook_adapter_is_never_touched(manager: ChannelManager) -> None:
    # Simulate a boot-installed webhook adapter (routes bound at app creation).
    entry = _entry("hooked", webhook=True)
    adapter = _FakeWebhookAdapter(entry)
    manager._install_adapter(entry, adapter)

    removed = await manager.reconcile([])
    edited = await manager.reconcile([_entry("hooked", webhook=True, token="new")])

    assert removed == {"hooked": "pending_restart"}
    assert edited == {"hooked": "pending_restart"}
    assert manager.get("hooked") is adapter and adapter.stopped is False
    await _teardown(manager)


async def test_start_failure_is_contained_to_its_channel(manager: ChannelManager) -> None:
    results = await manager.reconcile([_entry("bad", fail_start=True), _entry("good")])

    assert results["bad"] == "failed"
    assert results["good"] == "started"
    # The failed entry stays installed with its error surfaced, so
    # channels.status shows it and channels.restart can retry it.
    assert manager.get("bad") is not None
    assert manager.start_errors()["bad"]["error_type"] == "RuntimeError"
    assert manager.get("good").started
    # And the failure is recoverable live: fix the entry, reconcile again.
    recovered = await manager.reconcile([_entry("bad"), _entry("good")])
    assert recovered["bad"] == "rebuilt"
    assert manager.start_errors().get("bad") is None
    await _teardown(manager)


async def test_unknown_type_reports_failed_without_side_effects(
    manager: ChannelManager,
) -> None:
    results = await manager.reconcile([_entry("mystery", type="unknown")])

    assert results == {"mystery": "failed"}
    assert manager.get("mystery") is None
    await _teardown(manager)


async def test_identical_resave_after_start_failure_retries(manager: ChannelManager) -> None:
    # The natural operator retry: hit Save again with the SAME entry after a
    # transient failure. Fingerprint equality must not read as "unchanged"
    # for a channel that never started.
    flaky = _entry("tg-main", fail_start=True)
    first = await manager.reconcile([flaky])
    assert first == {"tg-main": "failed"}

    flaky.fail_start = False
    second = await manager.reconcile([flaky])

    assert second == {"tg-main": "rebuilt"}
    assert manager.get("tg-main").started
    assert manager.start_errors().get("tg-main") is None
    assert "tg-main" in manager._tasks
    await _teardown(manager)


async def test_concurrent_reconciles_never_orphan_tasks(manager: ChannelManager) -> None:
    # Two CRUD RPCs racing on the same name: the mutation lock must serialize
    # them so exactly one dispatch loop and one lease task survive.
    import asyncio

    release = asyncio.Event()
    started = asyncio.Event()
    second_entered = asyncio.Event()

    class _SlowAdapter(_FakeAdapter):
        async def start(self) -> None:
            started.set()
            await release.wait()
            self.started = True

    def _build(entry):
        if getattr(entry, "slow", False):
            return _SlowAdapter(entry)
        return _FakeAdapter(entry)

    import opensquilla.channels.manager as mm

    original = mm.build_managed_channel
    mm.build_managed_channel = _build
    tasks = []

    async def reconcile_second():
        second_entered.set()
        return await manager.reconcile([_entry("x", token="v2")])

    try:
        task_a = asyncio.create_task(manager.reconcile([_entry("x", token="v1", slow=True)]))
        tasks.append(task_a)
        await asyncio.wait_for(started.wait(), timeout=5.0)
        task_b = asyncio.create_task(reconcile_second())
        tasks.append(task_b)
        await asyncio.wait_for(second_entered.wait(), timeout=5.0)
        assert not task_b.done(), "second reconcile bypassed the mutation lock"
        release.set()
        result_a, result_b = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        # Serialized: A applied v1, B rebuilt to v2 — and exactly one runtime.
        assert result_a == {"x": "started"}
        assert result_b == {"x": "rebuilt"}
        assert manager.get("x").token == "v2"
        dispatch_tasks = [t for t in manager._tasks.values() if not t.done()]
        assert len(dispatch_tasks) == 1
        assert len(manager._lease_tasks) == 1
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
        finally:
            mm.build_managed_channel = original
            await asyncio.wait_for(_teardown(manager), timeout=5.0)


async def test_live_start_never_steals_a_pending_webhook_lease(
    manager: ChannelManager,
) -> None:
    # Migration webhook→websocket under the SAME transport account: the old
    # webhook adapter keeps running until restart, so starting the new one
    # would fence its lease out from under it. The whole migration waits.
    old_entry = _entry("hooked", webhook=True, token="shared-app-id")
    manager._install_adapter(old_entry, _FakeWebhookAdapter(old_entry))

    results = await manager.reconcile([_entry("fresh", token="shared-app-id")])

    assert results["hooked"] == "pending_restart"
    assert results["fresh"] == "pending_restart"
    assert manager.get("fresh") is None
    # A DIFFERENT account is unaffected.
    ok = await manager.reconcile(
        [
            _entry("fresh", token="other-app-id"),
            _entry("hooked", webhook=True, token="shared-app-id"),
        ]
    )
    assert ok["fresh"] == "started"
    await _teardown(manager)
