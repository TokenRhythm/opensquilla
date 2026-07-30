"""Build one self-contained, platform-specific OpenSquilla TUI host wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as platform_module
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_SOURCE = REPO_ROOT / "src" / "opensquilla" / "cli" / "tui" / "opentui" / "package"
COMPANION_PROJECT = REPO_ROOT / "packages" / "opensquilla-tui-host"
PINNED_BUN_VERSION = "1.3.14"
HOST_PROTOCOL_VERSION = 1
MACOS_SIGNING_IDENTIFIER = "ai.opensquilla.tui-host"

_PLATFORMS = {"darwin", "linux", "win32"}
_ARCHES = {"arm64", "x64"}
_WHEEL_TAGS = {
    ("darwin", "arm64"): "py3-none-macosx_13_0_arm64",
    ("darwin", "x64"): "py3-none-macosx_13_0_x86_64",
    ("linux", "arm64"): "py3-none-manylinux_2_28_aarch64",
    ("linux", "x64"): "py3-none-manylinux_2_28_x86_64",
    ("win32", "arm64"): "py3-none-win_arm64",
    ("win32", "x64"): "py3-none-win_amd64",
}
_BUN_COMPILE_TARGETS = {
    # Platform metadata follows Node's ``process.platform`` (``win32``), while
    # Bun's compiler spells that target ``windows``. Keep that translation here
    # so the future Windows release does not need to change the host protocol.
    ("win32", "arm64"): "bun-windows-arm64",
    ("win32", "x64"): "bun-windows-x64-baseline",
    # Platform wheels must not silently require a modern AVX2-class CPU.
    ("darwin", "x64"): "bun-darwin-x64-baseline",
    ("linux", "x64"): "bun-linux-x64-baseline",
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target_platform = args.platform or _current_platform()
    target_arch = args.arch or _current_arch()
    if target_platform not in _PLATFORMS or target_arch not in _ARCHES:
        raise SystemExit(f"unsupported TUI host target: {target_platform}/{target_arch}")

    product_version = _project_version(REPO_ROOT / "pyproject.toml")
    companion_version = _project_version(COMPANION_PROJECT / "pyproject.toml")
    if product_version != companion_version:
        raise SystemExit(
            "opensquilla and opensquilla-tui-host versions must match exactly: "
            f"{product_version!r} != {companion_version!r}"
        )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="opensquilla-tui-host-") as temp:
        staging = Path(temp) / "project"
        shutil.copytree(COMPANION_PROJECT, staging)
        package_dir = staging / "src" / "opensquilla_tui_host"
        executable_name = (
            "opensquilla-tui-host.exe" if target_platform == "win32" else "opensquilla-tui-host"
        )
        executable = package_dir / "bin" / executable_name
        executable.parent.mkdir(parents=True)

        if args.binary is not None:
            if args.bun_version != PINNED_BUN_VERSION:
                raise SystemExit(
                    "--binary requires --bun-version "
                    f"{PINNED_BUN_VERSION} so release provenance stays reproducible"
                )
            shutil.copy2(args.binary.resolve(), executable)
            bun_version = args.bun_version
        else:
            _require_native_target(target_platform, target_arch)
            bun = _resolve_bun(args.bun)
            bun_version = _bun_version(bun)
            if bun_version != PINNED_BUN_VERSION:
                raise SystemExit(
                    f"Bun {PINNED_BUN_VERSION} is required, found {bun_version}; "
                    "install the pinned version or pass --binary"
                )
            _run([bun, "install", "--frozen-lockfile"], cwd=HOST_SOURCE)
            bun_target = _bun_compile_target(target_platform, target_arch)
            build_env = None
            build_flags: list[str] = []
            if target_platform == "linux":
                # Release Linux wheels are manylinux/glibc artifacts. OpenTUI
                # 0.4 also publishes musl packages and selects between them via
                # OPENTUI_LIBC at runtime; inline the release choice so a user
                # environment cannot steer a manylinux binary onto the wrong
                # native loader branch.
                build_env = os.environ.copy()
                build_env["OPENTUI_LIBC"] = "glibc"
                build_flags.append("--env=OPENTUI_LIBC*")
            _run(
                [
                    bun,
                    "build",
                    "--compile",
                    "--minify",
                    *build_flags,
                    f"--target={bun_target}",
                    str(HOST_SOURCE / "src" / "main.mjs"),
                    f"--outfile={executable}",
                ],
                cwd=HOST_SOURCE,
                env=build_env,
            )

        if not executable.is_file():
            raise SystemExit(f"TUI host build did not produce {executable}")
        if target_platform != "win32":
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if target_platform == "darwin":
            _sign_macos_executable(
                executable,
                identity=args.codesign_identity,
                require_identity=args.require_codesign_identity,
            )
        digest = _sha256(executable)
        metadata = {
            "schema_version": 1,
            "product_version": product_version,
            "host_version": product_version,
            "protocol_version": HOST_PROTOCOL_VERSION,
            "platform": target_platform,
            "arch": target_arch,
            "build_id": args.build_id or digest[:16],
            "wheel_tag": _WHEEL_TAGS[(target_platform, target_arch)],
            "executable": f"bin/{executable_name}",
            "sha256": digest,
            "bun_version": bun_version,
            "bun_target": _bun_compile_target(target_platform, target_arch),
        }
        if target_platform == "linux":
            metadata["libc"] = "glibc"
        (package_dir / "_host_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.setdefault("SOURCE_DATE_EPOCH", "0")
        _run(
            ["uv", "build", "--wheel", str(staging), "--out-dir", str(output_dir)],
            cwd=REPO_ROOT,
            env=env,
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(_PLATFORMS))
    parser.add_argument("--arch", choices=sorted(_ARCHES))
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--bun", help="path or command name for the pinned Bun runtime")
    parser.add_argument("--binary", type=Path, help="package an already-built target binary")
    parser.add_argument(
        "--bun-version",
        help=f"Bun version recorded for --binary inputs (must be {PINNED_BUN_VERSION})",
    )
    parser.add_argument("--build-id", help="release build identifier (defaults to binary digest)")
    parser.add_argument(
        "--codesign-identity",
        help=(
            "macOS signing identity. Release builds must pass a Developer ID identity; "
            "native local builds default to an ad-hoc signature"
        ),
    )
    parser.add_argument(
        "--require-codesign-identity",
        action="store_true",
        help="fail instead of using an ad-hoc signature for a macOS artifact",
    )
    return parser


def _project_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _current_platform() -> str:
    value = platform_module.system().lower()
    return {"darwin": "darwin", "linux": "linux", "windows": "win32"}.get(value, value)


def _current_arch() -> str:
    value = platform_module.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(value, value)


def _bun_compile_target(target_platform: str, target_arch: str) -> str:
    return _BUN_COMPILE_TARGETS.get(
        (target_platform, target_arch),
        f"bun-{target_platform}-{target_arch}",
    )


def _require_native_target(target_platform: str, target_arch: str) -> None:
    current = (_current_platform(), _current_arch())
    target = (target_platform, target_arch)
    if target != current:
        raise SystemExit(
            f"source builds must run on the target platform ({target_platform}/{target_arch}); "
            f"current platform is {current[0]}/{current[1]}. "
            "Use --binary for staged cross-platform artifacts."
        )


def _resolve_bun(value: str | None) -> str:
    candidate = value or shutil.which("bun")
    if not candidate or not shutil.which(candidate):
        raise SystemExit("Bun is not executable; install the pinned version or pass --binary")
    return candidate


def _bun_version(bun: str) -> str:
    result = subprocess.run(
        [bun, "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sign_macos_executable(
    executable: Path,
    *,
    identity: str | None,
    require_identity: bool,
) -> None:
    """Replace Bun's invalid linker signature before hashing the artifact.

    Bun 1.3.14 emits a linker-signed Mach-O that runs locally but does not pass
    ``codesign --verify``. Local native builds receive a valid ad-hoc signature.
    Release callers pass ``--require-codesign-identity`` and a Developer ID so
    an ad-hoc build can never be mistaken for a distributable artifact.
    """

    if _current_platform() != "darwin":
        if identity or require_identity:
            raise SystemExit("macOS release TUI host artifacts must be signed on a macOS runner")
        # Cross-platform tests may package a staged fixture. Release callers
        # always set --require-codesign-identity and therefore cannot take this
        # unsigned path.
        return
    codesign = shutil.which("codesign")
    if not codesign:
        raise SystemExit("codesign is required to package a macOS TUI host")
    if require_identity and (not identity or not identity.startswith("Developer ID Application:")):
        raise SystemExit("a Developer ID codesign identity is required for release artifacts")

    selected_identity = identity or "-"
    entitlements = COMPANION_PROJECT / "macos-entitlements.plist"
    if not entitlements.is_file():
        raise SystemExit(f"macOS TUI host entitlements are missing: {entitlements}")
    command = [
        codesign,
        "--force",
        "--options",
        "runtime",
        "--identifier",
        MACOS_SIGNING_IDENTIFIER,
        "--entitlements",
        str(entitlements),
    ]
    if selected_identity != "-":
        command.append("--timestamp")
    command.extend(["--sign", selected_identity, str(executable)])
    _run(command, cwd=REPO_ROOT)
    _run([codesign, "--verify", "--strict", "--verbose=2", str(executable)], cwd=REPO_ROOT)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


if __name__ == "__main__":
    sys.exit(main())
