from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from scripts.verify_webui_artifact import MANIFEST_NAME, source_fingerprint


def build_isolated_core_wheel(repo_root: Path, temp_root: Path) -> Path:
    """Build the test-only core wheel from an isolated source tree.

    The source checkout intentionally has no generated Vue ``dist`` tree,
    while standard wheel builds fail closed without a verified artifact. This
    builder creates the same minimal synthetic artifact used by packaging
    contract tests without mutating the checkout under test.
    """

    if shutil.which("uv") is None:
        raise RuntimeError("uv is required to build the isolated core test wheel")

    repo_root = repo_root.resolve()
    temp_root = temp_root.resolve()
    build_root = temp_root / "source"
    build_root.mkdir(parents=True)

    def ignored(source: str, names: list[str]) -> set[str]:
        source_path = Path(source).resolve()
        generated = {
            name
            for name in names
            if name in {"node_modules", "coverage", "test-results", "__pycache__"}
            or name.endswith(".pyc")
        }
        if source_path == (repo_root / "src/opensquilla/gateway/static").resolve():
            generated.add("dist")
        if source_path == (repo_root / "opensquilla-webui").resolve():
            generated.add("dist")
        return generated

    for directory in ("src", "migrations", "opensquilla-webui", "scripts"):
        shutil.copytree(repo_root / directory, build_root / directory, ignore=ignored)
    for filename in (
        ".gitignore",
        "LICENSE",
        "README.md",
        "hatch_build.py",
        "pyproject.toml",
    ):
        shutil.copy2(repo_root / filename, build_root / filename)
    runtime_catalog = Path("desktop/electron/runtime/runtime-pack-catalog.json")
    (build_root / runtime_catalog).parent.mkdir(parents=True)
    shutil.copy2(repo_root / runtime_catalog, build_root / runtime_catalog)

    dist = build_root / "src" / "opensquilla" / "gateway" / "static" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "packaging-probe.js").write_bytes(
        b"window.__opensquillaPackagingProbe = true;\n"
    )
    (assets / "packaging-probe.css").write_bytes(b"body{}\n")
    (dist / "index.html").write_text(
        """<!doctype html>
<link rel="stylesheet" href="./assets/packaging-probe.css">
<script type="module" src="./assets/packaging-probe.js"></script>
""",
        encoding="utf-8",
    )

    records = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        content = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schemaVersion": 1,
        "sourceFingerprint": source_fingerprint(build_root / "opensquilla-webui"),
        "files": records,
    }
    (dist / MANIFEST_NAME).write_text(
        f"{json.dumps(manifest, indent=2)}\n",
        encoding="utf-8",
    )

    wheel_dir = temp_root / "wheel"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(wheel_dir)],
        cwd=build_root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"uv build failed: {result.stderr}")
    wheels = list(wheel_dir.glob("opensquilla-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected 1 wheel, got {wheels}")
    return wheels[0]
