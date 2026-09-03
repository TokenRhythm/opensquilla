"""Application Module for non-primary provider profile lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupRuntimePort,
    commit_setup_mutation,
)


@dataclass(frozen=True, slots=True)
class UpsertProfile:
    provider_id: str
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    api_key_env_pool: Sequence[str] | None = None
    keep_current_secret: bool = False
    base_url: str | None = None
    proxy: str | None = None


@dataclass(frozen=True, slots=True)
class ActivateProfile:
    provider_id: str
    model: str = ""
    router_action: str = "preserve"
    image_generation_intent: str = "preserve"


@dataclass(frozen=True, slots=True)
class RemoveActiveProfile:
    provider_id: str
    replacement_provider_id: str
    replacement_model: str = ""
    router_action: str = "preserve"
    image_generation_intent: str = "preserve"


@dataclass(frozen=True, slots=True)
class ProfileProbeCommand:
    provider_id: str
    values: Mapping[str, Any]


class ProfileProbePort(Protocol):
    async def probe_saved(self, command: ProfileProbeCommand) -> Mapping[str, Any]: ...

    async def probe_draft(self, command: ProfileProbeCommand) -> Mapping[str, Any]: ...

    async def discover_saved(self, command: ProfileProbeCommand) -> Mapping[str, Any]: ...

    async def discover_draft(self, command: ProfileProbeCommand) -> Mapping[str, Any]: ...


class ProfileMutationPort(Protocol):
    def upsert(self, config: Any, command: UpsertProfile) -> Any: ...

    def activate(self, config: Any, command: ActivateProfile) -> Any: ...

    def remove(self, config: Any, provider_id: str) -> Any: ...

    def remove_active(self, config: Any, command: RemoveActiveProfile) -> Any: ...

    def clear_credentials(self, config: Any, provider_id: str) -> Any: ...


class ProfileLifecycle:
    def __init__(
        self,
        config: SetupConfigPort,
        runtime: SetupRuntimePort,
        probes: ProfileProbePort,
        mutations: ProfileMutationPort,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._probes = probes
        self._mutations = mutations

    async def upsert(self, command: UpsertProfile) -> SetupMutation:
        current = self._config.active_config()
        previous = current.model_copy(deep=True)
        old_signature = _credential_signature(current, command.provider_id)
        result = self._mutations.upsert(current, command)
        changed_credentials = old_signature != _credential_signature(
            result.config, command.provider_id
        )

        async def reconcile(config: Any) -> None:
            if changed_credentials:
                await self._runtime.discard_profile_credentials(command.provider_id)
            await self._runtime.reconcile_profile_transition(
                previous, config, provider_id=command.provider_id
            )

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def activate(self, command: ActivateProfile) -> SetupMutation:
        result = self._mutations.activate(self._config.active_config(), command)

        async def reconcile(config: Any) -> None:
            await self._runtime.sync_primary_provider(config)
            await self._runtime.sync_media(config)
            await self._runtime.refresh_model_catalog(config)

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def remove(self, provider_id: str) -> SetupMutation:
        current = self._config.active_config()
        previous = current.model_copy(deep=True)
        result = self._mutations.remove(current, provider_id)

        async def reconcile(config: Any) -> None:
            await self._runtime.discard_profile_credentials(provider_id)
            await self._runtime.reconcile_profile_transition(
                previous, config, provider_id=provider_id
            )

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def remove_active(self, command: RemoveActiveProfile) -> SetupMutation:
        current = self._config.active_config()
        previous = current.model_copy(deep=True)
        result = self._mutations.remove_active(current, command)

        async def reconcile(config: Any) -> None:
            await self._runtime.discard_profile_credentials(command.provider_id)
            await self._runtime.reconcile_profile_transition(
                previous, config, provider_id=command.provider_id
            )
            await self._runtime.sync_primary_provider(config)
            await self._runtime.sync_media(config)
            await self._runtime.refresh_model_catalog(config)

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def clear_credentials(self, provider_id: str) -> SetupMutation:
        current = self._config.active_config()
        previous = current.model_copy(deep=True)
        result = self._mutations.clear_credentials(current, provider_id)

        async def reconcile(config: Any) -> None:
            await self._runtime.discard_profile_credentials(provider_id)
            await self._runtime.reconcile_profile_transition(
                previous, config, provider_id=provider_id
            )
            await self._runtime.sync_media(config)

        return await commit_setup_mutation(
            result,
            config_port=self._config,
            effects=(reconcile,),
            backup_credential_provider=provider_id,
        )

    async def probe(self, command: ProfileProbeCommand) -> dict[str, Any]:
        return dict(await self._probes.probe_saved(command))

    async def probe_draft(self, command: ProfileProbeCommand) -> dict[str, Any]:
        return dict(await self._probes.probe_draft(command))

    async def discover_models(self, command: ProfileProbeCommand) -> dict[str, Any]:
        return dict(await self._probes.discover_saved(command))

    async def discover_draft_models(
        self, command: ProfileProbeCommand
    ) -> dict[str, Any]:
        return dict(await self._probes.discover_draft(command))


def _credential_signature(config: Any, provider_id: str) -> tuple[object, ...]:
    provider = provider_id.strip().lower()
    for key, profile in (getattr(config, "llm_profiles", None) or {}).items():
        if str(key or "").strip().lower() == provider:
            return (
                str(getattr(profile, "api_key", "") or ""),
                str(getattr(profile, "api_key_env", "") or ""),
                tuple(getattr(profile, "api_key_env_pool", None) or ()),
            )
    return ()


__all__ = [
    "ActivateProfile",
    "ProfileLifecycle",
    "ProfileProbeCommand",
    "ProfileProbePort",
    "ProfileMutationPort",
    "RemoveActiveProfile",
    "UpsertProfile",
]
