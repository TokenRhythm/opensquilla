from __future__ import annotations

import asyncio
import contextvars
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.config import GatewayConfig


class StorageReachedError(Exception):
    pass


@pytest.fixture
def migration_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    events: list[str] = []

    async def reconcile(_state_dir):
        return 0

    class Storage:
        def __init__(self, _db_path):
            events.append("storage_constructed")

        async def connect(self, **_kwargs):
            events.append("storage_connect")
            raise StorageReachedError

    monkeypatch.setattr("opensquilla.env.load_env", lambda: None)
    monkeypatch.setattr("opensquilla.browser.initialize_desktop_browser", lambda: None)
    monkeypatch.setattr(boot, "_warn_workspace_state_mismatch", lambda *_: None)
    monkeypatch.setattr(boot, "_warn_legacy_home_detected", lambda *_: None)
    monkeypatch.setattr(boot, "_ensure_configured_agent_workspaces", lambda *_, **__: None)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda *_: None)
    monkeypatch.setattr(boot, "reset_session_streams", lambda: None)
    monkeypatch.setattr("opensquilla.process_tree.reconcile_persisted_processes", reconcile)
    monkeypatch.setattr(
        "opensquilla.observability.update_check.start_background_update_check",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.configure_runtime",
        lambda *_, **__: SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
    )
    monkeypatch.setattr("opensquilla.session.storage.SessionStorage", Storage)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda _: events.append("owner_acquired"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda _: events.append("owner_released"),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"retrieval_mode": "fts_only"},
    )
    return config, events


@pytest.mark.asyncio
async def test_migration_worker_preserves_result_and_context(monkeypatch, tmp_path):
    caller_thread = threading.get_ident()
    marker = contextvars.ContextVar("migration_test_marker", default="missing")
    token = marker.set("startup")
    expected = ["V001_test"]

    def apply(db_path, migrations_dir):
        assert threading.get_ident() != caller_thread
        assert marker.get() == "startup"
        assert db_path == str(tmp_path / "sessions.db")
        assert migrations_dir == tmp_path
        return expected

    monkeypatch.setattr("opensquilla.persistence.migrator.apply_pending", apply)
    try:
        result = await boot._apply_startup_migrations(str(tmp_path / "sessions.db"), tmp_path)
        assert result is expected
    finally:
        marker.reset(token)


@pytest.mark.asyncio
async def test_build_services_waits_for_migration_without_blocking_loop(
    monkeypatch, migration_boot,
):
    config, events = migration_boot
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def apply(*_):
        events.append("migration_started")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "event loop did not release migration worker"
        events.append("migration_finished")
        return ["V001_test"]

    monkeypatch.setattr("opensquilla.persistence.migrator.apply_pending", apply)
    startup = asyncio.create_task(boot.start_gateway_server(config=config, run=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0)
        assert events == ["owner_acquired", "migration_started"]
        assert not startup.done()
    finally:
        release.set()
        with pytest.raises(StorageReachedError):
            await startup
    assert events == [
        "owner_acquired", "migration_started", "migration_finished",
        "storage_constructed", "storage_connect", "owner_released",
    ]


@pytest.mark.asyncio
async def test_migration_failure_prevents_storage_and_releases_owner(monkeypatch, migration_boot):
    config, events = migration_boot
    failure = RuntimeError("migration failed")

    def apply(*_):
        events.append("migration_failed")
        raise failure

    monkeypatch.setattr("opensquilla.persistence.migrator.apply_pending", apply)
    with pytest.raises(RuntimeError) as raised:
        await boot.start_gateway_server(config=config, run=False)
    assert raised.value is failure
    assert events == ["owner_acquired", "migration_failed", "owner_released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_startup_drains_migration_before_releasing_owner(
    monkeypatch, migration_boot, worker_fails,
):
    config, events = migration_boot
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    def apply(*_):
        events.append("migration_started")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "cancelled startup failed to drain migration worker"
        events.append("migration_finished")
        if worker_fails:
            raise RuntimeError("migration failure after cancellation")
        return ["V001_test"]

    monkeypatch.setattr("opensquilla.persistence.migrator.apply_pending", apply)
    startup = asyncio.create_task(boot.start_gateway_server(config=config, run=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            startup.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not startup.done()
            assert events == ["owner_acquired", "migration_started"]
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await startup
            await asyncio.sleep(0)
            assert not unhandled
        finally:
            loop.set_exception_handler(previous_handler)
    assert events == [
        "owner_acquired", "migration_started", "migration_finished", "owner_released",
    ]


@pytest.mark.asyncio
async def test_startup_imports_runtime_off_loop_before_building_services(
    monkeypatch, migration_boot,
):
    config, events = migration_boot
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    caller_thread = threading.get_ident()

    def import_runtime(name):
        assert name == "opensquilla.engine.runtime"
        assert threading.get_ident() != caller_thread
        events.append("import_started")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "event loop did not release runtime importer"
        events.append("import_finished")
        return SimpleNamespace()

    async def build_services(**_):
        assert threading.get_ident() == caller_thread
        assert asyncio.get_running_loop() is loop
        events.append("services")
        raise StorageReachedError

    monkeypatch.setattr(boot, "import_module", import_runtime)
    monkeypatch.setattr(boot, "build_services", build_services)
    startup = asyncio.create_task(boot.start_gateway_server(config=config, run=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert events == ["owner_acquired", "import_started"]
        assert not startup.done()
    finally:
        release.set()
        with pytest.raises(StorageReachedError):
            await startup
    assert events == [
        "owner_acquired", "import_started", "import_finished", "services", "owner_released",
    ]


@pytest.mark.asyncio
async def test_runtime_import_failure_releases_owner_before_service_construction(
    monkeypatch, migration_boot,
):
    config, events = migration_boot
    failure = ImportError("runtime dependency missing")

    def import_runtime(_name):
        raise failure

    async def build_services(**_):
        pytest.fail("failed runtime import must prevent service construction")

    monkeypatch.setattr(boot, "import_module", import_runtime)
    monkeypatch.setattr(boot, "build_services", build_services)
    with pytest.raises(ImportError) as raised:
        await boot.start_gateway_server(config=config, run=False)
    assert raised.value is failure
    assert events == ["owner_acquired", "owner_released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("import_fails", [False, True])
async def test_cancelled_runtime_import_drains_without_publishing_services(
    monkeypatch, migration_boot, import_fails,
):
    config, events = migration_boot
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def import_runtime(_name):
        events.append("import_started")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "cancelled startup failed to drain runtime importer"
        events.append("import_finished")
        if import_fails:
            raise ImportError("runtime import failed during cancellation")
        return SimpleNamespace()

    async def build_services(**_):
        pytest.fail("cancelled runtime import must prevent service construction")

    monkeypatch.setattr(boot, "import_module", import_runtime)
    monkeypatch.setattr(boot, "build_services", build_services)
    startup = asyncio.create_task(boot.start_gateway_server(config=config, run=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            startup.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not startup.done()
            assert events == ["owner_acquired", "import_started"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
    assert events == ["owner_acquired", "import_started", "import_finished", "owner_released"]
