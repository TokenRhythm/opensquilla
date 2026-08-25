"""Narrow Gateway adapter for generated deliverable adoption."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.rpc_workbench_resources import (
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

        adopted = await adopt_generated_deliverable_if_editable(
            service=self.service,
            store=self.store,
            session_key=self.session_key,
            session_id=self.session_id,
            ref=ref,
        )
        if adopted is None:
            return
        document, revision, _binding, created = adopted
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


__all__ = ["GeneratedArtifactAdopter"]
