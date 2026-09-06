"""Application Module for provider credential reveal and clearing."""

from __future__ import annotations

from typing import Any, NotRequired, Protocol, TypedDict, cast

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupMutationEntry,
    SetupRuntimePort,
    commit_setup_mutation,
)


class CredentialRevealResult(TypedDict):
    ok: bool
    provider: str
    source: str
    envKey: NotRequired[str | None]
    apiKey: str


class CredentialClearDescription(TypedDict):
    credentialAvailable: bool
    credentialSource: str
    credentialEnv: str
    externalCredentialActive: bool


class CredentialResolutionPort(Protocol):
    def reveal_active(self, provider_id: str) -> CredentialRevealResult: ...

    def describe_clear_result(
        self, config: Any, provider_id: str, *, active: bool
    ) -> CredentialClearDescription: ...


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

    def reveal_active(self, provider_id: str) -> CredentialRevealResult:
        return cast(
            CredentialRevealResult,
            dict(self._credentials.reveal_active(provider_id)),
        )

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
        entry = cast(
            SetupMutationEntry,
            {
                **mutation.entry,
                **self._credentials.describe_clear_result(
                    self._config.active_config(), provider_id, active=True
                ),
            },
        )
        return SetupMutation(
            changed=mutation.changed,
            restart_required=mutation.restart_required,
            config_path=mutation.config_path,
            entry=entry,
            warnings=mutation.warnings,
        )


__all__ = [
    "CredentialClearDescription",
    "CredentialRevealResult",
    "CredentialResolutionPort",
    "ProviderCredentialMutationPort",
    "ProviderCredentials",
]
