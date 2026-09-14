from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY, get_run_context
from opensquilla.session.keys import DmScope, build_cron_key, build_direct_key
from opensquilla.session.manager import SessionIntent, SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_new_ordinary_task_gets_a_durable_managed_workspace(tmp_path: Path) -> None:
    factory = build_execution_workspace_factory(GatewayConfig(), profile_home=tmp_path)
    node = SessionNode(session_key="agent:main:webchat:local-first", session_id="sid")

    prepared = await factory(node)
    assert prepared is not None
    binding = prepared.binding

    assert binding is not None
    assert binding["kind"] == "managed"
    assert Path(binding["root"]).parent == tmp_path / "tasks"
    assert Path(binding["root"]).is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "channel"),
    [
        ("main", "telegram"),
        ("cron", "telegram"),
        ("heartbeat", "telegram"),
        ("main", "cron"),
        ("main", "heartbeat"),
    ],
)
async def test_scheduler_named_agents_and_channels_get_distinct_task_roots(
    tmp_path: Path, agent_id: str, channel: str,
) -> None:
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(GatewayConfig(), profile_home=tmp_path)
        ))
        roots = []
        for peer_id in ("first-user", "second-user"):
            key = build_direct_key(
                agent_id, peer_id, channel=channel, dm_scope=DmScope.PER_CHANNEL_PEER,
            )
            session = await manager.create(key, agent_id=agent_id)
            assert session.execution_workspace is not None
            assert session.execution_workspace["kind"] == "managed"
            root = Path(session.execution_workspace["root"])
            assert root.parent == tmp_path / "tasks"
            assert root.is_dir()
            restored = await storage.get_session(key)
            assert restored.execution_workspace == session.execution_workspace
            roots.append(root)
        assert roots[0] != roots[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", [
    build_cron_key("scheduled-task", "first-run"),
    "cron:scheduled-task",
    "heartbeat:scheduled-task",
    "system:scheduled-task",
])
async def test_scheduler_session_prefixes_do_not_allocate_task_roots(
    tmp_path: Path, session_key: str,
) -> None:
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(GatewayConfig(), profile_home=tmp_path)
        ))
        session = await manager.create(session_key)
        assert session.execution_workspace is None
        assert not (tmp_path / "tasks").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "channel"),
    [
        ("cron", "telegram"),
        ("heartbeat", "telegram"),
        ("main", "cron"),
        ("main", "heartbeat"),
    ],
)
async def test_resumed_unbound_scheduler_named_session_keeps_legacy_workspace(
    tmp_path: Path, agent_id: str, channel: str,
) -> None:
    config = GatewayConfig()
    legacy_root = tmp_path / "legacy-workspace"
    legacy_root.mkdir()
    key = build_direct_key(
        agent_id, "legacy-user", channel=channel, dm_scope=DmScope.PER_CHANNEL_PEER,
    )
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        legacy = SessionNode(
            session_key=key, session_id="legacy", agent_id=agent_id,
            origin={RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "safe", "workspace": str(legacy_root),
            }},
        )
        await storage.upsert_session(legacy)
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path)
        ))
        resumed, created = await manager.get_or_create(key, agent_id=agent_id)
        assert created is False
        assert resumed.session_id == legacy.session_id
        assert resumed.execution_workspace is None
        context = await get_run_context(
            manager, key, config=config, workspace=config.workspace_dir,
            include_user_grants=False,
        )
        assert context.workspace == str(legacy_root)
        assert not (tmp_path / "tasks").exists()


@pytest.mark.asyncio
async def test_binding_round_trip_and_reopen_resolve_same_task_directory(tmp_path: Path) -> None:
    config = GatewayConfig()
    database = tmp_path / "sessions.sqlite"
    async with SessionStorage(database) as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path)
        ))
        created = await manager.create("agent:main:webchat:round-trip")
        expected = created.execution_workspace
        restored = await storage.get_session(created.session_key)
        assert restored.execution_workspace == expected
        assert isinstance(restored.execution_workspace, dict)
    async with SessionStorage(database) as storage:
        manager = SessionManager(storage)
        restored = await storage.get_session(created.session_key)
        context = await get_run_context(
            manager, restored.session_key, config=config,
            workspace=config.workspace_dir, include_user_grants=False,
        )
        assert restored.execution_workspace == expected
        assert context.workspace == expected["root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_context", [False, True])
async def test_missing_bound_directory_never_falls_back(
    tmp_path: Path, saved_context: bool,
) -> None:
    config = GatewayConfig()
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path)
        ))
        created = await manager.create("agent:main:webchat:missing")
        root = Path(created.execution_workspace["root"])
        if saved_context:
            await manager.update(created.session_key, origin={RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "safe", "workspace": str(root),
            }})
        root.rmdir()
        with pytest.raises(ProjectWorkspaceStateError, match="unavailable"):
            await get_run_context(
                manager, created.session_key, config=config,
                workspace=config.workspace_dir, include_user_grants=False,
            )
        assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_binding", ["{invalid", "null", '"not-an-object"', "[]"])
async def test_corrupt_binding_does_not_become_legacy_null(
    tmp_path: Path, raw_binding: str,
) -> None:
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        node = SessionNode(session_key="agent:main:webchat:corrupt", session_id="corrupt")
        await storage.upsert_session(node)
        async with storage._write_transaction("test.corrupt_binding") as conn:
            await conn.execute(
                "UPDATE sessions SET execution_workspace=? WHERE session_key=?",
                (raw_binding, node.session_key),
            )
        with pytest.raises(ValueError, match="execution workspace"):
            await storage.get_session(node.session_key)


@pytest.mark.asyncio
async def test_legacy_null_binding_remains_null(tmp_path: Path) -> None:
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        node = SessionNode(session_key="agent:main:webchat:legacy", session_id="legacy")
        await storage.upsert_session(node)
        restored = await storage.get_session(node.session_key)
        assert restored.execution_workspace is None


@pytest.mark.asyncio
async def test_bound_task_is_not_a_legacy_project_candidate(tmp_path: Path) -> None:
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(GatewayConfig(), profile_home=tmp_path)
        ))
        created = await manager.create("agent:main:webchat:managed")
        origin = {RUN_CONTEXT_ORIGIN_KEY: {
            "run_mode": "full", "workspace": created.execution_workspace["root"],
        }}
        await manager.update(created.session_key, origin=origin)
        assert await storage.list_legacy_project_workspace_candidates() == []
        assert await storage.adopt_legacy_session_workspace(
            created.session_key, expected_agent_id="main", expected_origin=origin,
            path=created.execution_workspace["root"], path_key=created.execution_workspace["root"],
            display_name="must not become a project", trusted_at=1, now_ms=1,
        ) is None
        restored = await storage.get_session(created.session_key)
        assert restored.workspace_id is None
        assert restored.execution_workspace == created.execution_workspace


@pytest.mark.asyncio
async def test_task_roots_survive_retry_reset_and_branch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(GatewayConfig(), profile_home=tmp_path)
        ))
        first, created = await manager.get_or_create("agent:main:webchat:first")
        assert created is True
        second, _ = await manager.get_or_create("agent:main:webchat:second")
        first_root = Path(first.execution_workspace["root"])
        second_root = Path(second.execution_workspace["root"])
        assert first_root != second_root
        for revision in range(3):
            (first_root / "index.html").write_text(f"first-{revision}")
            (second_root / "index.html").write_text(f"second-{revision}")
            assert (first_root / "index.html").read_text() == f"first-{revision}"
        retry, created = await manager.get_or_create(first.session_key)
        assert created is False
        assert retry.execution_workspace == first.execution_workspace
        child = await manager.branch(first.session_key, "agent:main:webchat:branch")
        assert child.execution_workspace == first.execution_workspace
        reset, _ = await manager.apply_intent(first.session_key, SessionIntent.RESET_SAME_KEY)
        assert reset.execution_workspace == first.execution_workspace
        assert (first_root / "index.html").read_text() == "first-2"
        assert (second_root / "index.html").read_text() == "second-2"


@pytest.mark.asyncio
async def test_workspace_preview_is_idempotent_and_not_published(tmp_path: Path) -> None:
    import json

    from opensquilla.tools.builtin.workspace_preview import open_workspace_preview
    from opensquilla.tools.types import ToolContext, current_tool_context

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("<h1>live</h1>", encoding="utf-8")
    service = await ArtifactSessionService.open(tmp_path / "sessions.sqlite")
    try:
        adopter = GeneratedArtifactAdopter(
            service=service,
            store=ArtifactStore(tmp_path / "media"),
            session_key="agent:main:webchat:preview",
            session_id="sid",
            workspace=str(workspace),
        )

        context = ToolContext(
            is_owner=True, session_key=adopter.session_key, session_id=adopter.session_id,
            workspace_dir=str(workspace), workspace_preview_opener=adopter.open_workspace_preview,
        )
        token = current_tool_context.set(context)
        try:
            first = json.loads(await open_workspace_preview(path="index.html"))
            second = json.loads(await open_workspace_preview(path="index.html"))
        finally:
            current_tool_context.reset(token)

        assert first["created"] is True
        assert second["created"] is False
        assert first["resourceId"] == second["resourceId"]
        async with service.repository._read_transaction("test.preview") as conn:
            publication = await conn.execute("SELECT COUNT(*) FROM document_publications")
            assert (await publication.fetchone())[0] == 0
            revisions = await conn.execute("SELECT COUNT(*) FROM artifact_revisions")
            assert (await revisions.fetchone())[0] == 1
    finally:
        await service.close()
