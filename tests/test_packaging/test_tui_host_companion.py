"""Packaging contracts for the platform-specific OpenTUI companion."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_PYPROJECT = REPO_ROOT / "pyproject.toml"
COMPANION = REPO_ROOT / "packages" / "opensquilla-tui-host"
BUILDER = REPO_ROOT / "scripts" / "build_tui_host_companion.py"


def _version(path: Path) -> str:
    return str(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"])


def test_companion_version_and_bun_are_exactly_pinned() -> None:
    assert _version(COMPANION / "pyproject.toml") == _version(CORE_PYPROJECT)
    package = json.loads(
        (REPO_ROOT / "src/opensquilla/cli/tui/opentui/package/package.json").read_text()
    )
    pinned = (
        (REPO_ROOT / "src/opensquilla/cli/tui/opentui/package/.bun-version").read_text().strip()
    )
    assert pinned == "1.3.14"
    assert package["engines"]["bun"] == pinned
    source = BUILDER.read_text(encoding="utf-8")
    assert 'PINNED_BUN_VERSION = "1.3.14"' in source
    assert '"install", "--frozen-lockfile"' in source
    assert '"--options",' in source
    assert '"runtime",' in source
    assert 'MACOS_SIGNING_IDENTIFIER = "ai.opensquilla.tui-host"' in source
    assert '("linux", "x64"): "bun-linux-x64-baseline"' in source
    assert '("win32", "x64"): "bun-windows-x64-baseline"' in source
    assert 'build_env["OPENTUI_LIBC"] = "glibc"' in source
    assert 'build_flags.append("--env=OPENTUI_LIBC*")' in source
    entitlements = (COMPANION / "macos-entitlements.plist").read_text(encoding="utf-8")
    assert "com.apple.security.cs.allow-jit" in entitlements
    assert "com.apple.security.cs.disable-library-validation" in entitlements


def test_bun_native_tests_run_in_isolated_processes() -> None:
    package = json.loads(
        (REPO_ROOT / "src/opensquilla/cli/tui/opentui/package/package.json").read_text()
    )
    runner = (
        REPO_ROOT
        / "src/opensquilla/cli/tui/opentui/package/scripts/run-bun-tests.mjs"
    ).read_text()

    assert package["scripts"]["test:bun"] == "node scripts/run-bun-tests.mjs"
    assert 'entry.name.endsWith(".bun.test.mjs")' in runner
    assert "for (const testFile of testFiles)" in runner
    assert '"--max-concurrency=1"' in runner
    assert 'OPENSQUILLA_TUI_COLOR: "truecolor"' in runner
    assert "spawnSync(" in runner


def test_core_wheel_excludes_generated_tui_host_directories() -> None:
    data = tomllib.loads(CORE_PYPROJECT.read_text(encoding="utf-8"))
    excluded = set(data["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"])
    package_root = "src/opensquilla/cli/tui/opentui/package"
    assert {
        f"{package_root}/node_modules/**",
        f"{package_root}/bin/**",
        f"{package_root}/build/**",
        f"{package_root}/dist/**",
    } <= excluded


def test_prebuilt_companion_requires_pinned_bun_provenance(tmp_path: Path) -> None:
    binary = tmp_path / "fake-host"
    binary.write_bytes(b"host")
    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "darwin",
            "--arch",
            "arm64",
            "--binary",
            str(binary),
            "--bun-version",
            "1.3.13",
            "--output-dir",
            str(tmp_path / "dist"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires --bun-version 1.3.14" in result.stderr


@pytest.mark.parametrize("identity_args", [[], ["--codesign-identity", "-"]])
def test_release_companion_requires_native_codesign_identity(
    tmp_path: Path,
    identity_args: list[str],
) -> None:
    binary = tmp_path / "fake-host"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "darwin",
            "--arch",
            "arm64",
            "--binary",
            str(binary),
            "--bun-version",
            "1.3.14",
            "--require-codesign-identity",
            *identity_args,
            "--output-dir",
            str(tmp_path / "dist"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "codesign identity" in result.stderr or "signed on a macOS runner" in result.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_prebuilt_companion_wheel_has_platform_tag_and_public_api(tmp_path: Path) -> None:
    binary = tmp_path / "fake-host"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    command = [
        sys.executable,
        str(BUILDER),
        "--platform",
        "darwin",
        "--arch",
        "arm64",
        "--binary",
        str(binary),
        "--bun-version",
        "1.3.14",
        "--build-id",
        "test-build",
    ]
    out = tmp_path / "dist-a"
    result = subprocess.run(
        [*command, "--output-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    wheels = list(out.glob("opensquilla_tui_host-*-py3-none-macosx_13_0_arm64.whl"))
    assert len(wheels) == 1, list(out.iterdir())
    second_out = tmp_path / "dist-b"
    second = subprocess.run(
        [*command, "--output-dir", str(second_out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert second.returncode == 0, second.stderr
    second_wheel = next(second_out.glob("*.whl"))
    assert (
        hashlib.sha256(wheels[0].read_bytes()).digest()
        == hashlib.sha256(second_wheel.read_bytes()).digest()
    )

    install_dir = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_metadata = next(
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        )
        wheel_text = archive.read(wheel_metadata).decode()
        executable_member = next(
            name
            for name in archive.namelist()
            if name.endswith("opensquilla_tui_host/bin/opensquilla-tui-host")
        )
        executable_mode = archive.getinfo(executable_member).external_attr >> 16
        archive.extractall(install_dir)
    assert "Root-Is-Purelib: false" in wheel_text
    assert "Tag: py3-none-macosx_13_0_arm64" in wheel_text

    extracted_executable = install_dir / executable_member
    if sys.platform != "win32":
        assert executable_mode & stat.S_IXUSR
        extracted_executable.chmod(executable_mode)
    sys.path.insert(0, str(install_dir))
    try:
        module = importlib.import_module("opensquilla_tui_host")
        metadata = module.host_metadata()
        command = module.host_command()
        assert metadata.product_version == _version(CORE_PYPROJECT)
        assert metadata.host_version == metadata.product_version
        assert metadata.protocol_version == 1
        assert metadata.platform == "darwin"
        assert metadata.arch == "arm64"
        assert metadata.build_id == "test-build"
        assert metadata.bun_version == "1.3.14"
        assert command == (str(install_dir / "opensquilla_tui_host/bin/opensquilla-tui-host"),)
        assert Path(command[0]).read_bytes() == binary.read_bytes()
        assert metadata.sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()
    finally:
        sys.path.remove(str(install_dir))
        sys.modules.pop("opensquilla_tui_host.api", None)
        sys.modules.pop("opensquilla_tui_host", None)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
@pytest.mark.parametrize(
    ("target_platform", "target_arch", "wheel_tag", "executable_name"),
    [
        ("linux", "x64", "manylinux_2_28_x86_64", "opensquilla-tui-host"),
        ("linux", "arm64", "manylinux_2_28_aarch64", "opensquilla-tui-host"),
        ("win32", "x64", "win_amd64", "opensquilla-tui-host.exe"),
        ("win32", "arm64", "win_arm64", "opensquilla-tui-host.exe"),
    ],
)
def test_staged_companion_targets_keep_linux_and_windows_artifacts_isolated(
    tmp_path: Path,
    target_platform: str,
    target_arch: str,
    wheel_tag: str,
    executable_name: str,
) -> None:
    """Linux release work must not consume or rename future Windows artifacts."""

    binary = tmp_path / executable_name
    binary.write_bytes(b"staged-host")
    output_dir = tmp_path / f"{target_platform}-{target_arch}"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            target_platform,
            "--arch",
            target_arch,
            "--binary",
            str(binary),
            "--bun-version",
            "1.3.14",
            "--build-id",
            "platform-contract",
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(output_dir.glob("*.whl"))
    assert wheel.name.endswith(f"-py3-none-{wheel_tag}.whl")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = json.loads(archive.read("opensquilla_tui_host/_host_metadata.json").decode())
        wheel_metadata = next(name for name in names if name.endswith(".dist-info/WHEEL"))
        wheel_text = archive.read(wheel_metadata).decode()

    assert metadata["platform"] == target_platform
    assert metadata["arch"] == target_arch
    assert metadata["executable"] == f"bin/{executable_name}"
    assert metadata["wheel_tag"] == f"py3-none-{wheel_tag}"
    expected_bun_target = {
        ("linux", "x64"): "bun-linux-x64-baseline",
        ("win32", "x64"): "bun-windows-x64-baseline",
        ("win32", "arm64"): "bun-windows-arm64",
    }.get((target_platform, target_arch), f"bun-{target_platform}-{target_arch}")
    assert metadata["bun_target"] == expected_bun_target
    if target_platform == "linux":
        assert metadata["libc"] == "glibc"
    else:
        assert "libc" not in metadata
    assert f"Tag: py3-none-{wheel_tag}" in wheel_text
    assert f"opensquilla_tui_host/bin/{executable_name}" in names


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_core_wheel_stays_universal_and_excludes_host_artifacts(
    isolated_core_wheel: Path,
) -> None:
    assert isolated_core_wheel.name.endswith("-py3-none-any.whl")

    with zipfile.ZipFile(isolated_core_wheel) as archive:
        names = archive.namelist()
        wheel_metadata = next(name for name in names if name.endswith(".dist-info/WHEEL"))
        wheel_text = archive.read(wheel_metadata).decode()
    assert "Root-Is-Purelib: true" in wheel_text
    assert "Tag: py3-none-any" in wheel_text
    assert "opensquilla/dist/workspace_state.py" in names
    forbidden_parts = {"node_modules", "bin", "build", "dist"}
    leaked = [
        name
        for name in names
        if name.startswith("opensquilla/cli/tui/opentui/package/")
        and forbidden_parts.intersection(Path(name).parts)
    ]
    assert leaked == []
    host_native_suffixes = {".exe", ".node", ".dylib", ".so"}
    assert not [
        name
        for name in names
        if name.startswith("opensquilla/cli/tui/opentui/package/")
        and Path(name).suffix.lower() in host_native_suffixes
    ]
    assert all(not name.startswith("opensquilla_tui_host/") for name in names)
