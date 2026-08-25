"""Synthetic subprocess worker for artifact candidate hard-crash tests."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactSessionService,
)
from opensquilla.artifacts import ArtifactStore


async def _run(args: argparse.Namespace) -> None:
    service = await ArtifactSessionService.open(args.database)
    store = ArtifactStore(args.media_root)
    document = await service.get_document(args.document_id)
    revision = await service.get_revision(document.head_revision_id)
    payload = b"<h1>Synthetic crash candidate</h1>"
    artifact_id = store.allocate_artifact_id()
    sha256 = hashlib.sha256(payload).hexdigest()
    await service.register_mutation_candidate(
        document_id=document.document_id,
        turn_id=args.turn_id,
        candidate_session_id=args.session_id,
        candidate_artifact_id=artifact_id,
        candidate_artifact_sha256=sha256,
    )
    result = {"artifact_id": artifact_id}
    if args.phase != "journaled":
        ref = store.publish_bytes(
            payload,
            session_id=args.session_id,
            session_key=document.session_key,
            name="synthetic-crash.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            visibility="internal",
            artifact_id=artifact_id,
        )
        if args.phase == "committed":
            applied, change = await service.commit_change_set_atomically(
                document_id=document.document_id,
                base_revision_id=revision.revision_id,
                expected_document_state_revision=document.state_revision,
                operations=({"op": "replace_text"},),
                candidate_artifact=ArtifactBlobRef(
                    artifact_id=ref.id,
                    sha256=ref.sha256,
                    filename=ref.name,
                    media_type=ref.mime,
                    byte_size=ref.size,
                ),
                validation={"status": "passed"},
                actor=Actor(ActorKind.AGENT, "synthetic-crash-agent"),
                turn_id=args.turn_id,
            )
            result.update(
                change_set_id=change.change_set_id,
                revision_id=applied.revision.revision_id,
            )
    args.ready.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--phase",
        choices=("journaled", "published", "committed"),
        required=True,
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
