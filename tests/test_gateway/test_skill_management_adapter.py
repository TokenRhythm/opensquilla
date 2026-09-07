from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.skill_management import GatewaySkillManagementAdapter


@pytest.mark.asyncio
async def test_skill_management_adapter_projects_aliases_to_typed_commands() -> None:
    port = AsyncMock()
    port.reload.return_value = {"success": True}
    port.install.return_value = {"success": True}
    port.cancel.return_value = {"success": False, "cancelled": True}
    port.install_dependencies.return_value = {"success": True}
    port.uninstall.return_value = {"success": True}
    adapter = GatewaySkillManagementAdapter(port)

    await adapter.reload(None)
    operation_id = "00000000-0000-4000-8000-000000000001"
    await adapter.install(
        {
            "identifier": "@acme/demo",
            "source": "clawhub",
            "operation_id": operation_id,
            "force": True,
            "risk_confirmation": "ack",
        }
    )
    await adapter.cancel({"operationId": operation_id})
    await adapter.install_dependencies(
        {
            "name": "demo",
            "install_id": "python",
            "skill_install_id": "install-1",
            "instance_id": "managed:1",
        }
    )
    await adapter.uninstall({"install_id": "install-1", "allowDrift": True})

    install = port.install.await_args.args[0]
    assert install.identifier == "@acme/demo"
    assert install.operation_id == operation_id
    assert install.risk_confirmation == "ack"
    dependency = port.install_dependencies.await_args.args[0]
    assert dependency.dependency_id == "python"
    assert dependency.skill_install_id == "install-1"
    assert dependency.instance_id == "managed:1"
    uninstall = port.uninstall.await_args.args[0]
    assert uninstall.install_id == "install-1"
    assert uninstall.allow_drift is True


@pytest.mark.asyncio
async def test_skill_management_adapter_rejects_truthy_non_boolean_flags() -> None:
    port = AsyncMock()
    adapter = GatewaySkillManagementAdapter(port)

    with pytest.raises(ValueError, match="must be a boolean"):
        await adapter.install({"identifier": "demo", "force": 1})

    port.install.assert_not_awaited()
