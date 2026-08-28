"""Engine-level hook protocols.

Current production status:

* :class:`TurnHook` is active for turn event emission. ``TurnRunner`` registers
  :class:`DefaultTraceEmitterHook` by default.
* :class:`CompactionHook` is active when explicitly supplied to ``TurnRunner``;
  the compaction/history stage fires it around pre-turn compaction attempts.
* :class:`ToolHook` is supported by the tool-dispatch factory, but ``TurnRunner``
  does not register tool hooks by default.

Default implementations in :mod:`opensquilla.engine.hooks.defaults` preserve the
canonical inline behavior unless a caller explicitly supplies additional hooks.
"""

from __future__ import annotations

from opensquilla.engine.hooks.defaults import (
    DefaultMemoryFlushHook,
    DefaultTraceEmitterHook,
    DefaultTranscriptHook,
    NoopCompactionHook,
    NoopToolHook,
    NoopTurnHook,
    build_default_turn_hooks,
)
from opensquilla.engine.hooks.types import (
    CompactionHook,
    CompactionState,
    ToolHook,
    ToolHookCall,
    ToolHookResult,
    TurnEvent,
    TurnHook,
    TurnHookContext,
    TurnHookResult,
)

__all__ = [
    "CompactionHook",
    "CompactionState",
    "DefaultMemoryFlushHook",
    "DefaultTraceEmitterHook",
    "DefaultTranscriptHook",
    "NoopCompactionHook",
    "NoopToolHook",
    "NoopTurnHook",
    "ToolHook",
    "ToolHookCall",
    "ToolHookResult",
    "TurnEvent",
    "TurnHook",
    "TurnHookContext",
    "TurnHookResult",
    "build_default_turn_hooks",
]
