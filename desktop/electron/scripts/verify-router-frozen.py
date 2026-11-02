"""Freeze the production router probe and verify its native dependency closure.

Run source regressions first, then full frozen inference and a missing-version
resource negative control. This never builds or starts the full Desktop client.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROUTER_HIDDEN_IMPORTS = (
    "joblib", "sklearn", "sklearn.feature_extraction.text",
    "sklearn.decomposition._truncated_svd", "sklearn.decomposition._pca",
    "sklearn.preprocessing._data", "lightgbm", "tokenizers", "onnxruntime",
)
ROUTER_METADATA = ("scikit-learn", "lightgbm")


def package_file(package: str, relative: str) -> Path:
    spec = importlib.util.find_spec(package)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"router dependency is unavailable: {package}")
    path = Path(spec.origin).parent / relative
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def build_command(root: Path, output: Path) -> list[str]:
    # Match the router-related production build flags. The source entry imports
    # only the router, so unrelated Gateway/UI modules need no collection here.
    platform_library = {
        "win32": "bin/lib_lightgbm.dll",
        "darwin": "lib/lib_lightgbm.dylib",
    }.get(sys.platform, "lib/lib_lightgbm.so")
    library_destination = "lightgbm/bin" if sys.platform == "win32" else "lightgbm/lib"
    router = root / "src/opensquilla/squilla_router"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "opensquilla-router-probe",
        "--distpath", str(output / "dist"),
        "--workpath", str(output / "build"),
        "--specpath", str(output),
        "--paths", str(root / "src"),
        "--collect-binaries", "sklearn",
    ]
    for module in ROUTER_HIDDEN_IMPORTS:
        command.extend(("--hidden-import", module))
    for distribution in ROUTER_METADATA:
        command.extend(("--copy-metadata", distribution))
    for source, destination in (
        (router / "lightgbm_model/assets", "opensquilla/squilla_router/lightgbm_model/assets"),
        (router / "models/v4.2_phase3_inference/bge_onnx",  # Shared, hash-pinned encoder bytes.
         "opensquilla/squilla_router/models/v4.2_phase3_inference/bge_onnx"),
        (package_file("lightgbm", "VERSION.txt"), "lightgbm"),
    ):
        command.extend(("--add-data", f"{source}{os.pathsep}{destination}"))
    command.extend((
        "--add-binary",
        f"{package_file('lightgbm', platform_library)}{os.pathsep}{library_destination}",
    ))
    if sys.platform == "darwin":
        command.extend((
            "--add-binary", f"{package_file('sklearn', '.dylibs/libomp.dylib')}{os.pathsep}.",
        ))
    command.append(str(root / "desktop/electron/scripts/probe-router.py"))
    return command


def patch_mac_openmp(bundle: Path) -> None:
    if sys.platform != "darwin":
        return
    internal = bundle / "_internal"
    libomp = internal / "libomp.dylib"
    libraries = list(internal.rglob("lib_lightgbm.dylib"))
    if not libomp.is_file() or not libraries:
        raise RuntimeError("frozen macOS router is missing LightGBM or OpenMP")
    # Production resolves LightGBM's OpenMP dependency beside its native library
    # and signs both modified binaries, independent of the build host's rpath.
    for library in libraries:
        adjacent = library.with_name("libomp.dylib")
        if libomp.resolve() != adjacent.resolve():
            shutil.copyfile(libomp, adjacent)
        subprocess.run(["codesign", "--force", "--sign", "-", str(adjacent)], check=True)
        subprocess.run([
            "install_name_tool", "-change", "@rpath/libomp.dylib",
            "@loader_path/libomp.dylib", str(library),
        ], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(library)], check=True)


def run_frozen_probe(
    binary: Path, output: Path, environment: dict[str, str], label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [str(binary)], cwd=output, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired may carry bytes even with text=True. Preserve the
        # partial output before propagating the unchanged bounded failure.
        for stream in ("stdout", "stderr"):
            value = getattr(exc, stream) or ""
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            (output / f"{label}.{stream}.log").write_text(value, encoding="utf-8")
        raise
    (output / f"{label}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (output / f"{label}.stderr.log").write_text(result.stderr, encoding="utf-8")
    return result


def verify_frozen_probe(binary: Path, output: Path, environment: dict[str, str]) -> None:
    result = run_frozen_probe(binary, output, environment, "probe")
    if result.returncode:
        raise RuntimeError(f"frozen router inference failed:\n{result.stderr[-16_384:]}")
    identity = json.loads(result.stdout)
    if (
        identity.get("probe") != "opensquilla-desktop-router"
        or identity.get("frozen") is not True
        or identity.get("lightgbmVersionResource") is not True
    ):
        raise RuntimeError("router probe did not verify the frozen production runtime")
    print(json.dumps(identity), flush=True)

    version_resource = binary.parent / "_internal/lightgbm/VERSION.txt"
    version_bytes = version_resource.read_bytes()
    try:
        version_resource.unlink()
        missing = run_frozen_probe(binary, output, environment, "missing-version")
    finally:
        version_resource.write_bytes(version_bytes)
    if missing.returncode == 0 or missing.stdout.strip() or "VERSION.txt" not in missing.stderr:
        raise RuntimeError("frozen router probe did not reject its missing LightGBM VERSION.txt")
    print("Frozen router rejected missing VERSION.txt; original resource restored.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="opensquilla-router-frozen-"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile = output / "profile"
    profile.mkdir(exist_ok=True)
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
            "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "TMP", "TEMP", "TMPDIR",
            "PYINSTALLER_CONFIG_DIR",
        }
    }
    environment.update({
        "HOME": str(profile), "USERPROFILE": str(profile),
        "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(root / "src"), "CI_REPORT_DIR": str(output),
    })
    source_result = subprocess.run([
        sys.executable, "-m", "pytest", "tests/test_desktop/test_gateway_router_probe.py",
        "tests/test_lightgbm_router_model.py::"
        "test_missing_lightgbm_module_version_does_not_break_inference", "-q",
    ], cwd=root, env=environment, check=False)
    if source_result.returncode:
        return source_result.returncode

    build_log = output / "build.log"
    with build_log.open("w", encoding="utf-8") as log:
        build = subprocess.run(
            build_command(root, output), cwd=root, env=environment,
            stdout=log, stderr=subprocess.STDOUT, check=False,
        )
    if build.returncode:
        print(build_log.read_text(encoding="utf-8", errors="replace")[-16_384:])
        return build.returncode
    bundle = output / "dist/opensquilla-router-probe"
    patch_mac_openmp(bundle)
    executable = "opensquilla-router-probe.exe" if os.name == "nt" else "opensquilla-router-probe"
    frozen_environment = {key: value for key, value in environment.items() if key != "PYTHONPATH"}
    verify_frozen_probe(bundle / executable, output, frozen_environment)
    print(f"Router frozen probe artifacts: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
