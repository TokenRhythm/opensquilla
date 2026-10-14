"""Verify migrations and the Control UI are packaged and discoverable post-install.

Critical (C1): without this, default-enabled persistence would silently
boot on an out-of-date schema after fresh install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from scripts.verify_webui_artifact import MANIFEST_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_JS = b"window.__opensquillaPackagingProbe = true;\n"


def _run_install_probe(
    command: list[str], *, timeout: float, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout=0)
    try:
        return subprocess.run(
            command, cwd=REPO_ROOT, check=True, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        for name in ("stdout", "stderr"):
            output = getattr(exc, name, None)
            if output:
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                exc.add_note(f"wheel installation {name}:\n{output[-6000:]}")
        raise


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_wheel_contains_migrations_and_webui_artifact(
    isolated_core_wheel: Path,
) -> None:
    """The wheel carries migration history and the generated-artifact tree."""

    with zipfile.ZipFile(isolated_core_wheel) as wheel:
        names = wheel.namelist()
        packaged_probe = wheel.read("opensquilla/gateway/static/dist/assets/packaging-probe.js")
        packaged_runtime_catalog = wheel.read(
            "opensquilla/runtime_packs/runtime-pack-catalog.json"
        )
        metadata_names = [
            name for name in names
            if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1, metadata_names
        metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))

    # Locked dependency preparation must not hide a wheel that dropped a base
    # dependency declaration (including its version, extras, or marker).
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = {Requirement(value) for value in project["project"]["dependencies"]}
    declared = {Requirement(value) for value in metadata.get_all("Requires-Dist", [])}
    assert expected <= declared, sorted(str(value) for value in expected - declared)

    assert any(n.endswith("opensquilla/_migrations/V010__meta_skill_runs.py") for n in names), (
        f"V010 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    )
    assert any(n.endswith("opensquilla/_migrations/V021__usage_ledger.py") for n in names), (
        f"V021 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    )
    assert any(
        n.endswith("opensquilla/_migrations/V022__telemetry_daily_usage.py") for n in names
    ), f"V022 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V023__router_deployment_telemetry.py") for n in names
    ), f"V023 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V024__usage_native_billing_receipts.py")
        for n in names
    ), f"V024 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V030__meta_control_intents.py") for n in names
    ), f"V030 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V031__meta_launch_drafts.py") for n in names
    ), f"V031 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V032__meta_launch_discard_tombstones.py")
        for n in names
    ), f"V032 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V037__artifact_sessions.py") for n in names
    ), f"V037 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V038__artifact_prompt_annotations.py")
        for n in names
    ), f"V038 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V039__artifact_mutation_attempts.py")
        for n in names
    ), f"V039 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert any(
        n.endswith("opensquilla/_migrations/V040__document_resources.py") for n in names
    ), f"V040 missing from wheel; found: {[n for n in names if '_migrations' in n]}"
    assert "opensquilla/gateway/static/dist/index.html" in names
    assert "opensquilla/gateway/static/dist/desktop.html" in names
    assert f"opensquilla/gateway/static/dist/{MANIFEST_NAME}" in names
    assert packaged_probe == SYNTHETIC_JS
    assert packaged_runtime_catalog == (
        REPO_ROOT / "desktop/electron/runtime/runtime-pack-catalog.json"
    ).read_bytes()

    assert "opensquilla/gateway/templates/legacy_index.html" not in names
    for removed_prefix in (
        "opensquilla/gateway/static/js/",
        "opensquilla/gateway/static/css/",
        "opensquilla/gateway/static/fonts/",
        "opensquilla/gateway/static/vendor/",
    ):
        assert not any(name.startswith(removed_prefix) for name in names), (
            f"retired frontend assets leaked into wheel under {removed_prefix}"
        )


def test_usage_query_client_source_is_part_of_webui_build_inputs() -> None:
    """Protect the Usage client and its post-Vite runtime bundle guard."""

    source = (
        REPO_ROOT / "opensquilla-webui" / "src" / "composables" / "usage" / "useUsageQuery.ts"
    ).read_text(encoding="utf-8")
    package = json.loads((REPO_ROOT / "opensquilla-webui" / "package.json").read_text())
    bundle_guard = (
        REPO_ROOT / "opensquilla-webui" / "scripts" / "check-runtime-bundle.mjs"
    ).read_text(encoding="utf-8")

    assert re.search(
        r"import type\s*\{[^}]*\bObservability\b[^}]*\}\s*from '@/modules/observability'",
        source,
    )
    assert "return observability.usage(range, options)" in source
    assert "check-runtime-bundle.mjs" in package["scripts"]["build:artifact"]
    assert "usage.query" in bundle_guard


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_installed_wheel_resolves_migrations(
    tmp_path: Path,
    isolated_core_wheel: Path,
) -> None:
    """An installed wheel resolves both the historical and latest migration."""
    venv_dir = tmp_path / "venv"
    _run_install_probe(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        timeout=120,
    )
    py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )

    # The preceding locked development sync supplies the dependency cache.
    # Keep this a fresh, complete installation, but resolve against that same
    # lock rather than downloading newer dependencies during an offline test.
    constraints = tmp_path / "locked-core.txt"
    _run_install_probe(
        [
            "uv", "export", "--locked", "--offline", "--no-dev", "--no-emit-project",
            "--no-hashes", "--output-file", str(constraints),
        ],
        timeout=60,
    )
    # A locked sync caches wheel contents, but need not cache every registry
    # metadata response used by a second pip resolution. Install from the lock
    # first; the wheel's normal resolver can then use installed metadata. Both
    # phases and the dependency check share the original 300-second budget.
    install_deadline = time.monotonic() + 300
    _run_install_probe(
        [
            "uv", "sync", "--locked", "--offline", "--no-dev", "--no-default-groups",
            "--no-install-project", "--python", str(py),
        ],
        timeout=300,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir)},
    )
    _run_install_probe(
        [
            "uv", "pip", "install", "--offline", "--python", str(py),
            "--constraint", str(constraints), str(isolated_core_wheel),
        ],
        timeout=install_deadline - time.monotonic(),
    )
    _run_install_probe(
        ["uv", "pip", "check", "--python", str(py)],
        timeout=install_deadline - time.monotonic(),
    )

    result = subprocess.run(
        [
            str(py),
            "-I",
            "-c",
            (
                "import sys; from pathlib import Path;"
                "from opensquilla.gateway.boot import _resolve_migrations_dir;"
                " d = _resolve_migrations_dir();"
                " assert d.is_relative_to(Path(sys.prefix)),"
                "        f'resolver escaped installed environment: {d}';"
                " assert (d / 'V010__meta_skill_runs.py').exists(),"
                "        f'V010 missing in {d}';"
                " assert (d / 'V021__usage_ledger.py').exists(),"
                "        f'V021 missing in {d}';"
                " assert (d / 'V022__telemetry_daily_usage.py').exists(),"
                "        f'V022 missing in {d}';"
                " assert (d / 'V023__router_deployment_telemetry.py').exists(),"
                "        f'V023 missing in {d}';"
                " assert (d / 'V024__usage_native_billing_receipts.py').exists(),"
                "        f'V024 missing in {d}';"
                " assert (d / 'V030__meta_control_intents.py').exists(),"
                "        f'V030 missing in {d}';"
                " assert (d / 'V031__meta_launch_drafts.py').exists(),"
                "        f'V031 missing in {d}';"
                " assert (d / 'V032__meta_launch_discard_tombstones.py').exists(),"
                "        f'V032 missing in {d}';"
                " assert (d / 'V037__artifact_sessions.py').exists(),"
                "        f'V037 missing in {d}';"
                " assert (d / 'V038__artifact_prompt_annotations.py').exists(),"
                "        f'V038 missing in {d}';"
                " assert (d / 'V039__artifact_mutation_attempts.py').exists(),"
                "        f'V039 missing in {d}';"
                " assert (d / 'V040__document_resources.py').exists(),"
                "        f'V040 missing in {d}';"
                " print('OK', d)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"resolver failed: {result.stderr}"
    assert "OK" in result.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not on PATH")
@pytest.mark.skipif(os.name == "nt", reason="docker smoke uses Linux container images")
@pytest.mark.skipif(
    os.environ.get("OPENSQUILLA_SKIP_DOCKER_SMOKE") == "1",
    reason="docker smoke disabled via env",
)
@pytest.mark.skipif(
    os.environ.get("OPENSQUILLA_RUN_DOCKER_SMOKE") != "1",
    reason="docker smoke is opt-in; it pulls external images",
)
def test_docker_image_resolves_migrations() -> None:
    """`docker build` + `docker run` resolves _migrations through V027.

    Verifies (C1 v2): .dockerignore no longer excludes migrations/.
    """
    tag = "opensquilla-test:meta-runs-persistence"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, f"docker build failed: {build.stderr[-2000:]}"

    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            tag,
            "-c",
            (
                "from opensquilla.gateway.boot import _resolve_migrations_dir;"
                " d = _resolve_migrations_dir();"
                " assert (d / 'V010__meta_skill_runs.py').exists();"
                " assert (d / 'V021__usage_ledger.py').exists();"
                " assert (d / 'V022__telemetry_daily_usage.py').exists();"
                " assert (d / 'V023__router_deployment_telemetry.py').exists();"
                " assert (d / 'V024__usage_native_billing_receipts.py').exists();"
                " assert (d / 'V030__meta_control_intents.py').exists();"
                " assert (d / 'V031__meta_launch_drafts.py').exists();"
                " assert (d / 'V032__meta_launch_discard_tombstones.py').exists();"
                " assert (d / 'V037__artifact_sessions.py').exists();"
                " assert (d / 'V038__artifact_prompt_annotations.py').exists();"
                " assert (d / 'V039__artifact_mutation_attempts.py').exists();"
                " assert (d / 'V040__document_resources.py').exists();"
                " print('OK', d)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, f"docker run failed: {run.stderr}"
    assert "OK" in run.stdout
