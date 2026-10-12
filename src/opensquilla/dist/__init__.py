"""opensquilla.dist — distribution artefact builders.

``workspace-state.json`` is a reproducible, versioned inventory of what this
opensquilla install ships: bundled channels, bundled tools, gateway safety
defaults, package metadata, and the package's Python requirement.
"""

from opensquilla.dist.workspace_state import (
    BUNDLED_CHANNELS,
    BUNDLED_TOOLS,
    GATEWAY_DEFAULTS,
    SCHEMA_VERSION,
    build_workspace_state,
    to_json,
)

__all__ = [
    "BUNDLED_CHANNELS",
    "BUNDLED_TOOLS",
    "GATEWAY_DEFAULTS",
    "SCHEMA_VERSION",
    "build_workspace_state",
    "to_json",
]
