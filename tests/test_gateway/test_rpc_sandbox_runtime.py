from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE
from opensquilla.runtime_packs.models import (
    RuntimeAvailability,
    RuntimeComponentStatus,
    RuntimeError,
    RuntimeOperation,
    RuntimeOperationKind,
    RuntimeOperationState,
    RuntimePackStatus,
    RuntimeSource,
)


def _ctx(tmp_path, *, owner: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path)),
        principal=SimpleNamespace(is_owner=owner),
    )


def _status(*, operation: RuntimeOperation | None = None) -> RuntimePackStatus:
    return RuntimePackStatus(
        schema_version=1,
        management_supported=True,
        target="darwin-arm64",
        catalog_version="2026-08-21.2",
        source_order=(RuntimeSource.GITHUB, RuntimeSource.OSS),
        components=(
            RuntimeComponentStatus(
                component_id="python",
                availability=RuntimeAvailability.MISSING,
                catalog_version="2026-08-21.2",
                active_version=None,
                installed_bytes=None,
                removable=False,
                resume_available=False,
                resume_bytes=0,
                operation=operation,
                last_error=operation.error if operation else None,
            ),
        ),
        next_poll_after_ms=750 if operation else 5_000,
    )


def _operation(state: RuntimeOperationState = RuntimeOperationState.QUEUED) -> RuntimeOperation:
    return RuntimeOperation(
        operation_id="runtime-operation-1",
        component_id="python",
        kind=RuntimeOperationKind.INSTALL,
        state=state,
        progress_bytes=0,
        total_bytes=100,
        source=None,
        started_at_ms=1,
        updated_at_ms=1,
    )


def test_runtime_rpc_scope_contract() -> None:
    assert METHOD_SCOPES["sandbox.runtime.status"] == READ_SCOPE
    assert METHOD_SCOPES["sandbox.runtime.install"] == ADMIN_SCOPE
    assert METHOD_SCOPES["sandbox.runtime.cancel"] == ADMIN_SCOPE
    assert METHOD_SCOPES["sandbox.runtime.remove"] == ADMIN_SCOPE


@pytest.mark.asyncio
async def test_runtime_status_is_fail_open_and_path_free(monkeypatch, tmp_path) -> None:
    import opensquilla.runtime_packs as runtime_packs
    from opensquilla.gateway import rpc_sandbox

    monkeypatch.setattr(runtime_packs, "status_snapshot", lambda _state: _status())

    payload = await rpc_sandbox._handle_sandbox_runtime_status({}, _ctx(tmp_path))

    assert payload["managementSupported"] is True
    assert payload["target"] == "darwin-arm64"
    assert payload["sourceOrder"] == ["github", "oss"]
    assert "url" not in repr(payload).casefold()
    assert str(tmp_path) not in repr(payload)


@pytest.mark.asyncio
async def test_runtime_install_returns_operation_without_waiting_for_status(
    monkeypatch,
    tmp_path,
) -> None:
    import opensquilla.runtime_packs as runtime_packs
    from opensquilla.gateway import rpc_sandbox

    operation = _operation()
    observed: list[tuple[str, object]] = []

    def start(component_id, state_dir):
        observed.append((component_id, state_dir))
        return operation

    monkeypatch.setattr(runtime_packs, "start_install", start)

    def status_must_not_run(*_args):
        raise AssertionError("mutation RPC must not wait for Runtime Pack status")

    monkeypatch.setattr(runtime_packs, "status_snapshot", status_must_not_run)

    payload = await rpc_sandbox._handle_sandbox_runtime_install(
        {"componentId": "python"},
        _ctx(tmp_path),
    )

    assert observed == [("python", str(tmp_path))]
    assert payload["operation"]["operationId"] == operation.operation_id
    assert set(payload) == {"operation"}


@pytest.mark.asyncio
async def test_runtime_mutations_require_owner(tmp_path) -> None:
    from opensquilla.gateway import rpc_sandbox

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sandbox._handle_sandbox_runtime_install(
            {"componentId": "python"},
            _ctx(tmp_path, owner=False),
        )
    assert excinfo.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_runtime_cancel_projects_stale_operation_as_conflict(monkeypatch, tmp_path) -> None:
    import opensquilla.runtime_packs as runtime_packs
    from opensquilla.gateway import rpc_sandbox

    def cancel(*_args):
        raise runtime_packs.RuntimePackError("stale")

    monkeypatch.setattr(runtime_packs, "cancel_install", cancel)

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sandbox._handle_sandbox_runtime_cancel(
            {"componentId": "python", "operationId": "old-operation"},
            _ctx(tmp_path),
        )
    assert excinfo.value.code == "RUNTIME_JOB_CONFLICT"


@pytest.mark.asyncio
async def test_policy_defaults_do_not_consult_runtime_pack_installation_state(
    monkeypatch,
    tmp_path,
) -> None:
    import opensquilla.runtime_packs as runtime_packs
    import opensquilla.sandbox.runtime_launcher as runtime_launcher
    import opensquilla.sandbox.runtime_manifest as runtime_manifest
    from opensquilla.gateway import rpc_sandbox

    def status_must_not_run(*_args):
        raise AssertionError("policy defaults must remain independent from runtime state")

    monkeypatch.setattr(runtime_packs, "status_snapshot", status_must_not_run)

    def resolver_must_not_run():
        raise AssertionError("the finalized catalog must satisfy the legacy projection")

    monkeypatch.setattr(runtime_launcher, "bundled_runtime_resolver", resolver_must_not_run)
    monkeypatch.setattr(runtime_manifest, "runtime_target", lambda: "darwin-arm64")

    payload = await rpc_sandbox._handle_sandbox_policy_defaults({}, _ctx(tmp_path))

    assert payload["runtimeTarget"] == "darwin-arm64"
    assert payload["runtimeVersions"] == {
        "python": {"version": "3.13.15+20260814", "available": False},
        "node": {"version": "24.19.0", "available": False},
    }


def test_runtime_error_payload_does_not_leak_internal_exception() -> None:
    error = RuntimeError(
        code="NETWORK_ERROR",
        message="Runtime Pack download could not reach the source",
        retryable=True,
        source=RuntimeSource.OSS,
    )
    payload = error.to_public_dict()
    assert set(payload) == {"code", "message", "retryable", "source"}
