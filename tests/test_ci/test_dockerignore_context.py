"""Exercise the repository's .dockerignore with Docker's own pattern matcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]


def test_dockerfile_copies_runtime_catalog_required_by_wheel_build() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY desktop/electron/runtime/runtime-pack-catalog.json "
        "./desktop/electron/runtime/runtime-pack-catalog.json"
    ) in dockerfile


def _mcp_constraints_script() -> str:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY uv.lock ./uv.lock" in dockerfile
    assert 'pip install --constraint mcp-constraints.txt ".[recommended]"' in dockerfile
    return dockerfile.split("RUN python - <<'MCP'\n", 1)[1].split("\nMCP\n", 1)[0]


def test_docker_mcp_constraints_follow_the_lock(tmp_path: Path) -> None:
    _write(tmp_path / "uv.lock", '''[[package]]
name = "mcp"
version = "2.3.0"
[[package]]
name = "mcp-types"
version = "2.3.0"
[[package]]
name = "unrelated"
version = "1.0.0"
''')
    result = subprocess.run(
        [sys.executable, "-c", _mcp_constraints_script()], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "mcp-constraints.txt").read_text() == (
        "mcp==2.3.0\nmcp-types==2.3.0\n"
    )


@pytest.mark.parametrize("extra", ["", '''[[package]]
name = "mcp-types"
version = "2.2.0"
[[package]]
name = "mcp-types"
version = "2.3.0"
'''])
def test_docker_mcp_constraints_reject_incomplete_or_ambiguous_lock(
    tmp_path: Path, extra: str,
) -> None:
    _write(tmp_path / "uv.lock", '[[package]]\nname = "mcp"\nversion = "2.2.0"\n' + extra)
    result = subprocess.run(
        [sys.executable, "-c", _mcp_constraints_script()], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "Expected one locked version for mcp-types" in result.stderr
    assert not (tmp_path / "mcp-constraints.txt").exists()


def _write(path: Path, contents: str = "probe\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


@pytest.mark.skipif(
    os.environ.get("OPENSQUILLA_DOCKERIGNORE_E2E") != "1",
    reason="set OPENSQUILLA_DOCKERIGNORE_E2E=1 in the Docker contract CI check",
)
def test_dockerignore_filters_real_build_context(tmp_path: Path) -> None:
    """A scratch build makes Docker, rather than a test reimplementation, decide."""
    if shutil.which("docker") is None:
        pytest.fail("Docker contract check was requested but docker is unavailable")

    context = tmp_path / "context"
    output = tmp_path / "output"
    context.mkdir()
    shutil.copy2(_ROOT / ".dockerignore", context / ".dockerignore")
    _write(context / "Dockerfile", "FROM scratch\nCOPY . /context/\n")

    # Files that must never enter either Dockerfile stage.
    _write(context / ".env", "ROOT_SECRET=1\n")
    _write(context / "opensquilla-webui/.env.local", "VITE_SECRET=1\n")
    _write(context / "opensquilla-webui/.npmrc", "//registry.example/:_authToken=secret\n")
    _write(context / "config/tls/server.pem", "private certificate material\n")
    _write(context / "config/tls/server.key", "private key material\n")
    _write(
        context / "src/opensquilla/gateway/static/dist/assets/stale-hash.js",
        "stale bundle\n",
    )
    _write(
        context / "opensquilla-webui/dist/assets/stale-hash.js",
        "stale source bundle\n",
    )

    # Required build metadata and active public assets survive. Retired music
    # is excluded even when an upgraded checkout still contains personal files.
    _write(context / "opensquilla-webui/.node-version", "22.12.0\n")
    _write(context / "opensquilla-webui/public/music/local.mp3", "local music\n")
    _write(context / "opensquilla-webui/public/music/album/track.aac", "local music\n")
    _write(context / "opensquilla-webui/public/music/playlist.local.json", "{broken")
    _write(context / "opensquilla-webui/public-assets/opensquilla-mark.png", "image\n")
    _write(context / "src/opensquilla/__init__.py")
    _write(context / "scripts/verify_webui_artifact.py")
    _write(context / "scripts/freeze_migration_registry.py")
    _write(context / "scripts/private-build-notes.py")
    _write(context / "uv.lock", "version = 1\n")

    result = subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--file",
            str(context / "Dockerfile"),
            "--output",
            f"type=local,dest={output}",
            str(context),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    copied = output / "context"
    assert (copied / "opensquilla-webui/.node-version").is_file()
    assert not (copied / "opensquilla-webui/public/music").exists()
    assert (copied / "opensquilla-webui/public-assets/opensquilla-mark.png").is_file()
    assert (copied / "src/opensquilla/__init__.py").is_file()
    assert (copied / "scripts/verify_webui_artifact.py").is_file()
    assert (copied / "scripts/freeze_migration_registry.py").is_file()
    assert not (copied / "scripts/private-build-notes.py").exists()
    assert (copied / "uv.lock").is_file()

    assert not (copied / ".env").exists()
    assert not (copied / "opensquilla-webui/.env.local").exists()
    assert not (copied / "opensquilla-webui/.npmrc").exists()
    assert not (copied / "config/tls/server.pem").exists()
    assert not (copied / "config/tls/server.key").exists()
    assert not (copied / "src/opensquilla/gateway/static/dist").exists()
    assert not (copied / "opensquilla-webui/dist").exists()
