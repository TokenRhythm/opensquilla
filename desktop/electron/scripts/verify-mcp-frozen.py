"""Freeze the production MCP probe/bridge and run it against a real source Gateway.

This verifies the MCP dependency closure without building the full Desktop or
its router models. Release packaging still runs the complete Gateway smoke.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MCP_METADATA = ("httpx2", "httpcore2")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="opensquilla-mcp-frozen-"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    script_dir = root / "desktop/electron/scripts"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "opensquilla-mcp-probe",
        "--distpath", str(output / "dist"),
        "--workpath", str(output / "build"),
        "--specpath", str(output),
        "--paths", str(root / "src"),
        "--hidden-import", "mcp",
        "--add-data", f"{script_dir / 'gateway-entry.py'}{os.pathsep}.",
    ]
    for distribution in MCP_METADATA:
        command.extend(("--copy-metadata", distribution))
    command.append(str(script_dir / "fixtures/mcp-frozen/entry.py"))
    build_log = output / "build.log"
    with build_log.open("w", encoding="utf-8") as log:
        build = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    if build.returncode:
        print(build_log.read_text(encoding="utf-8", errors="replace")[-16_384:])
        return build.returncode

    executable = "opensquilla-mcp-probe.exe" if os.name == "nt" else "opensquilla-mcp-probe"
    binary = output / "dist/opensquilla-mcp-probe" / executable
    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "OPENSQUILLA_TEST_FROZEN_MCP_PROBE": str(binary),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_desktop/test_gateway_functional_probes.py::"
         "test_mcp_probe_uses_real_stdio_server_and_gateway", "-q"],
        cwd=root, env=environment,
    )
    print(f"MCP-only frozen probe artifacts: {output}", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
