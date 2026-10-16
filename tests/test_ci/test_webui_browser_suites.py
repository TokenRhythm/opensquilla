"""Fan-out must preserve every browser gate and its resource isolation."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

SELECTOR_PATH = Path(".github/scripts/webui_browser_suites.py")
SELECTOR = runpy.run_path(str(SELECTOR_PATH))
GENERAL_SHARDS = SELECTOR["GENERAL_SHARDS"]
specs_for_suite = SELECTOR["specs_for_suite"]
EXPECTED_GENERAL = frozenset("""
assistant-activity.spec.ts auth-connection-recovery.spec.ts
chat-send-lifecycle.spec.ts chat-send-lifecycle.real.spec.ts
cross-window-sync.real.spec.ts composer-paste.spec.ts durable-delivery.spec.ts
history-content-reading.spec.ts history-hydration.spec.ts http-delivery.spec.ts
idle-chat-recovery.spec.ts modern-session-recovery.spec.ts
provider-error-experience.spec.ts retired-bgm-upgrade.spec.ts support-bundle.spec.ts
console-clarity.spec.ts mobile-tabs.spec.ts header-responsive.spec.ts
topbar-global-controls.spec.ts topbar-visual.spec.ts router-physical-model.spec.ts
user-message-newlines.spec.ts session-created-card.spec.ts session-switch-transport.spec.ts
new-task-ensemble-race.spec.ts ensemble-new-task-legacy-turn.spec.ts goal-mode.spec.ts
plan-questionnaire-lifecycle.spec.ts plan-presentation.spec.ts task-progress.spec.ts
queue-steer.spec.ts share.spec.ts
""".split())


def _jobs() -> dict:
    return yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_general_browser_shards_preserve_the_entire_existing_inventory_once() -> None:
    shards = [specs_for_suite(shard) for shard in GENERAL_SHARDS]
    counts = Counter(spec for shard in shards for spec in shard)
    assert set(counts) == EXPECTED_GENERAL
    assert set(counts.values()) == {1}
    assert [len(shard) for shard in shards] == [8, 8, 8, 8]
    assert not set(counts).intersection(specs_for_suite("virtualization"))
    for spec in (*counts, *specs_for_suite("virtualization")):
        assert (Path("opensquilla-webui/e2e") / spec).is_file()


def test_browser_selector_cli_and_unknown_shards_fail_closed() -> None:
    result = subprocess.run(
        [sys.executable, str(SELECTOR_PATH), "general-2"],
        text=True, capture_output=True, check=True,
    )
    assert tuple(result.stdout.splitlines()) == specs_for_suite("general-2")
    with pytest.raises(ValueError, match="Unknown browser suite"):
        specs_for_suite("general-5")
    rejected = subprocess.run(
        [sys.executable, str(SELECTOR_PATH), "general-5"],
        text=True, capture_output=True, check=False,
    )
    assert rejected.returncode != 0
    assert not rejected.stdout


def test_browser_capacity_inventory_and_dedicated_job_cannot_silently_skip() -> None:
    jobs = _jobs()
    recovery, capacity = jobs["webui-chat-recovery"], jobs["webui-capacity"]
    assert recovery["strategy"] == {
        "fail-fast": False,
        "matrix": {"suite": [*GENERAL_SHARDS, "clipboard", "virtualization"]},
    }
    assert "'webui-capacity'" in capacity["if"]
    assert capacity["needs"] == ["plan-ci", "frontend-artifact"]
    assert "webui-chat-recovery" not in capacity["needs"]
    assert "strategy" not in capacity
    for job in (recovery, capacity):
        assert job["runs-on"] == "ubuntu-22.04"
        assert job["env"]["OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI"] == "gateway"
        download = _step(job, "Download verified frontend artifact")
        assert download["with"]["name"] == "opensquilla-webui-dist"
        sandbox = _step(job, "Install and verify Linux guest sandbox")
        assert "bwrap --unshare-user --unshare-net" in sandbox["run"]
        office = _step(job, "Prepare pinned offline Office preview assets")["run"]
        assert "npm run prepare:office-assets -- --offline" in office
        admission = _step(job, "Validate renderer capacity inventory")
        assert "if" not in admission
        assert not admission.get("continue-on-error")
        assert "uv run --no-sync pytest" in admission["run"]
        assert "tests/test_ci/test_webui_browser_suites.py" in admission["run"]
    run = _step(capacity, "Run production-dist renderer capacity contracts")
    assert "if" not in run
    assert not run.get("continue-on-error")
    assert run["working-directory"] == "opensquilla-webui"
    for flag in ("--grep=@capacity", "--workers=1", "--retries=0"):
        assert flag in run["run"]
    selected = {arg for arg in run["run"].split() if arg.endswith(".spec.ts")}
    marked = {
        path.name for path in Path("opensquilla-webui/e2e").glob("*.spec.ts")
        if re.search(r"\btag:[^\r\n]*['\"]@capacity['\"]", path.read_text(encoding="utf-8"))
    }
    assert selected == marked == set(specs_for_suite("capacity"))
    assert selected <= set(specs_for_suite("virtualization"))


def test_browser_fanout_preserves_worker_retry_filters_and_failure_evidence() -> None:
    jobs = _jobs()
    job = jobs["webui-chat-recovery"]
    general = _step(job, "Run production-dist chat and Goal recovery browser contracts")
    assert general["if"] == "${{ startsWith(matrix.suite, 'general-') }}"
    assert "--workers=2" in general["run"]
    assert "webui_browser_suites.py" in general["run"]
    assert "set -euo pipefail" in general["run"]
    clipboard = _step(job, "Run production-dist message clipboard contracts serially")
    assert clipboard["if"] == "${{ matrix.suite == 'clipboard' }}"
    for flag in (
        "test:e2e:message-copy", "--workers=1", "--retries=0", "--trace=retain-on-failure",
    ):
        assert flag in clipboard["run"]
    virtualization = _step(job, "Run production-dist long-list virtualization contracts")
    assert virtualization["if"] == "${{ matrix.suite == 'virtualization' }}"
    for flag in ("--grep-invert=@capacity", "--workers=1", "--retries=0"):
        assert flag in virtualization["run"]
    assert set(specs_for_suite("virtualization")) == {
        "chat-virtualization.spec.ts", "conversation-minimap.spec.ts",
        "floating-composer.spec.ts", "long-task-resilience.spec.ts",
        "native-gateway.spec.ts", "sidebar-drag.spec.ts",
        "sidebar-hover-geometry.spec.ts", "sidebar-virtualization.spec.ts",
    }
    upload = _step(job, "Upload chat recovery evidence")
    assert upload["if"] == "${{ failure() }}"
    assert "matrix.suite" in upload["with"]["name"]
    metrics = _step(jobs["webui-capacity"], "Upload chat recovery evidence")
    assert "steps.webui-capacity.outcome == 'success'" in metrics["if"]
    assert "long-task-performance.json" in metrics["with"]["path"]
    assert "sidebar-performance.json" in metrics["with"]["path"]


def test_frontend_source_checks_start_early_and_unit_shards_keep_office_assets() -> None:
    jobs = _jobs()
    for job_id in ("frontend-check", "frontend-unit-tests", "gateway-contract-linux",
                   "gateway-contract-verification-linux", "gateway-contract-windows",
                   "gateway-contract-verification-windows"):
        assert jobs[job_id]["needs"] == ["plan-ci"]
    units = jobs["frontend-unit-tests"]
    assert units["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}}
    prepare = _step(units, "Prepare pinned offline Office preview assets")
    assert prepare["run"] == "npm run prepare:office-assets"
    run = _step(units, "Run frontend unit tests")
    assert run["run"] == "npm run test:unit -- --shard=${{ matrix.shard }}/4"
    assert jobs["wheel-webui-roundtrip"]["needs"] == ["plan-ci", "frontend-artifact"]
