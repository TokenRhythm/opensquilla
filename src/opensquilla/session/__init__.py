"""opensquilla.session — Session management: lifecycle, storage, key construction, compaction."""

from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionResult,
    build_compaction_config_from_provider,
    call_compact_with_optional_config,
    compact_accepts_config,
    compact_context,
)
from opensquilla.session.keys import (
    DmScope,
    PeerKind,
    build_channel_key,
    build_cron_key,
    build_direct_key,
    build_group_key,
    build_main_key,
    build_subagent_key,
    build_thread_key,
    build_webchat_key,
    canonicalize_session_key,
    derive_chat_type,
    normalize_account_id,
    normalize_agent_id,
    parse_thread_suffix,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    ChatType,
    InputProvenanceKind,
    QueueMode,
    SendPolicy,
    SessionIntent,
    SessionNode,
    SessionStatus,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage

__all__ = [
    # Models
    "SessionNode",
    "SessionSummary",
    "TranscriptEntry",
    "AgentTaskRecord",
    "SessionStatus",
    "AgentTaskStatus",
    "ChatType",
    "QueueMode",
    "SendPolicy",
    "SessionIntent",
    "InputProvenanceKind",
    # Storage
    "SessionStorage",
    # Manager
    "SessionManager",
    # Keys
    "DmScope",
    "PeerKind",
    "build_main_key",
    "build_webchat_key",
    "build_direct_key",
    "build_group_key",
    "build_channel_key",
    "build_thread_key",
    "build_subagent_key",
    "build_cron_key",
    "canonicalize_session_key",
    "parse_thread_suffix",
    "derive_chat_type",
    "normalize_agent_id",
    "normalize_account_id",
    # Compaction
    "CompactionConfig",
    "CompactionRequest",
    "CompactionResult",
    "build_compaction_config_from_provider",
    "call_compact_with_optional_config",
    "compact_accepts_config",
    "compact_context",
]
