"""Repository contracts for explicit source imports and immutable publications."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    DocumentImportMode,
    DocumentSourceType,
    MutationAttemptStatus,
)


class PredictableIds:
    def __init__(self) -> None:
        self.next = 0

    def __call__(self, prefix: str) -> str:
        self.next += 1
        return f"{prefix}_{self.next}"


def blob(artifact_id: str, text: str, *, name: str = "page.html") -> ArtifactBlobRef:
    import hashlib

    payload = text.encode()
    return ArtifactBlobRef(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        filename=name,
        media_type="text/html",
        byte_size=len(payload),
    )


@pytest.mark.asyncio
async def test_generated_deliverable_adoption_is_atomic_direct_and_idempotent(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "generated-resources.db",
        id_factory=PredictableIds(),
    )
    actor = Actor(ActorKind.AGENT, "agent-main")
    deliverable = blob("art-public-html", "<h1>generated</h1>")
    try:
        async def adopt():
            return await service.adopt_generated_deliverable(
                session_key="agent:main:webchat:generated",
                session_id="session-generated",
                name=deliverable.filename,
                kind=ArtifactKind.HTML,
                deliverable=deliverable,
                actor=actor,
            )

        results = await asyncio.gather(*(adopt() for _ in range(12)))

        assert sum(created for _commit, _binding, created in results) == 1
        assert len({commit.document.document_id for commit, _binding, _ in results}) == 1
        assert len({binding.binding_id for _commit, binding, _ in results}) == 1
        first_commit, first_binding, _created = results[0]
        assert first_commit.revision.artifact_id == deliverable.artifact_id
        assert first_commit.revision.artifact_sha256 == deliverable.sha256
        assert first_binding.document_id == first_commit.document.document_id
        assert first_binding.source_type is DocumentSourceType.DELIVERABLE
        assert first_binding.source_resource_id == deliverable.artifact_id
        assert first_binding.mode is DocumentImportMode.COPY
        assert len(
            await service.list_documents(
                session_key="agent:main:webchat:generated",
                session_id="session-generated",
            )
        ) == 1

        next_head = blob("art-internal-next", "<h1>edited</h1>")
        advanced = await service.commit_revision(
            document_id=first_commit.document.document_id,
            expected_head_revision_id=first_commit.revision.revision_id,
            expected_state_revision=first_commit.document.state_revision,
            artifact=next_head,
            actor=Actor(ActorKind.USER, "local-owner"),
        )
        replay, replay_binding, created = await adopt()
        assert created is False
        assert replay.document == advanced.document
        assert replay.revision == advanced.revision
        assert replay_binding == first_binding

        with pytest.raises(ArtifactConflictError, match="head revision changed"):
            await service.get_document_head(
                first_commit.document.document_id,
                expected_revision_id=first_commit.revision.revision_id,
            )
        current = await service.get_document_head(
            first_commit.document.document_id,
            expected_revision_id=advanced.revision.revision_id,
        )
        assert current == advanced
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_generated_deliverable_adoption_binds_legacy_adopted_document(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "legacy-generated-resource.db",
        id_factory=PredictableIds(),
    )
    actor = Actor(ActorKind.AGENT, "agent-main")
    deliverable = blob("art-legacy-public-html", "<h1>legacy</h1>")
    try:
        legacy, adopted = await service.adopt_document(
            session_key="agent:main:webchat:legacy-generated",
            session_id="session-legacy-generated",
            name=deliverable.filename,
            kind=ArtifactKind.HTML,
            initial_artifact=deliverable,
            actor=actor,
        )
        assert adopted is True

        commit, binding, created = await service.adopt_generated_deliverable(
            session_key="agent:main:webchat:legacy-generated",
            session_id="session-legacy-generated",
            name=deliverable.filename,
            kind=ArtifactKind.HTML,
            deliverable=deliverable,
            actor=actor,
        )

        assert created is True
        assert commit == legacy
        assert binding.document_id == legacy.document.document_id
        assert binding.source_resource_id == deliverable.artifact_id
        assert len(
            await service.list_documents(
                session_key="agent:main:webchat:legacy-generated",
                session_id="session-legacy-generated",
            )
        ) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_copy_import_is_idempotent_and_source_occurrence_is_not_duplicated(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "resources.db",
        id_factory=PredictableIds(),
    )
    actor = Actor(ActorKind.USER, "user-1")
    candidate = blob("art-copy-1", "<h1>one</h1>")
    try:
        attempt, created = await service.reserve_document_import_attempt(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            idempotency_key="import-request-1",
            source_type=DocumentSourceType.ATTACHMENT,
            source_resource_id="att_occurrence_one",
            source_sha256=candidate.sha256,
            source_name="upload.html",
            source_mime=candidate.media_type,
            source_size=candidate.byte_size,
            document_name=candidate.filename,
            mode=DocumentImportMode.COPY,
            candidate_artifact_id=candidate.artifact_id,
        )
        assert created is True
        result = await service.apply_document_import_attempt(
            session_id="session-one",
            idempotency_key="import-request-1",
            candidate_artifact=candidate,
            document_name="page.html",
            kind=ArtifactKind.HTML,
            actor=actor,
        )

        replay_attempt, replay_created = await service.reserve_document_import_attempt(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            idempotency_key="import-request-1",
            source_type=DocumentSourceType.ATTACHMENT,
            source_resource_id="att_occurrence_one",
            source_sha256=candidate.sha256,
            source_name="upload.html",
            source_mime=candidate.media_type,
            source_size=candidate.byte_size,
            document_name=candidate.filename,
            mode=DocumentImportMode.COPY,
            candidate_artifact_id="ignored-on-replay",
        )
        replay = await service.apply_document_import_attempt(
            session_id="session-one",
            idempotency_key="import-request-1",
            candidate_artifact=candidate,
            document_name="page.html",
            kind=ArtifactKind.HTML,
            actor=actor,
        )

        assert replay_created is False
        assert replay_attempt == result.attempt
        assert replay == result
        assert result.attempt.status is MutationAttemptStatus.APPLIED
        assert result.binding.source_resource_id == "att_occurrence_one"
        assert len(
            await service.list_documents(
                session_key="agent:main:webchat:one",
                session_id="session-one",
            )
        ) == 1

        second_candidate = blob("art-copy-unused", "<h1>one</h1>")
        await service.reserve_document_import_attempt(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            idempotency_key="import-request-2",
            source_type=DocumentSourceType.ATTACHMENT,
            source_resource_id="att_occurrence_one",
            source_sha256=second_candidate.sha256,
            source_name="upload.html",
            source_mime=second_candidate.media_type,
            source_size=second_candidate.byte_size,
            document_name=second_candidate.filename,
            mode=DocumentImportMode.COPY,
            candidate_artifact_id=second_candidate.artifact_id,
        )
        reused = await service.apply_document_import_attempt(
            session_id="session-one",
            idempotency_key="import-request-2",
            candidate_artifact=second_candidate,
            document_name="page.html",
            kind=ArtifactKind.HTML,
            actor=actor,
        )
        assert reused.commit.document.document_id == result.commit.document.document_id
        assert reused.binding.binding_id == result.binding.binding_id
        assert reused.commit.revision.artifact_id == candidate.artifact_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_import_idempotency_reuse_and_cross_session_binding_fail_closed(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "resources.db",
        id_factory=PredictableIds(),
    )
    candidate = blob("art-copy-1", "<h1>one</h1>")
    try:
        await service.reserve_document_import_attempt(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            idempotency_key="same-key",
            source_type=DocumentSourceType.ATTACHMENT,
            source_resource_id="att_occurrence_one",
            source_sha256=candidate.sha256,
            source_name="upload.html",
            source_mime=candidate.media_type,
            source_size=candidate.byte_size,
            document_name=candidate.filename,
            mode=DocumentImportMode.COPY,
            candidate_artifact_id=candidate.artifact_id,
        )
        with pytest.raises(ArtifactConflictError, match="different input"):
            await service.reserve_document_import_attempt(
                session_key="agent:main:webchat:one",
                session_id="session-one",
                idempotency_key="same-key",
                source_type=DocumentSourceType.ATTACHMENT,
                source_resource_id="att_occurrence_two",
                source_sha256=candidate.sha256,
                source_name="upload.html",
                source_mime=candidate.media_type,
                source_size=candidate.byte_size,
                document_name=candidate.filename,
                mode=DocumentImportMode.COPY,
                candidate_artifact_id="another-candidate",
            )
        assert (
            await service.get_document_source_binding_for_resource(
                session_id="session-two",
                source_type=DocumentSourceType.ATTACHMENT,
                source_resource_id="att_occurrence_one",
            )
            is None
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_publication_receipt_is_revision_pinned_and_idempotent(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "resources.db",
        id_factory=PredictableIds(),
    )
    actor = Actor(ActorKind.USER, "user-1")
    initial = blob("art-internal-head", "<h1>one</h1>")
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            name="page.html",
            kind=ArtifactKind.HTML,
            initial_artifact=initial,
            actor=actor,
        )
        deliverable = ArtifactBlobRef(
            artifact_id="art-public-copy",
            sha256=initial.sha256,
            filename="published.html",
            media_type=initial.media_type,
            byte_size=initial.byte_size,
        )
        attempt, reserved = await service.reserve_document_publish_attempt(
            session_key="agent:main:webchat:one",
            session_id="session-one",
            idempotency_key="publish-1",
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            candidate_artifact=deliverable,
        )
        applied = await service.apply_document_publish_attempt(
            session_id="session-one",
            idempotency_key="publish-1",
            actor=actor,
        )
        replay = await service.apply_document_publish_attempt(
            session_id="session-one",
            idempotency_key="publish-1",
            actor=actor,
        )

        assert reserved is True
        assert attempt.status is MutationAttemptStatus.RESERVED
        assert applied == replay
        assert applied.attempt.status is MutationAttemptStatus.APPLIED
        assert applied.publication.revision_id == created.revision.revision_id
        assert applied.publication.deliverable_artifact_id == deliverable.artifact_id
        assert len(
            await service.list_document_publications(session_id="session-one")
        ) == 1
    finally:
        await service.close()
