from __future__ import annotations

from typing import cast

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactCatalogQuery,
    ArtifactIdentity,
    DocumentImport,
    PromptAnnotationCreate,
    WorkbenchResourceOpen,
    WorkbenchResourceQuery,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.artifact_workbench import (
    GatewayArtifactWorkbenchAdapter,
)
from opensquilla.gateway.adapters.artifact_workbench_contract import (
    ARTIFACT_WORKBENCH_CONTRACT_METHODS,
)
from opensquilla.gateway.rpc import RpcContext


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_field", ["resourceRef", "resource"])
@pytest.mark.parametrize(
    ("resource_type", "id_field"),
    (
        ("attachment", "attachmentId"),
        ("document", "documentId"),
        ("deliverable", "artifactId"),
        ("url", "urlId"),
    ),
)
async def test_resource_aliases_execute_the_same_application_port_once(
    resource_field: str,
    resource_type: str,
    id_field: str,
) -> None:
    calls: list[WorkbenchResourceQuery] = []

    class Port:
        async def get_resource(self, query: WorkbenchResourceQuery) -> dict[str, object]:
            calls.append(query)
            return {"resource": {"resource": {"type": "document", "id": "document-1"}}}

    ctx = cast(RpcContext, object())
    port = Port()
    params = {
        "sessionKey": "session-1",
        resource_field: {"type": resource_type, id_field: "resource-1"},
    }
    handler = GatewayArtifactWorkbenchAdapter.bind(
        "workbench.resources.get", lambda _ctx: port
    )

    assert await handler(params, ctx) == {
        "resource": {"resource": {"type": "document", "id": "document-1"}}
    }
    assert len(calls) == 1
    assert calls[0].session_key == "session-1"
    assert calls[0].resource.resource_type == resource_type
    assert calls[0].resource.resource_id == "resource-1"


@pytest.mark.asyncio
async def test_invalid_identity_fails_before_the_existing_implementation() -> None:
    calls = 0

    class Port:
        async def get_artifact(self, identity: ArtifactIdentity) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"artifact": {"id": identity.artifact_id}}

    handler = GatewayArtifactWorkbenchAdapter.bind("artifacts.get", lambda _ctx: Port())

    with pytest.raises(ValueError, match="artifactId"):
        await handler({"sessionKey": "session-1"}, cast(RpcContext, object()))
    assert calls == 0


@pytest.mark.asyncio
async def test_resource_identity_aliases_fail_closed_when_they_disagree() -> None:
    class Port:
        async def get_resource(self, query: WorkbenchResourceQuery) -> dict[str, object]:
            raise AssertionError(f"port must not receive ambiguous identity: {query}")

    handler = GatewayArtifactWorkbenchAdapter.bind(
        "workbench.resources.get", lambda _ctx: Port()
    )

    with pytest.raises(ValueError, match="identity aliases must match"):
        await handler(
            {
                "sessionKey": "session-1",
                "resource": {
                    "type": "document",
                    "documentId": "document-1",
                    "id": "document-2",
                },
            },
            cast(RpcContext, object()),
        )


@pytest.mark.asyncio
async def test_resource_type_remains_case_insensitive_for_legacy_clients() -> None:
    calls: list[WorkbenchResourceQuery] = []

    class Port:
        async def get_resource(self, query: WorkbenchResourceQuery) -> dict[str, object]:
            calls.append(query)
            return {"resource": {}}

    handler = GatewayArtifactWorkbenchAdapter.bind(
        "workbench.resources.get", lambda _ctx: Port()
    )
    await handler(
        {
            "sessionKey": "session-1",
            "resource": {"type": "Document", "documentId": "document-1"},
        },
        cast(RpcContext, object()),
    )

    assert calls[0].resource.resource_type == "document"


@pytest.mark.asyncio
async def test_artifact_catalog_keeps_legacy_string_limit_coercion() -> None:
    calls: list[ArtifactCatalogQuery] = []

    class Port:
        async def list_artifacts(self, query: ArtifactCatalogQuery) -> dict[str, object]:
            calls.append(query)
            return {"artifacts": []}

    handler = GatewayArtifactWorkbenchAdapter.bind("artifacts.list", lambda _ctx: Port())
    await handler(
        {"sessionKey": "session-1", "limit": "7"},
        cast(RpcContext, object()),
    )

    assert calls[0].limit == 7


@pytest.mark.asyncio
async def test_document_import_does_not_promote_request_id_to_idempotency_key() -> None:
    class Port:
        async def import_document(self, command: DocumentImport) -> dict[str, object]:
            raise AssertionError(f"invalid import must not reach its port: {command}")

    handler = GatewayArtifactWorkbenchAdapter.bind("documents.import", lambda _ctx: Port())
    with pytest.raises(ValueError, match="idempotencyKey or clientRequestId is required"):
        await handler(
            {
                "sessionKey": "session-1",
                "source": {"type": "attachment", "attachmentId": "attachment-1"},
                "mode": "copy",
                "expectedSha256": "a" * 64,
                "requestId": "unsupported-alias",
            },
            cast(RpcContext, object()),
        )


@pytest.mark.asyncio
async def test_resource_open_ignores_unrecognized_request_id_alias() -> None:
    calls: list[WorkbenchResourceOpen] = []

    class Port:
        async def open_resource(self, command: WorkbenchResourceOpen) -> dict[str, object]:
            calls.append(command)
            return {"mode": "readonly", "resource": {}}

    handler = GatewayArtifactWorkbenchAdapter.bind(
        "workbench.resources.open", lambda _ctx: Port()
    )
    await handler(
        {
            "sessionKey": "session-1",
            "resource": {"type": "document", "documentId": "document-1"},
            "requestId": "unsupported-alias",
        },
        cast(RpcContext, object()),
    )

    assert calls[0].idempotency_key is None


@pytest.mark.asyncio
async def test_prompt_annotation_create_preserves_an_explicit_empty_body() -> None:
    calls: list[PromptAnnotationCreate] = []

    class Port:
        async def create_annotation(
            self, command: PromptAnnotationCreate
        ) -> dict[str, object]:
            calls.append(command)
            return {"annotation": {}}

    params = {
        "sessionKey": "session-1",
        "annotationId": "ann_12345678901234567890123456789012",
        "documentId": "document-1",
        "revisionId": "revision-1",
        "selection": {
            "selectionId": "selection-1",
            "tagName": "img",
            "elementPath": "[[\"\",\"img\",1]]",
            "elementProofSha256": "a" * 64,
        },
        "body": "",
    }
    handler = GatewayArtifactWorkbenchAdapter.bind(
        "artifacts.prompt_annotations.create", lambda _ctx: Port()
    )

    assert await handler(params, cast(RpcContext, object())) == {"annotation": {}}
    assert len(calls) == 1
    assert calls[0].body == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "message"),
    (
        (
            "artifacts.documents.list",
            {"sessionKey": "session-1", "limit": "10"},
            "limit must be a positive integer",
        ),
        (
            "artifacts.revisions.list",
            {"sessionKey": "session-1", "documentId": "document-1", "limit": False},
            "limit must be a positive integer",
        ),
        (
            "artifacts.changes.list",
            {"sessionKey": "session-1", "documentId": "document-1", "limit": 0},
            "limit must be a positive integer",
        ),
        (
            "artifacts.prompt_annotations.list",
            {"sessionKey": "session-1", "limit": -1},
            "limit must be a positive integer",
        ),
        (
            "workbench.resources.list",
            {"sessionKey": "session-1", "types": []},
            "types must be a non-empty array",
        ),
        (
            "workbench.resources.list",
            {"sessionKey": "session-1", "types": ["document", 1]},
            "types contains an unsupported resource type",
        ),
        (
            "workbench.resources.list",
            {"sessionKey": "session-1", "limit": None},
            "limit must be a positive integer",
        ),
        (
            "documents.editSessions.start",
            {"sessionKey": "session-1", "documentId": "document-1", "mode": "view"},
            "mode must be edit",
        ),
        (
            "artifacts.edit.capabilities",
            {"documentId": "document-1"},
            "sessionKey",
        ),
    ),
)
async def test_invalid_legacy_workbench_inputs_are_not_normalized_to_defaults(
    method: str,
    params: dict[str, object],
    message: str,
) -> None:
    adapter = GatewayArtifactWorkbenchAdapter(object(), params)

    with pytest.raises(ValueError, match=message):
        await adapter.dispatch(method)


def test_all_workbench_methods_have_generated_descriptors_and_guest_policy() -> None:
    assert len(ARTIFACT_WORKBENCH_CONTRACT_METHODS) == 30
    assert all(
        method in GATEWAY_METHOD_CONTRACTS
        for method in ARTIFACT_WORKBENCH_CONTRACT_METHODS
    )
    assert GATEWAY_METHOD_CONTRACTS["artifacts.list"].guest_allowed is True
    assert GATEWAY_METHOD_CONTRACTS["artifacts.get"].guest_allowed is True
    assert all(
        not GATEWAY_METHOD_CONTRACTS[method].guest_allowed
        for method in ARTIFACT_WORKBENCH_CONTRACT_METHODS
        if method not in {"artifacts.list", "artifacts.get"}
    )
