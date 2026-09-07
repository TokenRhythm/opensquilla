"""Exercise the production verifier in PowerShell with mocked OS trust boundaries.

No certificates or signing credentials are used. The fixture exposes placeholder
SignTool applications and functions at the same paths, so SDK/PATH discovery and
target selection run unchanged while tool results and Authenticode data are controlled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/verify-windows-signatures.ps1"
POLICY = ROOT / ".github/signing/windows-signing-policy.json"
POWERSHELLS = list(
    dict.fromkeys(path for name in ("pwsh", "powershell") if (path := shutil.which(name)))
)

WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$global:scenario = Get-Content -LiteralPath $env:SCENARIO_PATH -Raw | ConvertFrom-Json
# PowerShell may replace ProgramFiles during startup; isolate SDK discovery
# after startup so the real machine's Windows SDK cannot affect the result.
$env:ProgramFiles = $global:scenario.programFiles
${env:ProgramFiles(x86)} = $global:scenario.programFilesX86
foreach ($tool in $global:scenario.tools) {
    Set-Item -Path "Function:$tool" -Value {
        $record = @{ kind = 'tool'; path = $MyInvocation.MyCommand.Name; arguments = @($args) }
        [IO.File]::AppendAllText($env:CALLS_PATH, (($record | ConvertTo-Json -Compress) + "`n"))
        $global:LASTEXITCODE = [int]$global:scenario.exitCode
    }
}
function Get-AuthenticodeSignature {
    param([string]$LiteralPath)
    $record = @{ kind = 'authenticode'; path = $LiteralPath }
    [IO.File]::AppendAllText($env:CALLS_PATH, (($record | ConvertTo-Json -Compress) + "`n"))
    $timestamp = $null
    if ($global:scenario.timestamp) {
        $timestamp = [pscustomobject]@{ Subject = 'Test timestamp authority' }
    }
    return [pscustomobject]@{
        Status = $global:scenario.status
        StatusMessage = 'Controlled test result'
        SignerCertificate = [pscustomobject]@{
            Thumbprint = $global:scenario.thumbprint
            Subject = $global:scenario.publisher
        }
        TimeStamperCertificate = $timestamp
    }
}
$arguments = @{}
foreach ($property in $global:scenario.arguments.PSObject.Properties) {
    $arguments[$property.Name] = $property.Value
}
& $env:VERIFIER_PATH @arguments
"""


@pytest.fixture(
    params=POWERSHELLS or [None], ids=lambda path: Path(path).stem if path else "no-pwsh"
)
def powershell(request: pytest.FixtureRequest) -> str:
    if request.param is None:
        pytest.skip("PowerShell is required to execute Windows signature contract tests")
    return str(request.param)


class VerifierFixture:
    def __init__(self, root: Path, powershell: str) -> None:
        self.root = root
        self.powershell = powershell
        self.script = root / "repository with spaces/.github/scripts/verify-windows-signatures.ps1"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.script)
        policy_path = self.script.parent.parent / "signing/windows-signing-policy.json"
        policy_path.parent.mkdir()
        shutil.copyfile(POLICY, policy_path)
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        self.path_bin = root / "path tools"
        self.path_bin.mkdir()
        self.program_files = root / "Program Files"
        self.program_files_x86 = root / "Program Files (x86)"
        self.environment = {
            **os.environ,
            "PATH": str(self.path_bin),
            "SIGNTOOL_PATH": "",
            "ProgramFiles": str(self.program_files),
            "ProgramFiles(x86)": str(self.program_files_x86),
            "SCENARIO_PATH": str(root / "scenario.json"),
            "CALLS_PATH": str(root / "calls.jsonl"),
            "VERIFIER_PATH": str(self.script),
        }
        self.installer = self.file("candidate with spaces.exe")
        self.scenario: dict = {
            "tools": [],
            "exitCode": 0,
            "status": "Valid",
            "thumbprint": policy["certificateSha1"],
            "publisher": f"CN={policy['publisherSubjectContains']}, O=Test",
            "timestamp": True,
            "programFiles": str(self.program_files),
            "programFilesX86": str(self.program_files_x86),
            "arguments": {"InstallerPath": str(self.installer)},
        }

    def file(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def tool(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o755)
        self.scenario["tools"].append(str(path))
        return path

    def sdk_tool(self, version: str, *, native: bool = False, arch: str = "x64") -> Path:
        base = self.program_files if native else self.program_files_x86
        return self.tool(base / f"Windows Kits/10/bin/{version}/{arch}/signtool.exe")

    def run(self) -> subprocess.CompletedProcess[str]:
        Path(self.environment["SCENARIO_PATH"]).write_text(
            json.dumps(self.scenario), encoding="utf-8"
        )
        wrapper = self.root / "wrapper.ps1"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        return subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def calls(self, kind: str) -> list[dict]:
        path = Path(self.environment["CALLS_PATH"])
        if not path.exists():
            return []
        return [
            record
            for line in path.read_text().splitlines()
            if (record := json.loads(line))["kind"] == kind
        ]


@pytest.fixture
def verifier(tmp_path: Path, powershell: str) -> VerifierFixture:
    return VerifierFixture(tmp_path, powershell)


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def test_verifier_parses_in_powershell(powershell: str) -> None:
    parser = r"""
$parseErrors = $null
$tokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $env:SCRIPT_PATH, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { $parseErrors | Out-String | Write-Error; exit 1 }
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", parser],
        env={**os.environ, "SCRIPT_PATH": str(SCRIPT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert_success(result)


@pytest.mark.parametrize("source", ["parameter", "environment", "path", "sdk", "native-sdk"])
def test_signtool_discovery_and_precedence(verifier: VerifierFixture, source: str) -> None:
    # The newest directory may not contain an x64 verifier; compare SDK versions
    # numerically (10.0.10000.0 is newer than 10.0.9999.0).
    selected = verifier.sdk_tool("10.0.10000.0", native=source == "native-sdk")
    verifier.sdk_tool("10.0.9999.0")
    verifier.sdk_tool("10.0.99999.0", arch="x86")
    verifier.sdk_tool("not-a-version")
    if source in ("path", "environment", "parameter"):
        selected = verifier.tool(verifier.path_bin / "signtool.exe")
    if source in ("environment", "parameter"):
        selected = verifier.tool(verifier.root / "environment tool/signtool.exe")
        verifier.environment["SIGNTOOL_PATH"] = str(selected)
    if source == "parameter":
        selected = verifier.tool(verifier.root / "explicit tool/signtool.exe")
        verifier.scenario["arguments"]["SignToolPath"] = str(selected)

    assert_success(verifier.run())
    calls = verifier.calls("tool")
    assert len(calls) == 1
    assert Path(calls[0]["path"]) == selected
    assert calls[0]["arguments"] == ["verify", "/pa", "/all", "/v", "/tw", str(verifier.installer)]
    assert [entry["path"] for entry in verifier.calls("authenticode")] == [str(verifier.installer)]


@pytest.mark.parametrize("source", ["parameter", "environment"])
@pytest.mark.parametrize("invalid", ["missing", "directory", "whitespace"])
def test_explicit_invalid_signtool_fails_without_fallback(
    verifier: VerifierFixture, source: str, invalid: str
) -> None:
    verifier.tool(verifier.path_bin / "signtool.exe")
    verifier.sdk_tool("10.0.10000.0")
    path = {
        "missing": str(verifier.root / "missing.exe"),
        "directory": str(verifier.path_bin),
        "whitespace": " ",
    }[invalid]
    if source == "parameter":
        verifier.scenario["arguments"]["SignToolPath"] = path
        verifier.environment["SIGNTOOL_PATH"] = str(verifier.path_bin / "signtool.exe")
    else:
        verifier.environment["SIGNTOOL_PATH"] = path
    result = verifier.run()
    assert result.returncode != 0
    assert "does not point to a SignTool file" in result.stdout + result.stderr
    assert not verifier.calls("tool")


def test_missing_signtool_reports_how_to_install_or_select_it(verifier: VerifierFixture) -> None:
    verifier.sdk_tool("10.0.10000.0", arch="x86")
    result = verifier.run()
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "signtool.exe was not found on PATH or in a Windows 10 SDK x64 directory" in output
    assert "SIGNTOOL_PATH" in output
    assert not verifier.calls("authenticode")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("exitCode", 9, "SignTool verification failed"),
        ("status", "NotSigned", "Authenticode status is NotSigned"),
        ("status", "HashMismatch", "Authenticode status is HashMismatch"),
        ("thumbprint", "0" * 40, "certificate thumbprint is unexpected"),
        ("publisher", "CN=Unrelated publisher", "publisher is unexpected"),
        ("timestamp", False, "does not have an Authenticode timestamp certificate"),
    ],
)
def test_untrusted_signature_is_rejected(
    verifier: VerifierFixture, field: str, value: object, error: str
) -> None:
    verifier.tool(verifier.path_bin / "signtool.exe")
    verifier.scenario[field] = value
    result = verifier.run()
    assert result.returncode != 0
    assert error in result.stdout + result.stderr
    assert len(verifier.calls("tool")) == 1
    assert len(verifier.calls("authenticode")) == (0 if field == "exitCode" else 1)


ARTIFACT_TARGETS = [
    "OpenSquilla-0.5.5-win-x64.exe",
    "win-unpacked/OpenSquilla.exe",
    "win-unpacked/resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe",
    "win-unpacked/resources/elevate.exe",
]
INSTALLED_TARGETS = [
    "OpenSquilla.exe",
    "resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe",
    "resources/elevate.exe",
    "Uninstall OpenSquilla.exe",
]


@pytest.mark.parametrize("mode", ["artifact", "installed"])
@pytest.mark.parametrize("missing_index", [None, 0, 1, 2, 3])
def test_every_required_executable_is_verified(
    verifier: VerifierFixture, mode: str, missing_index: int | None
) -> None:
    verifier.tool(verifier.path_bin / "signtool.exe")
    targets = ARTIFACT_TARGETS if mode == "artifact" else INSTALLED_TARGETS
    paths = [verifier.file(f"targets/{target}") for target in targets]
    if missing_index is not None:
        paths[missing_index].unlink()
    verifier.scenario["arguments"] = {
        "ArtifactRoot" if mode == "artifact" else "InstalledRoot": str(verifier.root / "targets")
    }
    result = verifier.run()
    if missing_index is not None:
        assert result.returncode != 0
        assert (
            "is missing" in result.stdout + result.stderr
            or "got 0" in result.stdout + result.stderr
        )
    else:
        assert_success(result)
        assert [Path(call["arguments"][-1]) for call in verifier.calls("tool")] == paths
        assert [Path(call["path"]) for call in verifier.calls("authenticode")] == paths


@pytest.mark.parametrize("mode", ["artifact", "installed"])
def test_ambiguous_installer_or_uninstaller_is_rejected(
    verifier: VerifierFixture, mode: str
) -> None:
    verifier.tool(verifier.path_bin / "signtool.exe")
    targets = ARTIFACT_TARGETS if mode == "artifact" else INSTALLED_TARGETS
    for target in targets:
        verifier.file(f"targets/{target}")
    extra = "OpenSquilla-0.5.6-win-x64.exe" if mode == "artifact" else "Uninstall Other.exe"
    verifier.file(f"targets/{extra}")
    verifier.scenario["arguments"] = {
        "ArtifactRoot" if mode == "artifact" else "InstalledRoot": str(verifier.root / "targets")
    }
    result = verifier.run()
    assert result.returncode != 0
    assert "got 2" in result.stdout + result.stderr
    assert not verifier.calls("tool")
