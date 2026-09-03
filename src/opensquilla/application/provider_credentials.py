"""Application Module for provider credential reveal and clearing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupRuntimePort,
    commit_setup_mutation,
)


class CredentialResolutionPort(Protocol):
    def reveal_active(self, provider_id: str) -> Mapping[str, Any]: ...

    def describe_clear_result(
        self, config: Any, provider_id: str, *, active: bool
    ) -> Mapping[str, Any]: ...


class ProviderCredentialMutationPort(Protocol):
    def clear_active(self, config: Any, provider_id: str) -> Any: ...


class ProviderCredentials:
    def __init__(
        self,
        config: SetupConfigPort,
        runtime: SetupRuntimePort,
        credentials: CredentialResolutionPort,
        mutations: ProviderCredentialMutationPort,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._credentials = credentials
        self._mutations = mutations

    def reveal_active(self, provider_id: str) -> dict[str, Any]:
        return dict(self._credentials.reveal_active(provider_id))

    async def clear_active(self, provider_id: str) -> SetupMutation:
        result = self._mutations.clear_active(
            self._config.active_config(), provider_id
        )

        async def reconcile(config: Any) -> None:
            await self._runtime.sync_primary_provider(config)
            await self._runtime.sync_media(config)
            await self._runtime.refresh_model_catalog(config)

        mutation = await commit_setup_mutation(
            result,
            config_port=self._config,
            effects=(reconcile,),
            backup_credential_provider=provider_id,
        )
        entry = {
            **mutation.entry,
            **self._credentials.describe_clear_result(
                self._config.active_config(), provider_id, active=True
            ),
        }
        return SetupMutation(
            changed=mutation.changed,
            restart_required=mutation.restart_required,
            config_path=mutation.config_path,
            entry=entry,
            warnings=mutation.warnings,
        )


__all__ = [
    "CredentialResolutionPort",
    "ProviderCredentialMutationPort",
    "ProviderCredentials",
]
