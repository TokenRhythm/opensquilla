from __future__ import annotations

from typing import Any, cast

import pytest

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
async def test_resource_aliases_execute_the_same_application_port_once(
    resource_field: str,
) -> None:
    calls: list[tuple[dict[str, Any] | None, RpcContext]] = []

    async def implementation(
        params: dict[str, Any] | None, ctx: RpcContext
    ) -> dict[str, Any]:
        calls.append((params, ctx))
        return {"resource": {"resource": {"type": "document", "id": "document-1"}}}

    ctx = cast(RpcContext, object())
    params = {
        "sessionKey": "session-1",
        resource_field: {"type": "document", "id": "document-1"},
    }
    handler = GatewayArtifactWorkbenchAdapter.bind(
        "workbench.resources.get", implementation
    )

    assert await handler(params, ctx) == {
        "resource": {"resource": {"type": "document", "id": "document-1"}}
    }
    assert calls == [(params, ctx)]


@pytest.mark.asyncio
async def test_invalid_identity_fails_before_the_existing_implementation() -> None:
    calls = 0

    async def implementation(
        params: dict[str, Any] | None, ctx: RpcContext
    ) -> dict[str, Any]:
        nonlocal calls
        del params, ctx
        calls += 1
        return {"artifact": {}}

    handler = GatewayArtifactWorkbenchAdapter.bind("artifacts.get", implementation)

    with pytest.raises(ValueError, match="artifactId"):
        await handler({"sessionKey": "session-1"}, cast(RpcContext, object()))
    assert calls == 0


@pytest.mark.asyncio
async def test_prompt_annotation_create_preserves_an_explicit_empty_body() -> None:
    calls: list[dict[str, Any] | None] = []

    async def implementation(
        params: dict[str, Any] | None, ctx: RpcContext
    ) -> dict[str, Any]:
        del ctx
        calls.append(params)
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
        "artifacts.prompt_annotations.create", implementation
    )

    assert await handler(params, cast(RpcContext, object())) == {"annotation": {}}
    assert calls == [params]


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
