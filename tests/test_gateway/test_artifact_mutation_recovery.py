"""Restart recovery contracts for journaled artifact mutation candidates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
from opensquilla.gateway.artifact_mutation_recovery import (
    reconcile_pending_artifact_mutations,
)

USER = Actor(ActorKind.USER, "user-recovery")
AGENT = Actor(ActorKind.AGENT, "agent-recovery")
SESSION_ID = "synthetic-recovery-session"


def _blob(label: str) -> ArtifactBlobRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactBlobRef(
        artifact_id=f"artifact-{label}",
        sha256=digest,
        filename="page.html",
        media_type="text/html",
        byte_size=len(label),
    )


def _blob_from_ref(ref) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=ref.id,
        sha256=ref.sha256,
        filename=ref.name,
        media_type=ref.mime,
        byte_size=ref.size,
    )


async def _created(service: ArtifactSessionService):
    return await service.create_document(
        session_key="agent:main:webchat:synthetic-recovery",
        session_id=SESSION_ID,
        name="Synthetic recovery page",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob("base"),
        actor=USER,
    )


async def _reserve(
    service: ArtifactSessionService,
    *,
    document_id: str,
    revision_id: str,
    turn_id: str,
):
    return await service.reserve_mutation_attempt(
        document_id=document_id,
        turn_id=turn_id,
        tool_use_id=f"tool-{turn_id}",
        base_revision_id=revision_id,
        proposal_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_restart_terminalizes_reserve_only_attempt(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="reserve-only",
        )

        summary = await reconcile_pending_artifact_mutations(
            service,
            ArtifactStore(tmp_path / "media"),
        )

        assert summary.examined == 1
        assert summary.failed == 1
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="reserve-only",
            tool_use_id="tool-reserve-only",
        )
        assert receipt.status is MutationAttemptStatus.FAILED
        assert receipt.failure_code == "process_restarted_before_candidate"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_global_recovery_database_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")

    async def fail_list(**_kwargs):
        raise RuntimeError("synthetic recovery database failure")

    monkeypatch.setattr(service, "list_unresolved_mutation_attempts", fail_list)
    try:
        with pytest.raises(RuntimeError, match="recovery database failure"):
            await reconcile_pending_artifact_mutations(
                service,
                ArtifactStore(tmp_path / "media"),
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_deletes_published_candidate_without_commit(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="published-only",
        )
        payload = b"<h1>candidate</h1>"
        artifact_id = store.allocate_artifact_id()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="published-only",
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
        )
        store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            visibility="internal",
            artifact_id=artifact_id,
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.failed == 1
        assert summary.deleted_candidates == 1
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(artifact_id, session_id=SESSION_ID)
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_recovers_commit_before_attempt_terminalization(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    service = await ArtifactSessionService.open(path)
    created = await _created(service)
    await _reserve(
        service,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        turn_id="committed",
    )
    payload = b"<h1>committed</h1>"
    artifact_id = store.allocate_artifact_id()
    await service.register_mutation_candidate(
        document_id=created.document.document_id,
        turn_id="committed",
        candidate_session_id=SESSION_ID,
        candidate_artifact_id=artifact_id,
        candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
    )
    ref = store.publish_bytes(
        payload,
        session_id=SESSION_ID,
        session_key=created.document.session_key,
        name="candidate.html",
        mime="text/html",
        source="artifact_html_agent_edit",
        visibility="internal",
        artifact_id=artifact_id,
    )
    applied, change = await service.commit_change_set_atomically(
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        expected_document_state_revision=created.document.state_revision,
        operations=({"op": "replace_text"},),
        candidate_artifact=_blob_from_ref(ref),
        validation={"status": "passed"},
        actor=AGENT,
        turn_id="committed",
    )
    await service.close()

    recovered = await ArtifactSessionService.open(path)
    try:
        summary = await reconcile_pending_artifact_mutations(recovered, store)
        assert summary.applied == 1
        assert summary.deleted_candidates == 0
        receipt = await recovered.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="committed",
            tool_use_id="tool-committed",
        )
        assert receipt.status is MutationAttemptStatus.APPLIED
        assert receipt.change_set_id == change.change_set_id
        assert receipt.revision_id == applied.revision.revision_id
        assert store.resolve_for_download(artifact_id, session_id=SESSION_ID)[0] == ref
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_marks_candidate_cleanup_integrity_error_ambiguous(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="unsafe-bucket",
        )
        payload = b"<h1>candidate</h1>"
        artifact_id = store.allocate_artifact_id()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="unsafe-bucket",
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
        )
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            artifact_id=artifact_id,
        )
        (store.path_for(ref).parent / ".artifact-id").write_text(
            store.allocate_artifact_id() + "\n",
            encoding="ascii",
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.ambiguous == 1
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="unsafe-bucket",
            tool_use_id="tool-unsafe-bucket",
        )
        assert receipt.status is MutationAttemptStatus.AMBIGUOUS
        assert receipt.failure_code == "restart_candidate_cleanup_failed"
        assert store.path_for(ref).read_bytes() == payload
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["journaled", "published", "committed"])
async def test_subprocess_hard_crash_reconciles_candidate_journal(
    tmp_path: Path,
    phase: str,
) -> None:
    database = tmp_path / "sessions.db"
    media_root = tmp_path / "media"
    ready = tmp_path / "worker-ready.json"
    turn_id = f"hard-crash-{phase}"
    service = await ArtifactSessionService.open(database)
    created = await _created(service)
    await _reserve(
        service,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        turn_id=turn_id,
    )
    document_id = created.document.document_id
    await service.close()

    repository_root = Path(__file__).resolve().parents[2]
    worker = repository_root / "tests/helpers/artifact_candidate_crash_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--database",
            str(database),
            "--media-root",
            str(media_root),
            "--ready",
            str(ready),
            "--document-id",
            document_id,
            "--turn-id",
            turn_id,
            "--session-id",
            SESSION_ID,
            "--phase",
            phase,
        ],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"synthetic crash worker did not reach {phase}: {stdout}\n{stderr}"
        )
    worker_result = json.loads(ready.read_text(encoding="utf-8"))
    process.kill()
    process.communicate(timeout=5)

    recovered = await ArtifactSessionService.open(database)
    store = ArtifactStore(media_root)
    try:
        summary = await reconcile_pending_artifact_mutations(recovered, store)
        receipt = await recovered.reconcile_mutation_attempt(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=f"tool-{turn_id}",
        )
        if phase == "committed":
            assert summary.applied == 1
            assert summary.deleted_candidates == 0
            assert receipt.status is MutationAttemptStatus.APPLIED
            assert receipt.change_set_id == worker_result["change_set_id"]
            assert receipt.revision_id == worker_result["revision_id"]
            store.resolve_for_download(
                worker_result["artifact_id"],
                session_id=SESSION_ID,
            )
            assert len(await recovered.list_revisions(document_id)) == 2
            assert len(await recovered.list_change_sets(document_id)) == 1
        else:
            assert summary.failed == 1
            assert receipt.status is MutationAttemptStatus.FAILED
            with pytest.raises(ArtifactNotFoundError):
                store.resolve_for_download(
                    worker_result["artifact_id"],
                    session_id=SESSION_ID,
                )
            assert len(await recovered.list_revisions(document_id)) == 1
            assert await recovered.list_change_sets(document_id) == ()
    finally:
        await recovered.close()
