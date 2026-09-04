"""Transport-neutral Skill management use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict


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


class SkillReloadError(TypedDict, total=False):
    name: str
    code: str
    message: str


class SkillReloadResult(TypedDict):
    success: bool
    changed: bool
    partial: bool
    generation: int
    added: list[str]
    removed: list[str]
    modified: list[str]
    errors: list[SkillReloadError]


class SkillMutationResult(TypedDict):
    success: bool
    cancelled: NotRequired[bool]
    message: NotRequired[str]
    name: NotRequired[str]
    installId: NotRequired[str]


class SkillCancelResult(TypedDict):
    success: bool
    cancelled: bool
    message: str
    pending: bool


class SkillDependencyInstallResult(TypedDict):
    success: bool
    kind: str
    message: str
    missing_still: object


class SkillManagementPort(Protocol):
    async def reload(self) -> SkillReloadResult: ...

    async def install(self, command: InstallSkill) -> SkillMutationResult: ...

    async def cancel(self, command: CancelSkillInstall) -> SkillCancelResult: ...

    async def install_dependencies(
        self, command: InstallSkillDependencies
    ) -> SkillDependencyInstallResult: ...

    async def uninstall(self, command: UninstallSkill) -> SkillMutationResult: ...


class SkillManagement:
    """Own explicit mutation intents above the durable Skill runtime."""

    def __init__(self, port: SkillManagementPort) -> None:
        self._port = port

    async def reload(self) -> SkillReloadResult:
        return await self._port.reload()

    async def install(self, command: InstallSkill) -> SkillMutationResult:
        return await self._port.install(command)

    async def cancel(self, command: CancelSkillInstall) -> SkillCancelResult:
        return await self._port.cancel(command)

    async def install_dependencies(
        self, command: InstallSkillDependencies
    ) -> SkillDependencyInstallResult:
        return await self._port.install_dependencies(command)

    async def uninstall(self, command: UninstallSkill) -> SkillMutationResult:
        return await self._port.uninstall(command)


__all__ = [
    "CancelSkillInstall",
    "InstallSkill",
    "InstallSkillDependencies",
    "SkillManagement",
    "SkillManagementPort",
    "SkillCancelResult",
    "SkillDependencyInstallResult",
    "SkillMutationResult",
    "SkillReloadResult",
    "UninstallSkill",
]
