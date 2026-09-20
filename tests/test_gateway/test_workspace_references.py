from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from opensquilla.execution_workspaces import configured_execution_workspace
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.workspace_references import read_workspace_reference
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.source_edit_contract import build_line_receipt, workspace_reference_id

_KEY = "agent:main:webchat:source-reference-test"


@pytest_asyncio.fixture
async def source_context(tmp_path: Path, request):
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "src.py"
    newline = getattr(request, "param", b"\n")
    source.write_bytes(newline.join([b"first", b"second", b"third", b""]))
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    session = SessionNode(
        session_key=_KEY,
        execution_workspace=configured_execution_workspace(root),
    )
    await storage.upsert_session(session)
    manager = SimpleNamespace(storage=storage)
    ctx = RpcContext(
        conn_id="source-reference-test",
        session_manager=manager,
        config=GatewayConfig(workspace_dir=str(tmp_path / "wrong-default")),
    )
    reference = build_line_receipt(
        source, start_line=2, end_line=3, display_path="src.py",
        session_key=_KEY, workspace_id=workspace_reference_id(root),
    )["reference"]
    try:
        yield ctx, {"sessionKey": _KEY, "reference": reference}, source
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_context", [b"\n", b"\r\n"], ids=["lf", "crlf"], indirect=True)
async def test_reads_scoped_source_and_generated_contract(source_context):
    ctx, params, source = source_context
    response = await get_dispatcher().dispatch(
        "read-source", "workspaces.references.read", params, ctx,
    )
    assert response.ok, response.error
    result = response.payload
    assert result["content"] == source.read_bytes().decode("utf-8")
    assert result["revision"] == params["reference"]["state"]["revision"]
    assert (result["startLine"], result["endLine"], result["totalLines"]) == (2, 3, 3)
    assert result["reference"]["scope"] == params["reference"]["scope"]
    assert result["reference"]["capabilities"]["reveal"] is False
    assert str(source.parent) not in str(result)


@pytest.mark.asyncio
async def test_adapts_historical_empty_scope_only_to_requested_session(source_context):
    ctx, params, _ = source_context
    params["reference"]["scope"] = {}
    result = await read_workspace_reference(params, ctx)
    assert result["reference"]["scope"]["sessionKey"] == _KEY
    assert result["reference"]["scope"]["workspaceId"].startswith("workspace_")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "guest", "channel", "subagent"])
async def test_other_principals_cannot_read_local_files(source_context, role):
    ctx, params, _ = source_context
    ctx.principal = Principal(
        role=role, scopes=frozenset({"operator.admin"}), is_owner=False, authenticated=True,
    )
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "OWNER_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/tmp/example.py", "../example.py", "src/../example.py", "C:/example.py",
    "C:example.py", "\\\\host\\share\\example.py", "src\\example.py",
    "src//example.py", "src/./example.py", "src.py\x00", "src.py:stream",
])
async def test_rejects_noncanonical_paths_on_every_platform(source_context, path):
    ctx, params, _ = source_context
    params["reference"]["id"] = path
    params["reference"]["locator"]["relativePath"] = path
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "INVALID_REFERENCE"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [
    {"workspaceId": "another-workspace"},
    {"sessionKey": "agent:main:webchat:another"},
    {"sessionKey": ""},
    {"gatewayInstanceId": "another-gateway"},
])
async def test_rejects_cross_scope_references(source_context, scope):
    ctx, params, _ = source_context
    params["reference"]["scope"] = scope
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "WORKSPACE_MISMATCH"


@pytest.mark.asyncio
async def test_rejects_stale_revision_without_returning_new_content(source_context):
    ctx, params, source = source_context
    source.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "STALE_REFERENCE"
    assert "changed" not in str(error.value.details)


@pytest.mark.asyncio
async def test_newline_only_change_invalidates_source_revision(source_context):
    ctx, params, source = source_context
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "STALE_REFERENCE"


@pytest.mark.asyncio
async def test_rejects_symlink_escape_even_when_revision_matches(source_context):
    ctx, params, source = source_context
    outside = source.parent.parent / "outside.py"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("The platform does not grant symlink creation")
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "INVALID_REFERENCE"


@pytest.mark.asyncio
async def test_rejects_missing_source_and_session(source_context):
    ctx, params, source = source_context
    source.unlink()
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "FILE_NOT_FOUND"
    params["reference"]["scope"] = {}
    params["sessionKey"] = "agent:main:webchat:missing"
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_rejects_sensitive_file_even_with_known_revision(source_context):
    ctx, params, source = source_context
    sensitive = source.with_name(".env")
    sensitive.write_text("EXAMPLE_KEY=synthetic\n")
    params["reference"] = build_line_receipt(
        sensitive, start_line=1, end_line=1, display_path=".env",
    )["reference"]
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "FILE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_rejects_invalid_line_range_and_missing_revision(source_context):
    ctx, params, _ = source_context
    malformed = copy.deepcopy(params)
    malformed["reference"]["state"].pop("revision")
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(malformed, ctx)
    assert error.value.code == "INVALID_REFERENCE"
    params["reference"]["locator"]["endLine"] = 500
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "INVALID_REFERENCE"


@pytest.mark.asyncio
async def test_registered_workspace_scope_and_removal_are_revalidated(source_context):
    from opensquilla.gateway.rpc_workspaces import _handle_workspaces_open

    ctx, params, source = source_context
    created = await _handle_workspaces_open({"path": str(source.parent), "trusted": True}, ctx)
    workspace_id = created["workspace"]["id"]
    storage = ctx.session_manager.storage
    await storage.bind_session_workspace(_KEY, workspace_id)
    params["reference"]["scope"]["workspaceId"] = workspace_id
    result = await read_workspace_reference(params, ctx)
    assert result["reference"]["scope"]["workspaceId"] == workspace_id
    await storage.remove_project_workspace(workspace_id)
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_rejects_oversized_source_without_reading_whole_file(source_context):
    ctx, params, source = source_context
    source.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "FILE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_rejects_binary_even_when_revision_matches(source_context):
    from opensquilla.tools.source_edit_contract import source_revision_for_path

    ctx, params, source = source_context
    source.write_bytes(b"a\x00b\n")
    params["reference"]["state"]["revision"] = source_revision_for_path(source)
    with pytest.raises(RpcHandlerError) as error:
        await read_workspace_reference(params, ctx)
    assert error.value.code == "FILE_UNAVAILABLE"
