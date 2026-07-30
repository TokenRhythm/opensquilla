"""Verify migrations and the Control UI are packaged and discoverable post-install.

Critical (C1): without this, default-enabled persistence would silently
boot on an out-of-date schema after fresh install.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.verify_webui_artifact import MANIFEST_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_JS = b"window.__opensquillaPackagingProbe = true;\n"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_wheel_contains_migrations_and_webui_artifact(
    isolated_core_wheel: Path,
) -> None:
    """The wheel carries migration history and the generated-artifact tree."""

    with zipfile.ZipFile(isolated_core_wheel) as wheel:
        names = wheel.namelist()
        packaged_probe = wheel.read("opensquilla/gateway/static/dist/assets/packaging-probe.js")

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
    assert "opensquilla/gateway/static/dist/index.html" in names
    assert f"opensquilla/gateway/static/dist/{MANIFEST_NAME}" in names
    assert packaged_probe == SYNTHETIC_JS

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

    assert "const USAGE_QUERY_METHOD = 'usage.query'" in source
    assert "check-runtime-bundle.mjs" in package["scripts"]["build:artifact"]
    assert "usage.query" in bundle_guard


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_installed_wheel_resolves_migrations(
    tmp_path: Path,
    isolated_core_wheel: Path,
) -> None:
    """An installed wheel resolves both the historical and latest migration."""
    venv_dir = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--seed", str(venv_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=120,
    )
    pip = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "pip"
    py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    # 120s was tight enough that Windows CI runners began timing out as
    # the base dependency list grew (each transitive wheel adds I/O the
    # Defender real-time scanner has to walk through). Ubuntu still
    # completes in ~30s; Windows now needs ~90-150s. Bumping the budget
    # rather than skipping preserves the test's intent — verify the
    # built wheel installs cleanly into a fresh venv and the migration
    # resolver finds V010 afterwards.
    subprocess.run(
        [str(pip), "install", str(isolated_core_wheel)],
        check=True,
        capture_output=True,
        timeout=300,
    )

    result = subprocess.run(
        [
            str(py),
            "-c",
            (
                "from opensquilla.gateway.boot import _resolve_migrations_dir;"
                " d = _resolve_migrations_dir();"
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
    """`docker build` + `docker run` resolves _migrations through V024.

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
                " print('OK', d)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, f"docker run failed: {run.stderr}"
    assert "OK" in run.stdout
