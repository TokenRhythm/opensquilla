"""Publish one versioned HTML document in the receipt test's private Gateway."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from opensquilla.artifact_session import (  # noqa: E402
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
)
from opensquilla.artifacts import (  # noqa: E402
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactStore,
)
from opensquilla.session.storage import SessionStorage  # noqa: E402


async def main() -> None:
    state = Path(sys.argv[1]).resolve(strict=True)
    session_key = sys.argv[2]
    async with SessionStorage(state / "sessions.db") as storage:
        session = await storage.get_session(session_key)
        if session is None:
            raise ValueError("The receipt fixture session must already exist")
        ref = ArtifactStore(state / "media").publish_bundle(
            ArtifactBundle(
                entrypoint="fixture.html",
                files=(
                    ArtifactBundleSourceFile(
                        path="fixture.html",
                        mime="text/html",
                        data=b'<html><h1 id="synthetic-heading">Synthetic heading</h1></html>',
                    ),
                ),
            ),
            session_key=session_key,
            session_id=session.session_id,
            name="fixture.html",
            mime="text/html",
            source="receipt-annotation-fixture",
        )
        service = await ArtifactSessionService.from_session_storage(storage)
        try:
            document = await service.create_document(
                session_key=session_key,
                session_id=session.session_id,
                name="fixture.html",
                kind=ArtifactKind.HTML,
                initial_artifact=ArtifactBlobRef(
                    artifact_id=ref.id,
                    sha256=ref.sha256,
                    filename=ref.name,
                    media_type=ref.mime,
                    byte_size=ref.size,
                ),
                actor=Actor(ActorKind.USER, "receipt-annotation-fixture"),
            )
            print(
                json.dumps(
                    {
                        "documentId": document.document.document_id,
                        "resourceId": f"document:{document.document.document_id}",
                        "revisionId": document.revision.revision_id,
                        "sha256": document.revision.artifact_sha256,
                    }
                )
            )
        finally:
            await service.close()


asyncio.run(main())
