from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactCatalog,
    ArtifactCatalogQuery,
    ArtifactIdentity,
    ChangeHistory,
    ChangeIdentity,
    DocumentEditSession,
    DocumentSource,
    EditSessionMutation,
    RevisionHistory,
    RevisionListQuery,
    SourceEdit,
    SourcePatch,
    SourceRead,
)


@pytest.mark.asyncio
async def test_workbench_modules_dispatch_explicit_domain_requests() -> None:
    port = AsyncMock()
    port.list_artifacts.return_value = {"artifacts": []}
    port.get_artifact.return_value = {"artifact": {"id": "artifact-1"}}
    port.list_revisions.return_value = {"revisions": []}
    port.get_change.return_value = {"changeSet": {"changeSetId": "change-1"}}
    port.heartbeat_edit_session.return_value = {"editSession": {"status": "active"}}
    port.read_source.return_value = {"source": {"language": "html"}}

    catalog = ArtifactCatalog(port)
    revisions = RevisionHistory(port)
    changes = ChangeHistory(port)
    edit_sessions = DocumentEditSession(port)
    source = DocumentSource(port)

    query = ArtifactCatalogQuery("agent:main:webchat:test", limit=50)
    identity = ArtifactIdentity("agent:main:webchat:test", "artifact-1")
    revision_query = RevisionListQuery(
        "agent:main:webchat:test", "document-1", limit=20
    )
    change = ChangeIdentity("agent:main:webchat:test", "document-1", "change-1")
    heartbeat = EditSessionMutation("agent:main:webchat:test", "edit-1", 2)

    await catalog.list(query)
    await catalog.get(identity)
    await revisions.list(revision_query)
    await changes.get(change)
    await edit_sessions.heartbeat(heartbeat)
    await source.read(SourceRead("agent:main:webchat:test", "document-1"))

    port.list_artifacts.assert_awaited_once_with(query)
    port.get_artifact.assert_awaited_once_with(identity)
    port.list_revisions.assert_awaited_once_with(revision_query)
    port.get_change.assert_awaited_once_with(change)
    port.heartbeat_edit_session.assert_awaited_once_with(heartbeat)


def test_source_patch_requires_complete_edit_session_fencing() -> None:
    with pytest.raises(ValueError, match="fencing fields"):
        SourcePatch(
            session_key="agent:main:webchat:test",
            document_id="document-1",
            expected_head_revision_id="revision-1",
            expected_source_sha256="a" * 64,
            expected_state_revision=1,
            edits=(SourceEdit(0, 0, "hello"),),
            request_id="request-1",
            edit_session_id="edit-1",
        )


def test_workbench_identities_fail_closed_before_port_access() -> None:
    with pytest.raises(ValueError, match="session key"):
        ArtifactCatalogQuery("")
    with pytest.raises(ValueError, match="artifact id"):
        ArtifactIdentity("agent:main:webchat:test", "")
    with pytest.raises(ValueError, match="positive"):
        EditSessionMutation("agent:main:webchat:test", "edit-1", 0)
