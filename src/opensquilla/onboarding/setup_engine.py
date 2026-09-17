"""Shared onboarding setup engine for CLI and RPC paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.audio_specs import audio_provider_catalog_payload
from opensquilla.onboarding.channel_specs import channel_catalog_payload
from opensquilla.onboarding.config_store import PersistResult, load_config, persist_config
from opensquilla.onboarding.image_generation_specs import (
    image_generation_provider_catalog_payload,
)
from opensquilla.onboarding.image_generation_state import (
    default_image_generation_intent_for_provider,
)
from opensquilla.onboarding.memory_embedding_specs import (
    memory_embedding_provider_catalog_payload,
)
from opensquilla.onboarding.mutations import (
    MutationResult,
    disable_image_generation,
    upsert_audio_provider,
    upsert_channel,
    upsert_image_generation_provider,
    upsert_llm_ensemble,
    upsert_llm_provider,
    upsert_memory_embedding,
    upsert_router,
    upsert_search_provider,
)
from opensquilla.onboarding.next_steps import format_next_steps
from opensquilla.onboarding.provider_specs import provider_catalog_payload
from opensquilla.onboarding.router_specs import router_catalog_payload
from opensquilla.onboarding.search_specs import search_provider_catalog_payload
from opensquilla.onboarding.status import OnboardingStatus, get_onboarding_status

IMAGE_GENERATION_SECTION_ALIASES = frozenset(
    {"image", "image-generation", "image_generation"}
)
MEMORY_EMBEDDING_SECTION_ALIASES = frozenset(
    {"memory", "memory-embedding", "memory_embedding"}
)
AUDIO_SECTION_ALIASES = frozenset({"audio", "voice-audio", "voice_audio"})
ENSEMBLE_SECTION_ALIASES = frozenset({"ensemble", "llm-ensemble", "llm_ensemble"})

_CATALOG_SECTION_ALIASES = {
    "provider": "providers",
    "providers": "providers",
    "router": "routerProfiles",
    "search": "searchProviders",
    "channels": "channels",
    "channel": "channels",
    **{alias: "imageGenerationProviders" for alias in IMAGE_GENERATION_SECTION_ALIASES},
    **{alias: "audioProviders" for alias in AUDIO_SECTION_ALIASES},
    **{alias: "memoryEmbeddingProviders" for alias in MEMORY_EMBEDDING_SECTION_ALIASES},
}


def setup_catalog_payload(section: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "providers": provider_catalog_payload(),
        "routerProfiles": router_catalog_payload(),
        "searchProviders": search_provider_catalog_payload(),
        "channels": channel_catalog_payload(),
        "imageGenerationProviders": image_generation_provider_catalog_payload(),
        "audioProviders": audio_provider_catalog_payload(),
        "memoryEmbeddingProviders": memory_embedding_provider_catalog_payload(),
    }
    if section is None:
        return payload
    normalized = section.strip().lower()
    key = _CATALOG_SECTION_ALIASES.get(normalized)
    if key is None:
        raise ValueError(f"unknown setup section: {section!r}")
    return {key: payload[key]}


def _optional_str(value: Any) -> str | None:
    """``None`` = "not passed" (keep-current); anything else coerces to str."""
    return None if value is None else str(value)


def _optional_bool(value: Any) -> bool | None:
    """``None`` = "not passed" (keep-current); anything else coerces to bool."""
    return None if value is None else bool(value)


def _strict_bool(value: Any, *, field: str, default: bool = False) -> bool:
    """Parse an additive boolean intent without accepting truthy strings."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


class SetupEngine:
    """Apply onboarding sections against one in-memory config before persisting."""

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        path: str | Path | None = None,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self.config = config if config is not None else load_config(self.path)
        self.restart_required = False
        self.warnings: list[str] = []

    def status(self) -> OnboardingStatus:
        return get_onboarding_status(self.config)

    def catalog(self, section: str | None = None) -> dict[str, Any]:
        return setup_catalog_payload(section)

    def apply(self, section: str, payload: dict[str, Any]) -> MutationResult:
        normalized = section.strip().lower()
        if normalized in {"provider", "providers"}:
            provider_id = str(payload["providerId"])
            # Keep-current semantics: keys absent from the payload (or set to
            # None) mean "leave the stored value alone" on a same-provider
            # re-save; explicit values — including an explicit empty string —
            # keep their legacy meaning in the mutation.
            res = upsert_llm_provider(
                self.config,
                provider_id=provider_id,
                model=_optional_str(payload.get("model")),
                api_key=str(payload.get("apiKey") or ""),
                api_key_env=str(payload.get("apiKeyEnv") or ""),
                preserve_api_key=_strict_bool(
                    payload.get("preserveApiKey"),
                    field="preserveApiKey",
                ),
                base_url=_optional_str(payload.get("baseUrl")),
                proxy=_optional_str(payload.get("proxy")),
                preset_id=str(payload.get("presetId") or ""),
                image_generation_intent=(
                    default_image_generation_intent_for_provider(provider_id)
                    if payload.get("imageGenerationIntent") is None
                    else str(payload.get("imageGenerationIntent"))
                ),
            )
        elif normalized == "router":
            res = upsert_router(
                self.config,
                mode=str(payload.get("mode", "recommended")),
                default_tier=payload.get("defaultTier"),
                tiers=payload.get("tiers"),
            )
        elif normalized in ENSEMBLE_SECTION_ALIASES:
            # Keep-current semantics ride the mutation: keys absent from the
            # payload (``None``) never touch the stored [llm_ensemble] values.
            enabled = payload.get("enabled")
            selection_mode = payload.get("selectionMode")
            model_options = payload.get("modelOptions")
            if model_options is not None and not isinstance(model_options, (list, tuple)):
                raise ValueError("modelOptions must be a list of model ids")
            candidates = payload.get("candidates")
            if candidates is not None and not isinstance(candidates, (list, tuple)):
                raise ValueError("candidates must be a list of candidate objects")
            if candidates is not None and any(
                not isinstance(candidate, dict) for candidate in candidates
            ):
                raise ValueError("candidates must be a list of candidate objects")
            all_failed_policy = payload.get("allFailedPolicy")
            res = upsert_llm_ensemble(
                self.config,
                enabled=None if enabled is None else bool(enabled),
                selection_mode=None if selection_mode is None else str(selection_mode),
                model_options=(
                    None
                    if model_options is None
                    else [str(option) for option in model_options]
                ),
                candidates=(
                    None
                    if candidates is None
                    else [dict(candidate) for candidate in candidates]
                ),
                min_successful_proposers=payload.get("minSuccessfulProposers"),
                proposer_max_retries=payload.get("proposerMaxRetries"),
                all_failed_policy=(
                    None if all_failed_policy is None else str(all_failed_policy)
                ),
            )
        elif normalized == "search":
            # Keep-current semantics for the global search settings: values
            # absent from the payload (None) never touch the stored
            # max_results/proxy/use_env_proxy/fallback_policy/diagnostics.
            fallback_policy = payload.get("fallbackPolicy")
            res = upsert_search_provider(
                self.config,
                provider_id=str(payload["providerId"]),
                api_key=str(payload.get("apiKey") or ""),
                api_key_env=str(payload.get("apiKeyEnv") or ""),
                max_results=payload.get("maxResults"),
                proxy=_optional_str(payload.get("proxy")),
                use_env_proxy=_optional_bool(payload.get("useEnvProxy")),
                fallback_policy=(
                    None if fallback_policy in (None, "") else str(fallback_policy)
                ),
                diagnostics=_optional_bool(payload.get("diagnostics")),
            )
        elif normalized in {"channel", "channels"}:
            entry = payload.get("entry", payload)
            if not isinstance(entry, dict):
                raise ValueError("channel payload must contain an entry object")
            res = upsert_channel(self.config, entry_payload=entry)
        elif normalized in IMAGE_GENERATION_SECTION_ALIASES:
            raw_enabled = payload.get("enabled")
            if raw_enabled is None:
                # Keep-current: a deliberate enabled=false persisted in the
                # config must survive a key rotation that omits the flag. A
                # config that never stored the flag keeps the legacy
                # configure-implies-enable behavior for a fresh setup.
                image_cfg = self.config.image_generation
                enabled = (
                    bool(image_cfg.enabled)
                    if "enabled" in image_cfg.model_fields_set
                    else True
                )
            else:
                enabled = bool(raw_enabled)
            provider_id = str(payload.get("providerId") or "")
            if not enabled and not provider_id:
                res = disable_image_generation(self.config)
            else:
                fallbacks = payload.get("fallbacks")
                if fallbacks is not None and not isinstance(fallbacks, list):
                    raise ValueError("fallbacks must be a list of provider/model references")
                res = upsert_image_generation_provider(
                    self.config,
                    provider_id=provider_id,
                    primary=str(payload.get("primary") or ""),
                    api_key=str(payload.get("apiKey") or ""),
                    api_key_env=str(payload.get("apiKeyEnv") or ""),
                    base_url=_optional_str(payload.get("baseUrl")),
                    enabled=enabled,
                    size=str(payload.get("size") or ""),
                    output_format=str(payload.get("outputFormat") or ""),
                    fallbacks=(
                        [str(fallback) for fallback in fallbacks]
                        if fallbacks is not None
                        else None
                    ),
                    clear_fallbacks=_strict_bool(
                        payload.get("clearFallbacks"), field="clearFallbacks"
                    ),
                    credential_mode=(
                        None
                        if payload.get("credentialMode") is None
                        else str(payload.get("credentialMode"))
                    ),
                )
        elif normalized in AUDIO_SECTION_ALIASES:
            res = upsert_audio_provider(
                self.config,
                provider_id=str(payload.get("providerId") or "elevenlabs"),
                api_key=str(payload.get("apiKey") or ""),
                api_key_env=str(payload.get("apiKeyEnv") or ""),
                base_url=str(payload.get("baseUrl") or ""),
                enabled=bool(payload.get("enabled", True)),
                tts_voice=str(payload.get("ttsVoice") or ""),
                tts_model=str(payload.get("ttsModel") or ""),
                language_code=str(payload.get("languageCode") or ""),
            )
        elif normalized in MEMORY_EMBEDDING_SECTION_ALIASES:
            res = upsert_memory_embedding(
                self.config,
                provider=str(payload["providerId"]),
                model=payload.get("model"),
                api_key=payload.get("apiKey"),
                api_key_env=payload.get("apiKeyEnv"),
                base_url=payload.get("baseUrl"),
                onnx_dir=payload.get("onnxDir"),
            )
        else:
            raise ValueError(f"unknown setup section: {section!r}")

        self.config = res.config
        self.restart_required = self.restart_required or res.restart_required
        self.warnings.extend(res.warnings)
        return res

    def persist(self, *, backup: bool = True) -> PersistResult:
        return persist_config(
            self.config,
            path=self.path,
            backup=backup,
            restart_required=self.restart_required,
        )

    def preview_next_steps(self) -> str:
        return format_next_steps(self.config, config_path=self.path)
