"""Small frozen entry that exercises the production Desktop MCP probe and bridge."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import mcp

from opensquilla.mcp_server import OpenSquillaMCPBridge, create_mcp_server

if __name__ == "__main__":
    if not getattr(sys, "frozen", False):
        raise SystemExit("This probe must run from a PyInstaller bundle")
    bundle = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    if not Path(mcp.__file__).resolve().is_relative_to(bundle.resolve()):
        raise SystemExit("The MCP SDK was not loaded from the bundle")

    if len(sys.argv) == 5 and sys.argv[1:4] == ["mcp-server", "run", "--gateway"]:
        bridge = OpenSquillaMCPBridge(gateway_url=sys.argv[4])
        create_mcp_server(bridge).run(transport="stdio")
    elif len(sys.argv) == 3 and sys.argv[1] == "--_desktop-mcp-probe":
        spec = importlib.util.spec_from_file_location(
            "desktop_gateway_entry", bundle / "gateway-entry.py"
        )
        assert spec is not None and spec.loader is not None
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        raise SystemExit(entry._run_desktop_mcp_probe(sys.argv[2]))
    else:
        raise SystemExit("Expected the Desktop MCP probe or its stdio server command")
