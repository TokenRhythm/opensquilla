from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opensquilla.gateway.boot import build_services
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.persistence.migrator import _native_sqlite_path
from opensquilla.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
async def test_extended_length_state_keeps_gateway_services_operational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.application import approval_queue as approval_queue_module
    from opensquilla.gateway import uploads as uploads_module
    from opensquilla.gateway.app import create_gateway_app
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.gateway.uploads import get_upload_store, set_upload_store

    reset_approval_queue()
    original_upload_store = uploads_module._default_store
    set_upload_store(None)
    for name in (
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
        "OPENSQUILLA_MEMORY_DB",
        "OPENSQUILLA_MEMORY_DIR",
        "OPENSQUILLA_SCHEDULER_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_FAST_START", "1")
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setattr(
        "opensquilla.agents.scope.maybe_migrate_legacy_memory",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "opensquilla.migration.legacy_detect.detect_legacy_home",
        lambda *_: None,
    )

    long_root = tmp_path / "long-state"
    state_dir = long_root
    index = 0
    while len(str(state_dir / "gateway-startup-smoke.db")) < 300:
        state_dir /= f"external-state-segment-{index:02d}-0123456789"
        index += 1
    os.makedirs(_native_sqlite_path(state_dir))
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_dir))

    monkeypatch.setattr(
        approval_queue_module,
        "_DEFAULT_APPROVAL_QUEUE_PATH",
        state_dir / "approval_queue.sqlite",
    )
    workspace = tmp_path / "workspace"
    agent_state = state_dir / "agents" / "main"
    memory_dir = agent_state / "memory"
    os.makedirs(_native_sqlite_path(memory_dir))
    with open(_native_sqlite_path(agent_state / "MEMORY.md"), "w", encoding="utf-8") as handle:
        handle.write("# Memory\n\nboot fact\n")
    with open(_native_sqlite_path(memory_dir / "topic.md"), "w", encoding="utf-8") as handle:
        handle.write("# Topic\n\nlong state fact\n")

    config = GatewayConfig(
        state_dir=str(state_dir),
        workspace_dir=str(workspace),
        config_path=str(tmp_path / "config.toml"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={
            "source": "state",
            "retrieval_mode": "fts_only",
            "flush_enabled": False,
            "repair_enabled": False,
            "sync_interval_minutes": 0.0,
            "ttl_sweep_interval_minutes": 0.0,
            "capture_assistant": True,
        },
        sandbox={"auto_setup": False},
    )
    session_db = state_dir / "gateway-startup-smoke.db"
    services = None
    registry = ToolRegistry()
    try:
        services = await build_services(
            config=config,
            tool_registry=registry,
            session_db_path=str(session_db),
            seed_agent_workspaces=False,
        )

        storage = get_session_storage(services.session_manager)
        assert storage is not None
        async with storage.conn.execute("SELECT 1") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        assert os.path.isfile(_native_sqlite_path(session_db))
        assert os.path.isfile(_native_sqlite_path(state_dir / "approval_queue.sqlite"))

        app = create_gateway_app(
            config=config,
            session_manager=services.session_manager,
            tool_registry=registry,
        )
        assert app is not None
        upload_store = get_upload_store()
        expected_marker_dir = state_dir / "media" / "uploads"
        assert upload_store.marker_dir == expected_marker_dir
        assert not str(upload_store.marker_dir).startswith("\\\\?\\")
        upload_id = await upload_store.put("note.txt", "text/plain", b"long-state upload")
        assert os.path.isfile(_native_sqlite_path(expected_marker_dir / f"{upload_id}.meta"))

        manager = services.memory_managers["main"]
        assert manager.db_path == agent_state / "memory.db"
        assert manager.workspace_dir == agent_state
        assert manager.memory_dir == memory_dir
        assert all(
            not str(path).startswith("\\\\?\\")
            for path in (manager.db_path, manager.workspace_dir, manager.memory_dir)
        )
        assert await manager.store.list_paths() == ["MEMORY.md", "memory/topic.md"]
        captured = await manager.capture_turn(
            session_key="agent:main:gateway-long-state",
            session_id="gateway-long-state",
            user_text="persist this",
            assistant_text="persisted",
        )
        assert captured is not None
        assert os.path.isfile(_native_sqlite_path(agent_state / captured))

        memory_save = registry.get("memory_save")
        memory_get = registry.get("memory_get")
        memory_delete = registry.get("memory_delete")
        assert memory_save is not None
        assert memory_get is not None
        assert memory_delete is not None
        saved = await memory_save.handler(
            content="tool-written long state fact",
            path="memory/tool-long-state.md",
            mode="replace",
        )
        assert "Saved to memory/tool-long-state.md" in saved
        assert await memory_get.handler(path="memory/tool-long-state.md") == (
            "tool-written long state fact"
        )
        deleted = await memory_delete.handler(path="memory/tool-long-state.md")
        assert deleted == "Deleted memory/tool-long-state.md and removed from index."
        assert not os.path.exists(
            _native_sqlite_path(agent_state / "memory" / "tool-long-state.md")
        )

        assert services.cron_scheduler is not None
        assert os.path.isfile(_native_sqlite_path(state_dir / "scheduler.db"))
    finally:
        if services is not None:
            await services.close()

        from opensquilla.sandbox.integration import reset_runtime
        from opensquilla.session.material_cleanup import reset_session_material_cleanup
        from opensquilla.tools.builtin import admin as admin_tools

        admin_tools.set_scheduler(None)  # type: ignore[arg-type]
        admin_tools.set_gateway_config(None)
        reset_approval_queue()
        reset_session_material_cleanup()
        reset_runtime()
        set_upload_store(original_upload_store)
        native_root = _native_sqlite_path(long_root)
        if os.path.exists(native_root):
            shutil.rmtree(native_root)
