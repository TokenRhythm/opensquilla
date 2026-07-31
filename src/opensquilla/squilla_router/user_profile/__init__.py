"""Offline user-profile production (Dream-time LLM reads sessions).

During the post-Dream window an in-process LLM reads conversation transcripts
and emits one complete, versioned profile. Thumbs up/down and routing logs are
deliberately not inputs: every inferred field comes from conversation content.

The package is provider-neutral. The gateway injects the resolved Dream
provider and its stream adapter into the orchestrator. Every entry point is
fail-open, so profile production can never fail a turn or a Dream run.
"""

from __future__ import annotations
