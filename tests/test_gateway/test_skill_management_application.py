from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.skill_management import (
    CancelSkillInstall,
    InstallSkill,
    InstallSkillDependencies,
    SkillManagement,
    UninstallSkill,
)


@pytest.mark.asyncio
async def test_skill_management_dispatches_explicit_commands() -> None:
    port = AsyncMock()
    port.reload.return_value = {"success": True, "generation": 2}
    port.install.return_value = {"success": True, "name": "demo"}
    port.cancel.return_value = {"success": False, "cancelled": True}
    port.install_dependencies.return_value = {"success": True, "kind": "uv"}
    port.uninstall.return_value = {"success": True, "name": "demo"}
    management = SkillManagement(port)

    install = InstallSkill(
        identifier="@acme/demo",
        operation_id="operation-1",
        force=True,
        risk_confirmation="ack",
    )
    dependency = InstallSkillDependencies(dependency_id="python", name="demo")
    uninstall = UninstallSkill(name="demo", allow_drift=True)

    await management.reload()
    await management.install(install)
    await management.cancel(CancelSkillInstall("operation-1"))
    await management.install_dependencies(dependency)
    await management.uninstall(uninstall)

    port.install.assert_awaited_once_with(install)
    port.install_dependencies.assert_awaited_once_with(dependency)
    port.uninstall.assert_awaited_once_with(uninstall)


def test_skill_management_commands_require_business_identity() -> None:
    with pytest.raises(ValueError, match="identifier is required"):
        InstallSkill(identifier="")
    with pytest.raises(ValueError, match="operation identity is required"):
        CancelSkillInstall(operation_id="")
    with pytest.raises(ValueError, match="skill identity is required"):
        InstallSkillDependencies(dependency_id="python")
    with pytest.raises(ValueError, match="skill identity is required"):
        UninstallSkill()
