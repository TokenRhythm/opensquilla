"""Validate a staged TUI host artifact and emit a platform wheel tag."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Refuse to produce a universal or incomplete companion wheel."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version  # Hatch calls this build variant (normally "standard"), not package version.
        package_version = str(self.metadata.version)
        package_dir = Path(self.root) / "src" / "opensquilla_tui_host"
        metadata_path = package_dir / "_host_metadata.json"
        if not metadata_path.is_file():
            raise RuntimeError(
                "TUI host metadata is missing; use scripts/build_tui_host_companion.py"
            )

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        product_version = metadata.get("product_version")
        host_version = metadata.get("host_version")
        if product_version != package_version or host_version != package_version:
            raise RuntimeError(
                "TUI host metadata version mismatch: "
                f"package={package_version!r}, product={product_version!r}, "
                f"host={host_version!r}"
            )

        wheel_tag = metadata.get("wheel_tag")
        if not isinstance(wheel_tag, str) or not wheel_tag.startswith("py3-none-"):
            raise RuntimeError(f"invalid TUI host wheel tag: {wheel_tag!r}")
        if wheel_tag == "py3-none-any":
            raise RuntimeError("TUI host wheels must be platform-specific")

        executable_relpath = metadata.get("executable")
        if not isinstance(executable_relpath, str):
            raise RuntimeError("TUI host metadata has no executable path")
        executable = package_dir / executable_relpath
        if not executable.is_file():
            raise RuntimeError(f"TUI host executable is missing: {executable}")

        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        if metadata.get("sha256") != digest:
            raise RuntimeError("TUI host executable checksum does not match metadata")

        build_data["tag"] = wheel_tag
        build_data["pure_python"] = False
