"""Fail-closed discovery for the source-only OpenTUI development host."""

from __future__ import annotations

import platform as platform_module
import shlex
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TuiSourceCheckoutHint:
    """Commands that prepare and launch the host from a verified checkout."""

    install_command: str
    launch_command: str


def resolve_tui_source_checkout_hint(
    *,
    module_file: str | Path | None = None,
    platform_name: str | None = None,
    arch: str | None = None,
) -> TuiSourceCheckoutHint | None:
    """Return source-host commands only when this module belongs to a checkout.

    Wheels also contain the JavaScript sources, so their presence alone is not
    proof that the development workflow is available.  The loaded module must
    have the repository layout and a Git worktree marker.  Windows stays quiet
    until the development hint has a native PowerShell form.
    """

    platform = (sys.platform if platform_name is None else platform_name).strip().lower()
    machine = (platform_module.machine() if arch is None else arch).strip().lower()
    normalized_arch = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }.get(machine)
    if platform not in {"darwin", "linux"} or normalized_arch is None:
        return None

    try:
        module_path = Path(__file__ if module_file is None else module_file).resolve(strict=True)
        root = module_path.parents[4]
    except (IndexError, OSError, RuntimeError):
        return None

    expected_module = root / "src" / "opensquilla" / "cli" / "tui" / "source_checkout.py"
    package = root / "src" / "opensquilla" / "cli" / "tui" / "opentui" / "package"
    required = (
        root / ".git",
        root / "pyproject.toml",
        root / "uv.lock",
        package / "package.json",
        package / "bun.lock",
        package / "src" / "main.mjs",
    )
    if module_path != expected_module or any(not path.exists() for path in required):
        return None
    if any(
        unicodedata.category(character).startswith("C")
        for path in (root, package)
        for character in str(path)
    ):
        return None

    return TuiSourceCheckoutHint(
        install_command=(
            f"bun install --frozen-lockfile --cwd {shlex.quote(str(package))}"
        ),
        launch_command=(
            "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 "
            f"uv --directory {shlex.quote(str(root))} run opensquilla chat --ui tui"
        ),
    )
