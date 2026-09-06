"""RPC handlers for onboarding (catalog, status, provider/channel mutations).

Mutations are applied against the gateway's *active* in-memory config when the
RPC context provides one (``ctx.config``). The same context exposes the
running ``provider_selector``; provider mutations are mirrored into it so a
``configure`` from the WebUI takes effect on the next chat without a restart.

Channel mutations reconcile live through the boot-registered channels
reconciler when one is available; webhook-mode entries (HTTP routes bound at
boot) and reconciler-less contexts stay restart-gated.

The onboarding mutation/store modules import ``opensquilla.gateway.config`` at
module top level, which transitively re-enters ``opensquilla.gateway`` during
boot. To avoid the circular import, we import those bindings lazily inside the
handler bodies.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.setup_config_runtime import (
    active_gateway_config as _active_config,
)
from opensquilla.gateway.setup_config_runtime import (
    install_gateway_config_candidate as _apply_inplace,
)
from opensquilla.gateway.setup_config_runtime import (
    persist_setup_candidate as _persist,
)
from opensquilla.onboarding.redaction import is_redacted_secret_sentinel
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS

if TYPE_CHECKING:
    from opensquilla.application.capability_setup import CapabilitySetup
    from opensquilla.application.profile_lifecycle import ProfileLifecycle, ProfileProbeCommand
    from opensquilla.application.provider_setup import (
        DiscoverPrimaryModels,
        ImageModelDiscoveryResult,
        ProbePrimaryProvider,
        ProviderModelDiscoveryResult,
        ProviderSetup,
    )
    from opensquilla.application.provider_setup import (
        ProviderProbeResult as ProviderProbePayload,
    )
    from opensquilla.application.setup_workflow import SetupWorkflow
    from opensquilla.onboarding.probe import (
        ProviderProbeResult as ProviderProbeExecutionResult,
    )


@contextmanager
def _validation_error(code: str) -> Iterator[None]:
    """Translate a mutation validation error into a stable, client-localizable
    ``RpcHandlerError`` code, keeping the original English text as the message so
    the Web UI can fall back to it (and developers keep the detail).

    Catches both ``ValueError`` (bad fields) and ``KeyError`` (an unknown/
    unverified provider id), since on these onboarding config paths both are user
    validation failures, not internal faults. Other exceptions propagate
    unchanged and still collapse to the dispatcher's coarse codes — only the
    high-value onboarding validation paths are wrapped.
    """
    try:
        yield
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError(code, str(exc)) from exc


@contextmanager
def _channel_error() -> Iterator[None]:
    """Channel mutations raise ``KeyError`` for an unknown name and ``ValueError``
    for bad fields; map them to distinct stable codes."""
    try:
        yield
    except KeyError as exc:
        raise RpcHandlerError("onboarding.channel.not_found", str(exc)) from exc
    except ValueError as exc:
        # A ChannelValidationError additionally carries per-field detail so the
        # Web UI can anchor errors to fields instead of parsing the message.
        details = getattr(exc, "field_errors", None)
        raise RpcHandlerError(
            "onboarding.channel.invalid",
            str(exc),
            details={"fields": details} if details else None,
        ) from exc

log = structlog.get_logger(__name__)

_d = get_dispatcher()


def _status_payload(ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.gateway.adapters.setup_workflow import setup_status

    return cast(
        dict[str, Any],
        setup_status(
            _active_config(ctx),
            is_owner=ctx.principal.is_owner,
        ),
    )


def _setup_workflow(ctx: RpcContext) -> SetupWorkflow:
    from opensquilla.application.setup_workflow import SetupWorkflow
    from opensquilla.gateway.adapters.setup_workflow import (
        GatewaySetupWorkflowPort,
    )

    port = GatewaySetupWorkflowPort(
        _active_config(ctx),
        is_owner=ctx.principal.is_owner,
    )
    return SetupWorkflow(port, port)


def _setup_application_ports(ctx: RpcContext) -> tuple[Any, Any]:
    """Bind the Gateway context to narrow setup configuration/runtime Ports."""

    from opensquilla.gateway.adapters.setup_config import GatewaySetupConfigPort
    from opensquilla.gateway.adapters.setup_mutations import (
        GatewaySetupRuntimePort,
    )

    config = GatewaySetupConfigPort(ctx)
    runtime = GatewaySetupRuntimePort(
        ctx.provider_selector,
        ctx.subscription_manager,
    )
    return config, runtime


class _GatewayProviderProbeRuntime:
    def __init__(self, config: Any, usage_event_sink: Any) -> None:
        self._config = config
        self._usage_event_sink = usage_event_sink

    async def probe_primary(
        self, command: ProbePrimaryProvider
    ) -> ProviderProbePayload:
        return cast(
            "ProviderProbePayload",
            await _probe_primary_provider(
                command,
                config=self._config,
                usage_event_sink=self._usage_event_sink,
            ),
        )

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> ProviderModelDiscoveryResult:
        return cast(
            "ProviderModelDiscoveryResult",
            await _discover_primary_models(command, config=self._config),
        )

    async def discover_image_models(
        self, provider_id: str
    ) -> ImageModelDiscoveryResult:
        return cast(
            "ImageModelDiscoveryResult",
            await _discover_image_models(provider_id),
        )


def _provider_probe_port(ctx: RpcContext) -> Any:
    return _GatewayProviderProbeRuntime(
        _active_config(ctx),
        ctx.usage_event_sink,
    )


def _setup_mutation_port() -> Any:
    from opensquilla.gateway.adapters.setup_mutations import OnboardingSetupMutationPort

    return OnboardingSetupMutationPort()


def _credential_resolution_port(ctx: RpcContext) -> Any:
    from opensquilla.gateway.adapters.setup_mutations import (
        GatewayCredentialResolutionPort,
    )

    return GatewayCredentialResolutionPort(
        _active_config(ctx),
        is_owner=ctx.principal.is_owner,
    )


def _provider_setup(ctx: RpcContext) -> ProviderSetup:
    from opensquilla.application.provider_setup import ProviderSetup

    config, runtime = _setup_application_ports(ctx)
    return ProviderSetup(
        config,
        runtime,
        _provider_probe_port(ctx),
        _setup_mutation_port(),
    )


def _capability_setup(ctx: RpcContext) -> CapabilitySetup:
    from opensquilla.application.capability_setup import CapabilitySetup

    config, runtime = _setup_application_ports(ctx)
    return CapabilitySetup(config, runtime, _setup_mutation_port())


class _GatewayProfileProbeRuntime:
    def __init__(
        self,
        config: Any,
        *,
        connection_id: str,
        usage_event_sink: Any,
    ) -> None:
        self._config = config
        self._connection_id = connection_id
        self._usage_event_sink = usage_event_sink

    async def probe_saved(
        self, command: ProfileProbeCommand
    ) -> ProviderProbePayload:
        return cast(
            "ProviderProbePayload",
            await _probe_saved_profile(
                command,
                config=self._config,
                connection_id=self._connection_id,
                usage_event_sink=self._usage_event_sink,
            ),
        )

    async def probe_draft(
        self, command: ProfileProbeCommand
    ) -> ProviderProbePayload:
        return cast(
            "ProviderProbePayload",
            await _probe_draft_profile(
                command,
                config=self._config,
                connection_id=self._connection_id,
                usage_event_sink=self._usage_event_sink,
            ),
        )

    async def discover_saved(
        self, command: ProfileProbeCommand
    ) -> ProviderModelDiscoveryResult:
        return cast(
            "ProviderModelDiscoveryResult",
            await _discover_saved_profile_models(
                command,
                config=self._config,
                connection_id=self._connection_id,
            ),
        )

    async def discover_draft(
        self, command: ProfileProbeCommand
    ) -> ProviderModelDiscoveryResult:
        return cast(
            "ProviderModelDiscoveryResult",
            await _discover_draft_profile_models(
                command,
                config=self._config,
                connection_id=self._connection_id,
            ),
        )


def _profile_probe_port(ctx: RpcContext) -> Any:
    return _GatewayProfileProbeRuntime(
        _active_config(ctx),
        connection_id=ctx.conn_id,
        usage_event_sink=ctx.usage_event_sink,
    )


async def _provider_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_setup import ProbePrimaryProvider

    p = params if isinstance(params, dict) else {}
    command = ProbePrimaryProvider(
        provider_id=str(_require(params, "providerId")),
        model=str(p.get("model", "") or ""),
        api_key=str(p.get("apiKey", "") or ""),
        api_key_env=str(p.get("apiKeyEnv", "") or ""),
        base_url=str(p.get("baseUrl", "") or ""),
        proxy=str(p.get("proxy", "") or ""),
        preserve_api_key=bool(p.get("preserveApiKey", False)),
    )
    return cast(dict[str, Any], await _provider_setup(ctx).probe_primary(command))


async def _models_discover(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_setup import (
        DiscoverPrimaryModels,
    )

    p = params if isinstance(params, dict) else {}
    command = DiscoverPrimaryModels(
        provider_id=str(_require(params, "providerId")),
        api_key=str(p.get("apiKey", "") or ""),
        api_key_env=str(p.get("apiKeyEnv", "") or ""),
        base_url=str(p.get("baseUrl", "") or ""),
        proxy=str(p.get("proxy", "") or ""),
        force_refresh=_bool_param(params, "forceRefresh"),
    )
    return cast(
        dict[str, Any],
        await _provider_setup(ctx).discover_primary_models(command),
    )


async def _image_generation_models_discover(
    params: Any, ctx: RpcContext
) -> dict[str, Any]:

    return cast(
        dict[str, Any],
        await _provider_setup(ctx).discover_image_models(
            str(_require(params, "providerId"))
        ),
    )


def _profile_probe_command(params: Any) -> Any:
    from opensquilla.application.profile_lifecycle import ProfileProbeCommand

    p = params if isinstance(params, dict) else {}
    return ProfileProbeCommand(
        provider_id=str(_require(params, "providerId")),
        values=dict(p),
    )


def _profile_lifecycle(ctx: RpcContext) -> ProfileLifecycle:
    from opensquilla.application.profile_lifecycle import ProfileLifecycle

    config, runtime = _setup_application_ports(ctx)
    return ProfileLifecycle(
        config,
        runtime,
        _profile_probe_port(ctx),
        _setup_mutation_port(),
    )


async def _llm_profile_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _profile_lifecycle(ctx).probe(_profile_probe_command(params)),
    )


async def _llm_profile_draft_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _profile_lifecycle(ctx).probe_draft(_profile_probe_command(params)),
    )


async def _llm_profile_models_discover(
    params: Any, ctx: RpcContext
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _profile_lifecycle(ctx).discover_models(_profile_probe_command(params)),
    )


async def _llm_profile_draft_models_discover(
    params: Any, ctx: RpcContext
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _profile_lifecycle(ctx).discover_draft_models(
            _profile_probe_command(params)
        ),
    )


async def _onboarding_status(_params: Any, ctx: RpcContext) -> dict[str, Any]:
    return cast(dict[str, Any], await _setup_workflow(ctx).status())


async def _onboarding_catalog(_params: Any, ctx: RpcContext) -> dict[str, Any]:
    return cast(dict[str, Any], await _setup_workflow(ctx).catalog())


def _require(params: Any, key: str) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise ValueError(f"params.{key} is required")
    return params[key]


def _param(params: Any, key: str, default: Any) -> Any:
    """``params.get`` that also maps an explicit JSON ``null`` to ``default``.

    The onboarding mutations widened several parameters to ``None`` =
    keep-current for the CLI, but over RPC the legacy contract is pinned:
    an absent key AND an explicit ``null`` both mean the legacy default
    (reset/derive/clear), so hand-written clients sending ``null`` keep the
    pre-widening behavior instead of silently keeping stored values.
    """
    if not isinstance(params, dict):
        return default
    value = params.get(key, default)
    return default if value is None else value


def _bool_param(params: Any, key: str, default: bool = False) -> bool:
    value = _param(params, key, default)
    if not isinstance(value, bool):
        raise ValueError(f"params.{key} must be a boolean")
    return value


def _provider_candidate_identity(
    cfg: Any,
    provider_id: str,
    candidate_base_url: str,
) -> tuple[bool, bool]:
    """Return ``(same_provider, stored_credentials_may_be_reused)``."""
    from opensquilla.onboarding.endpoint_identity import (
        base_url_allows_credential_reuse,
    )

    active_provider = str(getattr(cfg.llm, "provider", "") or "").strip().lower()
    requested_provider = str(provider_id or "").strip().lower()
    same_provider = active_provider == requested_provider
    return same_provider, same_provider and base_url_allows_credential_reuse(
        str(getattr(cfg.llm, "base_url", "") or ""),
        candidate_base_url,
    )


def _request_changes_active_provider_connection(params: Any, cfg: Any) -> bool:
    """Return whether explicitly supplied connection fields differ from active.

    The Web UI may echo the saved Base URL and proxy when asking for a manual
    refresh.  Presence alone does not make that request an unsaved draft: only
    a different effective value must keep entitlement data ephemeral.
    """

    if not isinstance(params, dict):
        return False
    llm = getattr(cfg, "llm", None)
    from opensquilla.endpoint_identity import base_url_matches_official_api
    from opensquilla.provider.tokenrhythm_catalog import (
        canonical_tokenrhythm_base_url,
    )

    requested_provider = str(
        params.get("providerId") or getattr(llm, "provider", "") or ""
    ).strip().lower()

    comparisons = (
        ("apiKey", "api_key"),
        ("apiKeyEnv", "api_key_env"),
        ("baseUrl", "base_url"),
        ("proxy", "proxy"),
    )
    for rpc_field, config_field in comparisons:
        raw_value = params.get(rpc_field)
        if raw_value is None:
            continue
        candidate = str(raw_value or "").strip()
        if rpc_field == "apiKey" and is_redacted_secret_sentinel(candidate):
            continue
        # Blank discovery/probe fields retain the saved value.
        if not candidate:
            continue
        active = str(getattr(llm, config_field, "") or "").strip()
        if rpc_field == "baseUrl":
            # Treat scheme/host casing, an explicit default port, and a
            # trailing slash as the same deployment while keeping the API
            # path, query, fragment, and user-info boundary fail-closed.
            if requested_provider == "tokenrhythm":
                active_identity = canonical_tokenrhythm_base_url(active)
                candidate_identity = canonical_tokenrhythm_base_url(candidate)
                if not active_identity or candidate_identity != active_identity:
                    return True
            elif not base_url_matches_official_api(active, candidate):
                return True
        elif candidate != active:
            return True
    return False


async def _provider_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_setup import (
        ConfigurePrimaryProvider,
    )

    with _validation_error("onboarding.provider.invalid"):
        command = ConfigurePrimaryProvider(
            provider_id=str(_require(params, "providerId")),
            model=str(_param(params, "model", "")),
            api_key=str(_param(params, "apiKey", "")),
            api_key_env=str(_param(params, "apiKeyEnv", "")),
            preserve_api_key=_bool_param(params, "preserveApiKey"),
            base_url=str(_param(params, "baseUrl", "")),
            proxy=str(_param(params, "proxy", "")),
            preset_id=str(_param(params, "presetId", "")),
            router_action=str(_param(params, "routerAction", "preserve")),
            image_generation_intent=str(
                _param(params, "imageGenerationIntent", "preserve")
            ),
        )
        result = await _provider_setup(ctx).configure_primary(command)
    return cast(dict[str, Any], result.to_payload())


async def _llm_profile_upsert(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Create/update a non-primary provider profile without exposing its secret."""
    from opensquilla.application.profile_lifecycle import UpsertProfile

    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    pool = p.get("apiKeyEnvPool") if "apiKeyEnvPool" in p else None
    if pool is not None and not isinstance(pool, list):
        raise RpcHandlerError(
            "onboarding.llmProfile.invalid",
            "params.apiKeyEnvPool must be an array of environment-variable names",
        )
    preserve_value = p.get("keepCurrentSecret", p.get("preserveApiKey", False))
    if not isinstance(preserve_value, bool):
        raise RpcHandlerError(
            "onboarding.llmProfile.invalid",
            "params.keepCurrentSecret must be a boolean",
        )
    with _validation_error("onboarding.llmProfile.invalid"):
        result = await _profile_lifecycle(ctx).upsert(
            UpsertProfile(
                provider_id=str(provider_id),
                model=p.get("model") if "model" in p else None,
                api_key=p.get("apiKey") if "apiKey" in p else None,
                api_key_env=p.get("apiKeyEnv") if "apiKeyEnv" in p else None,
                api_key_env_pool=pool,
                keep_current_secret=preserve_value,
                base_url=p.get("baseUrl") if "baseUrl" in p else None,
                proxy=p.get("proxy") if "proxy" in p else None,
            )
        )
    return cast(dict[str, Any], result.to_payload())


async def _llm_profile_credential_clear(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Clear stored profile credentials without removing the profile."""

    provider_id = str(_require(params, "providerId"))
    with _validation_error("onboarding.llmProfile.invalid"):
        result = await _profile_lifecycle(ctx).clear_credentials(provider_id)
    entry = {
        **result.entry,
        **_credential_resolution_port(ctx).describe_clear_result(
            _active_config(ctx), provider_id, active=False
        ),
    }
    return {
        "changed": result.changed,
        "restartRequired": result.restart_required,
        "configPath": result.config_path,
        "entry": entry,
        "warnings": list(result.warnings),
    }


async def _llm_profile_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Remove a profile only when no Router/Ensemble deployment references it."""

    provider_id = str(_require(params, "providerId"))
    with _validation_error("onboarding.llmProfile.invalid"):
        result = await _profile_lifecycle(ctx).remove(provider_id)
    return cast(dict[str, Any], result.to_payload())


async def _llm_profile_active_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Atomically replace and remove the current primary provider."""
    from opensquilla.application.profile_lifecycle import (
        RemoveActiveProfile,
    )
    from opensquilla.onboarding.mutations import (
        LlmProfileActivationError,
        LlmProfileRemovalError,
    )

    provider_id = str(_require(params, "providerId"))
    replacement_provider_id = str(_require(params, "replacementProviderId"))
    replacement_model = str(_param(params, "replacementModel", "") or "")
    router_action = str(_param(params, "routerAction", "preserve"))
    image_generation_intent = str(
        _param(params, "imageGenerationIntent", "preserve")
    )
    try:
        result = await _profile_lifecycle(ctx).remove_active(
            RemoveActiveProfile(
                provider_id=provider_id,
                replacement_provider_id=replacement_provider_id,
                replacement_model=replacement_model,
                router_action=router_action,
                image_generation_intent=image_generation_intent,
            )
        )
    except LlmProfileActivationError as exc:
        code_by_reason = {
            "primary_pool_unsupported": (
                "onboarding.llmProfile.primary_pool_unsupported"
            ),
            "router_provider_conflict": (
                "onboarding.llmProfile.router_provider_conflict"
            ),
        }
        raise RpcHandlerError(
            code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid"),
            str(exc),
            details={
                "reason": exc.reason,
                "providerId": provider_id.strip().lower(),
                "replacementProviderId": replacement_provider_id.strip().lower(),
                **exc.details,
            },
        ) from exc
    except LlmProfileRemovalError as exc:
        code_by_reason = {
            "active_mismatch": "onboarding.llmProfile.active_mismatch",
            "profile_referenced": "onboarding.llmProfile.referenced",
        }
        raise RpcHandlerError(
            code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid"),
            str(exc),
            details={
                "reason": exc.reason,
                "providerId": provider_id.strip().lower(),
                "replacementProviderId": replacement_provider_id.strip().lower(),
                **exc.details,
            },
        ) from exc
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError("onboarding.llmProfile.invalid", str(exc)) from exc

    return cast(dict[str, Any], result.to_payload())


async def _llm_profile_activate(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Promote one stored profile without moving secrets through the client."""
    from opensquilla.application.profile_lifecycle import (
        ActivateProfile,
    )
    from opensquilla.onboarding.mutations import LlmProfileActivationError

    provider_id = str(_require(params, "providerId"))
    model = str(_param(params, "model", "") or "")
    router_action = _param(params, "routerAction", "preserve")
    image_generation_intent = _param(
        params,
        "imageGenerationIntent",
        "preserve",
    )
    try:
        result = await _profile_lifecycle(ctx).activate(
            ActivateProfile(
                provider_id=provider_id,
                model=model,
                router_action=str(router_action),
                image_generation_intent=str(image_generation_intent),
            )
        )
    except LlmProfileActivationError as exc:
        code_by_reason = {
            "primary_pool_unsupported": (
                "onboarding.llmProfile.primary_pool_unsupported"
            ),
            "router_provider_conflict": (
                "onboarding.llmProfile.router_provider_conflict"
            ),
        }
        code = code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid")
        details = {
            "reason": exc.reason,
            "providerId": provider_id.strip().lower(),
            **exc.details,
        }
        raise RpcHandlerError(
            code,
            str(exc),
            details=details,
        ) from exc
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError("onboarding.llmProfile.invalid", str(exc)) from exc

    return cast(dict[str, Any], result.to_payload())


def _llm_profile_rpc_session_key(connection_id: str, provider_id: str) -> str:
    provider = str(provider_id or "").strip().lower()
    return f"onboarding-profile-rpc:{connection_id}:{provider}"


def _resolved_llm_profile_config(
    cfg: Any,
    provider_id: str,
    model: str,
    *,
    session_key: str,
) -> Any:
    """Resolve a stored profile or raise a stable, secret-free validation error."""
    from opensquilla.engine.selector_override import acquire_profile_credential
    from opensquilla.provider.deployment import resolve_provider_deployment

    provider = str(provider_id or "").strip().lower()
    profiles = getattr(cfg, "llm_profiles", None) or {}
    if not any(str(key or "").strip().lower() == provider for key in profiles):
        raise ValueError(f"provider profile {provider!r} does not exist")
    resolution = resolve_provider_deployment(
        cfg,
        provider,
        model,
        session_key=session_key,
        credential_pool_acquirer=acquire_profile_credential,
    )
    if not resolution.ready or resolution.provider_config is None:
        raise ValueError(
            f"provider profile {resolution.provider!r} is not executable: {resolution.reason}"
        )
    return resolution


def _report_llm_profile_rpc_failure(
    provider_id: str,
    session_key: str,
    failure_kind: str,
) -> None:
    """Park a failed pooled credential; non-pool/profile failures are no-ops."""
    if not failure_kind:
        return
    from opensquilla.engine.selector_override import report_profile_credential_failure
    from opensquilla.provider.failures import ProviderFailureKind

    try:
        kind = ProviderFailureKind(failure_kind)
    except ValueError:
        return
    report_profile_credential_failure(provider_id, session_key, kind)


def _profile_draft_config(
    command: ProfileProbeCommand,
    config: Any,
) -> tuple[str, Any]:
    """Build an in-memory profile draft without persisting or hot-applying it."""
    from opensquilla.onboarding.mutations import upsert_llm_profile

    provider = command.provider_id.strip().lower()
    values = command.values
    preserve_value = values.get("keepCurrentSecret", True)
    if not isinstance(preserve_value, bool):
        raise ValueError("params.keepCurrentSecret must be a boolean")
    profiles = getattr(config, "llm_profiles", None) or {}
    if not any(str(key or "").strip().lower() == provider for key in profiles):
        raise ValueError(f"provider profile {provider!r} does not exist")
    draft = upsert_llm_profile(
        config,
        provider_id=provider,
        api_key=values.get("apiKey") if "apiKey" in values else None,
        api_key_env=values.get("apiKeyEnv") if "apiKeyEnv" in values else None,
        preserve_api_key=preserve_value,
        base_url=values.get("baseUrl") if "baseUrl" in values else None,
        proxy=values.get("proxy") if "proxy" in values else None,
    )
    return provider, draft.config


async def _usage_accounted_provider_probe(
    usage_event_sink: Any,
    *,
    provider_id: str,
    model: str,
    api_key: str,
    api_key_env: str,
    base_url: str,
    proxy: str,
    allow_default_api_key_env: bool,
) -> ProviderProbeExecutionResult:
    """Probe one deployment under the shared physical-call usage boundary."""
    import uuid

    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        account_provider_stream,
        bind_usage_accounting_scope,
        provider_accounts_physical_usage,
    )
    from opensquilla.onboarding.probe import probe_llm_provider

    usage_scope = None
    chat_stream_factory = None
    if usage_event_sink is not None:
        execution_id = uuid.uuid4().hex
        usage_scope = UsageAccountingScope(
            sink=usage_event_sink,
            context=UsageExecutionContext(
                execution_id=execution_id,
                agent_run_id=execution_id,
                turn_id=execution_id,
                session_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "opensquilla:system:onboarding-provider-probe",
                ).hex,
                agent_id="system",
                run_kind="onboarding_probe",
            ),
        )

        def chat_stream_factory(provider: Any, messages: Any, chat_config: Any) -> Any:
            if provider_accounts_physical_usage(provider):
                return provider.chat(messages, config=chat_config)
            return account_provider_stream(
                lambda: provider.chat(messages, config=chat_config),
                provider=str(provider_id),
                model=str(model),
            )

    probe_kwargs: dict[str, Any] = {
        "provider_id": provider_id,
        "model": model,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url,
        "proxy": proxy,
        "allow_default_api_key_env": allow_default_api_key_env,
    }
    if chat_stream_factory is not None:
        probe_kwargs["chat_stream_factory"] = chat_stream_factory
    with bind_usage_accounting_scope(usage_scope):
        return await probe_llm_provider(**probe_kwargs)


async def _probe_saved_profile(
    command: ProfileProbeCommand,
    *,
    config: Any,
    connection_id: str,
    usage_event_sink: Any,
) -> dict[str, Any]:
    """Run a small live probe using the stored profile's resolved deployment."""
    provider_id = command.provider_id
    if "model" not in command.values:
        raise ValueError("params.model is required")
    model = str(command.values.get("model") or "").strip()
    cfg = config
    session_key = _llm_profile_rpc_session_key(connection_id, provider_id)
    with _validation_error("onboarding.llmProfile.invalid"):
        resolution = _resolved_llm_profile_config(
            cfg,
            provider_id,
            model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await _usage_accounted_provider_probe(
            usage_event_sink,
            provider_id=deployment.provider,
            model=deployment.model,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    from opensquilla.onboarding.probe_history import record_probe

    record_probe(cfg, deployment.provider, ok=result.ok, failure_kind=result.failure_kind)
    return result.to_payload()


async def _probe_draft_profile(
    command: ProfileProbeCommand,
    *,
    config: Any,
    connection_id: str,
    usage_event_sink: Any,
) -> dict[str, Any]:
    """Probe the editor's current profile draft without saving any field."""
    if "model" not in command.values:
        raise ValueError("params.model is required")
    model = str(command.values.get("model") or "").strip()
    with _validation_error("onboarding.llmProfile.invalid"):
        provider_id, draft = _profile_draft_config(command, config)
        session_key = _llm_profile_rpc_session_key(connection_id, provider_id)
        resolution = _resolved_llm_profile_config(
            draft,
            provider_id,
            model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await _usage_accounted_provider_probe(
            usage_event_sink,
            provider_id=deployment.provider,
            model=deployment.model,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    # Do not return request fields or the cloned config: both may contain keys.
    return result.to_payload()


async def _discover_saved_profile_models(
    command: ProfileProbeCommand,
    *,
    config: Any,
    connection_id: str,
) -> dict[str, Any]:
    """Discover picker-safe models through one stored profile deployment."""
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    provider_id = command.provider_id
    cfg = config
    placeholder_model = str(getattr(cfg.llm, "model", "") or "profile-discovery")
    session_key = _llm_profile_rpc_session_key(connection_id, provider_id)
    with _validation_error("onboarding.llmProfile.invalid"):
        resolution = _resolved_llm_profile_config(
            cfg,
            provider_id,
            placeholder_model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await discover_selectable_provider_models(
            provider_id=deployment.provider,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
            force_refresh=_bool_param(command.values, "forceRefresh"),
            persist_catalog=True,
            catalog_config=cfg,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    return result.to_payload()


async def _discover_draft_profile_models(
    command: ProfileProbeCommand,
    *,
    config: Any,
    connection_id: str,
) -> dict[str, Any]:
    """Discover models through the editor's unsaved profile deployment."""
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    with _validation_error("onboarding.llmProfile.invalid"):
        provider_id, draft = _profile_draft_config(command, config)
        placeholder_model = str(getattr(draft.llm, "model", "") or "profile-discovery")
        session_key = _llm_profile_rpc_session_key(connection_id, provider_id)
        resolution = _resolved_llm_profile_config(
            draft,
            provider_id,
            placeholder_model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await discover_selectable_provider_models(
            provider_id=deployment.provider,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
            force_refresh=_bool_param(command.values, "forceRefresh"),
            persist_catalog=False,
            catalog_config=draft,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    return result.to_payload()


async def _probe_primary_provider(
    command: ProbePrimaryProvider,
    *,
    config: Any,
    usage_event_sink: Any,
) -> dict[str, Any]:
    """Live probe of a candidate provider config without saving it."""
    provider_id = command.provider_id
    cfg = config
    api_key = str(command.api_key or "")
    if is_redacted_secret_sentinel(api_key):
        # A round-tripped redaction mask is a display value, not a
        # credential: fall through to the stored-credential reuse below
        # instead of probing with a literal '***' bearer token.
        api_key = ""
    api_key_env = str(command.api_key_env or "")
    base_url = str(command.base_url or "")
    proxy = str(command.proxy or "")
    # Draft probes carry explicit fields; only a bare providerId(+model)
    # request verifies the saved deployment and may update probe history.
    request_overrides = _request_changes_active_provider_connection(
        {
            "providerId": provider_id,
            "apiKey": api_key,
            "apiKeyEnv": api_key_env,
            "baseUrl": base_url,
            "proxy": proxy,
        },
        cfg,
    )
    # A provider id is not an endpoint identity for configurable providers.
    # Stored credentials may follow an omitted URL or a same-origin path
    # change, but never a scheme/host/effective-port change.
    same_provider, reuse_stored_credentials = _provider_candidate_identity(
        cfg,
        str(provider_id),
        base_url,
    )
    if same_provider:
        if not api_key and not api_key_env and reuse_stored_credentials:
            api_key = str(getattr(cfg.llm, "api_key", "") or "")
            api_key_env = str(getattr(cfg.llm, "api_key_env", "") or "")
        if not base_url:
            base_url = str(getattr(cfg.llm, "base_url", "") or "")
        if not proxy:
            proxy = str(getattr(cfg.llm, "proxy", "") or "")
    model = str(command.model or "")
    allow_default_api_key_env = not same_provider or reuse_stored_credentials
    with _validation_error("onboarding.provider.invalid"):
        if model.strip():
            result = await _usage_accounted_provider_probe(
                usage_event_sink,
                provider_id=str(provider_id),
                model=model,
                api_key=api_key,
                api_key_env=api_key_env,
                base_url=base_url,
                proxy=proxy,
                allow_default_api_key_env=allow_default_api_key_env,
            )
        else:
            # A model is unnecessary for an endpoint/credential connectivity
            # check; model discovery exercises that path without a chat turn.
            from opensquilla.onboarding.probe import (
                ProviderProbeResult,
                discover_provider_models,
            )

            listing = await discover_provider_models(
                provider_id=str(provider_id),
                api_key=api_key,
                api_key_env=api_key_env,
                base_url=base_url,
                proxy=proxy,
                allow_default_api_key_env=allow_default_api_key_env,
            )
            result = ProviderProbeResult(
                ok=listing.ok,
                provider_id=str(provider_id),
                model="",
                failure_kind=listing.failure_kind,
                message=listing.detail,
            )
    saved_model = str(getattr(cfg.llm, "model", "") or "").strip()
    if (
        same_provider
        and reuse_stored_credentials
        and not request_overrides
        and (not model.strip() or model.strip() == saved_model)
    ):
        from opensquilla.onboarding.probe_history import record_probe

        record_probe(
            cfg,
            str(provider_id),
            ok=bool(getattr(result, "ok", False)),
            failure_kind=str(getattr(result, "failure_kind", "") or ""),
        )
    return result.to_payload()


async def _provider_credential_reveal(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_credentials import ProviderCredentials

    config, runtime = _setup_application_ports(ctx)
    return cast(
        dict[str, Any],
        ProviderCredentials(
            config,
            runtime,
            _credential_resolution_port(ctx),
            _setup_mutation_port(),
        ).reveal_active(str(_require(params, "providerId"))),
    )


async def _provider_credential_clear(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Clear stored credentials for the active provider, preserving its setup."""
    from opensquilla.application.provider_credentials import ProviderCredentials

    provider_id = str(_require(params, "providerId"))
    config, runtime = _setup_application_ports(ctx)
    with _validation_error("onboarding.provider.invalid"):
        result = await ProviderCredentials(
            config,
            runtime,
            _credential_resolution_port(ctx),
            _setup_mutation_port(),
        ).clear_active(provider_id)
    live_config = _active_config(ctx)
    # Selector sync may resolve the provider's registry-default environment
    # key on a scratch config. The selector may keep using that external key,
    # but the cleared live config itself holds no cached secret and therefore
    # must not retain stale runtime-secret provenance.
    if not str(getattr(live_config.llm, "api_key", "") or ""):
        live_config._runtime_secret_paths.discard("llm.api_key")
    return cast(dict[str, Any], result.to_payload())


async def _discover_primary_models(
    command: DiscoverPrimaryModels,
    *,
    config: Any,
) -> dict[str, Any]:
    """List verified picker-safe models without persisting anything.

    Admin-scoped (like ``onboarding.provider.probe``): the request carries
    candidate credentials, so it must not be reachable at the read/write
    tiers even though it changes no state.

    Selector discovery is fail-closed: only registry-verified providers on
    their official hosts are queried. Self-hosted and arbitrary endpoints
    remain manual-entry surfaces; raw CLI diagnostics retain their broader
    endpoint-probing behavior.

    Blank credentials fall back to the stored config's only while a supplied
    candidate Base URL remains same-origin; omitted Base URLs reuse the stored
    endpoint.
    """
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    provider_id = command.provider_id
    cfg = config
    api_key = str(command.api_key or "")
    if is_redacted_secret_sentinel(api_key):
        # Same keep-current boundary as onboarding.provider.probe: never
        # send a round-tripped '***' mask upstream as a bearer token.
        api_key = ""
    api_key_env = str(command.api_key_env or "")
    base_url = str(command.base_url or "")
    proxy = str(command.proxy or "")
    force_refresh = command.force_refresh
    request_overrides = _request_changes_active_provider_connection(
        {
            "providerId": provider_id,
            "apiKey": api_key,
            "apiKeyEnv": api_key_env,
            "baseUrl": base_url,
            "proxy": proxy,
        },
        cfg,
    )
    same_provider, reuse_stored_credentials = _provider_candidate_identity(
        cfg,
        str(provider_id),
        base_url,
    )
    if same_provider:
        if not api_key and not api_key_env and reuse_stored_credentials:
            api_key = str(getattr(cfg.llm, "api_key", "") or "")
            api_key_env = str(getattr(cfg.llm, "api_key_env", "") or "")
        if not base_url:
            base_url = str(getattr(cfg.llm, "base_url", "") or "")
        if not proxy:
            proxy = str(getattr(cfg.llm, "proxy", "") or "")
    with _validation_error("onboarding.provider.invalid"):
        result = await discover_selectable_provider_models(
            provider_id=provider_id,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=base_url,
            proxy=proxy,
            allow_default_api_key_env=(
                not same_provider or reuse_stored_credentials
            ),
            force_refresh=force_refresh,
            persist_catalog=(
                same_provider and reuse_stored_credentials and not request_overrides
            ),
            catalog_config=cfg,
        )
    return result.to_payload()


async def _discover_image_models(provider_id: str) -> dict[str, Any]:
    """List image-output-capable models without persisting configuration.

    Unlike the general LLM picker, this endpoint only uses provider image
    catalogs.  The live request, when supported, is fixed to the provider's
    official image-model endpoint and never accepts an operator-supplied URL or
    credential.  Curated setup-catalog rows provide an offline-safe fallback.
    """
    from opensquilla.onboarding.image_generation_model_discovery import (
        discover_image_generation_models,
    )

    with _validation_error("onboarding.imageGeneration.invalid"):
        return await discover_image_generation_models(str(provider_id))


@_d.method("onboarding.router.catalog", scope="operator.read")
async def _router_catalog(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.router_specs import router_catalog_payload

    return router_catalog_payload()


async def _router_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.capability_setup import ConfigureRouter

    mode = params.get("mode", "recommended") if isinstance(params, dict) else "recommended"
    default_tier = params.get("defaultTier") if isinstance(params, dict) else None
    tiers = params.get("tiers") if isinstance(params, dict) else None
    cross_provider_tiers = params.get("crossProviderTiers") if isinstance(params, dict) else None
    tier_provider_mismatch = (
        params.get("tierProviderMismatch") if isinstance(params, dict) else None
    )
    with _validation_error("onboarding.router.invalid"):
        result = await _capability_setup(ctx).configure_router(
            ConfigureRouter(
                mode=str(mode),
                default_tier=default_tier,
                tiers=tiers,
                cross_provider_tiers=cross_provider_tiers,
                tier_provider_mismatch=tier_provider_mismatch,
            )
        )
    return cast(dict[str, Any], result.to_payload())


async def _ensemble_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Configure the [llm_ensemble] routing surface.

    Omitted params keep the current value (partial-payload merge in the
    mutation); the TurnRunner reads llm_ensemble live, so no restart.
    """
    from opensquilla.application.capability_setup import (
        ConfigureEnsemble,
    )

    p = params if isinstance(params, dict) else {}
    with _validation_error("onboarding.ensemble.invalid"):
        result = await _capability_setup(ctx).configure_ensemble(
            ConfigureEnsemble(
                enabled=p.get("enabled"),
                selection_mode=p.get("selectionMode"),
                model_options=p.get("modelOptions"),
                candidates=p.get("candidates"),
                min_successful_proposers=p.get("minSuccessfulProposers"),
                proposer_max_retries=p.get("proposerMaxRetries"),
                all_failed_policy=p.get("allFailedPolicy"),
            )
        )
    return cast(dict[str, Any], result.to_payload())


async def _channel_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import (
        merge_channel_entry_secrets,
        validate_channel_entry,
    )
    from opensquilla.onboarding.redaction import redact_channel_entry

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    # Merge-aware probe: blank secrets resolve against the stored entry the
    # same way onboarding.channel.upsert does, so probing a keep-current
    # payload validates the entry the upsert would actually persist instead
    # of hard-failing on the non-blank-secret requirement. A genuinely blank
    # secret (no stored entry to merge from) still fails validation.
    cfg = _active_config(ctx)
    with _channel_error():
        normalized = validate_channel_entry(merge_channel_entry_secrets(cfg, entry))
    type_name = str(normalized.get("type") or "")
    return {
        "status": "validated",
        "connected": False,
        "probeKind": "local_validation",
        "restartRequired": True,
        "entry": redact_channel_entry(type_name, normalized),
        "warnings": [
            "Configuration is locally valid; no provider connection was attempted."
        ],
    }


async def _search_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.capability_setup import ConfigureSearch

    with _validation_error("onboarding.search.invalid"):
        result = await _capability_setup(ctx).configure_search(
            ConfigureSearch(
                provider_id=str(_require(params, "providerId")),
                api_key=str(_param(params, "apiKey", "")),
                api_key_env=str(_param(params, "apiKeyEnv", "")),
                max_results=_param(params, "maxResults", DEFAULT_SEARCH_MAX_RESULTS),
                proxy=str(_param(params, "proxy", "")),
                use_env_proxy=_bool_param(params, "useEnvProxy"),
                fallback_policy=str(_param(params, "fallbackPolicy", "off")),
                diagnostics=_bool_param(params, "diagnostics"),
            )
        )
    return cast(dict[str, Any], result.to_payload())


async def _image_generation_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.capability_setup import (
        ConfigureImageGeneration,
    )

    p = params if isinstance(params, dict) else {}
    fallbacks = params.get("fallbacks") if isinstance(params, dict) else None
    with _validation_error("onboarding.imageGeneration.invalid"):
        if fallbacks is not None and not isinstance(fallbacks, list):
            raise ValueError("fallbacks must be a list of provider/model references")
        result = await _capability_setup(ctx).configure_image_generation(
            ConfigureImageGeneration(
                provider_id=str(_require(params, "providerId")),
                primary=str(p.get("primary", "")),
                api_key=str(p.get("apiKey", "")),
                api_key_env=str(p.get("apiKeyEnv", "")),
                base_url=p.get("baseUrl"),
                enabled=p.get("enabled", True),
                size=str(p.get("size", "")),
                output_format=str(p.get("outputFormat", "")),
                fallbacks=fallbacks,
                clear_fallbacks=p.get("clearFallbacks", False),
                credential_mode=p.get("credentialMode"),
            )
        )
    return cast(dict[str, Any], result.to_payload())


async def _memory_embedding_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.capability_setup import (
        ConfigureMemoryEmbedding,
    )

    p = params if isinstance(params, dict) else {}
    result = await _capability_setup(ctx).configure_memory_embedding(
        ConfigureMemoryEmbedding(
            provider_id=str(_require(params, "providerId")),
            model=str(p.get("model", "")),
            api_key=str(p.get("apiKey", "")),
            api_key_env=str(p.get("apiKeyEnv", "")),
            base_url=str(p.get("baseUrl", "")),
            onnx_dir=str(p.get("onnxDir", "")),
        )
    )
    return cast(dict[str, Any], result.to_payload())


async def _audio_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.capability_setup import ConfigureAudio

    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    result = await _capability_setup(ctx).configure_audio(
        ConfigureAudio(
            provider_id=provider_id,
            api_key=p.get("apiKey", ""),
            api_key_env=p.get("apiKeyEnv", ""),
            base_url=p.get("baseUrl", ""),
            enabled=p.get("enabled", True),
            tts_voice=p.get("ttsVoice", ""),
            tts_model=p.get("ttsModel", ""),
            language_code=p.get("languageCode", ""),
        ),
    )
    return cast(dict[str, Any], result.to_payload())


async def _capability_reset(params: Any, ctx: RpcContext) -> dict[str, Any]:

    with _validation_error("onboarding.capability.invalid"):
        result = await _capability_setup(ctx).reset(
            str(_require(params, "capabilityId"))
        )
    return cast(dict[str, Any], result.to_payload())


async def _reconcile_channels_live() -> dict[str, str] | None:
    """Run the boot-registered channel reconciler against the live config.

    ``None`` means no reconciler is registered (standalone/config-only
    contexts) — everything stays restart-gated. A reconciler failure also
    degrades to restart-gated: the config is already persisted and applied
    in place, so the honest fallback is the pre-reconcile contract.
    """
    from opensquilla.gateway.channels_bridge import get_channels_reconciler

    reconciler = get_channels_reconciler()
    if reconciler is None:
        return None
    try:
        return await reconciler()
    except Exception as exc:  # noqa: BLE001 - config stays valid either way
        log.warning("onboarding.channel_reconcile_failed", error=str(exc))
        return None


def _live_apply_fields(live: dict[str, str] | None, names: list[str]) -> dict[str, Any]:
    """Response fields describing what the reconciler actually did.

    ``restartRequired`` stays the compatibility signal older clients read; it
    is scoped to the channel(s) THIS call mutated — an unrelated channel's
    outstanding restart must not relabel a live-applied save. ``failed`` does
    NOT flag a restart — restarting won't fix a bad entry; the channel
    carries its error in channels.status and channels.restart retries it.
    ``liveApply`` keeps the full per-name outcome map for observability.
    """
    if live is None:
        return {"restartRequired": True, "liveApply": None}
    pending = any(live.get(name) == "pending_restart" for name in names)
    return {"restartRequired": pending, "liveApply": live}


async def _channel_upsert(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_channel

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    cfg = _active_config(ctx)
    with _channel_error():
        res = upsert_channel(cfg, entry_payload=entry)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    entry_name = str(res.public_payload.get("name") or entry.get("name") or "")
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [entry_name]),
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


async def _channel_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import remove_channel

    name = _require(params, "name")
    cfg = _active_config(ctx)
    with _channel_error():
        res = remove_channel(cfg, name=name)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [name]),
        "configPath": config_path,
        "removed": name,
    }


async def _toggle(ctx: RpcContext, params: Any, enabled: bool) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import set_channel_enabled

    name = _require(params, "name")
    cfg = _active_config(ctx)
    with _channel_error():
        res = set_channel_enabled(cfg, name=name, enabled=enabled)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [name]),
        "configPath": config_path,
        "name": name,
        "enabled": enabled,
    }


async def _channel_enable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, True)


async def _channel_disable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, False)


# Generated descriptors own identity/scope/validation for the setup methods.
# The compatibility functions above stay importable for focused tests and old
# internal callers, while dispatcher registration converges on one generic
# Contract handler per wire name.
from opensquilla.gateway.adapters.platform_setup_contract import (  # noqa: E402
    register_platform_setup_contract,
)

_PLATFORM_SETUP_IMPLEMENTATIONS = {
    "onboarding.status": _onboarding_status,
    "onboarding.catalog": _onboarding_catalog,
    "onboarding.provider.configure": _provider_configure,
    "onboarding.provider.probe": _provider_probe,
    "onboarding.models.discover": _models_discover,
    "onboarding.imageGeneration.models.discover": _image_generation_models_discover,
    "onboarding.provider.credential.reveal": _provider_credential_reveal,
    "onboarding.provider.credential.clear": _provider_credential_clear,
    "onboarding.llmProfile.upsert": _llm_profile_upsert,
    "onboarding.llmProfile.activate": _llm_profile_activate,
    "onboarding.llmProfile.remove": _llm_profile_remove,
    "onboarding.llmProfile.active.remove": _llm_profile_active_remove,
    "onboarding.llmProfile.credential.clear": _llm_profile_credential_clear,
    "onboarding.llmProfile.probe": _llm_profile_probe,
    "onboarding.llmProfile.draft.probe": _llm_profile_draft_probe,
    "onboarding.llmProfile.models.discover": _llm_profile_models_discover,
    "onboarding.llmProfile.draft.models.discover": (
        _llm_profile_draft_models_discover
    ),
    "onboarding.router.configure": _router_configure,
    "onboarding.ensemble.configure": _ensemble_configure,
    "onboarding.search.configure": _search_configure,
    "onboarding.imageGeneration.configure": _image_generation_configure,
    "onboarding.memory_embedding.configure": _memory_embedding_configure,
    "onboarding.audio.configure": _audio_configure,
    "onboarding.capability.reset": _capability_reset,
    "onboarding.channel.probe": _channel_probe,
    "onboarding.channel.upsert": _channel_upsert,
    "onboarding.channel.remove": _channel_remove,
    "onboarding.channel.enable": _channel_enable,
    "onboarding.channel.disable": _channel_disable,
}

_PLATFORM_SETUP_CONTRACT_HANDLERS = {
    method: register_platform_setup_contract(
        _d,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _PLATFORM_SETUP_IMPLEMENTATIONS.items()
}
