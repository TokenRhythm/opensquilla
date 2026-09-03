"""Transport-neutral Skill management use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class InstallSkill:
    identifier: str
    source: str = "clawhub"
    operation_id: str = ""
    force: bool = False
    replace_source: bool = False
    risk_confirmation: str = ""

    def __post_init__(self) -> None:
        if not self.identifier:
            raise ValueError("skill identifier is required")


@dataclass(frozen=True, slots=True)
class CancelSkillInstall:
    operation_id: str

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("skill install operation identity is required")


@dataclass(frozen=True, slots=True)
class InstallSkillDependencies:
    dependency_id: str
    name: str = ""
    skill_install_id: str = ""
    instance_id: str = ""

    def __post_init__(self) -> None:
        if not self.dependency_id:
            raise ValueError("skill dependency identity is required")
        if not self.name and not self.skill_install_id and not self.instance_id:
            raise ValueError("skill identity is required")


@dataclass(frozen=True, slots=True)
class UninstallSkill:
    name: str = ""
    install_id: str = ""
    allow_drift: bool = False

    def __post_init__(self) -> None:
        if not self.name and not self.install_id:
            raise ValueError("skill identity is required")


class SkillManagementPort(Protocol):
    async def reload(self) -> Mapping[str, Any]: ...

    async def install(self, command: InstallSkill) -> Mapping[str, Any]: ...

    async def cancel(self, command: CancelSkillInstall) -> Mapping[str, Any]: ...

    async def install_dependencies(
        self, command: InstallSkillDependencies
    ) -> Mapping[str, Any]: ...

    async def uninstall(self, command: UninstallSkill) -> Mapping[str, Any]: ...


class SkillManagement:
    """Own explicit mutation intents above the durable Skill runtime."""

    def __init__(self, port: SkillManagementPort) -> None:
        self._port = port

    async def reload(self) -> Mapping[str, Any]:
        return await self._port.reload()

    async def install(self, command: InstallSkill) -> Mapping[str, Any]:
        return await self._port.install(command)

    async def cancel(self, command: CancelSkillInstall) -> Mapping[str, Any]:
        return await self._port.cancel(command)

    async def install_dependencies(
        self, command: InstallSkillDependencies
    ) -> Mapping[str, Any]:
        return await self._port.install_dependencies(command)

    async def uninstall(self, command: UninstallSkill) -> Mapping[str, Any]:
        return await self._port.uninstall(command)


__all__ = [
    "CancelSkillInstall",
    "InstallSkill",
    "InstallSkillDependencies",
    "SkillManagement",
    "SkillManagementPort",
    "UninstallSkill",
]
