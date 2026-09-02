"""Gateway adapters for durable Artifact Workbench restart recovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Any

from opensquilla.application.artifact_workbench import ArtifactRecoveryPort
from opensquilla.artifact_session import ArtifactSessionService, DocumentImportAttempt
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway import artifact_mutation_recovery, document_resource_recovery
from opensquilla.gateway.document_resource_recovery import (
    DocumentImportRecoverySource,
)

ImportSourceResolver = Callable[
    [DocumentImportAttempt],
    Awaitable[DocumentImportRecoverySource | None],
]


def _counters(summary: Any) -> Mapping[str, int]:
    values = asdict(summary) if hasattr(summary, "__dataclass_fields__") else vars(summary)
    return {
        key: int(value)
        for key, value in values.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


class GatewayArtifactRecoveryPort(ArtifactRecoveryPort):
    """Retain the existing recovery implementations behind a typed startup Port."""

    def __init__(
        self,
        service: ArtifactSessionService,
        store: ArtifactStore,
        *,
        import_source_resolver: ImportSourceResolver,
    ) -> None:
        self._service = service
        self._store = store
        self._import_source_resolver = import_source_resolver

    async def recover_drafts(self) -> Mapping[str, int]:
        return _counters(
            await artifact_mutation_recovery.reject_orphaned_artifact_drafts(
                self._service, self._store
            )
        )

    async def recover_mutations(self) -> Mapping[str, int]:
        return _counters(
            await artifact_mutation_recovery.reconcile_pending_artifact_mutations(
                self._service, self._store
            )
        )

    async def recover_resources(self) -> Mapping[str, int]:
        return _counters(
            await document_resource_recovery.reconcile_pending_document_resources(
                self._service,
                self._store,
                import_source_resolver=self._import_source_resolver,
            )
        )


__all__ = ["GatewayArtifactRecoveryPort", "ImportSourceResolver"]
