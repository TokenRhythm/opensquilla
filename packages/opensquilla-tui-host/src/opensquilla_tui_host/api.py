"""Public API used by OpenSquilla core to locate its companion host."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path
from typing import Any


class HostArtifactUnavailableError(RuntimeError):
    """Raised when the installed companion artifact is missing or corrupt."""


@dataclass(frozen=True)
class HostMetadata:
    """Build identity for one installed platform companion."""

    schema_version: int
    product_version: str
    host_version: str
    protocol_version: int
    platform: str
    arch: str
    build_id: str
    wheel_tag: str
    executable: str
    sha256: str
    bun_version: str


def host_command() -> tuple[str, ...]:
    """Return the executable argv for the installed, verified host."""

    metadata = host_metadata()
    executable = _package_path() / metadata.executable
    if not executable.is_file():
        raise HostArtifactUnavailableError(f"TUI host executable is missing: {executable}")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != metadata.sha256:
        raise HostArtifactUnavailableError("TUI host executable checksum verification failed")
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise HostArtifactUnavailableError(f"TUI host executable is not executable: {executable}")
    return (str(executable),)


@lru_cache(maxsize=1)
def host_metadata() -> HostMetadata:
    """Return validated immutable metadata embedded beside the host binary."""

    metadata_path = _package_path() / "_host_metadata.json"
    try:
        payload: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostArtifactUnavailableError("TUI host build metadata is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise HostArtifactUnavailableError("TUI host build metadata must be an object")

    required = {field.name for field in HostMetadata.__dataclass_fields__.values()}
    missing = sorted(required - payload.keys())
    if missing:
        raise HostArtifactUnavailableError(
            f"TUI host build metadata schema mismatch: missing={missing}"
        )
    try:
        # Ignore additive metadata from a newer builder while schema v1 remains
        # compatible with these stable public fields.
        metadata = HostMetadata(**{key: payload[key] for key in required})
    except TypeError as exc:
        raise HostArtifactUnavailableError("TUI host build metadata is invalid") from exc
    if metadata.schema_version != 1:
        raise HostArtifactUnavailableError(
            f"Unsupported TUI host metadata schema: {metadata.schema_version}"
        )
    if metadata.product_version != metadata.host_version:
        raise HostArtifactUnavailableError("TUI host product and host versions do not match")
    try:
        distribution_version = version("opensquilla-tui-host")
    except PackageNotFoundError as exc:
        raise HostArtifactUnavailableError("TUI host distribution metadata is unavailable") from exc
    if distribution_version != metadata.product_version:
        raise HostArtifactUnavailableError(
            "TUI host distribution and product versions do not match"
        )
    return metadata


def _package_path() -> Path:
    return Path(str(files("opensquilla_tui_host")))
