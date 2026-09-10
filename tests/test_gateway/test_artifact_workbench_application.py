from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactCatalog,
    ArtifactCatalogQuery,
    ArtifactIdentity,
    ChangeHistory,
    ChangeIdentity,
    DocumentSource,
    RevisionHistory,
    RevisionListQuery,
    SourceRead,
)


@pytest.mark.asyncio
async def test_workbench_modules_dispatch_explicit_domain_requests() -> None:
    port = AsyncMock()
    port.list_artifacts.return_value = {"artifacts": []}
    port.get_artifact.return_value = {"artifact": {"id": "artifact-1"}}
    port.list_revisions.return_value = {"revisions": []}
    port.get_change.return_value = {"changeSet": {"changeSetId": "change-1"}}
    port.read_source.return_value = {"source": {"language": "html"}}

    catalog = ArtifactCatalog(port)
    revisions = RevisionHistory(port)
    changes = ChangeHistory(port)
    source = DocumentSource(port)

    query = ArtifactCatalogQuery("agent:main:webchat:test", limit=50)
    identity = ArtifactIdentity("agent:main:webchat:test", "artifact-1")
    revision_query = RevisionListQuery(
        "agent:main:webchat:test", "document-1", limit=20
    )
    change = ChangeIdentity("agent:main:webchat:test", "document-1", "change-1")

    await catalog.list(query)
    await catalog.get(identity)
    await revisions.list(revision_query)
    await changes.get(change)
    await source.read(SourceRead("agent:main:webchat:test", "document-1"))

    port.list_artifacts.assert_awaited_once_with(query)
    port.get_artifact.assert_awaited_once_with(identity)
    port.list_revisions.assert_awaited_once_with(revision_query)
    port.get_change.assert_awaited_once_with(change)




def test_workbench_identities_fail_closed_before_port_access() -> None:
    with pytest.raises(ValueError, match="session key"):
        ArtifactCatalogQuery("")
    with pytest.raises(ValueError, match="artifact id"):
        ArtifactIdentity("agent:main:webchat:test", "")
    with pytest.raises(ValueError, match="positive"):
        RevisionListQuery("agent:main:webchat:test", "document-1", limit=0)
