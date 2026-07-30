"""LLMProvider Protocol and provider-plugin extension contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .types import (
    ChatConfig,
    ErrorEvent,
    Message,
    ModelInfo,
    ProviderMessageCountProjection,
    QuotaStatus,
    StreamEvent,
    ToolDefinition,
)

if TYPE_CHECKING:
    from .selector import ProviderConfig, SelectorConfig


@dataclass(frozen=True)
class ProviderMetadata:
    """Read-only non-secret identity metadata exposed by provider implementations."""

    provider_name: str = ""
    provider_kind: str = ""
    model: str = ""
    base_url: str = ""
    # Configured registry identity (for example ``dashscope`` or
    # ``minimax_global``).  This is deliberately separate from
    # ``provider_name``, which identifies the adapter family, and
    # ``provider_kind``, which selects a wire-compatibility policy.
    provider_id: str = ""


@dataclass(frozen=True)
class ProviderConnectionConfig:
    """Provider connection fields for internal runtime calls."""

    provider_kind: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""


@runtime_checkable
class ProviderMetadataProvider(Protocol):
    def provider_metadata(self) -> ProviderMetadata:
        """Return read-only provider metadata without exposing secrets."""
        ...


@runtime_checkable
class ProviderConnectionConfigProvider(Protocol):
    def provider_connection_config(self) -> ProviderConnectionConfig:
        """Return internal connection fields for provider-owned runtime calls."""
        ...


@runtime_checkable
class ProviderMessageCountProjector(Protocol):
    """Optional, side-effect-free wire-message cardinality projection."""

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        """Project the adapter's final wire count without issuing a request."""
        ...


def _string_value(value: object) -> str:
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        value = get_secret_value()
    return str(value).strip()


def provider_metadata(provider: object | None) -> ProviderMetadata:
    """Return provider identity metadata, preferring the public protocol."""
    if provider is None:
        return ProviderMetadata()
    metadata_fn = getattr(provider, "provider_metadata", None)
    if callable(metadata_fn):
        metadata = metadata_fn()
        if isinstance(metadata, ProviderMetadata):
            return metadata

    provider_name = _string_value(getattr(provider, "provider_name", ""))
    provider_id = _string_value(getattr(provider, "provider_id", ""))
    provider_kind = _string_value(getattr(provider, "provider_kind", ""))
    model = _string_value(getattr(provider, "model", ""))
    base_url = _string_value(getattr(provider, "base_url", ""))

    # Metadata-provider migration path: new code should expose provider_metadata().
    provider_kind = provider_kind or _string_value(getattr(provider, "_provider_kind", ""))
    model = model or _string_value(getattr(provider, "_model", ""))
    base_url = base_url or _string_value(getattr(provider, "_base_url", ""))
    return ProviderMetadata(
        provider_name=provider_name,
        provider_kind=provider_kind,
        model=model,
        base_url=base_url,
        provider_id=provider_id or provider_name,
    )


def configured_provider_id(provider: object | None) -> str:
    """Return the operator-facing registry identity for a provider instance.

    Generic adapters intentionally keep their family ``provider_name`` (for
    example ``openai`` or ``anthropic``) because compatibility, error
    classification, and catalog logic rely on it.  Runtime telemetry must use
    the configured deployment identity instead, when one was supplied by the
    selector factory.
    """

    metadata = provider_metadata(provider)
    return metadata.provider_id or metadata.provider_name


def provider_connection_config(provider: object | None) -> ProviderConnectionConfig:
    """Return internal provider connection fields without broadening metadata."""
    if provider is None:
        return ProviderConnectionConfig()
    config_fn = getattr(provider, "provider_connection_config", None)
    if callable(config_fn):
        config = config_fn()
        if isinstance(config, ProviderConnectionConfig):
            return config

    metadata = provider_metadata(provider)
    api_key = _string_value(getattr(provider, "api_key", ""))
    api_key = api_key or _string_value(getattr(provider, "_api_key", ""))
    return ProviderConnectionConfig(
        provider_kind=metadata.provider_kind,
        model=metadata.model,
        api_key=api_key,
        base_url=metadata.base_url,
    )


def project_provider_message_count(
    provider: object | None,
    messages: list[Message],
    config: ChatConfig | None = None,
    *,
    additional_messages: int = 0,
) -> ProviderMessageCountProjection | None:
    """Return an optional provider projection without broadening ``LLMProvider``.

    Projection is a recovery aid, never a prerequisite for a normal provider
    call.  A missing, invalid, or failing optional implementation therefore
    resolves to ``None`` rather than changing the established chat contract.
    """

    if provider is None:
        return None
    projection_fn = getattr(provider, "project_message_count", None)
    if not callable(projection_fn):
        return None
    try:
        projection = projection_fn(
            messages,
            config,
            additional_messages=additional_messages,
        )
    except Exception:  # noqa: BLE001 - optional capability must stay best-effort
        return None
    return projection if isinstance(projection, ProviderMessageCountProjection) else None


def validate_provider_chat_request(
    provider: object | None,
    messages: list[Message],
) -> ErrorEvent | None:
    """Run an optional, side-effect-free provider request preflight.

    Validation is deliberately duck-typed instead of widening
    :class:`LLMProvider`: ordinary providers keep the established chat
    contract, while composite providers may reject requests before any
    physical model call or usage-accounting envelope starts.

    A missing, raising, or invalid optional implementation is ignored.  The
    provider remains the authoritative fallback boundary and must repeat its
    own validation immediately before starting work.
    """

    if provider is None:
        return None
    validation_fn = getattr(provider, "validate_chat_request", None)
    if not callable(validation_fn):
        return None
    try:
        validation_error = validation_fn(messages)
    except Exception:  # noqa: BLE001 - optional preflight must stay best-effort
        return None
    return validation_error if isinstance(validation_error, ErrorEvent) else None


@runtime_checkable
class LLMProvider(Protocol):
    """Unified async streaming interface for any LLM backend.

    Implementors must provide:
    - chat(): streams events for a conversation turn
    - list_models(): returns available models for this provider
    - provider_name: str identifier (e.g. "anthropic", "openai", "ollama")
    """

    provider_name: str

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a conversation turn.

        Yields StreamEvent instances in order:
        - TextDeltaEvent for text chunks
        - ToolUseStartEvent / ToolUseDeltaEvent / ToolUseEndEvent for tool calls
        - DoneEvent when the turn completes
        - ErrorEvent on failure (instead of raising)
        """
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Return all models available from this provider."""
        ...


class ProviderFailure(Exception):  # noqa: N818 - public compatibility name
    """Raised / wrapped when a primary provider turn fails.

    The selector passes instances of this exception (or any ``Exception``
    subclass) to ``failover_hook`` so plugin authors can inspect the
    underlying cause and decide which fallback chain to return.
    """


@runtime_checkable
class ProviderPlugin(Protocol):
    """Extension contract for provider-adjacent plugins.

    Plugins may implement any subset of these hooks; ``ModelSelector``
    consults them through ``resolve_failover_chain`` /
    ``resolve_quota_status``, which return the documented defaults when
    no hook is registered.
    """

    def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
        """Return the ordered fallback chain for a primary failure.

        The returned list excludes the primary. An empty list signals
        "no fallback available" and forces the caller to surface the
        original failure to the user.
        """
        ...

    def quota_hook(self, session_id: str) -> QuotaStatus:
        """Return the remaining quota for ``session_id``.

        Unlimited / not-enforced is signaled via the default
        ``QuotaStatus`` (sentinel ``-1`` on both counters, ``None`` abort
        reason). A non-None ``abort_reason`` is surfaced verbatim in the
        user-facing graceful-abort payload.
        """
        ...


def resolve_failover_chain(
    primary_failure: Exception,
    config: SelectorConfig,
    plugin: ProviderPlugin | None = None,
) -> list[ProviderConfig]:
    """Return the fallback chain honoring a plugin ``failover_hook`` if set.

    Default (no plugin, or plugin raising) returns the static
    ``config.fallbacks`` chain declared on ``SelectorConfig``.
    """
    if plugin is not None and hasattr(plugin, "failover_hook"):
        try:
            chain = plugin.failover_hook(primary_failure)
        except Exception:
            chain = None
        if chain is not None:
            return list(chain)
    return list(config.fallbacks)


def resolve_quota_status(
    session_id: str,
    plugin: ProviderPlugin | None = None,
) -> QuotaStatus:
    """Return the quota status honoring a plugin ``quota_hook`` if set.

    Default (no plugin, or plugin raising) returns an unlimited sentinel
    ``QuotaStatus`` with ``abort_reason=None``.
    """
    if plugin is not None and hasattr(plugin, "quota_hook"):
        try:
            status = plugin.quota_hook(session_id)
        except Exception:
            return QuotaStatus()
        if status is not None:
            return status
    return QuotaStatus()
