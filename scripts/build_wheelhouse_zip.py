#!/usr/bin/env python3
"""Build a platform-local OpenSquilla wheelhouse release zip.

The output is intentionally not a source checkout and not a macOS DMG. It is a
zip containing the OpenSquilla wheel, its freshly built Web UI, dependency
wheels for the current platform/Python, install scripts, a manifest, and an
operator-facing README.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

DEFAULT_RUNTIME_RELEASE = "20260414"
DEFAULT_RUNTIME_PYTHON_VERSION = "3.12.13"
PYTHON_BUILD_STANDALONE_REPO = "astral-sh/python-build-standalone"
LFS_POINTER_LINE = "version https://git-lfs.github.com/spec/v1"
RELEASE_NOTICE_RELS = ("LICENSE", "THIRD_PARTY_NOTICES.md")
WHEEL_ROUTER_PREFIX = "opensquilla/squilla_router/models"
ROUTER_PROVENANCE_WHEEL_PATH = (
    f"{WHEEL_ROUTER_PREFIX}/v4.2_phase3_inference/PROVENANCE.md"
)
TOKENJUICE_PROVENANCE_WHEEL_PATH = "opensquilla/plugins/tokenjuice/PROVENANCE.md"
ALLOWED_SKILL_REFERENCE_WHEEL_PATHS = frozenset(
    {
        "opensquilla/skills/bundled/html-coder/references/add-css-style.md",
        "opensquilla/skills/bundled/html-coder/references/add-javascript.md",
        "opensquilla/skills/bundled/html-coder/references/attributes.md",
        "opensquilla/skills/bundled/html-coder/references/essentials.md",
        "opensquilla/skills/bundled/html-coder/references/global-attributes.md",
        "opensquilla/skills/bundled/html-coder/references/glossary.md",
        "opensquilla/skills/bundled/html-coder/references/standards.md",
        "opensquilla/skills/bundled/pptx/references/pptxgenjs.md",
        "opensquilla/skills/bundled/pptx/references/python_pptx.md",
    }
)
ROUTER_ASSET_RELS = (
    "v4.2_phase3_inference/PROVENANCE.md",
    "v4.2_phase3_inference/artifact_manifest.json",
    "v4.2_phase3_inference/lgbm_main.bin",
    "v4.2_phase3_inference/router.runtime.yaml",
    "v4.2_phase3_inference/mlp/model.onnx",
    "v4.2_phase3_inference/features/tfidf.pkl",
    "v4.2_phase3_inference/bge_onnx/model.onnx",
)
REQUIRED_RUNTIME_MODULE_RELS = (
    "opensquilla/cli/main.py",
    "opensquilla/cli/dist_cmd.py",
    "opensquilla/dist/__init__.py",
    "opensquilla/dist/workspace_state.py",
)
FORBIDDEN_RELEASE_SEGMENTS = {".git", ".github", ".omx"}
FORBIDDEN_RELEASE_ROOTS = {"docs", "tests", "scripts"}
FORBIDDEN_RELEASE_TEXT_MARKERS = (
    "INTERNAL_ORG_NAME",
    "github.com/internal-org/opensquilla",
    ".internal/evidence",
    "INTERNAL_RELEASE_NOTE.md",
    "LOCAL_AGENT_NOTES.md",
)
TEXT_RELEASE_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".text",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
WEBUI_DIRECTORY = "opensquilla-webui"


@dataclass(frozen=True)
class RouterAssetCheck:
    missing_files: tuple[Path, ...]
    pointer_files: tuple[Path, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_files and not self.pointer_files


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True, env=env)


def read_project_version(repo_root: Path) -> str:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def build_subprocess_env(work_dir: Path, *, repo_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    cache_root = work_dir
    if repo_root is not None:
        resolved_repo_root = repo_root.resolve()
        cache_root = resolved_repo_root.parent / f".{resolved_repo_root.name}-wheelhouse-cache"
    uv_cache = cache_root / "uv-cache"
    pip_cache = cache_root / "pip-cache"
    uv_cache.mkdir(parents=True, exist_ok=True)
    pip_cache.mkdir(parents=True, exist_ok=True)
    env["UV_CACHE_DIR"] = str(uv_cache)
    env["PIP_CACHE_DIR"] = str(pip_cache)
    return env


def _parse_version_triplet(raw: str, *, label: str) -> tuple[int, int, int]:
    value = raw.strip().removeprefix("v")
    parts = value.split(".")
    if len(parts) < 2:
        raise SystemExit(f"Could not parse {label} version: {raw!r}")
    try:
        parsed = tuple(int(part) for part in parts[:3])
    except ValueError as exc:
        raise SystemExit(f"Could not parse {label} version: {raw!r}") from exc
    if len(parsed) == 2:
        return parsed + (0,)
    return parsed


def build_webui(repo_root: Path, env: dict[str, str]) -> None:
    """Install locked frontend dependencies and build the release Web UI."""

    webui_dir = repo_root / WEBUI_DIRECTORY
    required_version_path = webui_dir / ".node-version"
    if not required_version_path.is_file():
        raise SystemExit(f"Required Node.js version file is missing: {required_version_path}")
    required_version = _parse_version_triplet(
        required_version_path.read_text(encoding="utf-8"), label="required Node.js"
    )

    search_path = env.get("PATH")
    node = shutil.which("node", path=search_path)
    npm = shutil.which("npm", path=search_path)
    if node is None or npm is None:
        minimum = ".".join(str(part) for part in required_version)
        raise SystemExit(
            f"Building the wheelhouse requires Node.js >= {minimum} and npm so the "
            "Web UI can be packaged. Official wheels and Desktop installers do not "
            "require Node.js."
        )

    try:
        probe = subprocess.run(
            [node, "--version"],
            cwd=repo_root,
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Could not query Node.js version (exit {exc.returncode}).") from exc
    installed_version = _parse_version_triplet(probe.stdout, label="installed Node.js")
    if installed_version < required_version:
        required_text = ".".join(str(part) for part in required_version)
        installed_text = ".".join(str(part) for part in installed_version)
        raise SystemExit(
            f"Building the wheelhouse requires Node.js >= {required_text}; "
            f"found {installed_text}."
        )

    run([npm, "ci"], cwd=webui_dir, env=env)
    run([npm, "run", "build"], cwd=webui_dir, env=env)
    run([npm, "run", "verify:release-dist"], cwd=webui_dir, env=env)


def copy_release_notices(release_root: Path, repo_root: Path | None = None) -> None:
    root = repo_root or repo_root_from_script()
    for rel in RELEASE_NOTICE_RELS:
        source = root / rel
        if not source.is_file():
            raise SystemExit(f"Required release notice file is missing: {source}")
        shutil.copy2(source, release_root / rel)


def platform_tag() -> str:
    system = platform.system().lower() or "unknown"
    machine = platform.machine().lower() or "unknown"
    aliases = {
        "darwin": "macos",
        "amd64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
    }
    return f"{aliases.get(system, system)}-{aliases.get(machine, machine)}"


def release_name(
    *,
    app_version: str,
    platform_tag: str,
    python_major: int,
    python_minor: int,
    profile: str,
    portable: bool,
) -> str:
    kind = "portable" if portable else "wheelhouse"
    return (
        f"OpenSquilla-{app_version}-{platform_tag}-"
        f"py{python_major}{python_minor}-{profile}-{kind}"
    )


def python_runtime_target_triple(platform_tag: str) -> str:
    triples = {
        "linux-arm64": "aarch64-unknown-linux-gnu",
        "linux-x64": "x86_64-unknown-linux-gnu",
        "macos-arm64": "aarch64-apple-darwin",
        "macos-x64": "x86_64-apple-darwin",
        "windows-arm64": "aarch64-pc-windows-msvc",
        "windows-x64": "x86_64-pc-windows-msvc",
    }
    try:
        return triples[platform_tag]
    except KeyError as exc:
        raise SystemExit(f"No bundled Python runtime mapping for platform: {platform_tag}") from exc


def python_runtime_asset_name(
    *, python_version: str, runtime_release: str, platform_tag: str
) -> str:
    triple = python_runtime_target_triple(platform_tag)
    return f"cpython-{python_version}+{runtime_release}-{triple}-install_only_stripped.tar.gz"


def python_runtime_asset_url(asset_name: str, runtime_release: str) -> str:
    quoted = urllib.parse.quote(asset_name, safe="")
    return (
        "https://github.com/"
        f"{PYTHON_BUILD_STANDALONE_REPO}/releases/download/{runtime_release}/{quoted}"
    )


def required_router_assets(model_root: Path) -> tuple[Path, ...]:
    return tuple(model_root / rel for rel in ROUTER_ASSET_RELS)


def check_router_assets(model_root: Path) -> RouterAssetCheck:
    missing_files: list[Path] = []
    pointer_files: list[Path] = []
    for path in required_router_assets(model_root):
        if not path.is_file():
            missing_files.append(path)
            continue
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if first_line and first_line[0].strip() == LFS_POINTER_LINE:
            pointer_files.append(path)
    return RouterAssetCheck(tuple(missing_files), tuple(pointer_files))


def _release_name(path: str) -> str:
    name = path.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def _contains_forbidden_release_segment(path: str) -> bool:
    return any(part in FORBIDDEN_RELEASE_SEGMENTS for part in _release_name(path).split("/"))


def _is_allowed_runtime_markdown(path: str) -> bool:
    name = _release_name(path)
    if name == ROUTER_PROVENANCE_WHEEL_PATH:
        return True
    if name == TOKENJUICE_PROVENANCE_WHEEL_PATH:
        return True
    if name in ALLOWED_SKILL_REFERENCE_WHEEL_PATHS:
        return True
    if name.startswith("opensquilla/skills/bundled/") and name.endswith("/SKILL.md"):
        return True
    return name.startswith("opensquilla/identity/templates/bootstrap/") and name.endswith(".md")


def forbidden_release_wheel_entries(names: list[str] | tuple[str, ...]) -> list[str]:
    violations: list[str] = []
    for raw_name in names:
        name = _release_name(raw_name)
        if not name or name.endswith("/"):
            continue
        root = name.split("/", 1)[0]
        if (
            root in FORBIDDEN_RELEASE_ROOTS
            or _contains_forbidden_release_segment(name)
            or (name.endswith(".md") and not _is_allowed_runtime_markdown(name))
        ):
            violations.append(name)
    return violations


def forbidden_release_wheel_paths(wheel_path: Path) -> list[str]:
    with ZipFile(wheel_path) as archive:
        return forbidden_release_wheel_entries(tuple(archive.namelist()))


def forbidden_release_text_hits(wheel_path: Path) -> list[str]:
    hits: list[str] = []
    with ZipFile(wheel_path) as archive:
        for info in archive.infolist():
            name = _release_name(info.filename)
            if not name or name.endswith("/"):
                continue
            suffix = Path(name).suffix.lower()
            basename = Path(name).name
            if suffix not in TEXT_RELEASE_SUFFIXES and basename != "METADATA":
                continue
            text = archive.read(info).decode("utf-8", errors="ignore")
            for marker in FORBIDDEN_RELEASE_TEXT_MARKERS:
                if marker in text:
                    hits.append(f"{name}: {marker}")
    return hits


def missing_router_assets_in_wheel(wheel_path: Path) -> list[str]:
    expected = {f"{WHEEL_ROUTER_PREFIX}/{rel}" for rel in ROUTER_ASSET_RELS}
    with ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
    return sorted(expected - names)


def missing_required_runtime_modules_in_wheel(wheel_path: Path) -> list[str]:
    with ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
    return sorted(set(REQUIRED_RUNTIME_MODULE_RELS) - names)


def find_built_wheel(wheel_dir: Path) -> Path:
    wheels = sorted(wheel_dir.glob("opensquilla-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one OpenSquilla wheel in {wheel_dir}, found {len(wheels)}")
    return wheels[0]


def build_wheel(repo_root: Path, wheel_dir: Path, env: dict[str, str]) -> Path:
    args = ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), "--clear"]
    try:
        run(args, cwd=repo_root, env=env)
    except subprocess.CalledProcessError as exc:
        print(
            f"uv build failed with exit code {exc.returncode}; retrying once.",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(2)
        run(args, cwd=repo_root, env=env)
    return find_built_wheel(wheel_dir)


def pip_command(python_major: int, python_minor: int, *args: str) -> list[str]:
    if sys.version_info[:2] != (python_major, python_minor):
        raise SystemExit(
            "Wheelhouse Python mismatch: "
            f"running {sys.version_info.major}.{sys.version_info.minor}, "
            f"target is {python_major}.{python_minor}."
        )
    return [
        "uv",
        "run",
        "--python",
        sys.executable,
        "--with",
        "pip",
        "python",
        "-m",
        "pip",
        *args,
    ]


def build_wheelhouse_command(
    package_dir: Path,
    wheel_path: Path,
    profile: str,
    *,
    target_platform_tag: str,
    python_major: int,
    python_minor: int,
) -> list[str]:
    validate_wheelhouse_target_platform(target_platform_tag)
    extras = () if profile == "core" else (profile,)
    target = str(wheel_path if not extras else f"{wheel_path}[{','.join(extras)}]")
    return pip_command(
        python_major,
        python_minor,
        "wheel",
        "--wheel-dir",
        str(package_dir),
        target,
    )


def validate_wheelhouse_target_platform(target_platform_tag: str) -> None:
    host_platform_tag = platform_tag()
    if target_platform_tag == host_platform_tag:
        return
    raise SystemExit(
        "Wheelhouse builds must run on the target platform so dependency markers "
        f"resolve correctly: host={host_platform_tag}, target={target_platform_tag}. "
        "Use --skip-wheelhouse only for metadata/package-layout checks."
    )


def download_wheelhouse(
    package_dir: Path,
    wheel_path: Path,
    profile: str,
    env: dict[str, str],
    *,
    target_platform_tag: str,
    python_major: int,
    python_minor: int,
) -> None:
    validate_wheelhouse_target_platform(target_platform_tag)
    run(
        build_wheelhouse_command(
            package_dir,
            wheel_path,
            profile,
            target_platform_tag=target_platform_tag,
            python_major=python_major,
            python_minor=python_minor,
        ),
        cwd=wheel_path.parent,
        env=env,
    )


def download_python_runtime_archive(
    *,
    download_dir: Path,
    python_version: str,
    runtime_release: str,
    platform_tag: str,
) -> tuple[Path, str]:
    asset_name = python_runtime_asset_name(
        python_version=python_version,
        runtime_release=runtime_release,
        platform_tag=platform_tag,
    )
    archive_path = download_dir / asset_name
    if archive_path.exists():
        return archive_path, asset_name

    download_dir.mkdir(parents=True, exist_ok=True)
    url = python_runtime_asset_url(asset_name, runtime_release)
    print(f"+ download {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        archive_path.write_bytes(response.read())
    return archive_path, asset_name


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise SystemExit(f"Refusing unsafe runtime archive member: {member.name}")
    archive.extractall(destination, filter="data")


def extract_python_runtime_archive(archive_path: Path, runtime_root: Path) -> None:
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    extract_dir = runtime_root.parent / f"{runtime_root.name}.extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            _safe_extract_tar(archive, extract_dir)

        candidate = extract_dir / "python"
        if not candidate.is_dir():
            child_dirs = [path for path in extract_dir.iterdir() if path.is_dir()]
            if len(child_dirs) != 1:
                raise SystemExit(f"Could not locate Python runtime root in {archive_path}")
            candidate = child_dirs[0]

        shutil.copytree(candidate, runtime_root)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def prune_portable_runtime(runtime_root: Path) -> None:
    site_packages = runtime_root / "Lib" / "site-packages"
    if site_packages.is_dir():
        removable_names = {
            "_distutils_hack",
            "pip",
            "pkg_resources",
            "setuptools",
            "wheel",
        }
        removable_globs = (
            "pip-*.dist-info",
            "setuptools-*.dist-info",
            "wheel-*.dist-info",
        )
        for name in removable_names:
            path = site_packages / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
        for pattern in removable_globs:
            for path in site_packages.glob(pattern):
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()

    for pycache in sorted(runtime_root.rglob("__pycache__"), reverse=True):
        if pycache.is_dir():
            shutil.rmtree(pycache)
    for pyc in runtime_root.rglob("*.pyc"):
        if pyc.is_file():
            pyc.unlink()


def install_portable_wheelhouse(release_root: Path) -> None:
    """Preinstall wheelhouse contents into the bundled Python runtime.

    Portable zips should start like an app, not like a package manager. Avoid
    runtime venv/ensurepip/pip work on user machines; Windows PowerShell and
    antivirus hooks can make that path look hung even when the wheels are local.
    """

    package_dir = release_root / "packages"
    site_packages = release_root / "runtime" / "python" / "Lib" / "site-packages"
    if not package_dir.is_dir() or not site_packages.is_dir():
        raise SystemExit("Portable wheelhouse preinstall requires packages and site-packages.")

    for wheel_path in sorted(package_dir.glob("*.whl")):
        with zipfile.ZipFile(wheel_path) as wheel:
            for info in wheel.infolist():
                name = info.filename
                if not name or name.endswith("/"):
                    continue
                target_rel: Path | None
                if ".data/" in name:
                    prefix, data_rel = name.split(".data/", 1)
                    _ = prefix
                    kind, _, remainder = data_rel.partition("/")
                    if kind in {"purelib", "platlib"} and remainder:
                        target_rel = Path(remainder)
                    else:
                        continue
                else:
                    target_rel = Path(name)
                target = site_packages / target_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with wheel.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def render_install_sh(
    *,
    wheel_name: str,
    profile: str,
    python_major: int,
    python_minor: int,
) -> str:
    wheel_target = f"${{PACKAGE_DIR}}/{wheel_name}"
    if profile != "core":
        wheel_target = f"{wheel_target}[{profile}]"
    return f"""#!/bin/sh
if [ -z "${{BASH_VERSION:-}}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PACKAGE_DIR="${{SCRIPT_DIR}}/packages"
REQUIRED_PYTHON_MAJOR={python_major}
REQUIRED_PYTHON_MINOR={python_minor}

find_python() {{
  local candidate
  for candidate in "python${{REQUIRED_PYTHON_MAJOR}}.${{REQUIRED_PYTHON_MINOR}}" python3 python; do
    if ! command -v "${{candidate}}" >/dev/null 2>&1; then
      continue
    fi
if "${{candidate}}" - <<PY >/dev/null 2>&1
import sys
expected = (${{REQUIRED_PYTHON_MAJOR}}, ${{REQUIRED_PYTHON_MINOR}})
raise SystemExit(0 if sys.version_info[:2] == expected else 1)
PY
    then
      command -v "${{candidate}}"
      return 0
    fi
  done
  return 1
}}

resolve_opensquilla_bin() {{
  if command -v opensquilla >/dev/null 2>&1; then
    command -v opensquilla
    return 0
  fi
  if [[ -x "${{HOME}}/.local/bin/opensquilla" ]]; then
    printf '%s\\n' "${{HOME}}/.local/bin/opensquilla"
    return 0
  fi
  local user_python_bin
  user_python_bin="${{HOME}}/Library/Python/${{REQUIRED_PYTHON_MAJOR}}.${{REQUIRED_PYTHON_MINOR}}/bin/opensquilla"
  if [[ -x "${{user_python_bin}}" ]]; then
    printf '%s\\n' "${{user_python_bin}}"
    return 0
  fi
  return 1
}}

if [[ ! -d "${{PACKAGE_DIR}}" ]]; then
  echo "OpenSquilla package directory not found: ${{PACKAGE_DIR}}" >&2
  exit 1
fi

PYTHON_BIN="$(find_python || true)"
if [[ -z "${{PYTHON_BIN}}" ]]; then
  echo "Python ${{REQUIRED_PYTHON_MAJOR}}.${{REQUIRED_PYTHON_MINOR}} is required." >&2
  echo "Install it, then rerun: bash install.sh" >&2
  exit 1
fi

echo "Installing OpenSquilla from local wheelhouse..."
if command -v uv >/dev/null 2>&1; then
  uv tool install \\
    --python "${{PYTHON_BIN}}" \\
    --reinstall \\
    --no-index \\
    --find-links "${{PACKAGE_DIR}}" \\
    "{wheel_target}"
else
  "${{PYTHON_BIN}}" -m pip install \\
    --user \\
    --no-index \\
    --find-links "${{PACKAGE_DIR}}" \\
    "{wheel_target}"
fi

OPENSQUILLA_BIN="$(resolve_opensquilla_bin || true)"
if [[ -z "${{OPENSQUILLA_BIN}}" ]]; then
  echo "OpenSquilla installed, but the executable was not found on PATH." >&2
  echo "Add ~/.local/bin or your Python user scripts directory to PATH, then run opensquilla." >&2
  exit 1
fi

"${{OPENSQUILLA_BIN}}" onboard --if-needed

echo
echo "OpenSquilla is installed."
echo "Start it with:"
echo "  opensquilla gateway run"
echo
echo "Then open:"
echo "  http://127.0.0.1:18791/control/"
"""


def render_install_ps1(
    *,
    wheel_name: str,
    profile: str,
    python_major: int,
    python_minor: int,
) -> str:
    wheel_target = f"$PackageDir\\{wheel_name}"
    if profile != "core":
        wheel_target = f"{wheel_target}[{profile}]"
    python_version = f"{python_major}.{python_minor}"
    return f"""$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = Join-Path $ScriptDir 'packages'
$RequiredPythonMajor = {python_major}
$RequiredPythonMinor = {python_minor}

function Find-Python {{
    $candidates = @("py -{python_version}", "python")
    foreach ($candidate in $candidates) {{
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        $rest = @()
        if ($parts.Length -gt 1) {{ $rest = $parts[1..($parts.Length - 1)] }}
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {{ continue }}
        $check = "import sys; expected = ($RequiredPythonMajor, $RequiredPythonMinor); " +
            "raise SystemExit(0 if sys.version_info[:2] == expected else 1)"
        & $exe @rest -c $check *> $null
        if ($LASTEXITCODE -eq 0) {{ return @($exe) + $rest }}
    }}
    return $null
}}

function Resolve-OpenSquilla {{
    $cmd = Get-Command opensquilla -ErrorAction SilentlyContinue
    if ($cmd) {{ return $cmd.Source }}
    $scriptDir = Join-Path $env:APPDATA "Python\\Python{python_major}{python_minor}\\Scripts"
    $script = Join-Path $scriptDir "opensquilla.exe"
    if (Test-Path $script) {{ return $script }}
    return $null
}}

if (-not (Test-Path $PackageDir)) {{
    throw "OpenSquilla package directory not found: $PackageDir"
}}

$Python = Find-Python
if (-not $Python) {{
    throw "Python {python_version} is required. Install it, then rerun .\\install.ps1."
}}

Write-Host "Installing OpenSquilla from local wheelhouse..."
if (Get-Command uv -ErrorAction SilentlyContinue) {{
    & uv tool install `
        --python "{python_version}" `
        --reinstall `
        --no-index `
        --find-links $PackageDir `
        "{wheel_target}"
}} else {{
    $PythonExe = $Python[0]
    $PythonArgs = @()
    if ($Python.Length -gt 1) {{
        $PythonArgs = $Python[1..($Python.Length - 1)]
    }}
    & $PythonExe @PythonArgs -m pip install `
        --user `
        --no-index `
        --find-links $PackageDir `
        "{wheel_target}"
}}
if ($LASTEXITCODE -ne 0) {{
    throw "OpenSquilla installation failed with exit code $LASTEXITCODE."
}}

$OpenSquillaBin = Resolve-OpenSquilla
if (-not $OpenSquillaBin) {{
    throw "OpenSquilla installed, but the executable was not found on PATH."
}}

& $OpenSquillaBin onboard --if-needed
if ($LASTEXITCODE -ne 0) {{
    throw "OpenSquilla onboarding failed with exit code $LASTEXITCODE."
}}

Write-Host ""
Write-Host "OpenSquilla is installed."
Write-Host "Start it with:"
Write-Host "  opensquilla gateway run"
Write-Host ""
Write-Host "Then open:"
Write-Host "  http://127.0.0.1:18791/control/"
"""


def _install_target(base: str, profile: str) -> str:
    extras = () if profile == "core" else (profile,)
    return base if not extras else f"{base}[{','.join(extras)}]"


def render_start_sh(profile: str = "recommended") -> str:
    target = _install_target("opensquilla", profile)
    script = """#!/bin/sh
if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi
set -euo pipefail

CLI_MODE=0
if [[ "${1:-}" == "--cli" ]]; then
  CLI_MODE=1
  shift
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="${SCRIPT_DIR}/packages"
PYTHON_BIN="${SCRIPT_DIR}/runtime/python/bin/python3"
if [[ -z "${OPENSQUILLA_LLM_API_KEY:-}" && -n "${OPENROUTER_API_KEY:-}" ]]; then
  export OPENSQUILLA_LLM_API_KEY="${OPENROUTER_API_KEY}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Bundled Python runtime not found: ${PYTHON_BIN}" >&2
  exit 1
fi
if [[ ! -d "${PACKAGE_DIR}" ]]; then
  echo "OpenSquilla package directory not found: ${PACKAGE_DIR}" >&2
  exit 1
fi
OPENSQUILLA_WHEEL="$(
  find "${PACKAGE_DIR}" -maxdepth 1 -type f -name 'opensquilla-*.whl' |
    sort |
    head -n 1
)"
if [[ -z "${OPENSQUILLA_WHEEL}" ]]; then
  echo "OpenSquilla wheel not found in ${PACKAGE_DIR}" >&2
  exit 1
fi
WHEEL_HASH="$(
  "${PYTHON_BIN}" -c '
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()[:12])
' "${OPENSQUILLA_WHEEL}"
)"
VENV_DIR="${SCRIPT_DIR}/.venv-${WHEEL_HASH}"
VENV_PYTHON="${VENV_DIR}/bin/python"
INSTALL_MARKER="${VENV_DIR}/.opensquilla-wheelhouse-${WHEEL_HASH}"
export PATH="${SCRIPT_DIR}:${VENV_DIR}/bin:${SCRIPT_DIR}/runtime/python/bin:${PATH}"
RELEASE_ID="$(
  "${PYTHON_BIN}" -c '
import hashlib, sys
print(hashlib.sha256(f"{sys.argv[1]}|{sys.argv[2]}".encode("utf-8")).hexdigest()[:12])
' "${SCRIPT_DIR}" "${WHEEL_HASH}"
)"
DATA_BASE="${XDG_DATA_HOME:-${HOME}/.local/share}"
PORTABLE_DATA_DIR="${OPENSQUILLA_PORTABLE_HOME:-${DATA_BASE}/OpenSquilla/portable/${RELEASE_ID}}"
if [[ -z "${OPENSQUILLA_GATEWAY_CONFIG_PATH:-}" ]]; then
  export OPENSQUILLA_GATEWAY_CONFIG_PATH="${PORTABLE_DATA_DIR}/config.toml"
fi
if [[ -z "${OPENSQUILLA_STATE_DIR:-}" ]]; then
  export OPENSQUILLA_STATE_DIR="${PORTABLE_DATA_DIR}"
fi
if [[ -z "${OPENSQUILLA_GATEWAY_STATE_DIR:-}" ]]; then
  export OPENSQUILLA_GATEWAY_STATE_DIR="${OPENSQUILLA_STATE_DIR}/state"
fi
if [[ -z "${OPENSQUILLA_GATEWAY_WORKSPACE_DIR:-}" ]]; then
  export OPENSQUILLA_GATEWAY_WORKSPACE_DIR="${OPENSQUILLA_STATE_DIR}/workspace"
fi
mkdir -p "${OPENSQUILLA_STATE_DIR}"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Creating local OpenSquilla environment..."
  "${PYTHON_BIN}" -m venv --without-pip "${VENV_DIR}"
fi

if [[ ! -f "${INSTALL_MARKER}" ]]; then
  echo "Installing OpenSquilla from bundled wheels..."
  SITE_PACKAGES="$("${VENV_PYTHON}" -c 'import site; print(site.getsitepackages()[0])')"
  "${PYTHON_BIN}" - "${PACKAGE_DIR}" "${SITE_PACKAGES}" <<'PY'
import pathlib
import shutil
import sys
import zipfile

package_dir = pathlib.Path(sys.argv[1])
site_packages = pathlib.Path(sys.argv[2])
site_packages.mkdir(parents=True, exist_ok=True)
for wheel_path in sorted(package_dir.glob("*.whl")):
    with zipfile.ZipFile(wheel_path) as wheel:
        for info in wheel.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if ".data/" in name:
                _prefix, data_rel = name.split(".data/", 1)
                kind, _sep, remainder = data_rel.partition("/")
                if kind not in {"purelib", "platlib"} or not remainder:
                    continue
                target_rel = pathlib.Path(remainder)
            else:
                target_rel = pathlib.Path(name)
            target = site_packages / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with wheel.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
PY
  touch "${INSTALL_MARKER}"
fi

OPENSQUILLA_BIN="${VENV_PYTHON}"
OPENSQUILLA_MODULE=( "-m" "opensquilla.cli.main" )
if [[ "${CLI_MODE}" == "1" ]]; then
  exec "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" "$@"
fi

if [[ ! -f "${OPENSQUILLA_GATEWAY_CONFIG_PATH}" && -n "${OPENROUTER_API_KEY:-}" ]]; then
  "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" onboard \\
    --provider openrouter \\
    --api-key-env OPENROUTER_API_KEY \\
    --minimal
else
  "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" onboard
fi

echo
echo "Starting OpenSquilla gateway."
echo "Web UI: http://127.0.0.1:18791/control/"
echo "Press Ctrl+C in this terminal to stop the gateway."
if [[ -t 1 ]]; then
  exec "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" gateway run
else
  LOG_DIR="${OPENSQUILLA_STATE_DIR}/logs"
  mkdir -p "${LOG_DIR}"
  CONSOLE_LOG="${OPENSQUILLA_STATE_DIR}/logs/gateway-console.log"
  echo "Console log: ${CONSOLE_LOG}"
  "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" gateway run 2>&1 | tee -a "${CONSOLE_LOG}"
  exit "${PIPESTATUS[0]}"
fi
"""
    return script.replace("__TARGET__", target)


def render_start_ps1(profile: str = "recommended") -> str:
    target = _install_target("opensquilla", profile)
    requires_router_runtime = "$true" if profile == "recommended" else "$false"
    script = """param(
    [switch]$Cli,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:replace'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = Join-Path $ScriptDir 'packages'
$PythonBin = Join-Path $ScriptDir 'runtime\\python\\python.exe'
$VenvBase = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
$VenvRoot = Join-Path $VenvBase 'OpenSquilla\\venvs'
$RequiresRouterRuntime = __REQUIRES_ROUTER_RUNTIME__
if ((-not $env:OPENSQUILLA_LLM_API_KEY) -and $env:OPENROUTER_API_KEY) {
    $env:OPENSQUILLA_LLM_API_KEY = $env:OPENROUTER_API_KEY
}

if (-not (Test-Path $PythonBin)) {
    throw "Bundled Python runtime not found: $PythonBin"
}
if (-not (Test-Path $PackageDir)) {
    throw "OpenSquilla package directory not found: $PackageDir"
}

function Test-WindowsVCRedistInstalled {
    if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows
    )) {
        return $true
    }
    $runtimeKeys = @(
        'HKLM:\\SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64',
        'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64'
    )
    foreach ($key in $runtimeKeys) {
        if (-not (Test-Path $key)) {
            continue
        }
        $runtime = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($runtime -and $runtime.Installed -eq 1 -and $runtime.Major -ge 14) {
            return $true
        }
    }
    return $false
}

function Test-WindowsAdmin {
    if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows
    )) {
        return $true
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WindowsVCRedistInstaller {
    $redistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    $candidateInstallers = @(
        (Join-Path $ScriptDir 'vc_redist.x64.exe'),
        (Join-Path $ScriptDir 'redist\\vc_redist.x64.exe'),
        (Join-Path $ScriptDir 'runtime\\vc_redist.x64.exe')
    )
    $installerPath = $candidateInstallers |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ($installerPath) {
        return $installerPath
    }

    $downloadDir = Join-Path ([System.IO.Path]::GetTempPath()) 'OpenSquilla'
    $installerPath = Join-Path $downloadDir 'vc_redist.x64.exe'
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    Write-Host (
        'OpenSquilla: downloading Microsoft Visual C++ Redistributable ' +
        '2015-2022 x64 from Microsoft.'
    )
    try {
        Invoke-WebRequest -Uri $redistUrl -OutFile $installerPath -UseBasicParsing
        return $installerPath
    } catch {
        Write-Warning (
            'OpenSquilla: could not download Microsoft Visual C++ ' +
            "Redistributable from $redistUrl. Error: $($_.Exception.Message)"
        )
        return $null
    }
}

function Install-WindowsVCRedistWithInstaller {
    param(
        [switch]$Repair
    )

    $installerPath = Get-WindowsVCRedistInstaller
    if (-not $installerPath) {
        return $false
    }

    $action = if ($Repair) { 'repairing' } else { 'installing' }
    Write-Host (
        "OpenSquilla: $action Microsoft Visual C++ Redistributable 2015-2022 x64..."
    )
    $redistArgs = if ($Repair) {
        @('/repair', '/quiet', '/norestart')
    } else {
        @('/install', '/quiet', '/norestart')
    }
    try {
        if (Test-WindowsAdmin) {
            $process = Start-Process -FilePath $installerPath `
                -ArgumentList $redistArgs `
                -Wait `
                -PassThru
        } else {
            Write-Host (
                'OpenSquilla: administrator approval may be requested to ' +
                'install or repair Microsoft Visual C++ Redistributable.'
            )
            $process = Start-Process -FilePath $installerPath `
                -ArgumentList $redistArgs `
                -Verb RunAs `
                -Wait `
                -PassThru
        }
    } catch {
        Write-Warning (
            'OpenSquilla: Visual C++ Redistributable installer could not be ' +
            "started. Error: $($_.Exception.Message)"
        )
        return $false
    }

    if ($process.ExitCode -in @(0, 1638, 3010)) {
        Write-Host 'OpenSquilla: Microsoft Visual C++ Redistributable is ready.'
        if ($process.ExitCode -eq 3010) {
            Write-Warning (
                'OpenSquilla: the Visual C++ installer requested a reboot; ' +
                'restart Windows if ONNX Runtime still fails to load.'
            )
        }
        return $true
    }

    Write-Warning (
        'OpenSquilla: Microsoft Visual C++ Redistributable installer exited ' +
        "with code $($process.ExitCode)."
    )
    return $false
}

function Install-WindowsVCRedistIfNeeded {
    param(
        [switch]$Repair
    )

    if (-not $RequiresRouterRuntime) {
        return $true
    }
    if ($env:OPENSQUILLA_SKIP_VC_REDIST -eq '1') {
        Write-Host (
            'OpenSquilla: skipping Microsoft Visual C++ Redistributable check ' +
            'because OPENSQUILLA_SKIP_VC_REDIST=1.'
        )
        return $true
    }
    if ((Test-WindowsVCRedistInstalled) -and -not $Repair) {
        return $true
    }

    $redistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    if ($Repair) {
        if (Install-WindowsVCRedistWithInstaller -Repair) {
            return $true
        }
    } elseif (Install-WindowsVCRedistWithInstaller) {
        return $true
    }

    $winget = if ($Repair) { $null } else { Get-Command winget -ErrorAction SilentlyContinue }
    if ($winget) {
        Write-Host (
            'OpenSquilla: Microsoft Visual C++ Redistributable not detected; ' +
            'installing with winget.'
        )
        $wingetArgs = @(
            'install',
            '--id',
            'Microsoft.VCRedist.2015+.x64',
            '--exact',
            '--silent',
            '--accept-package-agreements',
            '--accept-source-agreements'
        )
        & winget @wingetArgs
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'OpenSquilla: Microsoft Visual C++ Redistributable installation completed.'
            return $true
        }
        Write-Warning (
            'OpenSquilla: winget could not install Microsoft Visual C++ ' +
            "Redistributable (exit $LASTEXITCODE)."
        )
    }

    Write-Warning (
        'OpenSquilla: Microsoft Visual C++ Redistributable 2015-2022 x64 is ' +
        'required for the bundled ONNX router.'
    )
    Write-Warning (
        'OpenSquilla can still start with safe router fallback, but bundled ' +
        'ONNX model routing is disabled until this runtime is installed.'
    )
    Write-Warning (
        "If automatic installation fails, install it manually: $redistUrl"
    )
    Write-Warning (
        'After installing, reopen PowerShell and restart OpenSquilla.'
    )
    return $false
}

function Test-OnnxRuntimeImport {
    if (-not (Test-Path $VenvPython)) {
        return $false
    }
    & $VenvPython -c "import onnxruntime as ort; print('onnxruntime', ort.__version__)" | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Repair-WindowsVCRedistForOnnxIfNeeded {
    if (-not $RequiresRouterRuntime) {
        return
    }
    if ($env:OPENSQUILLA_SKIP_VC_REDIST -eq '1') {
        return
    }
    if (Test-OnnxRuntimeImport) {
        return
    }

    Write-Warning (
        'OpenSquilla: ONNX Runtime failed to import after setup. Attempting ' +
        'Visual C++ Redistributable repair before starting the gateway.'
    )
    Install-WindowsVCRedistIfNeeded -Repair | Out-Null
    if (Test-OnnxRuntimeImport) {
        return
    }

    Write-Warning (
        'OpenSquilla: ONNX Runtime still failed after Visual C++ repair. If ' +
        'the router warning remains, check CPU/VM AVX compatibility or install ' +
        'the Microsoft Visual C++ Redistributable manually: ' +
        'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    )
}

if (-not (Test-Path $VenvRoot)) {
    New-Item -ItemType Directory -Path $VenvRoot -Force | Out-Null
}
$OpenSquillaWheel = Get-ChildItem -Path $PackageDir -Filter 'opensquilla-*.whl' |
    Sort-Object Name |
    Select-Object -First 1
if (-not $OpenSquillaWheel) {
    throw "OpenSquilla wheel not found in $PackageDir"
}
$Sha256 = [System.Security.Cryptography.SHA256]::Create()
$WheelStream = [System.IO.File]::OpenRead($OpenSquillaWheel.FullName)
try {
    $WheelHashFull = -join ($Sha256.ComputeHash($WheelStream) | ForEach-Object {
        $_.ToString('x2')
    })
} finally {
    $WheelStream.Dispose()
    $Sha256.Dispose()
}
$WheelHash = $WheelHashFull.Substring(0, 12).ToLowerInvariant()
$Hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes("$ScriptDir|$WheelHash")
)
$ReleaseId = -join ($Hash[0..5] | ForEach-Object { $_.ToString('x2') })
$VenvDir = Join-Path $VenvRoot $ReleaseId
$VenvPython = Join-Path $VenvDir 'Scripts\\python.exe'
$InstallMarker = Join-Path $VenvDir ".opensquilla-wheelhouse-$WheelHash"
$env:PATH = "$ScriptDir;$ScriptDir\\runtime\\python;$env:PATH"
$env:PATH = "$VenvDir\\Scripts;$env:PATH"
$PortableDataDir = if ($env:OPENSQUILLA_PORTABLE_HOME) {
    $env:OPENSQUILLA_PORTABLE_HOME
} else {
    Join-Path $VenvBase "OpenSquilla\\portable\\$ReleaseId"
}
if (-not $env:OPENSQUILLA_GATEWAY_CONFIG_PATH) {
    $env:OPENSQUILLA_GATEWAY_CONFIG_PATH = Join-Path $PortableDataDir 'config.toml'
}
if (-not $env:OPENSQUILLA_STATE_DIR) {
    $env:OPENSQUILLA_STATE_DIR = $PortableDataDir
}
if (-not $env:OPENSQUILLA_GATEWAY_STATE_DIR) {
    $env:OPENSQUILLA_GATEWAY_STATE_DIR = Join-Path $env:OPENSQUILLA_STATE_DIR 'state'
}
if (-not $env:OPENSQUILLA_GATEWAY_WORKSPACE_DIR) {
    $env:OPENSQUILLA_GATEWAY_WORKSPACE_DIR = Join-Path $env:OPENSQUILLA_STATE_DIR 'workspace'
}
New-Item -ItemType Directory -Path $env:OPENSQUILLA_STATE_DIR -Force | Out-Null
Install-WindowsVCRedistIfNeeded | Out-Null

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating local OpenSquilla environment..."
    & $PythonBin -m venv --without-pip $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSquilla environment creation failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path $InstallMarker)) {
    Write-Host "Installing OpenSquilla from bundled wheels..."
    $SitePackages = & $VenvPython -c "import site; print(site.getsitepackages()[0])"
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSquilla site-packages lookup failed with exit code $LASTEXITCODE."
    }
    $WheelInstallScript = @'
import pathlib
import shutil
import sys
import zipfile

package_dir = pathlib.Path(sys.argv[1])
site_packages = pathlib.Path(sys.argv[2])
site_packages.mkdir(parents=True, exist_ok=True)
for wheel_path in sorted(package_dir.glob("*.whl")):
    with zipfile.ZipFile(wheel_path) as wheel:
        for info in wheel.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if ".data/" in name:
                _prefix, data_rel = name.split(".data/", 1)
                kind, _sep, remainder = data_rel.partition("/")
                if kind not in {"purelib", "platlib"} or not remainder:
                    continue
                target_rel = pathlib.Path(remainder)
            else:
                target_rel = pathlib.Path(name)
            target = site_packages / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with wheel.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
'@
    $WheelInstallScript | & $PythonBin - $PackageDir $SitePackages
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSquilla bundled wheel installation failed with exit code $LASTEXITCODE."
    }
    New-Item -ItemType File -Path $InstallMarker -Force | Out-Null
}
Repair-WindowsVCRedistForOnnxIfNeeded

$OpenSquillaArgs = @("-m", "opensquilla.cli.main")

if ($Cli) {
    & $VenvPython @OpenSquillaArgs @CliArgs
    exit $LASTEXITCODE
}

if ((-not (Test-Path $env:OPENSQUILLA_GATEWAY_CONFIG_PATH)) -and $env:OPENROUTER_API_KEY) {
    & $VenvPython @OpenSquillaArgs onboard `
        --provider openrouter `
        --api-key-env OPENROUTER_API_KEY `
        --minimal
} else {
    & $VenvPython @OpenSquillaArgs onboard
}
if ($LASTEXITCODE -ne 0) {
    throw "OpenSquilla onboarding failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Starting OpenSquilla gateway."
Write-Host "Web UI: http://127.0.0.1:18791/control/"
Write-Host "Press Ctrl+C in this terminal to stop the gateway."
$OutputRedirected = [Console]::IsOutputRedirected
if (-not $OutputRedirected) {
    & $VenvPython @OpenSquillaArgs gateway run
    $GatewayExitCode = $LASTEXITCODE
} else {
    $LogDir = Join-Path $env:OPENSQUILLA_STATE_DIR 'logs'
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $ConsoleLog = Join-Path $LogDir 'gateway-console.log'
    Write-Host "Console log: $ConsoleLog"
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $VenvPython @OpenSquillaArgs gateway run 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.ToString()
                } else {
                    $_
                }
            } |
            Tee-Object -FilePath $ConsoleLog -Append
        $GatewayExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}
exit $GatewayExitCode

"""
    return script.replace("__TARGET__", target).replace(
        "__REQUIRES_ROUTER_RUNTIME__", requires_router_runtime
    )


def render_cli_sh() -> str:
    return """#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "${SCRIPT_DIR}/start.sh" --cli "$@"
"""


def render_cli_cmd() -> str:
    return (
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        'set "OSQ_POWERSHELL=powershell.exe"\r\n'
        'where pwsh.exe >nul 2>nul && set "OSQ_POWERSHELL=pwsh.exe"\r\n'
        '"%OSQ_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass '
        '-File "%~dp0start.ps1" -Cli %*\r\n'
    )


def render_shell_cmd() -> str:
    return (
        "@echo off\r\n"
        "title OpenSquilla Shell\r\n"
        'cd /d "%~dp0"\r\n'
        'set "OSQ_POWERSHELL=powershell.exe"\r\n'
        'where pwsh.exe >nul 2>nul && set "OSQ_POWERSHELL=pwsh.exe"\r\n'
        '"%OSQ_POWERSHELL%" -NoLogo -NoExit -NoProfile -ExecutionPolicy Bypass -Command '
        "\"Set-Location -LiteralPath '%~dp0'; "
        "function global:opensquilla { & (Join-Path (Get-Location) 'opensquilla.cmd') @args }; "
        "Write-Host 'OpenSquilla portable shell'; "
        "Write-Host 'Run commands like:'; "
        "Write-Host '  opensquilla onboard --provider openrouter'; "
        "Write-Host '  opensquilla gateway run'\"\r\n"
    )


def render_start_cmd() -> str:
    return (
        "@echo off\r\n"
        "title OpenSquilla Gateway\r\n"
        'cd /d "%~dp0"\r\n'
        'set "OSQ_POWERSHELL=powershell.exe"\r\n'
        'where pwsh.exe >nul 2>nul && set "OSQ_POWERSHELL=pwsh.exe"\r\n'
        '"%OSQ_POWERSHELL%" -NoLogo -NoExit -NoProfile -ExecutionPolicy Bypass '
        '-File "%~dp0start.ps1"\r\n'
    )


def render_readme(
    *,
    app_version: str,
    profile: str,
    platform_tag: str,
    python_major: int,
    python_minor: int,
    portable: bool,
) -> str:
    windows_target = platform_tag.startswith("windows-")
    if portable:
        release_kind = "Portable Release"
        unix_commands = "bash start.sh"
        windows_command = ".\\start.ps1"
        python_note = "Python is bundled in this zip."
        setup_note = (
            "Config, workspace, logs, memory, and runtime state use the normal "
            "user-level OpenSquilla directory."
        )
    else:
        release_kind = "Wheelhouse Release"
        unix_commands = "bash install.sh\nopensquilla gateway run"
        windows_command = ".\\install.ps1\nopensquilla gateway run"
        python_note = f"Requires Python {python_major}.{python_minor}."
        setup_note = (
            "The installer runs idempotent onboarding after installation. To "
            "reconfigure later, run `opensquilla onboard` for the full wizard or "
            "`opensquilla configure <section>` for one area."
        )
    if windows_target:
        if portable:
            command_section = f"""## Windows

1. Right-click `Start OpenSquilla.cmd` -> **Run as administrator**.
2. Complete onboarding.
3. Open `http://127.0.0.1:18791/control/`.

Notes:
- Keep the terminal open. Closing it stops the gateway.
- OpenSquilla 0.1.0 preview builds are unsigned. The supported portable launch
  path is administrator launch.
- If SmartScreen appears, choose **More info** -> **Run anyway**.
- If Smart App Control or enterprise policy blocks the unsigned app, use the
  `uv tool install` wheel path instead.
- Microsoft documents that SmartScreen checks downloaded apps and Smart App
  Control can block unknown, unsigned code.

<details>
<summary>Advanced portable usage</summary>

Use these options only when you want scripted setup or portable CLI commands.

To start from PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
{windows_command}
```

If `OPENROUTER_API_KEY` is present and no local config exists, the launcher
writes an OpenRouter env-reference config and starts the gateway without asking
you to paste the key.

The portable package does not install a global `opensquilla` command. For a
terminal where `opensquilla ...` commands work, run
`OpenSquilla Shell.cmd`, or run commands from this folder:

```powershell
.\\opensquilla.cmd onboard
```

</details>
"""
        else:
            command_section = f"""## Windows PowerShell

```powershell
Set-ExecutionPolicy -Scope Process Bypass
{windows_command}
```
"""
    else:
        if portable:
            command_section = f"""## macOS / Linux

1. Run the launcher from the extracted folder:

```sh
{unix_commands}
```

2. Keep the terminal open. Closing the terminal stops the gateway.
3. Complete onboarding. On first run, choose a provider and paste the requested
   keys; later starts let you review or change the config.
4. Open `http://127.0.0.1:18791/control/`.
"""
        else:
            command_section = f"""## macOS / Linux

```sh
{unix_commands}
```
"""

    web_ui_note = (
        ""
        if portable
        else "Open `http://127.0.0.1:18791/control/`.\n\n"
    )

    return f"""# OpenSquilla {app_version} {release_kind}

Build target:

- platform: `{platform_tag}`
- Python: `{python_major}.{python_minor}`
- profile: `{profile}`

{command_section.rstrip()}

{web_ui_note}{python_note}

{setup_note}
"""


def write_manifest(
    path: Path,
    *,
    app_version: str,
    profile: str,
    platform_tag: str,
    python_major: int,
    python_minor: int,
    wheel_name: str,
    package_count: int,
    include_router_assets: bool,
    portable: bool,
    runtime_release: str,
    runtime_asset: str,
) -> None:
    payload = {
        "name": "OpenSquilla wheelhouse zip",
        "version": app_version,
        "profile": profile,
        "platform_tag": platform_tag,
        "python": f"{python_major}.{python_minor}",
        "wheel_name": wheel_name,
        "package_count": package_count,
        "include_router_assets": include_router_assets,
        "portable": portable,
        "runtime_release": runtime_release,
        "runtime_asset": runtime_asset,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_release_tree(
    release_root: Path,
    wheel_path: Path,
    *,
    app_version: str,
    profile: str,
    platform_tag: str,
    python_major: int,
    python_minor: int,
    include_router_assets: bool,
    portable: bool,
    runtime_release: str,
    runtime_asset: str,
    runtime_root: Path | None = None,
) -> Path:
    if release_root.exists():
        shutil.rmtree(release_root)
    package_dir = release_root / "packages"
    package_dir.mkdir(parents=True)
    bundled_wheel = package_dir / wheel_path.name
    shutil.copy2(wheel_path, bundled_wheel)

    if portable:
        if runtime_root is None or not runtime_root.is_dir():
            raise SystemExit("Portable release requires a Python runtime directory.")
        runtime_target = release_root / "runtime" / "python"
        shutil.copytree(runtime_root, runtime_target)
        prune_portable_runtime(runtime_target)

        start_sh = release_root / "start.sh"
        start_sh.write_text(render_start_sh(profile), encoding="utf-8")
        start_sh.chmod(start_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        cli_sh = release_root / "opensquilla"
        cli_sh.write_text(render_cli_sh(), encoding="utf-8")
        cli_sh.chmod(cli_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (release_root / "start.ps1").write_text(render_start_ps1(profile), encoding="utf-8")
        if platform_tag.startswith("windows-"):
            (release_root / "Start OpenSquilla.cmd").write_text(
                render_start_cmd(),
                encoding="utf-8",
                newline="",
            )
            (release_root / "opensquilla.cmd").write_text(
                render_cli_cmd(),
                encoding="utf-8",
                newline="",
            )
            (release_root / "OpenSquilla Shell.cmd").write_text(
                render_shell_cmd(),
                encoding="utf-8",
                newline="",
            )
    else:
        install_sh = release_root / "install.sh"
        install_sh.write_text(
            render_install_sh(
                wheel_name=wheel_path.name,
                profile=profile,
                python_major=python_major,
                python_minor=python_minor,
            ),
            encoding="utf-8",
        )
        install_sh.chmod(install_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        (release_root / "install.ps1").write_text(
            render_install_ps1(
                wheel_name=wheel_path.name,
                profile=profile,
                python_major=python_major,
                python_minor=python_minor,
            ),
            encoding="utf-8",
        )

    (release_root / "README.md").write_text(
        render_readme(
            app_version=app_version,
            profile=profile,
            platform_tag=platform_tag,
            python_major=python_major,
            python_minor=python_minor,
            portable=portable,
        ),
        encoding="utf-8",
    )
    copy_release_notices(release_root)
    write_manifest(
        release_root / "manifest.json",
        app_version=app_version,
        profile=profile,
        platform_tag=platform_tag,
        python_major=python_major,
        python_minor=python_minor,
        wheel_name=wheel_path.name,
        package_count=len(list(package_dir.glob("*.whl"))),
        include_router_assets=include_router_assets,
        portable=portable,
        runtime_release=runtime_release,
        runtime_asset=runtime_asset,
    )
    return bundled_wheel


def create_zip(release_root: Path, zip_path: Path, *, archive_root: str | None = None) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    root_parent = release_root.parent
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path in sorted(p for p in release_root.rglob("*") if p.is_file()):
            if archive_root:
                rel = Path(archive_root, path.relative_to(release_root)).as_posix()
            else:
                rel = path.relative_to(root_parent).as_posix()
            info = ZipInfo(rel)
            info.compress_type = ZIP_DEFLATED
            source_mode = stat.S_IMODE(path.stat().st_mode)
            executable_by_path = rel.endswith(("/install.sh", "/start.sh")) or (
                "/runtime/python/bin/" in rel
            )
            mode = (
                0o755
                if executable_by_path
                or source_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                else 0o644
            )
            info.external_attr = mode << 16
            archive.writestr(info, path.read_bytes())


def sha256_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_sha256(path: Path) -> Path:
    digest = sha256_digest(path)
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return checksum_path


def write_sha256s(paths: tuple[Path, ...] | list[Path], checksum_path: Path) -> Path:
    lines = [f"{sha256_digest(path)}  {path.name}" for path in sorted(paths, key=lambda p: p.name)]
    checksum_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return checksum_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("recommended", "core"), default="recommended")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--work-dir", type=Path, default=Path("build/wheelhouse-zip"))
    parser.add_argument(
        "--bundle-python-runtime",
        action="store_true",
        help="Bundle a portable python-build-standalone runtime and start scripts.",
    )
    parser.add_argument(
        "--platform-tag",
        choices=(
            "linux-arm64",
            "linux-x64",
            "macos-arm64",
            "macos-x64",
            "windows-arm64",
            "windows-x64",
        ),
        help="Target platform tag. Defaults to the current host platform.",
    )
    parser.add_argument(
        "--python-runtime-release",
        default=DEFAULT_RUNTIME_RELEASE,
        help="python-build-standalone release tag to bundle.",
    )
    parser.add_argument(
        "--python-runtime-version",
        default=DEFAULT_RUNTIME_PYTHON_VERSION,
        help="Full CPython runtime version from python-build-standalone.",
    )
    parser.add_argument(
        "--python-runtime-archive",
        type=Path,
        help="Use a pre-downloaded python-build-standalone install_only archive.",
    )
    parser.add_argument(
        "--skip-wheelhouse",
        action="store_true",
        help="Only place the OpenSquilla wheel in packages/ for script debugging.",
    )
    parser.add_argument(
        "--skip-zip",
        action="store_true",
        help="Prepare the release directory without creating the zip.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    app_version = read_project_version(repo_root)
    include_router_assets = args.profile == "recommended"

    if include_router_assets:
        model_root = repo_root / "src" / "opensquilla" / "squilla_router" / "models"
        asset_check = check_router_assets(model_root)
        if not asset_check.ok:
            for path in asset_check.missing_files:
                print(f"Missing router asset: {path}", file=sys.stderr)
            for path in asset_check.pointer_files:
                print(f"Git LFS pointer file is not hydrated: {path}", file=sys.stderr)
            print(
                'Run: git lfs pull --include="src/opensquilla/squilla_router/models/**"',
                file=sys.stderr,
            )
            return 1

    work_dir = (repo_root / args.work_dir).resolve()
    env = build_subprocess_env(work_dir, repo_root=repo_root)
    wheel_dir = work_dir / "wheels"
    tag = args.platform_tag or platform_tag()
    if not args.skip_wheelhouse:
        validate_wheelhouse_target_platform(tag)
    name = release_name(
        app_version=app_version,
        platform_tag=tag,
        python_major=sys.version_info.major,
        python_minor=sys.version_info.minor,
        profile=args.profile,
        portable=args.bundle_python_runtime,
    )
    release_root = work_dir / name
    runtime_root: Path | None = None
    runtime_asset = ""

    if args.bundle_python_runtime and sys.version_info[:2] != (3, 12):
        raise SystemExit("Portable release builds currently require Python 3.12.")

    # Fail on missing frontend build prerequisites before downloading or
    # extracting an optional portable Python runtime.
    build_webui(repo_root, env)

    if args.bundle_python_runtime:
        if args.python_runtime_archive:
            runtime_archive = args.python_runtime_archive.resolve()
            runtime_asset = runtime_archive.name
        else:
            runtime_archive, runtime_asset = download_python_runtime_archive(
                download_dir=work_dir / "runtime-downloads",
                python_version=args.python_runtime_version,
                runtime_release=args.python_runtime_release,
                platform_tag=tag,
            )
        runtime_root = work_dir / "runtime" / "python"
        extract_python_runtime_archive(runtime_archive, runtime_root)

    wheel_path = build_wheel(repo_root, wheel_dir, env)
    missing_runtime_modules = missing_required_runtime_modules_in_wheel(wheel_path)
    if missing_runtime_modules:
        print("Built wheel is missing required runtime modules:", file=sys.stderr)
        for entry in missing_runtime_modules:
            print(f"  {entry}", file=sys.stderr)
        return 1

    wheel_violations = forbidden_release_wheel_paths(wheel_path)
    if wheel_violations:
        print("Built wheel contains forbidden release entries:", file=sys.stderr)
        for entry in wheel_violations:
            print(f"  {entry}", file=sys.stderr)
        return 1
    text_violations = forbidden_release_text_hits(wheel_path)
    if text_violations:
        print("Built wheel contains internal release text markers:", file=sys.stderr)
        for entry in text_violations:
            print(f"  {entry}", file=sys.stderr)
        return 1

    if include_router_assets:
        missing_from_wheel = missing_router_assets_in_wheel(wheel_path)
        if missing_from_wheel:
            print("Router assets are hydrated but missing from the built wheel:", file=sys.stderr)
            for entry in missing_from_wheel:
                print(f"  {entry}", file=sys.stderr)
            return 1

    bundled_wheel = prepare_release_tree(
        release_root,
        wheel_path,
        app_version=app_version,
        profile=args.profile,
        platform_tag=tag,
        python_major=sys.version_info.major,
        python_minor=sys.version_info.minor,
        include_router_assets=include_router_assets,
        portable=args.bundle_python_runtime,
        runtime_release=args.python_runtime_release if args.bundle_python_runtime else "",
        runtime_asset=runtime_asset,
        runtime_root=runtime_root,
    )
    package_dir = bundled_wheel.parent

    if not args.skip_wheelhouse:
        download_wheelhouse(
            package_dir,
            bundled_wheel,
            args.profile,
            env,
            target_platform_tag=tag,
            python_major=sys.version_info.major,
            python_minor=sys.version_info.minor,
        )
    write_manifest(
        release_root / "manifest.json",
        app_version=app_version,
        profile=args.profile,
        platform_tag=tag,
        python_major=sys.version_info.major,
        python_minor=sys.version_info.minor,
        wheel_name=bundled_wheel.name,
        package_count=len(list(package_dir.glob("*.whl"))),
        include_router_assets=include_router_assets,
        portable=args.bundle_python_runtime,
        runtime_release=args.python_runtime_release if args.bundle_python_runtime else "",
        runtime_asset=runtime_asset,
    )

    if args.skip_zip:
        print(release_root)
        return 0

    zip_path = (repo_root / args.output_dir / f"{name}.zip").resolve()
    archive_root = f"OpenSquilla-{app_version}" if args.bundle_python_runtime else None
    create_zip(release_root, zip_path, archive_root=archive_root)
    checksum_path = write_sha256(zip_path)
    checksums_path = write_sha256s(
        tuple(zip_path.parent.glob("*.zip")), zip_path.parent / "SHA256SUMS"
    )
    print(zip_path)
    print(checksum_path)
    print(checksums_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
