"""Candidate identity must fail closed before a Windows installer is run."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "windows_candidate_identity", ROOT / ".github/scripts/windows_candidate_identity.py",
)
assert SPEC and SPEC.loader
IDENTITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IDENTITY)


@pytest.fixture
def candidate(tmp_path):
    installer = tmp_path / "OpenSquilla-0.5.4-win-x64.exe"
    installer.write_bytes(b"synthetic installer")
    root = tmp_path / "app"
    for relative in (
        "OpenSquilla.exe", "resources/app.asar",
        "resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe",
        "resources/runtime/gateway/dependency-inventory.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    manifest = {
        "sourceSha": "a" * 40, "installerName": installer.name,
        "installerSha256": IDENTITY.digest(installer),
        **{field: IDENTITY.digest(path) for field, path in IDENTITY.installed_files(root).items()},
    }
    return installer, root, manifest


def test_candidate_verifies_exact_installed_bytes(candidate):
    installer, root, manifest = candidate
    IDENTITY.verify(manifest, installer, "a" * 40, manifest["installerSha256"], root)


@pytest.mark.parametrize("field", [
    "sourceSha", "installerName", "installerSha256", "executableSha256", "asarSha256",
    "gatewaySha256", "dependencyInventorySha256",
])
def test_candidate_rejects_missing_or_mismatched_identity(candidate, field):
    installer, root, manifest = candidate
    for value in (None, "b" * 64):
        changed = {**manifest, field: value}
        with pytest.raises(ValueError):
            IDENTITY.verify(changed, installer, "a" * 40, root=root)


def test_candidate_rejects_tampered_artifact_or_expected_digest(candidate):
    installer, root, manifest = candidate
    with pytest.raises(ValueError, match="hash mismatch"):
        IDENTITY.verify(manifest, installer, "a" * 40, "0" * 64, root)
    installer.write_bytes(b"different installer")
    with pytest.raises(ValueError, match="hash mismatch"):
        IDENTITY.verify(manifest, installer, "a" * 40, root=root)


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_startup_recovery_and_migration_are_independent_required_candidate_probes():
    job = workflow("windows-candidate-probes.yml")["jobs"]["candidate-probe"]
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["probe"]) == {
        "startup-compat", "ownership", "migration", "session",
    }
    assert "needs" not in job
    assert not job.get("continue-on-error")
    unsigned = workflow("windows-nsis-upgrade-regression.yml")["jobs"]
    assert unsigned["candidate-probes"]["needs"] == "build"
    assert "candidate-probes" in unsigned["acceptance-result"]["needs"]
    signed = workflow("wheelhouse-release.yml")["jobs"]
    dependencies = signed["internal-windows-candidate-probes"]["needs"]
    assert "audit-internal-windows-artifact" not in dependencies
    assert "always()" in signed["internal-windows-candidate-probes"]["if"]
    assert "candidate_artifact_id" in signed["internal-windows-candidate-probes"]["if"]
    assert "internal-windows-candidate-probes" in signed["internal-windows-acceptance"]["needs"]


def test_candidate_probe_inputs_invalidate_ci_attestations():
    policy = json.loads((ROOT / ".github/ci/trust-policy.v1.json").read_text())
    suites = json.loads((ROOT / ".github/ci/suites.v1.json").read_text())
    inputs = suites["suites"]["windows-nsis-regression"]["execution_inputs"]
    for path in (
        ".github/workflows/windows-candidate-probes.yml",
        ".github/scripts/windows_candidate_identity.py",
        ".github/scripts/verify-windows-candidate-probe.ps1",
        ".github/scripts/verify-packaged-startup-compatibility.py",
        ".github/scripts/verify-packaged-ownership-long-paths.py",
        ".github/scripts/verify-packaged-v054-upgrade.py",
    ):
        assert path in inputs
        assert path in policy["merge_critical_inputs"]


def test_reused_probes_pin_the_same_installer_as_upgrade_and_first_send():
    caller = workflow("desktop-fault-injection.yml")["jobs"]["windows-candidate-probes"]
    assert caller["with"]["installer_sha256"] == "${{ inputs.windows_expected_installer_sha256 }}"
    steps = workflow("windows-candidate-probes.yml")["jobs"]["candidate-probe"]["steps"]
    step = next(
        item for item in steps
        if item.get("name") == "Verify independent installed candidate probe"
    )
    assert step["env"]["EXPECTED_INSTALLER_HASH"] == "${{ inputs.installer_sha256 }}"
    assert "-InstallerSha256 $env:EXPECTED_INSTALLER_HASH" in step["run"]
    controller = (ROOT / ".github/scripts/verify-windows-candidate-probe.ps1").read_text()
    assert "'--installer-sha256', $InstallerSha256" in controller
    assert "@identityArgs --root $installRoot" in controller
