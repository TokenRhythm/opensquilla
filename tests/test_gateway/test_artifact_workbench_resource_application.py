from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from opensquilla.application.artifact_workbench import (
    DocumentImport,
    DocumentPublish,
    DocumentTransferApplication,
    MutationOutcomeApplication,
    MutationResolution,
    PromptAnnotationApplication,
    PromptAnnotationCreate,
    PromptAnnotationIdentity,
    PromptAnnotationMutation,
    PromptAnnotationQuery,
    PromptAnnotationSelection,
    ResourcePreviewApplication,
    WorkbenchPreviewCreate,
    WorkbenchResourceApplication,
    WorkbenchResourceListQuery,
    WorkbenchResourceOpen,
    WorkbenchResourceQuery,
    WorkbenchResourceRef,
)


class RecordingPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def _record(self, name: str, value: object) -> Mapping[str, Any]:
        self.calls.append((name, value))
        return {"operation": name}

    async def list_annotations(self, value: object) -> Mapping[str, Any]:
        return await self._record("annotations.list", value)

    async def create_annotation(self, value: object) -> Mapping[str, Any]:
        return await self._record("annotations.create", value)

    async def focus_annotation(self, value: object) -> Mapping[str, Any]:
        return await self._record("annotations.focus", value)

    async def update_annotation(self, value: object) -> Mapping[str, Any]:
        return await self._record("annotations.update", value)

    async def discard_annotation(self, value: object) -> Mapping[str, Any]:
        return await self._record("annotations.discard", value)

    async def list_resources(self, value: object) -> Mapping[str, Any]:
        return await self._record("resources.list", value)

    async def get_resource(self, value: object) -> Mapping[str, Any]:
        return await self._record("resources.get", value)

    async def open_resource(self, value: object) -> Mapping[str, Any]:
        return await self._record("resources.open", value)

    async def create_preview(self, value: object) -> Mapping[str, Any]:
        return await self._record("previews.create", value)

    async def import_document(self, value: object) -> Mapping[str, Any]:
        return await self._record("documents.import", value)

    async def publish_document(self, value: object) -> Mapping[str, Any]:
        return await self._record("documents.publish", value)

    async def resolve_mutation(self, value: object) -> Mapping[str, Any]:
        return await self._record("mutations.resolve", value)


@pytest.mark.asyncio
async def test_prompt_annotation_application_uses_explicit_commands() -> None:
    port = RecordingPort()
    application = PromptAnnotationApplication(port)
    selection = PromptAnnotationSelection("selection-1", "p", "0/1", "a" * 64)
    commands = [
        ("annotations.list", application.list(PromptAnnotationQuery("session-1"))),
        (
            "annotations.create",
            application.create(
                PromptAnnotationCreate(
                    "session-1", "annotation-1", "document-1", selection
                )
            ),
        ),
        (
            "annotations.focus",
            application.focus(PromptAnnotationIdentity("session-1", "annotation-1")),
        ),
        (
            "annotations.update",
            application.update(
                PromptAnnotationMutation("session-1", "annotation-1", 1, "updated")
            ),
        ),
        (
            "annotations.discard",
            application.discard(
                PromptAnnotationMutation("session-1", "annotation-1", 2)
            ),
        ),
    ]

    for expected, operation in commands:
        assert await operation == {"operation": expected}

    assert [name for name, _value in port.calls] == [name for name, _call in commands]


@pytest.mark.asyncio
async def test_resource_transfer_and_outcome_applications_keep_distinct_ports() -> None:
    port = RecordingPort()
    resources = WorkbenchResourceApplication(port)
    previews = ResourcePreviewApplication(port)
    transfers = DocumentTransferApplication(port)
    outcomes = MutationOutcomeApplication(port)
    resource = WorkbenchResourceRef("attachment", "attachment-1")

    await resources.list(WorkbenchResourceListQuery("session-1"))
    await resources.get(WorkbenchResourceQuery("session-1", resource))
    await resources.open(
        WorkbenchResourceOpen("session-1", resource, idempotency_key="open-1")
    )
    await previews.create(WorkbenchPreviewCreate("session-1", resource))
    await transfers.import_document(
        DocumentImport("session-1", resource, "import-1", client_request_id="request-1")
    )
    await transfers.publish_document(
        DocumentPublish("session-1", "document-1", "revision-1", "publish-1")
    )
    await outcomes.resolve(
        MutationResolution("session-1", "document.publish", "publish-1", "document-1")
    )

    assert [name for name, _value in port.calls] == [
        "resources.list",
        "resources.get",
        "resources.open",
        "previews.create",
        "documents.import",
        "documents.publish",
        "mutations.resolve",
    ]


def test_resource_and_mutation_commands_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported Workbench resource type"):
        WorkbenchResourceRef("file", "artifact-1")
    with pytest.raises(ValueError, match="unsupported resource open intent"):
        WorkbenchResourceOpen(
            "session-1",
            WorkbenchResourceRef("document", "document-1"),
            intent="execute",
        )
    with pytest.raises(ValueError, match="mutation request id"):
        MutationResolution("session-1", "document.publish", "")
