"""Office file/SQLite crash boundaries, using isolated databases and workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifact_session import Actor, ActorKind, ArtifactSessionService
from opensquilla.artifact_session import office_materialization as disk
from opensquilla.artifact_session import working_files as wf
from opensquilla.artifact_session.errors import ArtifactConflictError, ArtifactValidationError
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.document_resource_recovery import reconcile_pending_document_resources
from opensquilla.gateway.page_context import resolve_page_context
from opensquilla.gateway.rpc import RpcContext
from opensquilla.office_format import OFFICE_MIMES
from opensquilla.paths import native_io_path
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.test_gateway.test_office_working_versions import office_env as office_env
from tests.test_gateway.test_rpc_workbench_resources import (
    SESSION_KEY,
    _append_attachment,
    _dispatch,
)
from tests.test_office_format import office_package


@pytest.fixture
async def resource_env(tmp_path):
    storage = SessionStorage(str(tmp_path / "office.db"))
    await storage.connect()
    media = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media)
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(media), persist_transcripts=True),
        state_dir=str(tmp_path / "state"),
        config_path=None,
        workspace_dir=str(tmp_path / "workspace"),
    )
    try:
        yield SimpleNamespace(
            storage=storage,
            manager=manager,
            session=session,
            store=ArtifactStore(media),
            config=config,
            ctx=RpcContext(conn_id="office-recovery", session_manager=manager, config=config),
        )
    finally:
        await storage.close()


def scope(e):
    return {key: e.scope[key] for key in ("session_key", "session_id", "workspace")}


async def prepare(e):
    await wf.prepare_office_candidate(e.service, e.env.store, e.candidate, **scope(e))
    e.path.write_bytes(office_package(text="Updated candidate"))


async def commit(e):
    return await wf.commit_office_candidate(
        e.service,
        e.env.store,
        e.candidate,
        actor_id="recovery-test",
        **scope(e),
    )


async def restore(e, turn="restore-test"):
    head = await e.service.get_document_head(e.candidate["documentId"])
    return await wf.restore_working_revision(
        e.service,
        e.env.store,
        document_id=e.candidate["documentId"],
        session_key=e.scope["session_key"],
        session_id=e.scope["session_id"],
        target_revision_id=head.revision.revision_id,
        expected_head_revision_id=head.revision.revision_id,
        expected_state_revision=head.document.state_revision,
        actor=Actor(kind=ActorKind.USER, actor_id="restore-user"),
        turn_id=turn,
    )


async def intents(e):
    async with e.service.repository._read_transaction("test.office.intent") as conn:
        cursor = await conn.execute("SELECT * FROM artifact_office_materializations")
        return [dict(row) for row in await cursor.fetchall()]


@pytest.mark.parametrize("operation", ["commit", "restore", "first-bind"])
async def test_slow_office_stage_releases_shared_database_gate(office_env, monkeypatch, operation):
    e = office_env
    await prepare(e)
    if operation == "restore":
        e.binding.entry.write_bytes(office_package(text="External editable bytes"))
    if operation == "first-bind":
        async with e.service.repository._transaction("test.reset.binding") as conn:
            await conn.execute(
                "DELETE FROM artifact_working_files WHERE document_id=?",
                (e.candidate["documentId"],),
            )
        e.binding.entry.unlink()
    entered, release = threading.Event(), threading.Event()
    original = disk._write

    def slow_write(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(disk, "_write", slow_write)
    if operation == "commit":
        task = asyncio.create_task(commit(e))
    elif operation == "restore":
        task = asyncio.create_task(restore(e))
    else:
        task = asyncio.create_task(
            wf.ensure_working_files(
                e.service,
                e.env.store,
                document_id=e.candidate["documentId"],
                **scope(e),
            )
        )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await asyncio.wait_for(e.service.get_document_head(e.candidate["documentId"]), 1)
        async with e.env.storage._write_transaction(
            "test.unrelated.writer", budget_seconds=0.5
        ) as conn:
            await conn.execute("SELECT 1")
    finally:
        release.set()
        await task
    assert not await intents(e)


@pytest.mark.parametrize("tamper", ["stage", "canonical"])
async def test_changes_during_reservation_cannot_be_committed_or_strand_intent(
    office_env, monkeypatch, tamper
):
    e = office_env
    await prepare(e)
    original_bytes = e.binding.entry.read_bytes()
    external = office_package(text="Independent external edit")
    reserve = wf._reserve_office_materialization

    async def mutate_after_reserve(service, head, binding, plan, **kwargs):
        await reserve(service, head, binding, plan, **kwargs)
        (plan.member("new") if tamper == "stage" else binding.entry).write_bytes(external)

    monkeypatch.setattr(wf, "_reserve_office_materialization", mutate_after_reserve)
    with pytest.raises(ArtifactConflictError, match="changed"):
        await commit(e)
    assert e.binding.entry.read_bytes() == (original_bytes if tamper == "stage" else external)
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    assert not await intents(e)
    monkeypatch.setattr(wf, "_reserve_office_materialization", reserve)
    await restore(e)
    assert e.binding.entry.read_bytes() == original_bytes


@pytest.mark.parametrize("cancel_at", ["stage", "replace"])
async def test_cancel_settles_disk_worker_before_unlock_and_rolls_back(
    office_env, monkeypatch, cancel_at
):
    e = office_env
    await prepare(e)
    before = e.binding.entry.read_bytes()
    entered, release = threading.Event(), threading.Event()
    owner, name = (disk, "_write") if cancel_at == "stage" else (wf, "apply_office_file")
    original = getattr(owner, name)

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(owner, name, blocked)
    task = asyncio.create_task(commit(e))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert e.binding.entry.read_bytes() == before
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == 1
    assert not await intents(e)


async def crash_commit(e, tmp_path, *, after_commit):
    await prepare(e)
    pin = tmp_path / "crash-input.json"
    pin.write_text(
        json.dumps(
            dict(
                scope(e),
                candidate=e.candidate,
                db=str(tmp_path / "office.db"),
                media=str(e.env.store.media_root),
                after_commit=after_commit,
            )
        ),
        encoding="utf-8",
    )
    child = tmp_path / "crash.py"
    child.write_text(
        """import asyncio,json,os,sys
from opensquilla.session.storage import SessionStorage
from opensquilla.artifact_session import ArtifactSessionService,working_files as wf
from opensquilla.artifacts import ArtifactStore
async def main():
    p=json.load(open(sys.argv[1], encoding="utf-8"))
    storage=SessionStorage(p.pop("db")); await storage.connect()
    service=await ArtifactSessionService.from_session_storage(storage)
    store=ArtifactStore(p.pop("media")); candidate=p.pop("candidate")
    if p.pop("after_commit"):
        async def crash(*args): os._exit(77)
        wf._finish_office_materialization=crash
    else:
        original=wf._apply_office_materialization
        async def crash(*args):
            await original(*args)
            os._exit(76)
        wf._apply_office_materialization=crash
    await wf.commit_office_candidate(service,store,candidate,actor_id="crash-test",**p)
asyncio.run(main())
""",
        encoding="utf-8",
    )
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(child), str(pin)],
        capture_output=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.returncode == (77 if after_commit else 76), process.stderr.decode(
        errors="replace"
    )


@pytest.mark.parametrize("after_commit", [False, True])
async def test_actual_process_exit_recovers_from_durable_receipt(
    office_env, tmp_path, after_commit
):
    e = office_env
    original = e.binding.entry.read_bytes()
    await crash_commit(e, tmp_path, after_commit=after_commit)
    changed = e.path.read_bytes()
    assert e.binding.entry.read_bytes() == changed != original
    assert len(await intents(e)) == 1
    await reconcile_pending_document_resources(e.service, e.env.store)
    expected = changed if after_commit else original
    assert e.binding.entry.read_bytes() == expected
    assert len(await e.service.list_revisions(e.candidate["documentId"])) == (
        2 if after_commit else 1
    )
    assert not await intents(e)
    assert await wf.reconcile_office_materializations(e.service, e.env.store) == (0, 0)
    if not after_commit:
        await wf.prepare_office_candidate(e.service, e.env.store, e.candidate, **scope(e))
        assert await commit(e) is not None


async def leave_pending(e, monkeypatch, *, applied):
    await prepare(e)
    finish = wf._finish_office_materialization
    apply = wf._apply_office_materialization

    async def no_finish(*args):
        return None

    async def fail_after_apply(*args):
        await apply(*args)
        raise OSError("simulated exit before SQL commit")

    with monkeypatch.context() as patch:
        patch.setattr(wf, "_finish_office_materialization", no_finish)
        if not applied:
            patch.setattr(wf, "_apply_office_materialization", fail_after_apply)
            with pytest.raises(OSError):
                await commit(e)
        else:
            await commit(e)
    assert wf._finish_office_materialization is finish
    assert len(await intents(e)) == 1


@pytest.mark.parametrize("entrypoint", ["ensure", "prepare", "commit"])
@pytest.mark.parametrize("wrong_scope", ["session", "workspace"])
async def test_unauthorized_caller_cannot_trigger_recovery(
    office_env, monkeypatch, entrypoint, wrong_scope
):
    e = office_env
    await leave_pending(e, monkeypatch, applied=False)
    changed = e.binding.entry.read_bytes()
    args = scope(e)
    args["session_key" if wrong_scope == "session" else "workspace"] += "-other"
    with pytest.raises((ArtifactValidationError, ArtifactConflictError)):
        if entrypoint == "ensure":
            await wf.ensure_working_files(
                e.service, e.env.store, document_id=e.candidate["documentId"], **args
            )
        elif entrypoint == "prepare":
            await wf.prepare_office_candidate(e.service, e.env.store, e.candidate, **args)
        else:
            await wf.commit_office_candidate(
                e.service, e.env.store, e.candidate, actor_id="unauthorized", **args
            )
    assert e.binding.entry.read_bytes() == changed
    assert len(await intents(e)) == 1
    await wf.ensure_working_files(
        e.service, e.env.store, document_id=e.candidate["documentId"], **scope(e)
    )
    assert not await intents(e)


@pytest.mark.parametrize("applied", [False, True])
async def test_external_change_after_crash_is_preserved_until_explicit_restore(
    office_env, monkeypatch, applied
):
    e = office_env
    original = e.binding.entry.read_bytes()
    await leave_pending(e, monkeypatch, applied=applied)
    changed = e.path.read_bytes()
    external = office_package(text="External content after crash")
    e.binding.entry.write_bytes(external)
    row = (await intents(e))[0]
    plan = disk.OfficeFilePlan.from_dict(json.loads(row["plan_json"]))
    (plan.directory / "unknown.txt").write_text("Do not delete", encoding="utf-8")
    assert await wf.reconcile_office_materializations(e.service, e.env.store) == (0, 1)
    assert e.binding.entry.read_bytes() == external
    await restore(e)
    assert e.binding.entry.read_bytes() == (changed if applied else original)
    assert (plan.directory / "unknown.txt").read_text() == "Do not delete"
    assert plan.member("old").exists()
    backups = list((Path(e.binding.workspace) / "artifacts/.office-materializations").rglob("old"))
    assert any(path.read_bytes() == external for path in backups)
    assert not await intents(e)


@pytest.mark.parametrize("state", ["old", "missing"])
async def test_applied_receipt_recovers_missing_or_original_canonical(
    office_env, monkeypatch, state
):
    e = office_env
    old = e.binding.entry.read_bytes()
    await leave_pending(e, monkeypatch, applied=True)
    changed = e.path.read_bytes()
    if state == "old":
        e.binding.entry.write_bytes(old)
    else:
        e.binding.entry.unlink()
    assert await wf.reconcile_office_materializations(e.service, e.env.store) == (1, 0)
    assert e.binding.entry.read_bytes() == changed
    assert not await intents(e)


async def test_noop_restore_does_not_rewrite_or_keep_full_backups(office_env, monkeypatch):
    e = office_env
    before = e.binding.entry.stat()

    def forbidden(*args, **kwargs):
        raise AssertionError("no-op restore must not stage or replace bytes")

    monkeypatch.setattr(wf, "stage_office_file", forbidden)
    monkeypatch.setattr(wf, "apply_office_file", forbidden)
    await restore(e)
    after = e.binding.entry.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert not await intents(e)


@pytest.mark.parametrize("applied", [False, True])
def test_edit_during_recovery_read_is_not_overwritten(tmp_path, monkeypatch, applied):
    target = tmp_path / "file.docx"
    old, new, external = b"old", b"new", b"external"
    target.write_bytes(old)
    plan = disk.stage_office_file(
        document_id="doc",
        workspace=str(tmp_path.resolve()),
        target=target.name,
        payload=new,
        expected_sha256=hashlib.sha256(old).hexdigest(),
        allow_existing=True,
    )
    disk.apply_office_file(plan)
    if applied:
        target.write_bytes(old)
    read = disk.read_office_file

    def edit_after_read(path):
        result = read(path)
        if path == target:
            target.write_bytes(external)
        return result

    monkeypatch.setattr(disk, "read_office_file", edit_after_read)
    with pytest.raises(ArtifactConflictError):
        disk.reconcile_office_file(plan, applied=applied, committed_payload=lambda: new)
    assert target.read_bytes() == external


async def test_first_bind_interrupted_after_replace_recovers_before_reopen(office_env, monkeypatch):
    e = office_env
    original = e.binding.entry.read_bytes()
    async with e.service.repository._transaction("test.unbind") as conn:
        await conn.execute(
            "DELETE FROM artifact_working_files WHERE document_id=?", (e.candidate["documentId"],)
        )
    e.binding.entry.unlink()
    apply = wf._apply_office_materialization

    async def fail_after_apply(*args):
        await apply(*args)
        raise OSError("crash at first bind")

    async def no_finish(*args):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(wf, "_apply_office_materialization", fail_after_apply)
        patch.setattr(wf, "_finish_office_materialization", no_finish)
        with pytest.raises(OSError):
            await wf.ensure_working_files(
                e.service, e.env.store, document_id=e.candidate["documentId"], **scope(e)
            )
    assert await wf.get_working_files(e.service, e.candidate["documentId"]) is None
    assert e.binding.entry.read_bytes() == original
    assert await wf.reconcile_office_materializations(e.service, e.env.store) == (1, 0)
    assert not e.binding.entry.exists()
    reopened = await wf.ensure_working_files(
        e.service, e.env.store, document_id=e.candidate["documentId"], **scope(e)
    )
    assert reopened.entry.read_bytes() == original
    assert not await intents(e)


async def test_restore_interrupted_after_replace_recovers_previous_head(office_env, monkeypatch):
    e = office_env
    original = e.binding.entry.read_bytes()
    await prepare(e)
    changed = e.path.read_bytes()
    head = await commit(e)
    apply = wf._apply_office_materialization

    async def fail_after_apply(*args):
        await apply(*args)
        raise OSError("crash at restore")

    async def no_finish(*args):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(wf, "_apply_office_materialization", fail_after_apply)
        patch.setattr(wf, "_finish_office_materialization", no_finish)
        with pytest.raises(OSError):
            await wf.restore_working_revision(
                e.service,
                e.env.store,
                document_id=e.candidate["documentId"],
                session_key=e.scope["session_key"],
                session_id=e.scope["session_id"],
                target_revision_id=e.candidate["revisionId"],
                expected_head_revision_id=head.revision.revision_id,
                expected_state_revision=head.document.state_revision,
                actor=Actor(kind=ActorKind.USER, actor_id="restore-test"),
                turn_id="restore-crash",
            )
    assert e.binding.entry.read_bytes() == original
    assert await wf.reconcile_office_materializations(e.service, e.env.store) == (1, 0)
    assert e.binding.entry.read_bytes() == changed
    assert (await e.service.get_document_head(e.candidate["documentId"])).revision == head.revision


async def test_display_rename_cannot_bypass_pending_office_recovery(office_env, monkeypatch):
    e = office_env
    original = e.binding.entry.read_bytes()
    await leave_pending(e, monkeypatch, applied=False)
    head = await e.service.get_document_head(e.candidate["documentId"])
    await e.service.rename_document(
        document_id=e.candidate["documentId"],
        expected_state_revision=head.document.state_revision,
        name="renamed.txt",
        actor=Actor(kind=ActorKind.USER, actor_id="rename-user"),
    )
    await wf.ensure_working_files(
        e.service, e.env.store, document_id=e.candidate["documentId"], **scope(e)
    )
    assert e.binding.entry.read_bytes() == original
    assert not await intents(e)


def test_stage_integrity_is_checked_before_recording_fingerprint(tmp_path, monkeypatch):
    write = disk._write

    def tamper_after_write(path, payload):
        write(path, payload)
        if path.name == "new":
            path.write_bytes(b"tampered")

    monkeypatch.setattr(disk, "_write", tamper_after_write)
    with pytest.raises(ArtifactConflictError, match="changed while preparing"):
        disk.stage_office_file(
            document_id="doc",
            workspace=str(tmp_path.resolve()),
            target="file.docx",
            payload=b"original",
            expected_sha256=None,
            allow_existing=False,
        )
    assert not (tmp_path / "file.docx").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows long Office staging path regression")
@pytest.mark.parametrize("format", ["docx", "xlsx", "pptx"])
async def test_long_office_staging_first_bind_recovers_sqlite_receipt(
    resource_env, monkeypatch, format
):
    env = resource_env
    workspace = Path(env.config.workspace_dir)
    # Keep the canonical target below MAX_PATH while the durable staging path
    # crosses it, as in the annotation incident. The disk tests also cover an
    # entire workspace beyond MAX_PATH.
    workspace /= "w" * max(1, 180 - len(str(workspace)) - 1)
    native_io_path(workspace).mkdir(parents=True)
    env.config.workspace_dir = str(workspace)
    payload = office_package(format)
    attachment = await _append_attachment(
        env,
        message_id="long-office",
        name=f"Report.{format}",
        mime=OFFICE_MIMES[format],
        payload=payload,
        staged=True,
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    original_apply = wf._apply_office_materialization

    async def fail_after_apply(*args):
        await original_apply(*args)
        raise OSError("interrupted first bind after replacement")

    async def no_finish(*args):
        pass

    params = {
        "sessionKey": SESSION_KEY,
        "resource": {"type": "attachment", "id": attachment["attachment_id"]},
        "idempotencyKey": "long-office-open",
        "expectedSha256": attachment["sha256_ref"],
    }
    try:
        with monkeypatch.context() as patch:
            patch.setattr(wf, "_apply_office_materialization", fail_after_apply)
            patch.setattr(wf, "_finish_office_materialization", no_finish)
            failed = await _dispatch(env, "workbench.resources.open", params)
        assert failed.error is not None
        async with service.repository._read_transaction("test.long.office.receipt") as conn:
            cursor = await conn.execute("SELECT * FROM artifact_office_materializations")
            rows = await cursor.fetchall()
        assert len(rows) == 1
        plan = disk.OfficeFilePlan.from_dict(json.loads(rows[0]["plan_json"]))
        assert len(str(plan.directory)) > 260
        assert disk.read_office_file(plan.entry) == payload
        assert await wf.get_working_files(service, plan.document_id) is None
        assert await wf.reconcile_office_materializations(service, env.store) == (1, 0)
        assert not native_io_path(plan.entry).exists()
        assert not native_io_path(plan.directory).exists()
        reopened = await _dispatch(env, "workbench.resources.open", params)
        assert reopened.error is None, reopened.error
        binding = await wf.get_working_files(service, plan.document_id)
        assert binding is not None
        assert disk.read_office_file(binding.entry) == payload
        assert not str(binding.entry).startswith("\\\\?\\")
        assert await wf.reconcile_office_materializations(service, env.store) == (0, 0)
        resolved = await resolve_page_context(
            reopened.payload["pageContext"],
            env.ctx,
            session_key=SESSION_KEY,
            session_id=env.session.session_id,
            workspace=str(workspace),
            candidate_id="long-office-turn",
        )
        candidate = resolved["_officeCandidate"]
        candidate_scope = {
            "session_key": SESSION_KEY,
            "session_id": env.session.session_id,
            "workspace": str(workspace),
        }
        candidate_path = await wf.prepare_office_candidate(
            service, env.store, candidate, **candidate_scope
        )
        assert len(str(candidate_path)) > 260
        assert not str(candidate_path).startswith("\\\\?\\")
        assert disk.read_office_file(candidate_path) == payload
        # A ZIP comment changes bytes without changing the package's validity
        # or depending on a particular Office document/slide content fixture.
        edited = io.BytesIO(payload)
        with zipfile.ZipFile(edited, "a") as archive:
            archive.comment = b"long-path candidate edit"
        changed = edited.getvalue()
        native_io_path(candidate_path).write_bytes(changed)
        repeated = await wf.prepare_office_candidate(
            service, env.store, candidate, **candidate_scope
        )
        assert repeated == candidate_path
        assert disk.read_office_file(repeated) == changed
        committed = await wf.commit_office_candidate(
            service, env.store, candidate, actor_id="long-office-turn", **candidate_scope
        )
        assert committed is not None
        assert disk.read_office_file(binding.entry) == changed
        assert await wf.commit_office_candidate(
            service, env.store, candidate, actor_id="long-office-turn", **candidate_scope
        ) is None
        sibling = native_io_path(candidate_path.parent / "unknown.txt")
        sibling.write_bytes(b"retain")
        await wf.discard_office_candidate(service, candidate, workspace=str(workspace))
        await wf.discard_office_candidate(service, candidate, workspace=str(workspace))
        assert not native_io_path(candidate_path).exists()
        assert sibling.read_bytes() == b"retain"
        assert await wf.reconcile_office_materializations(service, env.store) == (0, 0)
    finally:
        await service.close()
