"""Durable per-session model-routing storage contracts."""

from __future__ import annotations

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionRoutingConflictError, SessionStorage


@pytest.mark.asyncio
async def test_legacy_mode_materializes_once_and_same_mode_retry_is_idempotent() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        key = "agent:main:webchat:routing-legacy"
        await storage.upsert_session(
            SessionNode(
                session_key=key,
                session_id="routing-legacy",
                agent_id="main",
                updated_at=100,
            )
        )

        resolved = await storage.resolve_model_routing_mode(key, "router")
        assert resolved == {
            "mode": "router",
            "revision": 1,
            "source": "legacy_initialized",
            "initialized": True,
        }
        initialized = await storage.get_session(key)
        assert initialized is not None
        assert initialized.updated_at == 100
        changed = await storage.set_model_routing_mode(
            key,
            "ensemble",
            expected_revision=1,
        )
        assert changed["revision"] == 2
        # An acknowledgement can be lost after the commit. Retrying the exact
        # requested mode with the prior generation must not manufacture a CAS
        # conflict or an extra generation.
        replay = await storage.set_model_routing_mode(
            key,
            "ensemble",
            expected_revision=1,
        )
        assert replay["revision"] == 2
        with pytest.raises(SessionRoutingConflictError):
            await storage.set_model_routing_mode(key, "direct", expected_revision=1)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_concrete_mode_resolution_never_opens_a_write_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "router")
        key = "agent:main:webchat:routing-read-only"
        await manager.create(key)

        async def fail_begin(*_args, **_kwargs) -> None:
            raise AssertionError("a concrete routing mode must use the read-only path")

        monkeypatch.setattr(storage, "_begin_immediate", fail_begin)
        assert await storage.resolve_model_routing_mode(key, "direct") == {
            "mode": "router",
            "revision": 0,
            "source": "session",
            "initialized": False,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_mode_materialization_does_not_reorder_session_list() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        legacy_key = "agent:main:webchat:routing-legacy-older"
        recent_key = "agent:main:webchat:routing-recent"
        await storage.upsert_session(
            SessionNode(
                session_key=legacy_key,
                session_id="routing-legacy-older",
                agent_id="main",
                updated_at=100,
            )
        )
        await storage.upsert_session(
            SessionNode(
                session_key=recent_key,
                session_id="routing-recent",
                agent_id="main",
                updated_at=200,
                model_routing_mode="direct",
            )
        )

        await storage.resolve_model_routing_mode(legacy_key, "ensemble")

        sessions = await storage.list_sessions()
        assert [session.session_key for session in sessions] == [recent_key, legacy_key]
        assert sessions[1].updated_at == 100
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_stale_general_upsert_cannot_overwrite_routing_cas() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-stale-upsert"
        created = await manager.create(key)
        stale = await storage.get_session(key)
        assert stale is not None

        changed = await storage.set_model_routing_mode(
            key,
            "router",
            expected_revision=0,
        )
        stale.display_name = "Unrelated stale update"
        await storage.upsert_session(stale, expected_session_id=created.session_id)

        persisted = await storage.get_session(key)
        assert persisted is not None
        assert (persisted.model_routing_mode, persisted.model_routing_revision) == (
            "router",
            changed["revision"],
        )
        assert persisted.display_name == "Unrelated stale update"
        assert (stale.model_routing_mode, stale.model_routing_revision) == (
            "router",
            changed["revision"],
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_stale_compaction_rewrite_cannot_overwrite_routing_cas() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-stale-compaction"
        await manager.create(key)
        stale = await storage.get_session(key)
        assert stale is not None

        changed = await storage.set_model_routing_mode(
            key,
            "ensemble",
            expected_revision=0,
        )
        stale.compaction_count = 1
        assert await storage.rewrite_compacted_session(
            node=stale,
            summary=None,
            entries=[],
        )

        persisted = await storage.get_session(key)
        assert persisted is not None
        assert persisted.compaction_count == 1
        assert (persisted.model_routing_mode, persisted.model_routing_revision) == (
            "ensemble",
            changed["revision"],
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_stale_session_reset_cannot_overwrite_routing_cas() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-stale-reset"
        created = await manager.create(key)
        reset = manager._build_reset_node(created)

        changed = await storage.set_model_routing_mode(
            key,
            "router",
            expected_revision=0,
        )

        async def _archive(_snapshot) -> None:
            return None

        await storage.reset_session(
            reset,
            expected_session_id=created.session_id,
            expected_epoch=created.epoch,
            archive_writer=_archive,
        )

        persisted = await storage.get_session(key)
        assert persisted is not None
        assert persisted.session_id == reset.session_id
        assert (persisted.model_routing_mode, persisted.model_routing_revision) == (
            "router",
            changed["revision"],
        )
        assert (reset.model_routing_mode, reset.model_routing_revision) == (
            "router",
            changed["revision"],
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_atomic_turn_reset_cannot_overwrite_routing_cas() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-stale-atomic-reset"
        created = await manager.create(key)
        reset = manager._build_reset_node(created)
        reset.updated_at = 300

        changed = await storage.set_model_routing_mode(
            key,
            "ensemble",
            expected_revision=0,
        )
        await storage.accept_turn(
            TranscriptEntry(
                session_id=reset.session_id,
                session_key=key,
                message_id="routing-reset-message",
                role="user",
                content="start over",
                created_at=300,
            ),
            expected_epoch=reset.epoch,
            updated_at=300,
            task_record=AgentTaskRecord(
                task_id="routing-reset-task",
                session_key=key,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=300,
                updated_at=300,
            ),
            source_scope="webui",
            request_session_key=key,
            client_request_id="routing-reset-request",
            request_fingerprint="sha256:routing-reset-request",
            session_node=reset,
            reset_from_session_id=created.session_id,
        )

        persisted = await storage.get_session(key)
        assert persisted is not None
        assert persisted.session_id == reset.session_id
        assert (persisted.model_routing_mode, persisted.model_routing_revision) == (
            "ensemble",
            changed["revision"],
        )
        assert (reset.model_routing_mode, reset.model_routing_revision) == (
            "ensemble",
            changed["revision"],
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_new_sessions_snapshot_provider_and_forks_copy_concrete_mode() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "router")
        fresh = await manager.create("agent:main:webchat:routing-fresh")
        assert fresh.model_routing_mode == "router"
        assert fresh.model_routing_revision == 0

        parent_key = "agent:main:webchat:routing-parent"
        await storage.upsert_session(
            SessionNode(session_key=parent_key, session_id="routing-parent", agent_id="main")
        )
        child = await manager.branch(
            parent_key,
            "agent:main:webchat:routing-child",
        )
        parent = await storage.get_session(parent_key)
        assert parent is not None
        assert parent.model_routing_mode == "router"
        assert parent.model_routing_revision == 1
        assert child.model_routing_mode == "router"
        assert child.model_routing_revision == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_global_change_only_affects_later_sessions_and_prefix_fork_copies_parent() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        global_state = {"mode": "ensemble"}
        manager = SessionManager(
            storage,
            model_routing_mode_provider=lambda: global_state["mode"],
            inject_time_prefix=False,
        )

        old = await manager.create("agent:main:webchat:routing-old")
        global_state["mode"] = "router"
        new = await manager.create("agent:main:webchat:routing-new")

        persisted_old = await storage.get_session(old.session_key)
        assert persisted_old is not None
        assert persisted_old.model_routing_mode == "ensemble"
        assert new.model_routing_mode == "router"

        changed = await manager.set_session_routing(
            old.session_key,
            "direct",
            expected_revision=0,
        )
        assert changed["mode"] == "direct"
        fork_before = await manager.append_message(
            old.session_key,
            "user",
            "Do not copy this marker.",
        )
        prepared = await manager.prepare_prefix_branch(
            old.session_key,
            "agent:main:webchat:routing-prefix-child",
            fork_before_message_id=fork_before.message_id,
        )

        assert prepared.node.model_routing_mode == "direct"
        assert prepared.node.model_routing_revision == 0
        # The global default remains independent of both parent and child.
        later = await manager.create("agent:main:webchat:routing-later")
        assert later.model_routing_mode == "router"
    finally:
        await storage.close()
