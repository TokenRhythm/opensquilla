from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "desktop" / "electron" / "scripts"
_PROBE = _SCRIPTS / "probe-code-execution.py"


def test_code_execution_probe_runs_the_real_tool_through_the_gateway_entry(tmp_path: Path) -> None:
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    profile = tmp_path / "profile"
    profile.mkdir()
    temporary = tmp_path / "temp"
    temporary.mkdir()
    environment.update({
        "HOME": str(profile), "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary),
        "OPENSQUILLA_STATE_DIR": str(profile),
        "PYTHONPATH": str(_ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
    })

    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "gateway-entry.py"),
         "--internal-child", "python-code", _PROBE.read_text(encoding="utf-8")],
        cwd=tmp_path, env=environment, text=True, encoding="utf-8", capture_output=True,
        timeout=45, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "probe": "opensquilla-desktop-code-execution", "frozen": False,
        "pythonExit": 0, "errorExit": 7, "pages": 1, "title": "Packaged Python tool smoke",
        "documents": {"csvRows": 2, "xlsxValue": 42, "pdfText": "中文文件验收 样本 42"},
        "published": 4,
    }
    artifact = profile / "workspace" / "code-execution" / "python-tool-smoke.pptx"
    with ZipFile(artifact) as archive:
        slides = [name for name in archive.namelist()
                  if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        assert slides == ["ppt/slides/slide1.xml"]
        assert b"Packaged Python tool smoke" in archive.read(slides[0])


@pytest.mark.parametrize(
    ("payload", "expected_exit"),
    [
        ({"exit_code": 2, "timed_out": False, "stderr": "No such option: -c"}, 0),
        ({"exit_code": 0, "timed_out": True}, 0),
        ({"exit_code": 0, "timed_out": False}, 7),
        ({"status": "blocked", "reason": "runtime_unavailable"}, 0),
    ],
    ids=["tool-failed", "timed-out", "lost-system-exit", "tool-not-executed"],
)
def test_probe_rejects_unsuccessful_tool_results(
    payload: dict[str, object], expected_exit: int,
) -> None:
    probe = runpy.run_path(str(_PROBE))

    with pytest.raises(RuntimeError, match="execute_code expected exit"):
        probe["_execution_result"](json.dumps(payload), expected_exit)


@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
def test_safe_release_gate_executes_and_denies_real_operations(tmp_path: Path) -> None:
    environment = {key: value for key, value in os.environ.items() if key in {"PATH"}}
    profile = tmp_path / "isolated profile"
    profile.mkdir()
    environment.update({
        "HOME": str(profile), "OPENSQUILLA_STATE_DIR": str(profile),
        "PYTHONPATH": str(_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
    })
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "gateway-entry.py"),
         "--internal-child", "python-code",
         (_SCRIPTS / "probe-safe-execution.py").read_text(encoding="utf-8")],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "probe": "opensquilla-desktop-safe-execution", "frozen": False,
        "backend": "seatbelt", "read": True, "write": True,
        "readonlyChild": True, "writeDenied": True, "networkDenied": True,
    }


def test_packaged_smoke_requires_safe_success_and_explicit_windows_provisioning() -> None:
    smoke = (_SCRIPTS / "smoke-gateway.mjs").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    assert "await verifyGatewaySafeExecution(gatewayBinary, env, tempHome)" in smoke
    assert "--internal-child', 'python-code', code" in smoke
    assert "cwd: tempHome" in smoke
    assert "frozen: true, backend" in smoke
    assert "writeDenied: true, networkDenied: true" in smoke
    assert "process.argv.includes('--provision-windows-sandbox')" in smoke
    assert "npm run verify:gateway-smoke -- --provision-windows-sandbox" in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    for platform in ("macos", "windows"):
        step = next(
            step for step in jobs[f"build-desktop-{platform}"]["steps"]
            if step["name"] == "Smoke packaged gateway"
        )
        provisioning = step["env"].get("OPENSQUILLA_SMOKE_PROVISION_SANDBOX")
        assert provisioning == ("1" if platform == "windows" else None)
