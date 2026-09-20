"""Keep the lightweight MCP freeze aligned with production packaging metadata."""

from __future__ import annotations

import runpy
from pathlib import Path


def test_mcp_frozen_probe_uses_production_distribution_metadata() -> None:
    root = Path(__file__).resolve().parents[2]
    scripts = root / "desktop/electron/scripts"
    verifier = runpy.run_path(str(scripts / "verify-mcp-frozen.py"))
    build = (scripts / "build-gateway.mjs").read_text(encoding="utf-8")
    assert set(verifier["MCP_METADATA"]) == {"httpx2", "httpcore2"}
    for distribution in verifier["MCP_METADATA"]:
        assert f"'--copy-metadata',\n  '{distribution}'" in build
