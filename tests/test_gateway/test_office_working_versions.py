from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifact_session import Actor, ActorKind, ArtifactBlobRef, ArtifactSessionService
from opensquilla.artifact_session.working_files import (
    commit_office_candidate,
    get_working_files,
    prepare_office_candidate,
    save_working_version,
)
from opensquilla.artifacts import ArtifactSource
from opensquilla.engine.types import ArtifactEvent, DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.gateway.page_context import normalize_page_context, resolve_page_context
from opensquilla.gateway.working_versions import with_working_versions
from opensquilla.office_format import OFFICE_MIMES
from tests.test_gateway.test_rpc_workbench_resources import (
    SESSION_KEY,
    _append_attachment,
    _dispatch,
)
from tests.test_gateway.test_rpc_workbench_resources import (
    resource_env as resource_env,
)
from tests.test_office_format import office_package


@pytest.fixture
async def office_env(resource_env):
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="office",
        name="Report.docx",
        mime=OFFICE_MIMES["docx"],
        payload=office_package(),
        staged=True,
    )
    response = await _dispatch(
        env,
        "workbench.resources.open",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment["attachment_id"]},
            "idempotencyKey": "office-open",
            "expectedSha256": attachment["sha256_ref"],
        },
    )
    assert response.error is None, response.error
    opened = response.payload
    context = opened["pageContext"]
    resolved = await resolve_page_context(
        context,
        env.ctx,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
        candidate_id="turn-office",
    )
    candidate = resolved.pop("_officeCandidate")
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await get_working_files(service, candidate["documentId"])
    scope = dict(
        config=env.config,
        session_manager=env.manager,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
        actor_id="turn-office",
        office_candidate=candidate,
    )
    try:
        yield SimpleNamespace(
            env=env,
            opened=opened,
            context=context,
            candidate=candidate,
            service=service,
            binding=binding,
            scope=scope,
            path=Path(resolved["workingFile"]),
        )
    finally:
        await service.close()


@pytest.mark.parametrize("format", OFFICE_MIMES)
async def test_office_import_preview_and_capability_axes(resource_env, format):
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id=format,
        name=f"Report.{format}",
        mime=OFFICE_MIMES[format],
        payload=office_package(format),
        staged=True,
    )
    resource = {"type": "attachment", "id": attachment["attachment_id"]}
    response = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resource": resource,
            "mode": "isolated",
        },
    )
    assert response.error is None, response.error
    descriptor = response.payload["preview"]
    assert descriptor["mode"] == "local-renderer"
    assert descriptor["sandboxProfile"] == "none"
    assert descriptor["adapter"] == {"id": "silurus-office", "version": "0.88.0", "format": format}
    opened = await _dispatch(
        env,
        "workbench.resources.open",
        {
            "sessionKey": SESSION_KEY,
            "resource": resource,
            "idempotencyKey": format,
        },
    )
    assert opened.error is None, opened.error
    caps = opened.payload["resource"]["capabilities"]
    assert caps["preview"] and caps["agentEdit"] and caps["selectionContext"]
    assert caps["manualEdit"] is False
    assert Path(opened.payload["workingFile"]).read_bytes() == office_package(format)


async def test_candidate_created_only_at_execution_and_commits_on_success(office_env):
    e = office_env
    assert not e.path.exists()  # Admission/queueing is side-effect free for candidates.
    assert json.loads(json.dumps(e.candidate)) == e.candidate  # Durable queue payload.
    original = e.binding.entry.read_bytes()
    changed = office_package(text="修改后的中文标题")

    async def stream():
        assert e.path.read_bytes() == original
        e.path.write_bytes(changed)
        assert e.binding.entry.read_bytes() == original
        yield DoneEvent(text="Updated")

    events = [item async for item in with_working_versions(stream(), **e.scope)]
    assert not any(isinstance(item, ErrorEvent) for item in events)
    revisions = await e.service.list_revisions(e.candidate["documentId"])
    assert len(revisions) == 2
    assert e.binding.entry.read_bytes() == changed
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(e.binding.entry) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name != "word/document.xml":
                assert before.read(name) == after.read(name)
    docx = pytest.importorskip("docx")
    assert docx.Document(e.binding.entry).paragraphs[0].text == "修改后的中文标题"
    assert not e.path.exists()
    # A duplicate completion is a no-op receipt, not a second revision.
    assert (
        await commit_office_candidate(
            e.service,
            e.env.store,
            e.candidate,
            session_key=SESSION_KEY,
            session_id=e.env.session.session_id,
            workspace=e.env.config.workspace_dir,
            actor_id="retry",
        )
        is None
    )


@pytest.mark.parametrize("outcome", ["no-op", "error", "invalid", "cancel"])
async def test_failed_cancelled_invalid_and_noop_do_not_create_version(office_env, outcome):
    e = office_env
    original = e.binding.entry.read_bytes()

    async def stream():
        if outcome != "no-op":
            e.path.write_bytes(
                b"invalid" if outcome == "invalid" else office_package(text="Changed")
            )
        if outcome == "cancel":
            yield TextDeltaEvent(text="Working")
        elif outcome == "error":
            yield ErrorEvent(code="TEST", message="failed")
            yield DoneEvent(text="failed")
        else:
            yield DoneEvent(text="done")

    wrapped = with_working_versions(stream(), **e.scope)
    if outcome == "cancel":
        await anext(wrapped)
        await wrapped.aclose()
        events = []
    else:
        events = [item async for item in wrapped]
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    assert e.binding.entry.read_bytes() == original
    assert e.path.exists() == (outcome == "invalid")
    if outcome == "invalid":
        assert any(
            item.code == "WORKING_VERSION_SAVE_FAILED"
            for item in events
            if isinstance(item, ErrorEvent)
        )


async def advance_head(e):
    head = await e.service.get_document_head(e.candidate["documentId"])
    ref = e.env.store.publish_bytes(
        office_package(text="Concurrent"),
        session_id=e.env.session.session_id,
        session_key=SESSION_KEY,
        name="Report.docx",
        mime=OFFICE_MIMES["docx"],
        source="test",
    )
    return await e.service.commit_revision(
        document_id=head.document.document_id,
        expected_head_revision_id=head.revision.revision_id,
        expected_state_revision=head.document.state_revision,
        artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "other"),
    )


@pytest.mark.parametrize("when", ["admission", "execution", "save"])
async def test_selection_pin_rejects_stale_at_each_boundary(office_env, when):
    e = office_env
    executed = False

    async def stream():
        nonlocal executed
        executed = True
        e.path.write_bytes(office_package(text="Candidate"))
        if when == "save":
            await advance_head(e)
        yield DoneEvent(text="done")

    if when != "save":
        await advance_head(e)
    if when == "admission":
        with pytest.raises(Exception):
            await resolve_page_context(
                e.context,
                e.env.ctx,
                session_key=SESSION_KEY,
                session_id=e.env.session.session_id,
                workspace=e.env.config.workspace_dir,
                candidate_id="stale-turn",
            )
    else:
        events = [item async for item in with_working_versions(stream(), **e.scope)]
        assert any(isinstance(item, ErrorEvent) for item in events)
        assert executed == (when == "save")
    head = await e.service.get_document_head(e.candidate["documentId"])
    assert head.revision.revision_id != e.candidate["revisionId"]
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 2


async def test_publishing_candidate_does_not_bypass_turn_commit(office_env):
    e = office_env
    await prepare_office_candidate(
        e.service,
        e.env.store,
        e.candidate,
        session_key=SESSION_KEY,
        session_id=e.env.session.session_id,
        workspace=e.env.config.workspace_dir,
    )
    e.path.write_bytes(office_package(text="Published early"))
    ref = e.env.store.publish_file(
        e.path,
        session_id=e.env.session.session_id,
        session_key=SESSION_KEY,
        name="Report.docx",
        mime=OFFICE_MIMES["docx"],
        source="test",
    )
    # No office_candidate object: also covers recovered runtime/adopter instances.
    adopter = GeneratedArtifactAdopter(
        e.service,
        e.env.store,
        SESSION_KEY,
        e.env.session.session_id,
        workspace=e.env.config.workspace_dir,
        source_paths={
            "early": ArtifactSource(artifact_id=ref.id, path=str(e.path), bundle_mode="none")
        },
    )
    await adopter(ArtifactEvent(**ref.to_dict(), publication_id="early"))
    assert (
        len(
            await e.service.list_documents(
                session_key=SESSION_KEY, session_id=e.env.session.session_id
            )
        )
        == 1
    )
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    # Ordinary autosave cannot silently bless a changed Office canonical file either.
    e.binding.entry.write_bytes(office_package(text="Bypass attempt"))
    assert (
        await save_working_version(
            e.service,
            e.env.store,
            document_id=e.candidate["documentId"],
            session_key=SESSION_KEY,
            session_id=e.env.session.session_id,
            actor_id="bypass",
        )
        is None
    )
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1


@pytest.mark.parametrize(
    "selection",
    [
        {"format": "pptx", "kind": "text", "elementIndex": 0},
        {"format": "docx", "kind": "range"},
        {"format": "xlsx", "kind": "text"},
        {"format": "pptx", "kind": "element", "bounds": {"x": float("nan")}},
        {"format": "docx", "kind": "text", "source": {"story": "body", "path": [-1]}},
    ],
)
def test_selection_rejects_write_handles_unbounded_and_invalid_data(selection):
    with pytest.raises(ValueError):
        normalize_page_context({"annotations": [{"text": "Change", "selection": selection}]})


async def test_selected_document_is_saved_outside_inventory_page(office_env, monkeypatch):
    e = office_env

    async def empty_inventory(*args, **kwargs):
        return ()

    monkeypatch.setattr(ArtifactSessionService, "list_documents", empty_inventory)

    async def stream():
        e.path.write_bytes(office_package(text="Outside page"))
        yield DoneEvent(text="done")

    events = [item async for item in with_working_versions(stream(), **e.scope)]
    assert not any(isinstance(item, ErrorEvent) for item in events)
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 2


async def test_materialization_failure_rolls_back_head_and_retains_candidate(
    office_env, monkeypatch
):
    from opensquilla.artifact_session import working_files

    e = office_env
    original = e.binding.entry.read_bytes()

    async def fail_materialize(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(working_files, "_apply_office_materialization", fail_materialize)

    async def stream():
        e.path.write_bytes(office_package(text="Candidate on disk"))
        yield DoneEvent(text="done")

    events = [item async for item in with_working_versions(stream(), **e.scope)]
    assert any(isinstance(item, ErrorEvent) for item in events)
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    assert e.binding.entry.read_bytes() == original
    assert e.path.exists()


async def test_candidate_retry_preserves_draft_and_rejects_unsaved_canonical_change(office_env):
    e = office_env
    args = dict(
        session_key=SESSION_KEY,
        session_id=e.env.session.session_id,
        workspace=e.env.config.workspace_dir,
    )
    await prepare_office_candidate(e.service, e.env.store, e.candidate, **args)
    draft = office_package(text="Existing draft")
    e.path.write_bytes(draft)
    assert await prepare_office_candidate(e.service, e.env.store, e.candidate, **args) == e.path
    assert e.path.read_bytes() == draft
    e.binding.entry.write_bytes(office_package(text="Uncommitted external edit"))
    with pytest.raises(Exception, match="unsaved changes"):
        await prepare_office_candidate(e.service, e.env.store, e.candidate, **args)


def test_followup_routes_drop_candidate_without_mutating_original_recovery_record():
    from opensquilla.gateway.routing import build_cli_route_envelope
    from opensquilla.gateway.task_runtime import TaskRuntime, _reusable_route_envelope

    candidate = {"documentId": "document", "revisionId": "revision", "sha256": "a" * 64}
    envelope = build_cli_route_envelope(session_key=SESSION_KEY, session_id="session")
    envelope.metadata["office_candidate"] = candidate
    # Runtime.send, cached routes and late steer all pass through this helper.
    followup = _reusable_route_envelope(envelope)
    assert "office_candidate" not in followup.metadata
    assert envelope.metadata["office_candidate"] == candidate
    task = SimpleNamespace(
        source_kind="cli",
        task_id="original",
        agent_id="main",
        session_key=SESSION_KEY,
        details={"session_id": "session", "metadata": envelope.metadata},
    )
    entry = SimpleNamespace(session_id="session", turn_context={"revision": 1})
    promoted = TaskRuntime._restart_recovery_envelope(task, [entry])
    assert promoted is not None and "office_candidate" not in promoted.metadata
    assert task.details["metadata"]["office_candidate"] == candidate


async def test_database_failure_after_materialization_restores_canonical_file(
    office_env, monkeypatch
):
    from contextlib import asynccontextmanager

    from opensquilla.artifact_session.repository import ArtifactSessionRepository

    e = office_env
    original = e.binding.entry.read_bytes()
    transaction = ArtifactSessionRepository._transaction

    @asynccontextmanager
    async def fail_commit(repository, label):
        async with transaction(repository, label) as conn:
            yield conn
            if label == "office_candidate.commit":
                assert e.binding.entry.read_bytes() != original
                raise OSError("commit failure after file replacement")

    monkeypatch.setattr(ArtifactSessionRepository, "_transaction", fail_commit)

    async def stream():
        e.path.write_bytes(office_package(text="Uncommitted candidate"))
        yield DoneEvent(text="done")

    events = [item async for item in with_working_versions(stream(), **e.scope)]
    assert any(isinstance(item, ErrorEvent) for item in events)
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    assert e.binding.entry.read_bytes() == original
    assert e.path.exists()


async def test_preflight_cancellation_settles_write_before_cleanup(office_env, monkeypatch):
    import asyncio
    import threading

    from opensquilla.artifact_session import working_files

    e = office_env
    writing = threading.Event()
    release = threading.Event()
    original_write = working_files._write_office_candidate

    def blocked_write(*args):
        writing.set()
        assert release.wait(5)
        return original_write(*args)

    monkeypatch.setattr(working_files, "_write_office_candidate", blocked_write)
    closed = []
    original_close = ArtifactSessionService.close

    async def record_close(service):
        closed.append(service)
        await original_close(service)

    monkeypatch.setattr(ArtifactSessionService, "close", record_close)
    executed = False

    async def stream():
        nonlocal executed
        executed = True
        yield DoneEvent(text="should not execute")

    wrapped = with_working_versions(stream(), **e.scope)
    step = asyncio.create_task(anext(wrapped))
    try:
        assert await asyncio.to_thread(writing.wait, 5)
        step.cancel()
        await asyncio.sleep(0)
        assert not step.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await step
        assert not executed and not e.path.exists()
        assert closed and closed[0] is not e.service
        assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    finally:
        release.set()
        await wrapped.aclose()
