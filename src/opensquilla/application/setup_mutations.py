"""Shared transaction boundary for setup configuration mutations.

The onboarding mutation functions build detached configuration candidates.
This module owns the application-level ordering around those candidates:
durable persistence first, live installation second, and runtime reconciliation
last.  Gateway-specific state is hidden behind narrow ports.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict, cast


class SetupMutationEntry(TypedDict, total=False):
    providerId: str
    provider: str
    model: str
    apiKey: str
    apiKeyEnv: str
    apiKeyEnvPool: list[str]
    baseUrl: str
    proxy: str
    enabled: bool
    presetId: str
    routerAction: str
    imageGenerationIntent: str
    mode: str
    defaultTier: str
    tiers: dict[str, object]
    crossProviderTiers: bool
    tierProviderMismatch: str
    selectionMode: str
    modelOptions: list[str]
    candidates: list[dict[str, object]]
    minSuccessfulProposers: int
    proposerMaxRetries: int
    allFailedPolicy: str
    maxResults: int
    useEnvProxy: bool
    fallbackPolicy: str
    diagnostics: bool
    primary: str
    size: str
    outputFormat: str
    fallbacks: list[str]
    credentialMode: str
    onnxDir: str
    ttsVoice: str
    ttsModel: str
    languageCode: str
    capabilityId: str
    reset: bool
    removed: bool
    credentialAvailable: bool
    credentialSource: str
    credentialEnv: str
    externalCredentialActive: bool


class SetupMutationPayload(TypedDict):
    changed: bool
    restartRequired: bool
    configPath: str
    entry: SetupMutationEntry
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class SetupMutation:
    """Secret-safe result exposed by setup Application Modules."""

    changed: bool
    restart_required: bool
    config_path: str
    entry: SetupMutationEntry
    warnings: tuple[str, ...]

    def to_payload(self) -> SetupMutationPayload:
        return SetupMutationPayload(
            changed=self.changed,
            restartRequired=self.restart_required,
            configPath=self.config_path,
            entry=cast(SetupMutationEntry, dict(self.entry)),
            warnings=list(self.warnings),
        )


class SetupConfigPort(Protocol):
    """Durable configuration boundary used by setup mutations."""

    def active_config(self) -> Any: ...

    def persist_candidate(
        self,
        candidate: Any,
        *,
        restart_required: bool,
        backup_credential_provider: str | None = None,
        remove_paths: Sequence[str] = (),
    ) -> str: ...

    def install_candidate(self, candidate: Any) -> Any: ...


class SetupRuntimePort(Protocol):
    """Live-runtime effects that are allowed only after durable persistence."""

    async def sync_primary_provider(self, config: Any) -> None: ...

    async def sync_media(self, config: Any) -> None: ...

    async def sync_search(self, config: Any) -> None: ...

    async def refresh_model_catalog(self, config: Any) -> None: ...

    async def broadcast_model_routing(self, config: Any, *, source: str) -> None: ...

    async def discard_profile_credentials(self, provider_id: str) -> None: ...

    async def reconcile_profile_transition(
        self,
        previous_config: Any,
        current_config: Any,
        *,
        provider_id: str,
    ) -> None: ...


PostCommitEffect = Callable[[Any], Awaitable[None]]


async def commit_setup_mutation(
    result: Any,
    *,
    config_port: SetupConfigPort,
    effects: Sequence[PostCommitEffect] = (),
    backup_credential_provider: str | None = None,
    remove_paths: Sequence[str] = (),
) -> SetupMutation:
    """Persist a detached candidate, install it, then run live effects.

    A persistence exception stops before either live installation or runtime
    reconciliation, which is the central safety invariant for setup writes.
    """

    config_path = config_port.persist_candidate(
        result.config,
        restart_required=bool(result.restart_required),
        backup_credential_provider=backup_credential_provider,
        remove_paths=remove_paths,
    )
    live_config = config_port.install_candidate(result.config)
    for effect in effects:
        await effect(live_config)
    return SetupMutation(
        changed=bool(result.changed),
        restart_required=bool(result.restart_required),
        config_path=config_path,
        entry=cast(SetupMutationEntry, dict(result.public_payload)),
        warnings=tuple(str(item) for item in result.warnings),
    )


__all__ = [
    "SetupConfigPort",
    "SetupMutation",
    "SetupMutationEntry",
    "SetupMutationPayload",
    "SetupRuntimePort",
    "commit_setup_mutation",
]
