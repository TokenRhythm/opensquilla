"""Bounded target resolution, request-local validation, and worker-thread I/O."""

from __future__ import annotations

import threading

import pytest

import opensquilla.gateway.rpc_artifact_editing as editing
import opensquilla.gateway.workbench_resource_runtime as runtime
from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
)
from opensquilla.artifacts import ArtifactStore
from opensquilla.office_format import OFFICE_MIMES
from tests.test_gateway import test_rpc_workbench_resources as resource_tests
from tests.test_office_format import office_package

SESSION_KEY = resource_tests.SESSION_KEY
_dispatch = resource_tests._dispatch
resource_env = resource_tests.resource_env


async def _office(env, *, visibility="listed", session=None, name="sample.pptx"):
    session = session or env.session
    ref = env.store.publish_bytes(
        office_package("pptx"), session_id=session.session_id,
        session_key=session.session_key, name=name, mime=OFFICE_MIMES["pptx"],
        source="read-regression", visibility=visibility,
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    result, _ = await service.adopt_document(
        session_key=session.session_key, session_id=session.session_id,
        name=ref.name, kind=ArtifactKind.PRESENTATION,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id, sha256=ref.sha256, filename=ref.name,
            media_type=ref.mime, byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "read-regression"),
    )
    return ref, result


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "workbench.resources.get", "workbench.previews.create", "workbench.resources.open",
])
async def test_targeted_office_resolution_does_not_inspect_unrelated_material(
    resource_env, monkeypatch, method,
):
    env = resource_env
    target, adopted = await _office(env)
    unrelated, _ = await _office(env, name="unrelated.pptx")
    env.store.path_for(unrelated).write_bytes(b"corrupt unrelated material")
    calls = []
    validations = []
    original = runtime._inspect_artifact_material
    validate = runtime.validate_office_bytes

    def inspect(store, artifact_id, **kwargs):
        calls.append(artifact_id)
        return original(store, artifact_id, **kwargs)

    def validate_once(*args):
        validations.append(1)
        return validate(*args)

    async def no_inventory(*args, **kwargs):
        pytest.fail("targeted operations must not enumerate the session inventory")

    monkeypatch.setattr(runtime, "_inspect_artifact_material", inspect)
    monkeypatch.setattr(runtime, "validate_office_bytes", validate_once)
    monkeypatch.setattr(runtime, "_resource_inventory", no_inventory)
    resource = {"type": "document", "id": adopted.document.document_id}
    params = {"sessionKey": SESSION_KEY, "resourceRef": resource}
    if method == "workbench.previews.create":
        params["mode"] = "isolated"
    if method == "workbench.resources.open":
        params.update(intent="edit-current", expectedSha256=target.sha256)
    response = await _dispatch(env, method, params)
    assert response.error is None, response.error
    assert response.payload["resource"]["sha256"] == target.sha256
    assert calls == [target.id]
    assert len(validations) == 1
    if method == "workbench.resources.open":
        assert response.payload["revision"]["revisionId"] == (
            response.payload["resource"]["relations"]["headRevisionId"]
        ) == response.payload["pageContext"]["revisionId"]


@pytest.mark.asyncio
async def test_inventory_reuses_identity_checks_without_retaining_payloads(
    resource_env, monkeypatch,
):
    env = resource_env
    refs = [(await _office(env, name=f"deck-{i}.pptx"))[0] for i in range(4)]
    assert len({ref.sha256 for ref in refs}) == 1
    env.store.path_for(refs[-1]).write_bytes(b"only this identity is corrupt")
    calls = []
    original = runtime._inspect_artifact_material

    def inspect(store, artifact_id, **kwargs):
        calls.append(artifact_id)
        assert kwargs["keep_payload"] is False
        checked = original(store, artifact_id, **kwargs)
        assert checked.payload is None
        return checked

    monkeypatch.setattr(runtime, "_inspect_artifact_material", inspect)
    response = await _dispatch(env, "workbench.resources.list", {"sessionKey": SESSION_KEY})
    assert response.error is None, response.error
    assert sorted(calls) == sorted(ref.id for ref in refs)
    deliveries = {
        item["resource"]["id"]: item for item in response.payload["resources"]
        if item["resource"]["type"] == "deliverable"
    }
    assert all(deliveries[ref.id]["capabilities"]["preview"] for ref in refs[:-1])
    assert deliveries[refs[-1].id]["capabilities"]["preview"] is False
    # No validation or failure state survives a subsequent RPC.
    env.store.path_for(refs[-1]).write_bytes(office_package("pptx"))
    repaired = await _dispatch(env, "workbench.resources.get", {
        "sessionKey": SESSION_KEY,
        "resourceRef": {"type": "deliverable", "id": refs[-1].id},
    })
    assert repaired.error is None, repaired.error
    assert repaired.payload["resource"]["capabilities"]["preview"] is True
    assert calls.count(refs[-1].id) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "workbench.resources.get", "workbench.previews.create", "workbench.resources.open",
])
async def test_targeted_lookup_keeps_internal_and_cross_session_artifacts_private(
    resource_env, method,
):
    env = resource_env
    internal, _ = await _office(env, visibility="internal")
    other = await env.manager.create("agent:main:webchat:read-other")
    foreign, foreign_document = await _office(env, session=other)
    for resource in (
        {"type": "deliverable", "id": internal.id},
        {"type": "deliverable", "id": foreign.id},
        {"type": "document", "id": foreign_document.document.document_id},
    ):
        params = {"sessionKey": SESSION_KEY, "resourceRef": resource}
        if method == "workbench.previews.create":
            params["mode"] = "isolated"
        if method == "workbench.resources.open":
            params["intent"] = "edit-current"
        response = await _dispatch(env, method, params)
        assert response.error is not None
        assert response.error.code == "DOCUMENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_office_capabilities_read_and_validate_on_worker(resource_env, monkeypatch):
    env = resource_env
    ref, adopted = await _office(env)
    loop_thread = threading.get_ident()
    operations = []
    original = ArtifactStore.read_verified_material
    validate = editing.validate_office_bytes

    def read(store, artifact_id, **kwargs):
        assert threading.get_ident() != loop_thread
        operations.append(("read", artifact_id))
        return original(store, artifact_id, **kwargs)

    def validate_off_thread(*args):
        assert threading.get_ident() != loop_thread
        operations.append(("validate", ref.id))
        return validate(*args)

    monkeypatch.setattr(ArtifactStore, "read_verified_material", read)
    monkeypatch.setattr(editing, "validate_office_bytes", validate_off_thread)
    capabilities = await editing._revision_capabilities(
        env.ctx, adopted.document, adopted.revision,
    )
    assert capabilities["preview"] is True
    assert capabilities["agentEdit"] is True
    assert operations == [("read", ref.id), ("validate", ref.id)]


@pytest.mark.asyncio
async def test_import_source_reuses_the_bounded_verified_payload(resource_env, monkeypatch):
    env = resource_env
    ref, _adopted = await _office(env)
    original = ArtifactStore.read_verified_material
    reads = []

    def read(store, artifact_id, **kwargs):
        reads.append((artifact_id, kwargs))
        return original(store, artifact_id, **kwargs)

    def no_second_read(*args, **kwargs):
        pytest.fail("import must reuse verified bytes rather than resolve/read the source again")

    monkeypatch.setattr(ArtifactStore, "read_verified_material", read)
    monkeypatch.setattr(ArtifactStore, "resolve_for_download", no_second_read)
    source = await runtime._resolve_import_source(
        env.ctx, session_key=SESSION_KEY, session_id=env.session.session_id,
        source_type="deliverable", resource_id=ref.id,
    )
    assert source.payload == office_package("pptx")
    assert source.sha256 == ref.sha256
    assert reads == [(ref.id, {
        "session_id": env.session.session_id,
        "max_bytes": runtime.MAX_OFFICE_BYTES,
        "validate_bundle": True,
    })]
