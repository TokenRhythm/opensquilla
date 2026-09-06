"""Gateway adapters for setup mutation Application ports.

The adapters bind explicit Gateway dependencies to narrow configuration,
runtime, and credential ports. Application Modules never receive or import a
transport context.
"""

from __future__ import annotations

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
    RemoveActiveProfile,
    UpsertProfile,
)
from opensquilla.application.provider_credentials import (
    CredentialClearDescription,
    CredentialResolutionPort,
    CredentialRevealResult,
    ProviderCredentialMutationPort,
)
from opensquilla.application.provider_setup import (
    ConfigurePrimaryProvider,
    PrimaryProviderMutationPort,
)
from opensquilla.application.setup_mutations import SetupRuntimePort


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
            tiers=dict(command.tiers) if command.tiers is not None else None,
            cross_provider_tiers=command.cross_provider_tiers,
            tier_provider_mismatch=command.tier_provider_mismatch,
        )

    def configure_ensemble(self, config: Any, command: ConfigureEnsemble) -> Any:
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        return upsert_llm_ensemble(
            config,
            enabled=command.enabled,
            selection_mode=command.selection_mode,
            model_options=(
                list(command.model_options)
                if command.model_options is not None
                else None
            ),
            candidates=(
                list(command.candidates) if command.candidates is not None else None
            ),
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


class GatewaySetupRuntimePort(SetupRuntimePort):
    """Apply post-commit setup effects through concrete runtime primitives."""

    def __init__(self, provider_selector: Any, subscription_manager: Any) -> None:
        self._provider_selector = provider_selector
        self._subscription_manager = subscription_manager

    async def sync_primary_provider(self, config: Any) -> None:
        from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
        from opensquilla.provider.selector import ProviderConfig

        if self._provider_selector is None or not hasattr(
            self._provider_selector, "sync_primary"
        ):
            return
        scratch = config.model_copy(deep=True)
        runtime = resolve_llm_runtime_config(scratch)
        if runtime.api_key_from_env and hasattr(config, "mark_runtime_secret"):
            config.mark_runtime_secret("llm.api_key")
        self._provider_selector.sync_primary(
            ProviderConfig(
                provider=runtime.provider,
                model=runtime.model,
                api_key=runtime.api_key,
                base_url=runtime.base_url,
                proxy=runtime.proxy,
                provider_routing=runtime.provider_routing,
            )
        )

    async def sync_media(self, config: Any) -> None:
        from opensquilla.gateway.setup_config_runtime import sync_media_runtime

        sync_media_runtime(config)

    async def sync_search(self, config: Any) -> None:
        from opensquilla.tools.builtin.web import configure_search

        configure_search(
            provider_name=config.search_provider,
            max_results=config.search_max_results,
            api_key=config.search_api_key,
            api_key_env=getattr(config, "search_api_key_env", ""),
            proxy=config.search_proxy,
            use_env_proxy=config.search_use_env_proxy,
            fallback_policy=config.search_fallback_policy,
            diagnostics=config.search_diagnostics,
        )

    async def refresh_model_catalog(self, config: Any) -> None:
        from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

        await refresh_live_model_catalog(config)

    async def broadcast_model_routing(self, config: Any, *, source: str) -> None:
        if self._subscription_manager is None:
            return
        from opensquilla.gateway.event_bridge import EventBridge
        from opensquilla.gateway.model_routing import model_routing_public_snapshot
        from opensquilla.gateway.scopes import READ_SCOPE
        from opensquilla.gateway.websocket import get_registry

        await EventBridge(
            self._subscription_manager,
            get_registry(),
        ).broadcast_scoped(
            "models.routing.changed",
            {**model_routing_public_snapshot(config), "source": source},
            required_scope=READ_SCOPE,
        )

    async def discard_profile_credentials(self, provider_id: str) -> None:
        from opensquilla.gateway.llm_runtime import discard_profile_credential_pool

        discard_profile_credential_pool(provider_id)

    async def reconcile_profile_transition(
        self,
        previous_config: Any,
        current_config: Any,
        *,
        provider_id: str,
    ) -> None:
        from opensquilla.gateway.model_catalog_refresh import (
            reconcile_tokenrhythm_profile_transition,
        )

        await reconcile_tokenrhythm_profile_transition(
            previous_config,
            current_config,
            provider_id=provider_id,
        )


class GatewayCredentialResolutionPort(CredentialResolutionPort):
    def __init__(self, config: Any, *, is_owner: bool) -> None:
        self._config = config
        self._is_owner = is_owner

    def reveal_active(self, provider_id: str) -> CredentialRevealResult:
        from opensquilla.gateway.llm_runtime import resolve_llm_credential
        from opensquilla.gateway.rpc import RpcHandlerError
        from opensquilla.onboarding.provider_specs import get_provider_setup_spec

        if not self._is_owner:
            raise RpcHandlerError(
                "onboarding.provider.credential.not_owner",
                "Only the local gateway owner can reveal provider credentials.",
            )
        llm = getattr(self._config, "llm", None)
        active_provider = str(getattr(llm, "provider", "") or "").strip().lower()
        requested_provider = str(provider_id or "").strip().lower()
        if requested_provider != active_provider:
            raise RpcHandlerError(
                "onboarding.provider.credential.inactive_provider",
                "Credential reveal only supports the active provider.",
            )
        try:
            spec = get_provider_setup_spec(active_provider)
        except KeyError as exc:
            raise RpcHandlerError(
                "onboarding.provider.credential.unsupported_provider",
                f"Unsupported active provider: {active_provider}",
            ) from exc
        credential = resolve_llm_credential(
            self._config,
            registry_env_key=str(getattr(spec, "env_key", "") or "").strip(),
            include_runtime_cache=False,
        )
        if credential.source in {"explicit", "env"} and credential.api_key:
            return CredentialRevealResult(
                ok=True,
                provider=active_provider,
                source=credential.source,
                envKey=credential.env_name,
                apiKey=credential.api_key,
            )
        raise RpcHandlerError(
            "onboarding.provider.credential.unavailable",
            "No revealable credential is available for the active provider.",
        )

    def describe_clear_result(
        self, config: Any, provider_id: str, *, active: bool
    ) -> CredentialClearDescription:
        from opensquilla.onboarding.status import get_onboarding_status

        provider = str(provider_id or "").strip().lower()
        status = get_onboarding_status(config)
        if active:
            row = dict(status.llm_credential_status)
            source = str(row.get("source") or "none")
            env_key = str(row.get("envKey") or "")
            available = bool(row.get("available"))
        else:
            row = next(
                (
                    dict(candidate)
                    for candidate in status.llm_profile_status
                    if str(candidate.get("provider") or "").strip().lower()
                    == provider
                ),
                {},
            )
            raw_source = str(row.get("credentialSource") or "none")
            if raw_source in {
                "member_env",
                "profile_env",
                "profile_pool",
                "profile_pool_env",
                "registry_env",
            }:
                source = "env"
            elif raw_source == "keyless":
                source = "not_required"
            elif raw_source in {"member", "profile", "inherited"}:
                source = "explicit"
            else:
                source = "none"
            env_key = str(row.get("credentialEnv") or "")
            available = source in {"explicit", "env", "not_required"}
        return CredentialClearDescription(
            credentialAvailable=available,
            credentialSource=source,
            credentialEnv=env_key,
            externalCredentialActive=source == "env",
        )


__all__ = [
    "GatewayCredentialResolutionPort",
    "GatewaySetupRuntimePort",
    "OnboardingSetupMutationPort",
]
