"""Shared subagent governance limits.

Single source of truth for spawn-depth caps and related governance constants.
Both the in-process engine path (``engine/subagent.py``) and the canonical
gateway path (``tools/builtin/sessions.py``) import from here so the cap
cannot drift between codepaths.
"""

from __future__ import annotations

MAX_SPAWN_DEPTH = 3
"""Maximum subagent nesting depth.

Depth 0 = main session, depth 1 = first subagent, depth 2 = sub-subagent,
depth 3 = leaf worker. Spawning at depth >= 3 is rejected.
"""
