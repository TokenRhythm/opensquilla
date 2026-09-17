from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
)
from opensquilla.artifact_session.working_files import ensure_working_files, get_working_files
from opensquilla.artifacts import ArtifactBundle, ArtifactBundleSourceFile, ArtifactStore
from opensquilla.engine.types import ControlTerminalEvent, DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.gateway.artifact_preview import (
    ArtifactPreviewLeaseService,
    create_artifact_preview_resource_app,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.working_versions import with_working_versions
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:working-versions"


@pytest.fixture
async def workspace_turn(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "state.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create(KEY)
    config = GatewayConfig(
        workspace_dir=str(tmp_path / "workspace"),
        attachments={"media_root": str(tmp_path / "media")},
    )
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b'<!doctype html><link rel="stylesheet" href="style.css"><h1>Test</h1>',
                ),
                ArtifactBundleSourceFile(path="style.css", mime="text/css", data=b"h1{color:navy}"),
            ),
        ),
        session_id=session.session_id,
        session_key=KEY,
        name="index.html",
        mime="text/html",
        source="test",
    )
    service = await ArtifactSessionService.from_session_storage(storage)
    initial = await service.create_document(
        session_key=KEY,
        session_id=session.session_id,
        name="index.html",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "test-user"),
    )
    working = await ensure_working_files(
        service,
        store,
        document_id=initial.document.document_id,
        session_key=KEY,
        session_id=session.session_id,
        workspace=config.workspace_dir,
    )
    preview = ArtifactPreviewLeaseService(config=config)
    preview.set_listener_port(28761)
    preview.register_working_files(
        session_id=session.session_id, artifact_id=ref.id, binding=working
    )
    scope = dict(
        config=config,
        session_manager=manager,
        session_key=KEY,
        session_id=session.session_id,
        workspace=config.workspace_dir,
        actor_id="test-turn",
        preview_service=preview,
    )
    try:
        yield SimpleNamespace(
            storage=storage,
            service=service,
            store=store,
            session=session,
            initial=initial,
            working=working,
            preview=preview,
            scope=scope,
        )
    finally:
        await service.close()
        await storage.close()


async def _events(*events):
    for event in events:
        yield event


async def test_task_dispatch_saves_existing_working_files_without_adopter(workspace_turn):
    from opensquilla.gateway.boot import dispatch_task_runtime_turn
    from opensquilla.gateway.routing import build_cli_route_envelope

    env = workspace_turn
    changed_css = "h1{color:rebeccapurple}"

    class Runner:
        async def run(self, message, session_key, **kwargs):
            assert kwargs["tool_context"].workspace_dir == env.scope["workspace"]
            (env.working.root / "style.css").write_text(changed_css)
            yield DoneEvent(text="Updated")

    observed_versions = []

    async def emit(session_key, event_name, payload):
        if event_name == "session.event.done":
            observed_versions.append(len(await env.service.list_revisions(env.working.document_id)))

    envelope = build_cli_route_envelope(session_key=KEY, agent_id="main")
    assert "generated_artifact_adopter" not in envelope.runtime_services
    run = SimpleNamespace(
        agent_id="main",
        task_id="ordinary-edit",
        session_key=KEY,
        message="Update the heading color",
        envelope=envelope,
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    await dispatch_task_runtime_turn(
        run,
        config=env.scope["config"],
        session_manager=env.scope["session_manager"],
        turn_runner=Runner(),
        event_emitter=emit,
    )
    # Done reports generation usage; persistence completes before dispatch returns.
    assert observed_versions == [1]
    assert len(await env.service.list_revisions(env.working.document_id)) == 2
    assert (env.working.root / "style.css").read_text() == changed_css


async def test_success_saves_resources_after_finalization_and_deduplicates(workspace_turn):
    env = workspace_turn
    (env.working.root / "style.css").write_text("h1{color:crimson}")
    observations = []
    async for event in with_working_versions(
        _events(TextDeltaEvent(text="Updated"), DoneEvent(text="Updated")), **env.scope
    ):
        if event.kind == "done":
            observations.append(len(await env.service.list_revisions(env.working.document_id)))
    assert observations == [1]
    assert len(await env.service.list_revisions(env.working.document_id)) == 2
    saved = await get_working_files(env.service, env.working.document_id)
    assert saved.base_revision_id != env.initial.revision.revision_id
    from unittest.mock import AsyncMock

    before = await env.service.get_document(env.working.document_id)
    audit = await env.service.list_audit_events(env.working.document_id)
    changes = await env.service.list_change_sets(env.working.document_id)
    emitted = AsyncMock()
    assert [
        event.kind
        async for event in with_working_versions(
            _events(DoneEvent(text="Explanation only")), event_emitter=emitted, **env.scope
        )
    ] == ["done"]
    assert len(await env.service.list_revisions(env.working.document_id)) == 2
    assert await env.service.get_document(env.working.document_id) == before
    assert await env.service.list_audit_events(env.working.document_id) == audit
    assert await env.service.list_change_sets(env.working.document_id) == changes
    emitted.assert_not_awaited()


@pytest.mark.parametrize("terminal", [ErrorEvent(code="PROVIDER_FAILED"), ControlTerminalEvent()])
async def test_failure_or_cancel_keeps_actual_files_without_success_version(
    workspace_turn, terminal
):
    env = workspace_turn
    env.working.entry.write_text("<h1>Incomplete edit remains</h1>")
    events = [
        event async for event in with_working_versions(_events(terminal, DoneEvent()), **env.scope)
    ]
    assert events[0] is terminal
    assert len(await env.service.list_revisions(env.working.document_id)) == 1
    assert "Incomplete edit" in env.working.entry.read_text()


async def test_consumer_close_closes_underlying_stream_without_snapshot(workspace_turn):
    env = workspace_turn
    closed = False

    async def source():
        nonlocal closed
        try:
            yield TextDeltaEvent(text="Still working")
            yield DoneEvent()
        finally:
            closed = True

    wrapped = with_working_versions(source(), **env.scope)
    await anext(wrapped)
    await wrapped.aclose()
    assert closed
    assert len(await env.service.list_revisions(env.working.document_id)) == 1


async def test_save_error_is_visible_and_preserves_files(workspace_turn, monkeypatch):
    env = workspace_turn
    env.working.entry.write_text("<h1>Unsaved edit</h1>")

    def fail(*_args, **_kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(ArtifactStore, "publish_bundle", fail)
    events = [event async for event in with_working_versions(_events(DoneEvent()), **env.scope)]
    assert [event.kind for event in events] == ["done", "error"]
    assert events[1].code == "WORKING_VERSION_SAVE_FAILED"
    assert len(await env.service.list_revisions(env.working.document_id)) == 1
    assert env.working.entry.read_text() == "<h1>Unsaved edit</h1>"


async def test_finalizer_exception_after_done_does_not_publish(workspace_turn):
    env = workspace_turn
    env.working.entry.write_text("<h1>Actual edit survives</h1>")

    async def source():
        yield DoneEvent(text="Generated")
        raise OSError("synthetic finalizer failure")

    wrapped = with_working_versions(source(), **env.scope)
    assert (await anext(wrapped)).kind == "done"
    with pytest.raises(OSError, match="synthetic finalizer"):
        await anext(wrapped)
    assert len(await env.service.list_revisions(env.working.document_id)) == 1
    assert env.working.entry.read_text() == "<h1>Actual edit survives</h1>"


async def test_notification_failure_does_not_misreport_committed_version(workspace_turn):
    env = workspace_turn
    env.working.entry.write_text("<h1>Saved version</h1>")

    async def broken_notification(_payload):
        raise ConnectionError("synthetic subscriber disconnected")

    events = [
        event
        async for event in with_working_versions(
            _events(DoneEvent()), event_emitter=broken_notification, **env.scope
        )
    ]
    assert [event.kind for event in events] == ["done"]
    assert len(await env.service.list_revisions(env.working.document_id)) == 2


async def test_actual_preview_tracks_css_bytes_and_bundle_etag(workspace_turn):
    env = workspace_turn
    lease, token = env.preview.create(
        artifact_id=env.initial.revision.artifact_id,
        session_id=env.session.session_id,
        session_key=KEY,
        mode="offline",
        client="desktop",
    )
    app = create_artifact_preview_resource_app(env.preview)
    origin = f"http://p-{token}.localhost:28761"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        before = await client.head("/index.html")
        assert before.status_code == 200
        assert before.headers["x-opensquilla-working-preview"] == "1"
        assert before.content == b""
        (env.working.root / "style.css").write_text("h1{color:green}")
        after = await client.head("/index.html")
        css = await client.get("/style.css")
        assert after.headers["etag"] != before.headers["etag"]
        assert css.text == "h1{color:green}"
        assert (await client.get("/%2e%2e/private.txt")).status_code == 404
    assert len(await env.service.list_revisions(env.working.document_id)) == 1
    old = env.store.resolve_preview_resource(
        env.initial.revision.artifact_id,
        session_id=env.session.session_id,
        logical_path="style.css",
    )
    assert old.path.read_text() == "h1{color:navy}"


@pytest.mark.parametrize("save_fails", (False, True), ids=("saved", "save-failed-after-done"))
async def test_task_runtime_settles_only_after_working_version_result(
    workspace_turn,
    monkeypatch,
    save_fails,
):
    import asyncio

    from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
    from opensquilla.gateway.boot import dispatch_task_runtime_turn
    from opensquilla.gateway.routing import build_cli_route_envelope
    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.session.models import AgentTaskStatus

    env = workspace_turn
    after_done, release_finalizer = asyncio.Event(), asyncio.Event()
    done_observed = asyncio.Event()
    emitted = []
    observations = []
    manager = env.scope["session_manager"]
    changed_css = "h1{color:darkgreen}"

    class Runner:
        async def run(self, _message, _session_key, **_kwargs):
            (env.working.root / "style.css").write_text(changed_css)
            yield DoneEvent(text="Updated")
            after_done.set()
            await release_finalizer.wait()
            await manager.append_message(KEY, role="assistant", content="Updated")

    async def emit(_session_key, event_name, payload):
        emitted.append((event_name, payload))
        if event_name in {"session.event.done", TURN_COMMITTED_EVENT}:
            revisions = await env.service.list_revisions(env.working.document_id)
            observations.append((event_name, len(revisions)))
            if event_name == "session.event.done":
                done_observed.set()
        if event_name == TURN_COMMITTED_EVENT:
            durable = await env.storage.get_agent_task(payload["task_id"])
            assert durable.status == AgentTaskStatus.SUCCEEDED

    async def run_turn(run):
        await dispatch_task_runtime_turn(
            run,
            config=env.scope["config"],
            session_manager=manager,
            turn_runner=Runner(),
            event_emitter=emit,
        )

    if save_fails:
        def fail_save(*_args, **_kwargs):
            raise OSError("synthetic version storage failure")

        monkeypatch.setattr(ArtifactStore, "publish_bundle", fail_save)

    runtime = TaskRuntime(
        storage=env.storage,
        turn_handler=run_turn,
        event_emitter=emit,
        running_heartbeat_interval_s=None,
    )
    envelope = build_cli_route_envelope(
        session_key=KEY, agent_id="main", session_id=env.session.session_id
    )
    assert "generated_artifact_adopter" not in envelope.runtime_services
    handle = await runtime.enqueue(envelope, "Update the heading color")
    try:
        await asyncio.wait_for(after_done.wait(), timeout=2)
        await asyncio.wait_for(done_observed.wait(), timeout=2)
        assert observations == [("session.event.done", 1)]
        running = await env.storage.get_agent_task(handle.task_id)
        assert running.status == AgentTaskStatus.RUNNING
        assert TURN_COMMITTED_EVENT not in [name for name, _ in emitted]
        release_finalizer.set()
        record = await runtime.wait(handle.task_id, timeout=2)
    finally:
        release_finalizer.set()
        await runtime.shutdown(cancel=False, timeout=2)

    names = [name for name, _ in emitted]
    durable = await env.storage.get_agent_task(handle.task_id)
    assert durable.status == record.status
    if save_fails:
        assert record.status == AgentTaskStatus.FAILED
        assert record.error_class == "WORKING_VERSION_SAVE_FAILED"
        assert "task.failed" in names
        assert TURN_COMMITTED_EVENT not in names
        assert "session.event.artifact_state" not in names
        assert names.index("session.event.done") < names.index("session.event.error")
        error = next(payload for name, payload in emitted if name == "session.event.error")
        assert error["code"] == "WORKING_VERSION_SAVE_FAILED"
        assert len(await env.service.list_revisions(env.working.document_id)) == 1
    else:
        assert record.status == AgentTaskStatus.SUCCEEDED
        assert observations == [("session.event.done", 1), (TURN_COMMITTED_EVENT, 2)]
        assert names.count(TURN_COMMITTED_EVENT) == 1
        assert "session.event.error" not in names
        assert names.count("session.event.artifact_state") == 1
        assert names.index("session.event.artifact_state") < names.index(TURN_COMMITTED_EVENT)
        artifact_state = next(
            payload for name, payload in emitted if name == "session.event.artifact_state"
        )
        head = await env.service.get_document_head(env.working.document_id)
        assert artifact_state["documentId"] == env.working.document_id
        assert artifact_state["revisionId"] == head.revision.revision_id
        assert artifact_state["revisionId"] != env.initial.revision.revision_id
        assert artifact_state["action"] == "revision.committed"
    assert (env.working.root / "style.css").read_text() == changed_css


async def test_success_after_restore_preserves_high_water_and_parent(workspace_turn):
    from unittest.mock import AsyncMock

    env = workspace_turn
    (env.working.root / "style.css").write_text("h1{color:crimson}")
    assert [
        event.kind
        async for event in with_working_versions(_events(DoneEvent(text="Updated")), **env.scope)
    ] == ["done"]
    second = await env.service.get_document_head(env.working.document_id)
    assert second.revision.generation == 2
    restored = await env.service.restore_revision(
        document_id=env.working.document_id,
        target_revision_id=env.initial.revision.revision_id,
        expected_head_revision_id=second.revision.revision_id,
        expected_state_revision=second.document.state_revision,
        actor=Actor(ActorKind.USER, "test-user"),
    )
    assert restored.revision == env.initial.revision
    assert restored.document.generation == 2
    binding = await ensure_working_files(
        env.service,
        env.store,
        document_id=env.working.document_id,
        session_key=KEY,
        session_id=env.session.session_id,
        workspace=env.scope["workspace"],
    )
    assert (binding.root / "style.css").read_text() == "h1{color:navy}"
    before = await env.service.get_document(env.working.document_id)
    audit = await env.service.list_audit_events(env.working.document_id)
    emitted = AsyncMock()
    assert [
        event.kind
        async for event in with_working_versions(
            _events(DoneEvent(text="Explanation only")), event_emitter=emitted, **env.scope
        )
    ] == ["done"]
    assert await env.service.get_document(env.working.document_id) == before
    assert await env.service.list_audit_events(env.working.document_id) == audit
    emitted.assert_not_awaited()
    (binding.root / "style.css").write_text("h1{color:green}")
    assert [
        event.kind
        async for event in with_working_versions(
            _events(DoneEvent(text="Changed after restore")), event_emitter=emitted, **env.scope
        )
    ] == ["done"]
    head = await env.service.get_document_head(env.working.document_id)
    revisions = await env.service.list_revisions(env.working.document_id)
    assert [revision.generation for revision in revisions] == [3, 2, 1]
    assert head.revision.generation == 3
    assert head.revision.parent_revision_id == env.initial.revision.revision_id
    assert revisions[1] == second.revision
    emitted.assert_awaited_once()
    assert emitted.call_args.args[0]["revisionId"] == head.revision.revision_id
