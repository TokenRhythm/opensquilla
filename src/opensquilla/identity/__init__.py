"""Identity module: agent persona parsing and system prompt assembly."""

from .bootstrap import AgentWorkspaceBootstrapResult, ensure_agent_workspace
from .parser import parse_agents, parse_identity, parse_soul
from .prompt import assemble_system_prompt
from .types import (
    AgentCapability,
    AgentIdentity,
    AgentProfile,
    AgentsDocument,
    IdentityFields,
    SoulDocument,
)
from .workspace import load_workspace_files, load_workspace_files_async

__all__ = [
    "AgentCapability",
    "AgentWorkspaceBootstrapResult",
    "AgentIdentity",
    "AgentProfile",
    "AgentsDocument",
    "IdentityFields",
    "SoulDocument",
    "assemble_system_prompt",
    "ensure_agent_workspace",
    "load_workspace_files",
    "load_workspace_files_async",
    "parse_agents",
    "parse_identity",
    "parse_soul",
]
