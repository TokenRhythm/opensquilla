"""Hatch build hook for the generated Vue control UI artifact."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Fail standard distributions closed when their embedded WebUI is stale."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del build_data
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"wheel", "sdist"}:
            return
        if version != "standard":
            raise RuntimeError(
                "Unsupported Hatchling build mode: "
                f"target={self.target_name!r}, version={version!r}"
            )

        root = Path(self.root).resolve()
        sys.path.insert(0, str(root))
        try:
            from scripts.verify_webui_artifact import (
                verify_dist,
                verify_sdist_source_inventory,
            )

            verify_dist(
                root / "src/opensquilla/gateway/static/dist",
                webui_root=root / "opensquilla-webui",
                # Source archives are easy to redistribute accidentally. Keep
                # standard sdists privacy-safe even when a checkout contains
                # ignored personal music; direct local wheels may still embed
                # an explicitly customized artifact.
                forbid_personal_bgm=self.target_name == "sdist",
            )
            if self.target_name == "sdist":
                verify_sdist_source_inventory(root / "opensquilla-webui")
        except (ImportError, OSError, RuntimeError) as exc:
            privacy_note = (
                " Standard sdists intentionally reject personal BGM; build a "
                "direct local wheel if you need a private customized artifact."
                if self.target_name == "sdist"
                else ""
            )
            raise RuntimeError(
                "A verified WebUI artifact is required for standard wheel/sdist builds. "
                "From a repository checkout, run "
                "`cd opensquilla-webui && npm ci && npm run build`, then retry. "
                "VCS URL installs cannot build the untracked generated artifact; use "
                "an official release wheel, or clone the repository and run "
                "`bash scripts/install_source.sh` (`powershell -ExecutionPolicy "
                "Bypass -File ./scripts/install_source.ps1` on Windows). "
                f"Validation failed: {exc}{privacy_note}"
            ) from exc
        finally:
            sys.path.remove(str(root))
