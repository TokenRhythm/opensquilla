"""Gateway adapters for setup mutation Application ports.

The adapters bind a single ``RpcContext`` to narrow configuration, runtime,
probe, and credential ports.  Application Modules never receive or import the
context itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from opensquilla.application.capability_setup import (
    CapabilityMutationPort,
    ConfigureAudio,
    ConfigureEnsemble,
    ConfigureImageGeneration,
    ConfigureMemoryEmbedding,
    ConfigureRouter,
    ConfigureSearch,
)
from opensquilla.application.profile_lifecycle import (
    ActivateProfile,
    ProfileMutationPort,
    ProfileProbeCommand,
    ProfileProbePort,
    RemoveActiveProfile,
    UpsertProfile,
)
from opensquilla.application.provider_credentials import (
    CredentialResolutionPort,
    ProviderCredentialMutationPort,
)
from opensquilla.application.provider_setup import (
    ConfigurePrimaryProvider,
    DiscoverPrimaryModels,
    PrimaryProviderMutationPort,
    ProbePrimaryProvider,
    ProviderProbePort,
)
from opensquilla.application.setup_mutations import SetupConfigPort, SetupRuntimePort
from opensquilla.gateway.rpc import RpcContext

ActiveConfig = Callable[[RpcContext], Any]
PersistCandidate = Callable[..., str]
InstallCandidate = Callable[[RpcContext, Any], None]
RuntimeEffect = Callable[[RpcContext, Any], Awaitable[None]]
ConfigEffect = Callable[[Any], None]
ProfileEffect = Callable[[str], None]
ProfileReconcile = Callable[[Any, Any, str], Awaitable[None]]
RoutingBroadcast = Callable[[RpcContext, Any, str], Awaitable[None]]
ProviderProbe = Callable[[Mapping[str, Any], RpcContext], Awaitable[Mapping[str, Any]]]
CredentialReveal = Callable[[RpcContext, str], Mapping[str, Any]]
CredentialDescribe = Callable[[Any, str, bool], Mapping[str, Any]]


class OnboardingSetupMutationPort(
    PrimaryProviderMutationPort,
    ProfileMutationPort,
    CapabilityMutationPort,
    ProviderCredentialMutationPort,
):
    """Bind existing pure onboarding mutations to typed Application commands."""

    def configure_primary(self, config: Any, command: ConfigurePrimaryProvider) -> Any:
        from opensquilla.onboarding.mutations import upsert_llm_provider

        return upsert_llm_provider(
            config,
            provider_id=command.provider_id,
            model=command.model,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            preserve_api_key=command.preserve_api_key,
            base_url=command.base_url,
            proxy=command.proxy,
            preset_id=command.preset_id,
            router_action=command.router_action,
            image_generation_intent=command.image_generation_intent,
        )

    def upsert(self, config: Any, command: UpsertProfile) -> Any:
        from opensquilla.onboarding.mutations import upsert_llm_profile

        return upsert_llm_profile(
            config,
            provider_id=command.provider_id,
            model=command.model,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            api_key_env_pool=(
                list(command.api_key_env_pool)
                if command.api_key_env_pool is not None
                else None
            ),
            preserve_api_key=command.keep_current_secret,
            base_url=command.base_url,
            proxy=command.proxy,
        )

    def activate(self, config: Any, command: ActivateProfile) -> Any:
        from opensquilla.onboarding.mutations import activate_llm_profile

        return activate_llm_profile(
            config,
            provider_id=command.provider_id,
            model=command.model,
            router_action=command.router_action,
            image_generation_intent=command.image_generation_intent,
        )

    def remove(self, config: Any, provider_id: str) -> Any:
        from opensquilla.onboarding.mutations import remove_llm_profile

        return remove_llm_profile(config, provider_id=provider_id)

    def remove_active(self, config: Any, command: RemoveActiveProfile) -> Any:
        from opensquilla.onboarding.mutations import remove_active_llm_profile

        return remove_active_llm_profile(
            config,
            provider_id=command.provider_id,
            replacement_provider_id=command.replacement_provider_id,
            replacement_model=command.replacement_model,
            router_action=command.router_action,
            image_generation_intent=command.image_generation_intent,
        )

    def clear_credentials(self, config: Any, provider_id: str) -> Any:
        from opensquilla.onboarding.mutations import clear_llm_profile_credentials

        return clear_llm_profile_credentials(config, provider_id=provider_id)

    def clear_active(self, config: Any, provider_id: str) -> Any:
        from opensquilla.onboarding.mutations import clear_llm_provider_credentials

        return clear_llm_provider_credentials(config, provider_id=provider_id)

    def configure_router(self, config: Any, command: ConfigureRouter) -> Any:
        from opensquilla.onboarding.mutations import upsert_router

        return upsert_router(
            config,
            mode=command.mode,
            default_tier=command.default_tier,
            tiers=command.tiers,
            cross_provider_tiers=command.cross_provider_tiers,
            tier_provider_mismatch=command.tier_provider_mismatch,
        )

    def configure_ensemble(self, config: Any, command: ConfigureEnsemble) -> Any:
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        return upsert_llm_ensemble(
            config,
            enabled=command.enabled,
            selection_mode=command.selection_mode,
            model_options=command.model_options,
            candidates=command.candidates,
            min_successful_proposers=command.min_successful_proposers,
            proposer_max_retries=command.proposer_max_retries,
            all_failed_policy=command.all_failed_policy,
        )

    def configure_search(self, config: Any, command: ConfigureSearch) -> Any:
        from opensquilla.onboarding.mutations import upsert_search_provider

        return upsert_search_provider(
            config,
            provider_id=command.provider_id,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            max_results=command.max_results,
            proxy=command.proxy,
            use_env_proxy=command.use_env_proxy,
            fallback_policy=command.fallback_policy,
            diagnostics=command.diagnostics,
        )

    def configure_image_generation(
        self, config: Any, command: ConfigureImageGeneration
    ) -> Any:
        from opensquilla.onboarding.mutations import upsert_image_generation_provider

        return upsert_image_generation_provider(
            config,
            provider_id=command.provider_id,
            primary=command.primary,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            base_url=command.base_url,
            enabled=command.enabled,
            size=command.size,
            output_format=command.output_format,
            fallbacks=list(command.fallbacks) if command.fallbacks is not None else None,
            clear_fallbacks=command.clear_fallbacks,
            credential_mode=command.credential_mode,
        )

    def configure_memory_embedding(
        self, config: Any, command: ConfigureMemoryEmbedding
    ) -> Any:
        from opensquilla.onboarding.mutations import upsert_memory_embedding

        return upsert_memory_embedding(
            config,
            provider=command.provider_id,
            model=command.model,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            base_url=command.base_url,
            onnx_dir=command.onnx_dir,
        )

    def configure_audio(self, config: Any, command: ConfigureAudio) -> Any:
        from opensquilla.onboarding.mutations import upsert_audio_provider

        return upsert_audio_provider(
            config,
            provider_id=command.provider_id,
            api_key=command.api_key,
            api_key_env=command.api_key_env,
            base_url=command.base_url,
            enabled=command.enabled,
            tts_voice=command.tts_voice,
            tts_model=command.tts_model,
            language_code=command.language_code,
        )

    def reset(self, config: Any, capability_id: str) -> Any:
        from opensquilla.onboarding.mutations import reset_capability

        return reset_capability(config, capability_id=capability_id)


class RpcContextSetupConfigPort(SetupConfigPort):
    def __init__(
        self,
        ctx: RpcContext,
        *,
        active: ActiveConfig,
        persist: PersistCandidate,
        install: InstallCandidate,
    ) -> None:
        self._ctx = ctx
        self._active = active
        self._persist = persist
        self._install = install

    def active_config(self) -> Any:
        return self._active(self._ctx)

    def persist_candidate(
        self,
        candidate: Any,
        *,
        restart_required: bool,
        backup_credential_provider: str | None = None,
        remove_paths: Sequence[str] = (),
    ) -> str:
        return self._persist(
            self._ctx,
            candidate,
            restart_required=restart_required,
            backup_credential_provider=backup_credential_provider,
            remove_paths=tuple(remove_paths),
        )

    def install_candidate(self, candidate: Any) -> Any:
        self._install(self._ctx, candidate)
        return self.active_config()


class RpcContextSetupRuntimePort(SetupRuntimePort):
    def __init__(
        self,
        ctx: RpcContext,
        *,
        sync_primary: ConfigEffect,
        sync_media: ConfigEffect,
        sync_search: ConfigEffect,
        refresh_catalog: Callable[[Any], Awaitable[None]],
        broadcast_routing: RoutingBroadcast,
        discard_profile: ProfileEffect,
        reconcile_profile: ProfileReconcile,
    ) -> None:
        self._ctx = ctx
        self._sync_primary = sync_primary
        self._sync_media = sync_media
        self._sync_search = sync_search
        self._refresh_catalog = refresh_catalog
        self._broadcast_routing = broadcast_routing
        self._discard_profile = discard_profile
        self._reconcile_profile = reconcile_profile

    async def sync_primary_provider(self, config: Any) -> None:
        self._sync_primary(config)

    async def sync_media(self, config: Any) -> None:
        self._sync_media(config)

    async def sync_search(self, config: Any) -> None:
        self._sync_search(config)

    async def refresh_model_catalog(self, config: Any) -> None:
        await self._refresh_catalog(config)

    async def broadcast_model_routing(self, config: Any, *, source: str) -> None:
        await self._broadcast_routing(self._ctx, config, source)

    async def discard_profile_credentials(self, provider_id: str) -> None:
        self._discard_profile(provider_id)

    async def reconcile_profile_transition(
        self,
        previous_config: Any,
        current_config: Any,
        *,
        provider_id: str,
    ) -> None:
        await self._reconcile_profile(previous_config, current_config, provider_id)


class RpcContextProviderProbePort(ProviderProbePort):
    def __init__(
        self,
        ctx: RpcContext,
        *,
        probe: ProviderProbe,
        discover: ProviderProbe,
        discover_images: ProviderProbe,
    ) -> None:
        self._ctx = ctx
        self._probe = probe
        self._discover = discover
        self._discover_images = discover_images

    async def probe_primary(self, command: ProbePrimaryProvider) -> Mapping[str, Any]:
        return await self._probe(_provider_probe_params(command), self._ctx)

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> Mapping[str, Any]:
        return await self._discover(_provider_discovery_params(command), self._ctx)

    async def discover_image_models(self, provider_id: str) -> Mapping[str, Any]:
        return await self._discover_images({"providerId": provider_id}, self._ctx)


class RpcContextProfileProbePort(ProfileProbePort):
    def __init__(
        self,
        ctx: RpcContext,
        *,
        probe_saved: ProviderProbe,
        probe_draft: ProviderProbe,
        discover_saved: ProviderProbe,
        discover_draft: ProviderProbe,
    ) -> None:
        self._ctx = ctx
        self._probe_saved = probe_saved
        self._probe_draft = probe_draft
        self._discover_saved = discover_saved
        self._discover_draft = discover_draft

    async def probe_saved(self, command: ProfileProbeCommand) -> Mapping[str, Any]:
        return await self._probe_saved(dict(command.values), self._ctx)

    async def probe_draft(self, command: ProfileProbeCommand) -> Mapping[str, Any]:
        return await self._probe_draft(dict(command.values), self._ctx)

    async def discover_saved(self, command: ProfileProbeCommand) -> Mapping[str, Any]:
        return await self._discover_saved(dict(command.values), self._ctx)

    async def discover_draft(self, command: ProfileProbeCommand) -> Mapping[str, Any]:
        return await self._discover_draft(dict(command.values), self._ctx)


class RpcContextCredentialResolutionPort(CredentialResolutionPort):
    def __init__(
        self,
        ctx: RpcContext,
        *,
        reveal: CredentialReveal,
        describe: CredentialDescribe,
    ) -> None:
        self._ctx = ctx
        self._reveal = reveal
        self._describe = describe

    def reveal_active(self, provider_id: str) -> Mapping[str, Any]:
        return self._reveal(self._ctx, provider_id)

    def describe_clear_result(
        self, config: Any, provider_id: str, *, active: bool
    ) -> Mapping[str, Any]:
        return self._describe(config, provider_id, active)


def _provider_probe_params(command: ProbePrimaryProvider) -> dict[str, Any]:
    return {
        "providerId": command.provider_id,
        "model": command.model,
        "apiKey": command.api_key,
        "apiKeyEnv": command.api_key_env,
        "baseUrl": command.base_url,
        "proxy": command.proxy,
        "preserveApiKey": command.preserve_api_key,
    }


def _provider_discovery_params(command: DiscoverPrimaryModels) -> dict[str, Any]:
    return {
        "providerId": command.provider_id,
        "apiKey": command.api_key,
        "apiKeyEnv": command.api_key_env,
        "baseUrl": command.base_url,
        "proxy": command.proxy,
        "forceRefresh": command.force_refresh,
    }


__all__ = [
    "OnboardingSetupMutationPort",
    "RpcContextCredentialResolutionPort",
    "RpcContextProfileProbePort",
    "RpcContextProviderProbePort",
    "RpcContextSetupConfigPort",
    "RpcContextSetupRuntimePort",
]
