from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sandbox import (
    _handle_sandbox_policy_defaults,
    _handle_sandbox_policy_get,
    _handle_sandbox_policy_update,
    _handle_sandbox_token_create,
    _handle_sandbox_token_list,
    _handle_sandbox_token_revoke,
)


def _ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="policy-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read", "operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
        config=SimpleNamespace(state_dir=str(tmp_path)),
    )


async def test_rpc_policy_get_and_update(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    baseline = await _handle_sandbox_policy_get({}, ctx)
    baseline["network"]["denyDomains"] = ["telemetry.example"]

    saved = await _handle_sandbox_policy_update(
        {
            "basePolicyVersion": baseline["policyVersion"],
            "policy": baseline,
        },
        ctx,
    )

    assert saved["policyVersion"] == 1
    assert saved["network"]["denyDomains"] == ["telemetry.example"]


async def test_rpc_policy_defaults_exposes_immutable_file_rules(tmp_path) -> None:
    payload = await _handle_sandbox_policy_defaults({}, _ctx(tmp_path))
    assert payload["builtinDenyWritePaths"]


async def test_rpc_policy_update_reports_version_conflict(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    baseline = await _handle_sandbox_policy_get({}, ctx)
    await _handle_sandbox_policy_update(
        {"basePolicyVersion": 0, "policy": baseline},
        ctx,
    )

    with pytest.raises(RpcHandlerError) as exc_info:
        await _handle_sandbox_policy_update(
            {"basePolicyVersion": 0, "policy": baseline},
            ctx,
        )

    assert exc_info.value.code == "POLICY_VERSION_CONFLICT"
    assert exc_info.value.details["currentPolicy"]["policyVersion"] == 1


async def test_rpc_named_token_lifecycle_returns_secret_only_on_create(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    issued = await _handle_sandbox_token_create(
        {"name": "LAN laptop", "hostExecute": True},
        ctx,
    )
    listed = await _handle_sandbox_token_list({}, ctx)

    assert issued["token"].startswith(f"osq_{issued['record']['publicId']}_")
    assert issued["record"]["capabilities"] == [
        "host.execute",
        "task.read",
        "task.submit",
    ]
    assert listed == {"tokens": [issued["record"]]}
    assert "token" not in listed["tokens"][0]

    revoked = await _handle_sandbox_token_revoke(
        {"publicId": issued["record"]["publicId"]},
        ctx,
    )
    assert revoked["revoked"] is True
    assert await _handle_sandbox_token_list({}, ctx) == {"tokens": []}
