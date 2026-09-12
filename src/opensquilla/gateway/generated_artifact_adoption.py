"""Narrow Gateway adapter for generated deliverable adoption."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import ArtifactRef, ArtifactSource, ArtifactStore
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.workbench_resource_runtime import (
    adopt_generated_deliverable_if_editable,
)

ArtifactStateEmitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class GeneratedArtifactAdopter:
    """Adopt editable turn artifacts without exposing persistence to the engine.

    The instance is bound to one accepted session turn.  It validates every
    public event against that authority before resolving the immutable object
    and asking the ArtifactSession layer to create its canonical Document.
    """

    service: ArtifactSessionService
    store: ArtifactStore
    session_key: str
    session_id: str
    event_emitter: ArtifactStateEmitter | None = None
    workspace: str | None = None
    preview_service: Any = None
    source_paths: dict[str, ArtifactSource] = field(default_factory=dict)

    def with_source_paths(
        self, source_paths: dict[str, ArtifactSource],
    ) -> GeneratedArtifactAdopter:
        """Bind a runtime turn's source records without mutating its caller."""
        return replace(self, source_paths=source_paths)

    async def artifact_ids_for_source(self, source: ArtifactSource) -> set[str]:
        if self.workspace is None:
            return set()
        workspace = str(Path(self.workspace).resolve())
        try:
            relative = Path(source.path).relative_to(workspace).as_posix()
        except ValueError:
            return set()
        async with self.service.repository._read_transaction("generated.source_artifacts") as conn:
            cursor = await conn.execute(
                "SELECT revision.artifact_id FROM artifact_working_sources AS source "
                "JOIN artifact_revisions AS revision USING(document_id) "
                "WHERE source.session_key=? AND source.session_id=? "
                "AND source.workspace=? AND source.source_path=?",
                (self.session_key, self.session_id, workspace, relative),
            )
            return {str(row[0]) for row in await cursor.fetchall()}

    async def __call__(self, event: ArtifactEvent) -> None:
        if not isinstance(event, ArtifactEvent):
            raise TypeError("generated artifact adopter requires an ArtifactEvent")
        artifact_id = event.id.strip()
        if not artifact_id:
            raise ValueError("generated artifact event is missing its artifact id")
        if event.session_id and event.session_id != self.session_id:
            raise ValueError("generated artifact event belongs to another session")
        if event.session_key and event.session_key != self.session_key:
            raise ValueError("generated artifact event belongs to another session key")

        ref = await asyncio.to_thread(
            self.store.get_ref,
            session_id=self.session_id,
            artifact_id=artifact_id,
        )
        if ref.session_key != self.session_key:
            raise ValueError("generated artifact metadata belongs to another session key")
        for event_value, stored_value, field_name in (
            (event.sha256, ref.sha256, "sha256"),
            (event.name, ref.name, "name"),
            (event.mime, ref.mime, "mime"),
        ):
            if event_value and event_value != stored_value:
                raise ValueError(f"generated artifact {field_name} changed before adoption")
        if event.size and event.size != ref.size:
            raise ValueError("generated artifact size changed before adoption")

        async with self.service.repository._read_transaction("generated.publication") as conn:
            cursor = await conn.execute(
                "SELECT publication_id FROM document_publications "
                "WHERE session_id=? AND session_key=? AND deliverable_artifact_id=? LIMIT 1",
                (self.session_id, self.session_key, ref.id),
            )
            already_published = await cursor.fetchone() is not None
        source = None
        candidate = self.source_paths.get(event.publication_id)
        if candidate is not None and candidate.artifact_id != ref.id:
            raise ValueError("generated publication belongs to another artifact")
        if self.workspace is not None and candidate is not None:
            from opensquilla.artifact_session.working_files import prepare_working_source

            try:
                source = await asyncio.to_thread(
                    prepare_working_source, self.store, ref, self.workspace, candidate
                )
            except (ValueError, OSError):
                source = None
        if await self._adopt_working_publication(
            ref, source, event.publication_id, notify=not already_published,
        ):
            return
        if already_published:
            return

        adopted = await adopt_generated_deliverable_if_editable(
            service=self.service,
            store=self.store,
            session_key=self.session_key,
            session_id=self.session_id,
            ref=ref,
            working_source=source,
        )
        if adopted is None:
            return
        document, revision, _binding, created = adopted
        if source is not None:
            await self._adopt_working_publication(
                ref, source, event.publication_id, notify=not created,
            )
            head = await self.service.get_document_head(document.document_id)
            document, revision = head.document, head.revision
        if self.workspace is not None:
            from opensquilla.artifact_session.working_files import ensure_working_files

            working = await ensure_working_files(
                self.service,
                self.store,
                document_id=document.document_id,
                session_key=self.session_key,
                session_id=self.session_id,
                workspace=self.workspace,
            )
            if self.preview_service is not None:
                self.preview_service.register_working_files(
                    session_id=self.session_id,
                    artifact_id=revision.artifact_id,
                    binding=working,
                )
        if not created or self.event_emitter is None:
            return
        latest = await self.service.latest_audit_event(document.document_id)
        if latest is None:
            return
        await self.event_emitter(
            {
                "artifactEventSeq": latest.sequence,
                "documentId": document.document_id,
                "revisionId": revision.revision_id,
                "changeSetId": None,
                "action": "document.created",
            }
        )

    async def _adopt_working_publication(
        self, ref: ArtifactRef, source: dict[str, str] | None, publication_id: str,
        *, notify: bool = True,
    ) -> bool:
        if self.workspace is None:
            return False
        candidate = self.source_paths.get(publication_id)
        if not publication_id or (candidate is not None and not Path(candidate.path).is_absolute()):
            return False
        from opensquilla.artifact_session.working_files import (
            get_working_files,
            save_working_version,
        )

        workspace = str(Path(self.workspace).resolve())
        async with self.service.repository._read_transaction("generated.working_source") as conn:
            cursor = await conn.execute(
                "SELECT working.* FROM artifact_working_files AS working "
                "JOIN artifact_documents AS document USING(document_id) "
                "WHERE document.session_id=? AND document.session_key=? AND working.workspace=?",
                (self.session_id, self.session_key, workspace),
            )
            rows = await cursor.fetchall()
        matches = []
        for row in rows:
            binding = await get_working_files(self.service, row["document_id"])
            if binding is None:
                continue
            if candidate is not None and candidate.path == str(binding.entry):
                matches.append(binding)
            elif candidate is None:
                identity = f"{binding.document_id}\0{publication_id}".encode()
                key = f"working-publish:{hashlib.sha256(identity).hexdigest()}"
                async with self.service.repository._read_transaction(
                    "generated.publication_replay",
                ) as conn:
                    cursor = await conn.execute(
                        "SELECT 1 FROM artifact_audit_events WHERE event_id=? AND document_id=?",
                        (key, binding.document_id),
                    )
                    if await cursor.fetchone() is not None:
                        matches.append(binding)
        if len(matches) != 1:
            return False
        binding = matches[0]
        saved = await save_working_version(
            self.service,
            self.store,
            document_id=binding.document_id,
            session_key=self.session_key,
            session_id=self.session_id,
            actor_id="generated-deliverable",
            published_ref=ref,
            working_source=source,
            publication_id=publication_id,
        )
        head = await self.service.get_document_head(binding.document_id)
        updated = await get_working_files(self.service, binding.document_id)
        if self.preview_service is not None and updated is not None:
            self.preview_service.register_working_files(
                session_id=self.session_id,
                artifact_id=head.revision.artifact_id,
                binding=updated,
            )
        latest = await self.service.latest_audit_event(binding.document_id)
        if (notify or saved is not None) and self.event_emitter is not None and latest is not None:
            await self.event_emitter(
                {
                    "artifactEventSeq": latest.sequence,
                    "documentId": binding.document_id,
                    "revisionId": head.revision.revision_id,
                    "changeSetId": None,
                    "action": "document.published",
                }
            )
        return True


__all__ = ["GeneratedArtifactAdopter"]
