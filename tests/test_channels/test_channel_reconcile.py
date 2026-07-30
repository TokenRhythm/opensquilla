"""Live reconcile: make running adapters match the config without a restart.

Adding, editing, or removing a channel used to demand a full gateway
restart; reconcile diffs the entries per name and starts, rebuilds, or
stops exactly the affected adapters. Webhook-mode adapters stay
restart-gated (their HTTP routes are bound at boot), and a bad entry's
blast radius is that entry — never the gateway.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.channels.manager as manager_module
from opensquilla.channels.manager import ChannelManager


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
        mgr._delivery_store.close()


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

    class _SlowAdapter(_FakeAdapter):
        async def start(self) -> None:
            await release.wait()
            self.started = True

    def _build(entry):
        if getattr(entry, "slow", False):
            return _SlowAdapter(entry)
        return _FakeAdapter(entry)

    import opensquilla.channels.manager as mm

    original = mm.build_managed_channel
    mm.build_managed_channel = _build
    try:
        task_a = asyncio.create_task(manager.reconcile([_entry("x", token="v1", slow=True)]))
        await asyncio.sleep(0.05)  # A holds the lock inside _safe_start
        task_b = asyncio.create_task(manager.reconcile([_entry("x", token="v2")]))
        await asyncio.sleep(0.05)
        release.set()
        result_a = await task_a
        result_b = await task_b
    finally:
        mm.build_managed_channel = original

    # Serialized: A applied v1, B rebuilt to v2 — and exactly one runtime.
    assert result_a == {"x": "started"}
    assert result_b == {"x": "rebuilt"}
    assert manager.get("x").token == "v2"
    dispatch_tasks = [t for t in manager._tasks.values() if not t.done()]
    assert len(dispatch_tasks) == 1
    assert len(manager._lease_tasks) == 1
    await _teardown(manager)


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
