from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
DRIVER = ROOT / "desktop/electron/scripts/test-packaged-real-update-flow.mjs"


def test_downloaded_release_audits_cover_both_official_baselines() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    for platform in ("macos", "windows"):
        audit = jobs[f"audit-downloaded-{platform}-release"]
        assert audit["strategy"]["matrix"]["baseline-version"] == ["0.5.3", "0.5.4"]
        assert audit["strategy"]["fail-fast"] is False
        assert audit["needs"] == "prestage-draft-updater-assets"
        updater_steps = [
            step for step in audit["steps"] if "Verify official baseline" in step["name"]
        ]
        preview_steps = [step for step in audit["steps"] if "Verify preview" in step["name"]]
        assert len(updater_steps) == len(preview_steps) == 1
        assert "prerelease == 'false'" in updater_steps[0]["if"]
        assert "prerelease == 'true'" in preview_steps[0]["if"]
        for step in (*updater_steps, *preview_steps):
            assert step["env"]["BASELINE_VERSION"] == "${{ matrix.baseline-version }}"
            assert "BASELINE_VERSION" in step["run"]
        if platform == "windows":
            assert audit["strategy"]["matrix"]["install-mode"] == ["default", "custom"]
            assert audit["runs-on"] == "windows-2022"
        # Build-time compatibility keeps the implicit v0.5.3 baseline.
        build = jobs[f"build-desktop-{platform}"]
        preservation = [step for step in build["steps"] if "v0.5.3-to-candidate" in step["name"]]
        assert len(preservation) == 1
        assert "baseline" not in preservation[0]["run"].lower()
        assert "0.5.4" not in preservation[0]["run"]


@pytest.mark.skipif(os.name == "nt", reason="Git executable mode is checked on POSIX hosts")
def test_macos_workflow_helpers_are_executable() -> None:
    for script in ("verify-release-macos-upgrade.sh", "verify-release-macos-real-update.sh"):
        assert os.access(SCRIPTS / script, os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="macOS Bash helper validation runs on POSIX hosts")
@pytest.mark.parametrize(
    "script", ["verify-release-macos-upgrade.sh", "verify-release-macos-real-update.sh"]
)
@pytest.mark.parametrize("baseline", ["", "0.5.2", "0.5.4rc1", "../0.5.4", "0.5.4;false"])
def test_macos_helpers_reject_unsupported_baseline_before_side_effects(
    tmp_path: Path, script: str, baseline: str
) -> None:
    sandbox = tmp_path / "runner"
    result = subprocess.run(
        ["bash", str(SCRIPTS / script), "missing-candidate", "synthetic", baseline],
        env={**os.environ, "RUNNER_TEMP": str(sandbox)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2
    assert "baseline version must be 0.5.3 or 0.5.4" in result.stderr
    assert not sandbox.exists()


@pytest.mark.skipif(os.name == "nt", reason="macOS Bash helper validation runs on POSIX hosts")
@pytest.mark.parametrize("baseline", [None, "0.5.3", "0.5.4"])
def test_macos_download_selects_exact_official_baseline(
    tmp_path: Path, baseline: str | None
) -> None:
    selected = baseline or "0.5.3"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured = tmp_path / "download-arguments"
    gh = fake_bin / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURED_ARGS"\nexit 41\n', encoding="utf-8")
    gh.chmod(0o755)
    candidate = tmp_path / "candidate.dmg"
    candidate.touch()
    arguments = [
        "bash",
        str(SCRIPTS / "verify-release-macos-upgrade.sh"),
        str(candidate),
        "synthetic",
    ]
    if baseline is not None:
        arguments.append(baseline)
    result = subprocess.run(
        arguments,
        env={
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "CAPTURED_ARGS": str(captured),
            "RUNNER_TEMP": str(tmp_path / "runner"),
            "GITHUB_WORKSPACE": str(ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 41
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert arguments[:3] == ["release", "download", f"v{selected}"]
    assert arguments[arguments.index("--repo") + 1] == "TokenRhythm/opensquilla"
    assert arguments[arguments.index("--pattern") + 1] == f"OpenSquilla-{selected}-mac-arm64.dmg"
    assert Path(arguments[arguments.index("--dir") + 1]).parts[-2:] == (
        f"opensquilla-release-preservation-synthetic-{selected}",
        f"v{selected}",
    )


@pytest.mark.skipif(os.name == "nt", reason="macOS Bash helper validation runs on POSIX hosts")
@pytest.mark.parametrize("candidate", ["0.5.3", "0.5.4", "0.5.5rc1"])
def test_macos_real_updater_requires_newer_stable_than_selected_baseline(
    tmp_path: Path, candidate: str
) -> None:
    manifest = tmp_path / "channel.json"
    manifest.write_text(
        json.dumps({"version": candidate, "tag": f"v{candidate}", "prerelease": False}),
        encoding="utf-8",
    )
    sandbox = tmp_path / "runner"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "verify-release-macos-real-update.sh"),
            str(manifest),
            "synthetic",
            "0.5.4",
        ],
        env={**os.environ, "RUNNER_TEMP": str(sandbox), "GITHUB_WORKSPACE": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert not sandbox.exists()


@pytest.fixture
def rehearsal_driver(tmp_path: Path) -> tuple[str, Path]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the packaged updater driver contract")
    # Run the actual driver with an in-memory desktop bridge. The loopback
    # manifest is real; no application, release download, or installer is run.
    driver = tmp_path / "driver.mjs"
    shutil.copyfile(DRIVER, driver)
    (tmp_path / "packaged-smoke-helpers.mjs").write_text(
        """
export function requiredOption(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) throw new Error(`Missing ${name}`)
  return process.argv[index + 1]
}
export async function waitFor(check) {
  if (!await check()) throw new Error('bridge unavailable')
}
export async function launchPackagedCandidate({ env }) {
  console.log('SYNTHETIC_DESKTOP_LAUNCHED')
  let checks = 0
  const version = process.env.SYNTHETIC_BASELINE_VERSION
  return {
    firstWindow: async () => ({ evaluate: async (callback) => {
      const body = callback.toString()
      if (body.includes('typeof window')) return true
      if (body.includes('getUpdateState')) return { currentVersion: version }
      if (body.includes('checkForUpdates')) {
        const root = env.OPENSQUILLA_DESKTOP_UPDATE_CHANNEL_ROOT
        const response = await fetch(`${root}/channels/stable.json`)
        if (checks++ === 0) {
          if (response.status !== 503) throw new Error('missing pre-handoff failure')
          return { status: 'error', errorCode: 'source_unreachable' }
        }
        const manifest = await response.json()
        return {
          status: 'available', latestVersion: manifest.version,
          source: 'oss', installMode: 'native',
        }
      }
      if (body.includes('downloadUpdate')) throw new Error(`DOWNLOAD_REACHED:${version}`)
      throw new Error(`unexpected desktop call: ${body}`)
    } }),
    process: () => ({ killed: false }),
    close: async () => {},
  }
}
""",
        encoding="utf-8",
    )
    return node, driver


def _run_rehearsal_driver(
    rehearsal_driver: tuple[str, Path],
    *,
    baseline: str | None,
    installed: str,
    candidate: str = "0.5.5",
) -> subprocess.CompletedProcess[str]:
    node, driver = rehearsal_driver
    manifest = driver.parent / "channel.json"
    manifest.write_text(
        json.dumps(
            {"schemaVersion": 1, "version": candidate, "tag": f"v{candidate}", "prerelease": False}
        ),
        encoding="utf-8",
    )
    arguments = [
        node,
        str(driver),
        "--executable",
        str(driver.parent / "synthetic-app"),
        "--user-data-dir",
        str(driver.parent / "user-data"),
        "--channel-manifest",
        str(manifest),
        "--expected-version",
        candidate,
        "--mode",
        "native",
    ]
    if baseline is not None:
        arguments.extend(["--baseline-version", baseline])
    return subprocess.run(
        arguments,
        env={**os.environ, "SYNTHETIC_BASELINE_VERSION": installed},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


@pytest.mark.parametrize("baseline", [None, "0.5.3", "0.5.4"])
def test_rehearsal_driver_accepts_selected_baseline(
    rehearsal_driver: tuple[str, Path], baseline: str | None
) -> None:
    selected = baseline or "0.5.3"
    result = _run_rehearsal_driver(rehearsal_driver, baseline=baseline, installed=selected)
    assert result.returncode != 0  # The stub deliberately stops before downloading.
    assert f"DOWNLOAD_REACHED:{selected}" in result.stderr


def test_rehearsal_driver_rejects_mislabeled_official_baseline(
    rehearsal_driver: tuple[str, Path],
) -> None:
    result = _run_rehearsal_driver(rehearsal_driver, baseline="0.5.4", installed="0.5.3")
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert "DOWNLOAD_REACHED" not in result.stderr


@pytest.mark.parametrize(
    ("baseline", "candidate", "message"),
    [
        ("0.5.2", "0.5.5", "--baseline-version must be"),
        ("0.5.4", "0.5.4", "candidate must be newer"),
        ("0.5.4", "0.5.3", "candidate must be newer"),
        ("0.5.4", "0.5.5rc1", "must be a canonical stable version"),
    ],
)
def test_rehearsal_driver_rejects_invalid_versions_before_launch(
    rehearsal_driver: tuple[str, Path], baseline: str, candidate: str, message: str
) -> None:
    result = _run_rehearsal_driver(
        rehearsal_driver, baseline=baseline, installed=baseline, candidate=candidate
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert "SYNTHETIC_DESKTOP_LAUNCHED" not in result.stdout
