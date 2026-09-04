"""Gateway Adapter for SkillManagement commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from opensquilla.application.skill_management import (
    CancelSkillInstall,
    InstallSkill,
    InstallSkillDependencies,
    SkillManagement,
    SkillManagementPort,
    UninstallSkill,
)
from opensquilla.gateway.rpc import RpcContext

type MutationHandler = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]


class GatewaySkillManagementPort(SkillManagementPort):
    """Terminate typed commands at the existing fenced Skill runtime."""

    def __init__(
        self,
        context: RpcContext,
        *,
        reload: MutationHandler,
        install: MutationHandler,
        cancel: MutationHandler,
        install_dependencies: MutationHandler,
        uninstall: MutationHandler,
    ) -> None:
        self._context = context
        self._reload = reload
        self._install = install
        self._cancel = cancel
        self._install_dependencies = install_dependencies
        self._uninstall = uninstall

    async def reload(self) -> Mapping[str, Any]:
        return await self._reload(None, self._context)

    async def install(self, command: InstallSkill) -> Mapping[str, Any]:
        return await self._install(
            {
                "identifier": command.identifier,
                "source": command.source,
                **({"operationId": command.operation_id} if command.operation_id else {}),
                **({"force": True} if command.force else {}),
                **({"replaceSource": True} if command.replace_source else {}),
                **(
                    {"riskConfirmation": command.risk_confirmation}
                    if command.risk_confirmation
                    else {}
                ),
            },
            self._context,
        )

    async def cancel(self, command: CancelSkillInstall) -> Mapping[str, Any]:
        return await self._cancel(
            {"operationId": command.operation_id},
            self._context,
        )

    async def install_dependencies(
        self, command: InstallSkillDependencies
    ) -> Mapping[str, Any]:
        return await self._install_dependencies(
            {
                "install_id": command.dependency_id,
                **({"name": command.name} if command.name else {}),
                **(
                    {"installId": command.skill_install_id}
                    if command.skill_install_id
                    else {}
                ),
                **({"instanceId": command.instance_id} if command.instance_id else {}),
            },
            self._context,
        )

    async def uninstall(self, command: UninstallSkill) -> Mapping[str, Any]:
        return await self._uninstall(
            {
                **({"name": command.name} if command.name else {}),
                **({"installId": command.install_id} if command.install_id else {}),
                **({"allowDrift": True} if command.allow_drift else {}),
            },
            self._context,
        )


class GatewaySkillManagementAdapter:
    """Translate stable v4 aliases to typed Skill management commands."""

    def __init__(self, port: SkillManagementPort) -> None:
        self._application = SkillManagement(port)

    async def reload(self, params: dict[str, Any] | None) -> dict[str, Any]:
        del params
        return dict(await self._application.reload())

    async def install(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict) or "identifier" not in params:
            raise ValueError("params.identifier is required")
        identifier = params["identifier"]
        if not isinstance(identifier, str):
            raise ValueError("params.identifier must be a string")
        source = params.get("source", "clawhub")
        if not isinstance(source, str):
            raise ValueError("params.source must be a string")
        command = InstallSkill(
            identifier=identifier,
            source=source,
            operation_id=self._identity(params, "operationId", "operation_id"),
            force=self._boolean(params, "force"),
            replace_source=self._boolean(params, "replaceSource"),
            risk_confirmation=self._identity(
                params, "riskConfirmation", "risk_confirmation"
            ),
        )
        return dict(await self._application.install(command))

    async def cancel(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params.operationId is required")
        operation_id = self._identity(params, "operationId", "operation_id")
        if not operation_id:
            raise ValueError("params.operationId is required")
        return dict(
            await self._application.cancel(CancelSkillInstall(operation_id))
        )

    async def install_dependencies(
        self, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params must be a dict")
        if "install_id" not in params:
            raise ValueError("params.install_id is required")
        dependency_id = params["install_id"]
        if not isinstance(dependency_id, str):
            raise ValueError("params.install_id must be a string")
        try:
            command = InstallSkillDependencies(
                dependency_id=dependency_id,
                name=str(params.get("name") or ""),
                skill_install_id=self._identity(
                    params, "installId", "skill_install_id"
                ),
                instance_id=self._identity(params, "instanceId", "instance_id"),
            )
        except ValueError as exc:
            raise ValueError(
                "params.name, params.installId, or params.instanceId is required"
            ) from exc
        return dict(await self._application.install_dependencies(command))

    async def uninstall(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params.name or params.installId is required")
        install_id = self._identity(params, "installId", "install_id")
        allow_drift = self._boolean(params, "allowDrift")
        try:
            command = UninstallSkill(
                name=str(params.get("name") or ""),
                install_id=install_id,
                allow_drift=allow_drift,
            )
        except ValueError as exc:
            raise ValueError("params.name or params.installId is required") from exc
        return dict(await self._application.uninstall(command))

    @staticmethod
    def _identity(params: Mapping[str, Any], camel: str, snake: str) -> str:
        values = [params[key] for key in (camel, snake) if key in params]
        if not values:
            return ""
        if any(not isinstance(value, str) for value in values):
            raise ValueError(f"params.{camel} must be a string")
        normalized = [cast(str, value).strip() for value in values]
        if len(set(normalized)) > 1:
            raise ValueError(f"params.{camel} and params.{snake} must match")
        return normalized[0]

    @staticmethod
    def _boolean(params: Mapping[str, Any], name: str) -> bool:
        if name not in params:
            return False
        value = params[name]
        if not isinstance(value, bool):
            raise ValueError(f"params.{name} must be a boolean")
        return value


__all__ = ["GatewaySkillManagementAdapter", "GatewaySkillManagementPort"]
