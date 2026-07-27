"""Stable discovery API for the self-contained OpenSquilla TUI host."""

from .api import HostArtifactUnavailableError, HostMetadata, host_command, host_metadata

__all__ = [
    "HostArtifactUnavailableError",
    "HostMetadata",
    "host_command",
    "host_metadata",
]
