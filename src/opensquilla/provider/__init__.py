"""opensquilla.provider — unified LLM provider abstraction layer."""

from .anthropic import AnthropicProvider
from .credentials import Credential, CredentialPool, NoCredentialsAvailable
from .ensemble import (
    EnsembleMemberConfig,
    EnsembleProvider,
    build_ensemble_provider_from_config,
)
from .failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider
from .protocol import (
    LLMProvider,
    ProviderFailure,
    ProviderMessageCountProjector,
    ProviderMetadata,
    ProviderMetadataProvider,
    ProviderPlugin,
    project_provider_message_count,
    provider_metadata,
    resolve_failover_chain,
    resolve_quota_status,
)
from .registry import (
    ProviderSpec,
    UnknownProviderError,
    get_provider_spec,
    list_provider_names,
    list_provider_specs,
)
from .selector import (
    ModelSelector,
    ProviderBuildError,
    ProviderConfig,
    SelectorConfig,
    build_provider,
)
from .smart_routing import RefusalDecision, should_refuse
from .types import (
    ChatConfig,
    ContentBlockCompaction,
    ContentBlockDocument,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    FailureInjector,
    Message,
    ModelCapabilities,
    ModelInfo,
    ProviderHeartbeatEvent,
    ProviderMessageCountProjection,
    ProviderMessageLimitProof,
    ProviderRequestCorrelation,
    QuotaStatus,
    ReasoningDeltaEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    derive_provider_request_correlation,
    synthetic_failure_event,
)

__all__ = [
    # Protocol
    "LLMProvider",
    "ProviderPlugin",
    "ProviderFailure",
    "ProviderMetadata",
    "ProviderMetadataProvider",
    "ProviderMessageCountProjector",
    "provider_metadata",
    "project_provider_message_count",
    "ProviderFailureKind",
    "resolve_failover_chain",
    "resolve_quota_status",
    "ProviderRecoveryAction",
    "classify_provider_error",
    "decide_recovery_action",
    # Providers
    "AnthropicProvider",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "OllamaProvider",
    "EnsembleProvider",
    "EnsembleMemberConfig",
    "build_ensemble_provider_from_config",
    # Registry
    "ProviderSpec",
    "UnknownProviderError",
    "get_provider_spec",
    "list_provider_names",
    "list_provider_specs",
    # Selector
    "ModelSelector",
    "SelectorConfig",
    "ProviderConfig",
    "ProviderBuildError",
    "build_provider",
    # Credentials
    "Credential",
    "CredentialPool",
    "NoCredentialsAvailable",
    # Smart routing
    "RefusalDecision",
    "should_refuse",
    # Types
    "StreamEvent",
    "TextDeltaEvent",
    "ReasoningDeltaEvent",
    "ToolUseStartEvent",
    "ToolUseDeltaEvent",
    "ToolUseEndEvent",
    "DoneEvent",
    "ErrorEvent",
    "ProviderHeartbeatEvent",
    "ProviderMessageCountProjection",
    "ProviderMessageLimitProof",
    "ProviderRequestCorrelation",
    "derive_provider_request_correlation",
    "ModelCapabilities",
    "ModelInfo",
    "ChatConfig",
    "Message",
    "QuotaStatus",
    "ToolDefinition",
    "ToolInputSchema",
    "ContentBlockText",
    "ContentBlockThinking",
    "ContentBlockToolUse",
    "ContentBlockToolResult",
    "ContentBlockCompaction",
    "ContentBlockDocument",
    # Test-only failure injection seam
    "FailureInjector",
    "synthetic_failure_event",
]
