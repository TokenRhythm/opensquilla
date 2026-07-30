"""Exercise the repository's .dockerignore with Docker's own pattern matcher."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]


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

    # Nested dotfiles and private BGM remain supported inputs. The former is
    # harmless build metadata; the latter is deliberately allowed for local
    # images and rejected separately for official images.
    _write(context / "opensquilla-webui/.node-version", "22.12.0\n")
    _write(context / "opensquilla-webui/public/music/local.mp3", "local music\n")
    _write(context / "src/opensquilla/__init__.py")

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
    assert (copied / "opensquilla-webui/public/music/local.mp3").is_file()
    assert (copied / "src/opensquilla/__init__.py").is_file()

    assert not (copied / ".env").exists()
    assert not (copied / "opensquilla-webui/.env.local").exists()
    assert not (copied / "opensquilla-webui/.npmrc").exists()
    assert not (copied / "config/tls/server.pem").exists()
    assert not (copied / "config/tls/server.key").exists()
    assert not (copied / "src/opensquilla/gateway/static/dist").exists()
