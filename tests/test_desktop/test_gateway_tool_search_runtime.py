"""Offline checks for Unicode search dependencies in the frozen Gateway."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "desktop/electron/scripts/gateway-entry.py"
PROBE = "--_desktop-tool-search-probe"
SUCCESS = "opensquilla-desktop-tool-search-ok"


def _run_probe(*, data_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(ENTRY), PROBE]
    if data_root is not None:
        # Keep the installed implementation but point its resource package at
        # an incomplete bundle, just as frozen analysis can omit lazy data.
        script = (
            "import anyascii, runpy, sys\n"
            "anyascii.__path__ = [sys.argv.pop(1)]\n"
            "entry = sys.argv.pop(1)\n"
            "sys.argv = [entry, *sys.argv[1:]]\n"
            "runpy.run_path(entry, run_name='__main__')\n"
        )
        command = [sys.executable, "-c", script, str(data_root), str(ENTRY), PROBE]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_gateway_tool_search_probe_exercises_unicode_data() -> None:
    result = _run_probe()

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SUCCESS
    assert result.stderr == ""


@pytest.mark.parametrize("data_package_exists", [False, True])
def test_gateway_tool_search_probe_rejects_incomplete_data(
    tmp_path: Path,
    data_package_exists: bool,
) -> None:
    if data_package_exists:
        data = tmp_path / "_data"
        data.mkdir()
        (data / "__init__.py").write_text("", encoding="utf-8")

    result = _run_probe(data_root=tmp_path)

    assert result.returncode == 1
    assert SUCCESS not in result.stdout
    assert result.stderr.strip() == (
        "OpenSquilla Desktop tool search could not load its Unicode resources."
    )
    assert str(tmp_path) not in result.stderr


def test_desktop_build_and_both_package_checks_include_unicode_probe() -> None:
    scripts = ROOT / "desktop/electron/scripts"
    build = (scripts / "build-gateway.mjs").read_text(encoding="utf-8")
    verifier = (scripts / "verify-package.mjs").read_text(encoding="utf-8")
    smoke = (scripts / "smoke-gateway.mjs").read_text(encoding="utf-8")

    assert "'--collect-all',\n  'anyascii'," in build
    assert f"verifyGatewayCommand(binary, label, ['{PROBE}'])" in verifier
    assert f"spawnSync(gatewayBinary, ['{PROBE}']" in smoke
    assert "verifyGatewayToolSearch(gatewayBinary, env)" in smoke
    assert SUCCESS in smoke
