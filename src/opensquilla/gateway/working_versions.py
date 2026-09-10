"""Save workspace snapshots at the Gateway's ordinary successful turn boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifact_session.working_files import get_working_files, save_working_version
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.types import ErrorEvent
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config

log = structlog.get_logger(__name__)


async def with_working_versions(
    stream: Any,
    *,
    config: Any,
    session_manager: Any,
    session_key: str,
    session_id: str | None,
    workspace: str | None,
    actor_id: str,
    event_emitter: Any = None,
    preview_service: Any = None,
) -> AsyncIterator[Any]:
    failed = False
    done = None
    try:
        async for event in stream:
            kind = getattr(event, "kind", None)
            if kind in {"error", "control_terminal"} or (
                kind == "answer_generation_reset" and getattr(event, "terminal", False)
            ):
                failed = True
            if kind == "done":
                done = event
            yield event
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
    if done is None:
        return
    if not failed and session_id and workspace:
        storage = get_session_storage(session_manager)
        if storage is not None and callable(getattr(storage, "_write_transaction", None)):
            service = None
            try:
                service = await ArtifactSessionService.from_session_storage(storage)
                documents = await service.list_documents(
                    session_key=session_key,
                    session_id=session_id,
                    limit=1000,
                )
                store = ArtifactStore(media_root_from_config(config))
                for document in documents:
                    binding = await get_working_files(service, document.document_id)
                    if binding is None or binding.workspace != str(Path(workspace).resolve()):
                        continue
                    result = await save_working_version(
                        service,
                        store,
                        document_id=document.document_id,
                        session_key=session_key,
                        session_id=session_id,
                        actor_id=actor_id,
                    )
                    if result is None:
                        continue
                    try:
                        updated = await get_working_files(service, document.document_id)
                        if preview_service is not None and updated is not None:
                            preview_service.register_working_files(
                                session_id=session_id,
                                artifact_id=result.revision.artifact_id,
                                binding=updated,
                            )
                        audit = await service.latest_audit_event(document.document_id)
                        if event_emitter is not None and audit is not None:
                            await event_emitter(
                                {
                                    "artifactEventSeq": audit.sequence,
                                    "documentId": document.document_id,
                                    "revisionId": result.revision.revision_id,
                                    "changeSetId": None,
                                    "action": "revision.committed",
                                }
                            )
                    except Exception as exc:
                        log.warning(
                            "working_version.notification_failed", error_type=type(exc).__name__
                        )
            except Exception as exc:
                log.warning("working_version.save_failed", error_type=type(exc).__name__)
                yield ErrorEvent(
                    code="WORKING_VERSION_SAVE_FAILED",
                    message=(
                        "The files remain in the workspace, but one or more saved versions "
                        "could not be confirmed."
                    ),
                )
            finally:
                if service is not None:
                    await service.close()
