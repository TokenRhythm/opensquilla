"""Memory system: long-term persistent memory for opensquilla agents."""

from .backend import MemoryBackend
from .embedding import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    NullEmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAIEmbeddingProvider,
    chunk_text,
)
from .flush import SILENT_REPLY_TOKEN, MemoryFlushPlan, resolve_flush_plan, should_flush
from .manager import MemoryManager, build_memory_managers
from .meta import MemoryIndexMeta
from .retrieval import MemoryRetriever
from .store import LongTermMemoryStore
from .sync_manager import MemorySyncManager, SessionDeltaTracker
from .sync_manager import MemorySyncManager as MemoryFileWatcher
from .types import (
    MemorySearchOpts,
    MemorySearchResult,
    MemorySource,
    SearchMode,
)

__all__ = [
    # types
    "MemorySearchResult",
    "MemorySearchOpts",
    "MemorySource",
    "SearchMode",
    # long-term store
    "MemoryBackend",
    "LongTermMemoryStore",
    # facade
    "MemoryManager",
    "build_memory_managers",
    # retrieval
    "MemoryRetriever",
    # embedding
    "EmbeddingProvider",
    "NullEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "LocalEmbeddingProvider",
    "chunk_text",
    # watcher (backward compat alias)
    "MemoryFileWatcher",
    # sync manager
    "MemorySyncManager",
    "SessionDeltaTracker",
    # flush
    "MemoryFlushPlan",
    "resolve_flush_plan",
    "should_flush",
    "SILENT_REPLY_TOKEN",
    # meta
    "MemoryIndexMeta",
]
