"""Concrete model, persistence and live-runtime capabilities for AppSettings."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.application.app_settings import EffectiveSetting, SettingsObject
from opensquilla.application.config_secrets import (
    inherit_runtime_secrets as _inherit_runtime_secrets,
)
from opensquilla.application.provider_configuration import ModelRoutingSnapshot
from opensquilla.gateway.config_persistence import persist_gateway_config
from opensquilla.gateway.model_routing import (
    model_routing_public_snapshot,
    reconcile_model_routing_write,
)
from opensquilla.gateway.provider_runtime import (
    resolve_provider_selector_config,
    sync_resolved_provider_selector,
)
from opensquilla.gateway.setup_config_runtime import sync_media_runtime
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.preset_registry import legacy_profile_ids

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.selector import ProviderConfig

log = structlog.get_logger(__name__)


def _update_config_in_place(old: Any, new: Any) -> None:
    """Copy all fields from new config into the existing config object in-memory."""
    for field_name in type(new).model_fields:
        setattr(old, field_name, getattr(new, field_name))
    _inherit_runtime_secrets(new, old)
    # Refresh runtime-override provenance against the state just applied:
    # after a reload/config.set swap, a record whose stored slot no longer
    # reflects disk provenance must not silently rewrite later persists
    # (e.g. a boot-time llm.base_url record surviving a hand-edit + reload
    # would make an unrelated save revert the operator's on-disk URL to the
    # boot-time stored value). ``reconcile_runtime_overrides`` keeps an old
    # record only while the new value still equals its applied value and
    # lets the candidate's freshly derived records win per path.
    if hasattr(old, "reconcile_runtime_overrides") and hasattr(new, "_runtime_field_overrides"):
        old.reconcile_runtime_overrides(new)


async def _notify_goal_config_changed(task_runtime: Any, previous_config: Any) -> None:
    """Apply the committed Goal kill-switch transition to the live service."""

    goal_service = getattr(task_runtime, "goal_service", None)
    hook = getattr(goal_service, "on_config_changed", None)
    if not callable(hook):
        return
    previous_goal = getattr(previous_config, "goal", previous_config)
    previous_enabled = bool(getattr(previous_goal, "execution_enabled", False))
    try:
        await hook(previous_execution_enabled=previous_enabled)
    except Exception:
        # The service reads the current root config dynamically, so a failed
        # best-effort pause hook still blocks every new automatic admission.
        log.warning("gateway.goal_config_reconcile_failed", exc_info=True)


def _sync_model_catalog_overrides(config: Any) -> None:
    """Re-apply ``[models.*]`` overrides onto the shared catalog.

    ``ModelCatalog`` is a boot-constructed singleton (see
    ``gateway.boot.build_services``); without this, a live ``[models.*]``
    edit made via ``config.set``/``patch``/``apply`` or ``opensquilla gateway
    reload`` would hot-apply into the config object but silently keep
    resolving prices/capabilities from the stale override snapshot until a
    full restart.
    """
    from opensquilla.gateway.boot import apply_model_catalog_overrides
    from opensquilla.provider.model_catalog import shared_catalog

    apply_model_catalog_overrides(shared_catalog(), config)


def _live_catalog_fingerprint(config: Any) -> tuple[str, str, str]:
    """Connection identity for deciding whether a config write should refresh."""

    from opensquilla.gateway.model_catalog_refresh import live_catalog_refresh_fingerprint

    return live_catalog_refresh_fingerprint(config)


async def _refresh_live_catalog_after_change(previous: tuple[str, str, str], config: Any) -> None:
    """Refresh public model metadata when its runtime connection changed."""

    from opensquilla.gateway.model_catalog_refresh import (
        refresh_live_model_catalog_if_changed,
    )

    await refresh_live_model_catalog_if_changed(previous, config)


def _tokenrhythm_profile_credential_signature(config: Any) -> tuple[object, ...]:
    profile = None
    for provider_id, candidate in (getattr(config, "llm_profiles", None) or {}).items():
        if str(provider_id or "").strip().lower() == "tokenrhythm":
            profile = candidate
            break
    if profile is None:
        return ()
    return (
        str(getattr(profile, "api_key", "") or ""),
        str(getattr(profile, "api_key_env", "") or ""),
        tuple(getattr(profile, "api_key_env_pool", None) or ()),
    )


async def _reconcile_tokenrhythm_profile_after_config_change(
    previous_config: Any,
    current_config: Any,
) -> None:
    """Apply local profile cleanup after a generic config transaction commits."""

    if previous_config is None or current_config is None:
        return
    if _tokenrhythm_profile_credential_signature(
        previous_config
    ) != _tokenrhythm_profile_credential_signature(current_config):
        from opensquilla.gateway.llm_runtime import discard_profile_credential_pool

        discard_profile_credential_pool("tokenrhythm")
    from opensquilla.gateway.model_catalog_refresh import (
        reconcile_tokenrhythm_profile_transition,
    )

    await reconcile_tokenrhythm_profile_transition(
        previous_config,
        current_config,
        provider_id="tokenrhythm",
    )


class GatewayAppSettingsPort:
    """Bind the settings owner's model/storage operations to the running Gateway."""

    profile_ids = legacy_profile_ids()
    replace = staticmethod(_update_config_in_place)
    reconcile_routing = staticmethod(reconcile_model_routing_write)
    routing_snapshot = staticmethod(model_routing_public_snapshot)
    catalog_fingerprint = staticmethod(_live_catalog_fingerprint)
    resolve_provider = staticmethod(resolve_provider_selector_config)

    def __init__(
        self,
        config: GatewayConfig | None,
        *,
        task_runtime: Any = None,
        provider_selector: Any = None,
        subscription_manager: Any = None,
        source: str = "config.patch",
    ) -> None:
        self.config = config
        self.task_runtime = task_runtime
        self.provider_selector = provider_selector
        self.subscription_manager = subscription_manager
        self.source = source

    def read_public_settings(self) -> SettingsObject:
        config = self.config
        if config is None:
            return {}
        value = (
            config.to_public_dict()
            if hasattr(config, "to_public_dict")
            else config.model_dump()
            if hasattr(config, "model_dump")
            else {}
        )
        return cast(SettingsObject, dict(value)) if isinstance(value, Mapping) else {}

    def read_effective_fields(self) -> Mapping[str, EffectiveSetting]:
        from opensquilla.provider.resolution import resolve_effective_llm

        return {
            path: {"value": field.value, "source": field.source}
            for path, field in resolve_effective_llm(self.config, shared_catalog()).items()
        }

    @staticmethod
    def validate_embedding(config: GatewayConfig) -> None:
        memory_cfg = getattr(config, "memory", None)
        if memory_cfg is None:
            return
        from opensquilla.memory.embedding_resolver import resolve_memory_embedding

        resolve_memory_embedding(memory_cfg, local_available=lambda *_: False)

    @staticmethod
    def build(payload: SettingsObject) -> GatewayConfig:
        from opensquilla.gateway.config import GatewayConfig, validate_compaction_deployment_write

        validate_compaction_deployment_write(payload)
        # Preserve BaseSettings construction and its environment-source handling.
        return GatewayConfig(**cast(dict[str, Any], payload))

    @staticmethod
    def profile_defaults(profile: str) -> SettingsObject:
        from opensquilla.gateway.config import _router_tier_profile_defaults

        return cast(SettingsObject, _router_tier_profile_defaults(profile))

    @staticmethod
    def persist(config: GatewayConfig) -> None:
        persist_gateway_config(config)

    def resolve_path(self) -> Path:
        from opensquilla.onboarding.config_store import resolve_config_path

        target, _source = resolve_config_path(getattr(self.config, "config_path", None) or None)
        return target

    @staticmethod
    def load(path: Path) -> GatewayConfig:
        from opensquilla.onboarding.config_store import load_config

        return load_config(path)

    def sync_provider(self, provider: ProviderConfig | None) -> None:
        sync_resolved_provider_selector(self, provider)

    async def notify_goal(self, previous: GatewayConfig | None) -> None:
        await _notify_goal_config_changed(self.task_runtime, previous)

    async def sync_runtime(self, previous: GatewayConfig | None, candidate: GatewayConfig) -> None:
        sync_media_runtime(candidate)
        _sync_model_catalog_overrides(candidate)
        await _reconcile_tokenrhythm_profile_after_config_change(
            previous, self.config if self.config is not None else candidate
        )

    async def refresh_catalog(
        self,
        previous: tuple[str, str, str],
        candidate: GatewayConfig,
        *,
        force: bool = False,
    ) -> None:
        # Read live authority after asynchronous reconciliation; an older
        # candidate must never reactivate credentials superseded by another write.
        current = self.config if self.config is not None else candidate
        if force:
            from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

            await refresh_live_model_catalog(current, force=True)
        else:
            await _refresh_live_catalog_after_change(previous, current)

    async def publish_routing(self, previous: SettingsObject, candidate: GatewayConfig) -> None:
        from opensquilla.gateway.adapters.provider_configuration import (
            GatewayModelRoutingRuntimePort,
        )

        await GatewayModelRoutingRuntimePort(
            self.provider_selector, self.subscription_manager
        ).publish_changed(cast(ModelRoutingSnapshot, previous), candidate, source=self.source)

    async def reconcile_dream(self) -> bool | None:
        from opensquilla.gateway.dream_bridge import get_dream_reconciler

        reconciler = get_dream_reconciler()
        if reconciler is None:
            return None
        try:
            await reconciler()
        except Exception as exc:
            log.warning("config.dream_link_reconcile_failed", error=str(exc))
            return False
        return True
