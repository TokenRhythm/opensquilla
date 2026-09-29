from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "first_module",
    [
        "opensquilla.tools.policy_runtime",
        "opensquilla.tools.registry",
        "opensquilla.mcp.desktop_browser",
    ],
)
def test_managed_browser_import_order_preserves_builtin_registration(first_module: str) -> None:
    # A fresh interpreter is essential: pytest's existing imports mask cycles
    # between policy visibility, MCP discovery, and built-in registration.
    environment = dict(os.environ)
    environment.pop("OPENSQUILLA_DESKTOP_BROWSER_URL", None)
    environment.pop("OPENSQUILLA_DESKTOP_BROWSER_TOKEN", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys\n"
            "importlib.import_module(sys.argv[1])\n"
            "from opensquilla.tools.registry import get_default_registry\n"
            "registry = get_default_registry()\n"
            "assert registry.get('browser') is not None\n"
            "assert registry.get('read_file') is not None\n",
            first_module,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "builtin_tool.import_failed" not in result.stdout + result.stderr
