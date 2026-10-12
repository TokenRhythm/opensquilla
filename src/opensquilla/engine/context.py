"""Context assembly — data classes and workspace file loading.

Prompt building is handled by identity.prompt (single path).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path

# Deprecated compatibility shim. Tool handlers should read request-scoped
# session identity from current_tool_context.get().session_key instead.
current_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_session_key", default=""
)


@dataclass
class ContextFiles:
    """Bootstrap context files loaded from workspace."""

    soul: str | None = None
    agents: str | None = None
    identity: str | None = None
    tools: str | None = None
    user: str | None = None
    memory: str | None = None

    def as_dict(self) -> dict[str, str]:
        mapping = {
            "SOUL.md": self.soul,
            "AGENTS.md": self.agents,
            "IDENTITY.md": self.identity,
            "TOOLS.md": self.tools,
            "USER.md": self.user,
            "MEMORY.md": self.memory,
        }
        return {k: v for k, v in mapping.items() if v is not None}


@dataclass
class ContextAssembly:
    """Assembled context for a single agent turn."""

    system_prompt: str
    workspace_dir: str | None = None
    context_files: ContextFiles = field(default_factory=ContextFiles)


def load_context_files(workspace_dir: str) -> ContextFiles:
    """Load bootstrap context files from workspace directory."""
    ctx = ContextFiles()
    workspace = Path(workspace_dir)

    file_map = {
        "SOUL.md": "soul",
        "AGENTS.md": "agents",
        "IDENTITY.md": "identity",
        "TOOLS.md": "tools",
        "USER.md": "user",
        "MEMORY.md": "memory",
    }
    for filename, attr in file_map.items():
        path = workspace / filename
        if path.exists():
            try:
                setattr(ctx, attr, path.read_text(encoding="utf-8"))
            except OSError:
                pass

    return ctx
