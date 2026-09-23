"""Gateway Adapter implementations for provider-configuration Ports."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, cast

from opensquilla.application.provider_configuration import (
    ModelCatalogResult,
    ModelRoutingSnapshot,
    PreparedModelRouting,
    ProviderStatusResult,
)
from opensquilla.gateway.model_routing import (
    apply_model_routing_mode,
    model_routing_patches,
    model_routing_public_snapshot,
)
from opensquilla.gateway.provider_runtime import resolve_provider_selector_config
from opensquilla.gateway.provider_status_runtime import read_provider_status
from opensquilla.gateway.setup_config_runtime import sync_media_runtime
from opensquilla.provider.model_catalog import ModelCatalog as ProviderModelCatalog
from opensquilla.provider.model_catalog import shared_catalog

_catalog = ProviderModelCatalog()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _metadata_record(metadata: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = metadata.get(name)
    return value if isinstance(value, dict) else None


def _tokenrhythm_metadata_value(
    metadata: dict[str, Any],
    field: str,
) -> int | None:
    for source_name in ("declared", "published"):
        source = _metadata_record(metadata, source_name)
        if source is not None:
            value = _positive_int(source.get(field))
            if value is not None:
                return value
    return None


def _tokenrhythm_metadata_capability(
    metadata: dict[str, Any],
    capability: str,
) -> bool | None:
    for source_name in ("declared", "published"):
        source = _metadata_record(metadata, source_name)
        if source is None:
            continue
        capabilities = source.get("capabilities")
        if not isinstance(capabilities, dict):
            continue
        value = capabilities.get(capability)
        if isinstance(value, bool):
            return value
    return None


def model_info_to_projection(model: dict[str, Any]) -> dict[str, Any]:
    """Project a provider ``ModelInfo`` into the stable public catalog row."""

    provider_id = str(model.get("provider", "") or "")
    model_id = str(model.get("model_id", "") or "")
    # Capacity is enriched separately from the shared runtime resolver. Keep
    # capability/source projection independent of mutable session overrides.
    entry = _catalog.resolve_entry(model_id, provider=provider_id)
    capabilities: list[str] = ["chat"]
    context_window = model.get("context_window", 0)
    max_output_tokens = model.get("max_output_tokens", 0)
    metadata = dict(model["metadata"]) if isinstance(model.get("metadata"), dict) else None

    if (
        provider_id.strip().lower() == "tokenrhythm"
        and metadata is not None
        and metadata.get("schemaVersion") == 1
    ):
        context_window = _tokenrhythm_metadata_value(metadata, "contextWindow")
        max_output_tokens = _tokenrhythm_metadata_value(metadata, "maxOutputTokens")
        if context_window is None:
            context_window = _positive_int(model.get("context_window")) or entry.context_window
        if max_output_tokens is None:
            max_output_tokens = (
                _positive_int(model.get("max_output_tokens")) or entry.max_output_tokens
            )
        declared_tools = _tokenrhythm_metadata_capability(metadata, "tools")
        declared_vision = _tokenrhythm_metadata_capability(metadata, "vision")
        declared_reasoning = _tokenrhythm_metadata_capability(metadata, "reasoning")
        supports_tools = (
            declared_tools if declared_tools is not None else bool(model.get("supports_tools"))
        )
        supports_vision = (
            declared_vision if declared_vision is not None else bool(model.get("supports_vision"))
        )
        supports_reasoning = (
            False if declared_reasoning is False else bool(model.get("supports_reasoning"))
        )
        if supports_tools:
            capabilities.append("tools")
        if supports_reasoning:
            capabilities.append("reasoning")
        if supports_vision:
            capabilities.append("vision")
    else:
        if model.get("supports_tools"):
            capabilities.append("tools")
        if model.get("supports_vision"):
            capabilities.append("vision")

    return {
        "id": model_id,
        "name": model.get("display_name") or model_id,
        "provider": provider_id,
        "contextWindow": context_window,
        "maxOutputTokens": max_output_tokens,
        "capabilities": capabilities,
        "pricing": {
            "inputPer1k": model.get("input_cost_per_1k", 0.0),
            "outputPer1k": model.get("output_cost_per_1k", 0.0),
        },
        "source": entry.source,
        "reasoningFormat": entry.reasoning_format,
        "metadata": metadata,
    }


def _snapshot_config_for_selector_leg(config: Any) -> Any:
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=str(getattr(config, "provider", "") or ""),
            model=str(getattr(config, "model", "") or ""),
            api_key=str(getattr(config, "api_key", "") or ""),
            api_key_env="",
            base_url=str(getattr(config, "base_url", "") or ""),
            proxy=str(getattr(config, "proxy", "") or ""),
            provider_routing=dict(getattr(config, "provider_routing", {}) or {}),
        )
    )


def _supports_snapshot_resolver(list_models_detailed: Any) -> bool:
    try:
        return "snapshot_resolver" in inspect.signature(list_models_detailed).parameters
    except (TypeError, ValueError):
        return False


def model_list_error_to_projection(error: Any) -> dict[str, Any]:
    return {
        "provider": str(getattr(error, "provider", "")),
        "kind": str(getattr(error, "kind", "")),
        "detail": str(getattr(error, "detail", "")),
    }


class GatewayModelCatalogPort:
    def __init__(
        self, provider_selector: Any, config: Any, *, include_configured_defaults: bool = False,
    ) -> None:
        self._provider_selector = provider_selector
        self._config = config
        self._include_configured_defaults = include_configured_defaults

    async def load_model_catalog(self) -> ModelCatalogResult:
        if not self._include_configured_defaults:
            return await self._load_active_catalog()
        # Settings and chat share the same selectable-discovery policy. Only
        # durable deployments are resolved here; draft credentials never enter
        # the chat catalog, and named auth profiles need a separate identity.
        from opensquilla.engine.selector_override import peek_profile_credential
        from opensquilla.onboarding.probe import (
            TRANSIENT_MODEL_DISCOVERY_FAILURES,
            ProviderModelsDiscoverResult,
            discover_selectable_provider_models,
        )
        from opensquilla.provider.deployment import resolve_provider_deployment
        from opensquilla.provider.preset_registry import get_preset
        from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

        models: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        inherited = getattr(self._provider_selector, "current_config", None)
        active_provider = str(getattr(inherited, "provider", "")).strip().lower()
        candidates: dict[str, str] = {}
        if active_provider:
            candidates[active_provider] = str(getattr(inherited, "model", "") or "")
        for key, profile in (getattr(self._config, "llm_profiles", None) or {}).items():
            provider = str(key).strip().lower()
            if provider in candidates:
                continue
            preset = get_preset(provider)
            candidates[provider] = str(getattr(profile, "model", "") or "").strip() or (
                preset.default_model if preset else ""
            )

        async def discover(provider: str, model: str) -> tuple[str, str, Any, Any] | None:
            try:
                get_provider_spec(provider)
            except UnknownProviderError:
                return None
            resolution = resolve_provider_deployment(
                # Listing only needs a connection, not a configured model.
                # Keep the placeholder out of the fallback rows below.
                self._config, provider, model or "catalog-discovery",
                inherited_provider_config=inherited,
                credential_pool_acquirer=peek_profile_credential,
            )
            if not resolution.ready:
                return provider, model, resolution, None
            deployment = resolution.provider_config
            assert deployment is not None
            try:
                discovered = await discover_selectable_provider_models(
                    provider_id=provider, api_key=deployment.api_key, api_key_env="",
                    base_url=deployment.base_url, proxy=deployment.proxy,
                    allow_default_api_key_env=False, persist_catalog=True,
                    catalog_config=self._config,
                )
            except Exception:
                discovered = ProviderModelsDiscoverResult(
                    ok=False, provider_id=provider, failure_kind="unknown",
                    detail="Provider model catalog could not be loaded.",
                )
            return provider, model, resolution, discovered

        # Providers refresh independently; one unavailable account does not
        # prevent other saved providers from contributing their model menus.
        discovered_results = await asyncio.gather(*(
            discover(provider, model) for provider, model in candidates.items()
        ))
        for discovered_result in discovered_results:
            if discovered_result is None:
                continue
            provider, model, resolution, discovered = discovered_result
            if not resolution.ready:
                errors.append({
                    "provider": provider, "kind": "deployment_unavailable",
                    "detail": resolution.reason,
                })
                continue
            if discovered.source == "live" or not discovered.ok:
                # A credential-scoped listing is authoritative. In particular,
                # auth failures cannot resurrect a preset model.
                for row in discovered.models:
                    entry = shared_catalog().resolve_entry(str(row["id"]), provider=provider)
                    models.append({
                        "id": row["id"], "name": row["name"], "provider": provider,
                        "contextWindow": row["contextWindow"],
                        "maxOutputTokens": row["maxOutputTokens"],
                        "capabilities": row["capabilities"],
                        "pricing": row.get("pricing") or {"inputPer1k": 0, "outputPer1k": 0},
                        "source": row.get("capabilitySource") or entry.source,
                        "reasoningFormat": entry.reasoning_format,
                        "metadata": row.get("metadata"),
                    })
                if not discovered.ok:
                    errors.append({
                        "provider": provider, "kind": discovered.failure_kind,
                        "detail": discovered.detail,
                    })
                if not (
                    not discovered.ok and not discovered.models
                    and discovered.failure_kind in TRANSIENT_MODEL_DISCOVERY_FAILURES
                ):
                    continue
            # No trustworthy discovery support: preserve the explicitly saved
            # model instead of guessing every model in a metadata catalog.
            spec = get_provider_spec(provider)
            if discovered.ok and spec.live_catalog_shape == "tokenrhythm":
                from opensquilla.provider.tokenrhythm_catalog import (
                    is_official_tokenrhythm_endpoint,
                )

                if is_official_tokenrhythm_endpoint(resolution.provider_config.base_url):
                    continue
            fallback_models = (model, *spec.static_model_ids) if discovered.ok else (model,)
            for configured_model in dict.fromkeys(fallback_models):
                if not configured_model:
                    continue
                entry = shared_catalog().resolve_entry(configured_model, provider=provider)
                capabilities = ["chat"]
                for name in ("tools", "vision", "reasoning"):
                    if getattr(entry, "supports_" + name):
                        capabilities.append(name)
                models.append({
                    "id": configured_model, "name": entry.display_name or configured_model,
                    "provider": provider, "contextWindow": entry.context_window,
                    "maxOutputTokens": entry.max_output_tokens, "capabilities": capabilities,
                    "pricing": {
                        "inputPer1k": (entry.input_cost_per_mtok or 0) / 1000,
                        "outputPer1k": (entry.output_cost_per_mtok or 0) / 1000,
                    },
                    "source": entry.source, "reasoningFormat": entry.reasoning_format,
                    "metadata": {"catalogScope": "configured_default"},
                })
        return cast(ModelCatalogResult, {"models": models, "errors": errors})

    async def _load_active_catalog(self) -> ModelCatalogResult:
        from opensquilla.provider.model_capacity import (
            custom_capacity_identity,
            install_custom_capacity,
            resolve_model_capacities,
        )

        catalog = shared_catalog()
        identities = {provider: custom_capacity_identity(self._config, provider)
                      for provider in ("custom", "custom_anthropic")}

        def project(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for provider, identity in identities.items():
                selected = [item for item in items if item.get("provider") == provider]
                if selected:
                    install_custom_capacity(catalog, identity, provider, selected)
            projected = [model_info_to_projection(item) for item in items]
            # Only custom endpoint rows need the shared capacity enrichment.
            # Other providers can carry credential-scoped snapshot limits that
            # must not be replaced by a different deployment's catalog entry.
            capacities = resolve_model_capacities(catalog, self._config, [
                {"provider": str(item["provider"]), "model": str(item["id"])}
                for item in projected
                if item["provider"].strip().lower() in identities
            ])["models"]
            by_key = {(row["provider"], row["model"]): row for row in capacities}
            for item in projected:
                limits = by_key.get((item["provider"].strip().lower(), item["id"].strip()))
                if limits is None:
                    continue
                item["contextWindow"] = limits["contextWindow"]["value"]
                item["maxOutputTokens"] = limits["maxOutputTokens"]["value"]
            return projected

        models: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        selector = self._provider_selector
        if selector is None or not getattr(selector, "is_configured", True):
            return ModelCatalogResult(models=[], errors=[])
        try:
            from opensquilla.provider.registry import (
                UnknownProviderError,
                get_provider_spec,
            )

            list_models_detailed = selector.list_models_detailed
            if _supports_snapshot_resolver(list_models_detailed):
                from opensquilla.gateway.model_catalog_refresh import (
                    cached_tokenrhythm_models,
                )

                def snapshot_resolver(config: Any) -> Any:
                    try:
                        spec = get_provider_spec(str(getattr(config, "provider", "") or ""))
                    except UnknownProviderError:
                        return None
                    if spec.live_catalog_shape != "tokenrhythm":
                        return None
                    return cached_tokenrhythm_models(_snapshot_config_for_selector_leg(config))

                detailed = await list_models_detailed(snapshot_resolver=snapshot_resolver)
                models = project(detailed.models)
                errors = [model_list_error_to_projection(item) for item in detailed.errors]
            else:
                current = getattr(selector, "current_config", None)
                provider_id = str(getattr(current, "provider", "") or "")
                try:
                    spec = get_provider_spec(provider_id)
                except UnknownProviderError:
                    spec = None
                if spec is not None and spec.live_catalog_shape == "tokenrhythm":
                    from opensquilla.gateway.model_catalog_refresh import (
                        cached_tokenrhythm_models,
                    )

                    cached = cached_tokenrhythm_models(self._config)
                    models = project([item.model_dump() for item in cached])
                else:
                    detailed = await list_models_detailed()
                    models = project(detailed.models)
                    errors = [model_list_error_to_projection(item) for item in detailed.errors]
        except Exception:
            pass
        return cast(ModelCatalogResult, {"models": models, "errors": errors})


class GatewayModelRoutingPolicyPort:
    """Translate domain routing intent into a detached config candidate."""

    def snapshot(self, config: Any) -> ModelRoutingSnapshot:
        return cast(ModelRoutingSnapshot, model_routing_public_snapshot(config))

    def prepare(self, config: Any, mode: str) -> PreparedModelRouting:
        patches = model_routing_patches(config, mode)
        candidate = config.model_copy(deep=True)
        apply_model_routing_mode(candidate, mode, activation_config=config)
        from opensquilla.onboarding.router_policy import validate_router_candidate

        validate_router_candidate(candidate)
        return PreparedModelRouting(candidate, tuple(patches))

    def prepare_recommended(
        self,
        config: Any,
        provider_id: str,
        *,
        activate_router: bool = False,
    ) -> PreparedModelRouting:
        from opensquilla.onboarding.router_policy import (
            PrimaryProviderChangedError,
            reconcile_recommended_router,
            validate_router_candidate,
            validate_router_reactivation,
        )
        primary = str(config.llm.provider).strip().lower()
        # Both the summary and recommendation action follow the saved primary.
        # A foreign/custom ladder must be replaceable with its recommendation;
        # the expected primary also rejects a stale client after a switch.
        if provider_id.strip().lower() != primary:
            raise PrimaryProviderChangedError(
                "The primary provider changed; reload before resetting Router",
            )
        candidate = config.model_copy(deep=True)
        reconcile_recommended_router(candidate, primary)
        # A first sparse save must retain the chosen primary even when it
        # equals the default; otherwise a provider-less reload may infer the
        # legacy provider from the direct model and retarget this ladder.
        candidate.mark_force_persist("llm.provider")
        patched = [
            "squilla_router.tiers",
            "squilla_router.tier_profile",
            "squilla_router.preset_binding",
        ]
        for path in patched:
            candidate.mark_force_persist(path)
        if activate_router:
            patched.extend(apply_model_routing_mode(candidate, "router", activation_config=config))
            validate_router_candidate(candidate)
        else:
            validate_router_reactivation(config, candidate)
        return PreparedModelRouting(candidate, tuple(patched))


class GatewayModelRoutingRuntimePort:
    """Reconcile a committed routing candidate with live Gateway state."""

    def __init__(self, provider_selector: Any, subscription_manager: Any) -> None:
        self._provider_selector = provider_selector
        self._subscription_manager = subscription_manager

    def prepare_reconciliation(self, config: Any) -> Any:
        # Resolve environment-backed provider values before persistence.  The
        # candidate tracks their provenance so sparse config writes never bake
        # runtime credentials into the durable file.
        return resolve_provider_selector_config(config)

    async def reconcile(self, config: Any, prepared: Any) -> None:
        if prepared is not None and hasattr(self._provider_selector, "sync_primary"):
            self._provider_selector.sync_primary(prepared)
        sync_media_runtime(config)

    async def publish_changed(
        self,
        previous: ModelRoutingSnapshot,
        config: Any,
        *,
        source: str,
        force: bool = False,
    ) -> None:
        current = model_routing_public_snapshot(config)
        if self._subscription_manager is None or (
            current == previous and not force and source != "models.routing.resetRecommended"
        ):
            return
        from opensquilla.gateway.event_bridge import EventBridge
        from opensquilla.gateway.scopes import READ_SCOPE
        from opensquilla.gateway.websocket import get_registry

        await EventBridge(
            self._subscription_manager,
            get_registry(),
        ).broadcast_scoped(
            "models.routing.changed",
            {**current, "source": source},
            required_scope=READ_SCOPE,
        )


class GatewayProviderStatusPort:
    """Provider status projection backed by concrete Gateway dependencies."""

    def __init__(
        self,
        *,
        config: Any,
        provider_selector: Any,
        provider_stats: Any,
    ) -> None:
        self._config = config
        self._provider_selector = provider_selector
        self._provider_stats = provider_stats

    async def load_provider_status(
        self,
        *,
        provider_id: str | None,
        probe_models: bool,
    ) -> ProviderStatusResult:
        return cast(
            ProviderStatusResult,
            await read_provider_status(
                config=self._config,
                provider_selector=self._provider_selector,
                provider_stats=self._provider_stats,
                provider_id=provider_id,
                probe_models=probe_models,
            ),
        )


__all__ = [
    "GatewayModelCatalogPort",
    "GatewayModelRoutingPolicyPort",
    "GatewayModelRoutingRuntimePort",
    "GatewayProviderStatusPort",
    "model_list_error_to_projection",
    "model_info_to_projection",
]
