from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest

MODULE: dict[str, Any] = runpy.run_path(
    ".github/scripts/plan_ci.py", run_name="ci_suite_planner"
)
PlanError = MODULE["PlanError"]
canonical_json = MODULE["canonical_json"]
load_config = MODULE["load_config"]
plan_changes = MODULE["plan_changes"]

CONFIG_PATH = Path(".github/ci/suites.v1.json")


@pytest.fixture
def suite_config() -> dict[str, Any]:
    return load_config(CONFIG_PATH, repo=Path.cwd())


def _plan(
    tmp_path: Path, suite_config: dict[str, Any], *paths: str
) -> dict[str, Any]:
    for relative in paths:
        candidate = Path(relative)
        if (
            relative.startswith("tests/")
            and candidate.name.startswith("test_")
            and candidate.suffix == ".py"
        ):
            test_path = tmp_path / candidate
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.touch()
    return plan_changes(paths, repo=tmp_path, config=suite_config)


def _write_test_module(root: Path, path: str, source: str = "") -> None:
    candidate = root / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(source, encoding="utf-8")


def _matrix(plan: dict[str, Any]) -> set[tuple[str, str]]:
    return {(cell["os"], cell["shard"]) for cell in plan["desktop_matrix"]}


def _platform_cells(plan: dict[str, Any], suite: str) -> set[tuple[str, str]]:
    return {
        (cell["os"], cell["shard"])
        for cell in plan["platform_matrix"]
        if cell["suite"] == suite
    }


def test_docs_only_plan_is_small_and_canonical(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "README.zh-Hans.md", "docs/ci.md")

    assert plan["required_suites"] == ["readme-locale", "workflow-lint"]
    assert plan["desktop_matrix"] == []
    assert plan["python_matrix"] == {"ubuntu": [], "windows": []}
    assert _platform_cells(plan, "readme-locale") == {
        ("ubuntu-latest", "default")
    }
    assert plan["python_targets"] == []
    assert plan["full_fallback"] is False
    assert plan["reason_codes"] == ["docs_only"]
    assert set(plan["suite_execution_digests"]) == set(plan["required_suites"])
    assert json.loads(canonical_json(plan)) == plan
    assert " " not in canonical_json(plan)


def test_plan_and_digest_are_order_independent(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    paths = ["src/opensquilla/provider/openai.py", "docs/providers.md"]

    first = _plan(tmp_path, suite_config, *paths)
    second = _plan(tmp_path, suite_config, *reversed(paths), paths[0])

    assert first == second
    without_digest = {key: value for key, value in first.items() if key != "plan_digest"}
    expected = hashlib.sha256(
        json.dumps(
            without_digest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    assert first["plan_digest"] == expected


def test_ordinary_python_change_selects_targets_without_full_fallback(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "src/opensquilla/provider/openai.py")

    assert plan["full_fallback"] is False
    assert "python-targeted" in plan["required_suites"]
    assert "windows-compat" not in plan["required_suites"]
    assert plan["python_targets"] == [
        "tests/test_*router*.py",
        "tests/test_cross_provider_tiers.py",
        "tests/test_provider",
        "tests/test_provider*.py",
    ]
    assert plan["reason_codes"] == ["python_targeted"]


def test_pr_1347_test_only_change_uses_exact_targets_and_windows_shards(
    suite_config: dict[str, Any],
) -> None:
    paths = [
        "tests/test_gateway/test_rpc_sessions.py",
        "tests/test_live_artifact_prompt_annotations_e2e.py",
        "tests/test_recovery/test_recovery_cmd.py",
    ]
    importing_consumer = "tests/test_gateway/test_p1a_exact_abort_contract.py"

    plan = plan_changes(paths, repo=Path.cwd(), config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == sorted([*paths, importing_consumer])
    assert plan["python_matrix"] == {
        "ubuntu": [],
        "windows": [
            "desktop-installer-contracts",
            "gateway-sqlite",
            "recovery-migration",
        ],
    }
    assert plan["desktop_matrix"] == []
    assert set(plan["required_suites"]) == {
        "macos-recovery",
        "python-targeted",
        "readme-locale",
        "windows-high-risk",
        "workflow-lint",
    }
    assert plan["reason_codes"] == [
        "macos_recovery_test_changed",
        "test_dependency_closure",
        "test_only_targeted",
    ]


def test_deleted_governed_test_uses_existing_parent_and_keeps_windows_shard(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    path = "tests/test_gateway/test_rpc_sessions.py"
    (tmp_path / "tests/test_gateway").mkdir(parents=True)

    plan = plan_changes([path], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == ["tests/test_gateway"]
    assert plan["python_matrix"]["windows"] == ["gateway-sqlite"]
    assert "deleted_test_targeted" in plan["reason_codes"]


def test_governed_test_rename_targets_old_parent_and_new_exact_file(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    old_path = "tests/test_gateway/test_rpc_sessions.py"
    new_path = "tests/test_gateway/test_rpc_sessions_fork.py"
    new_test = tmp_path / new_path
    new_test.parent.mkdir(parents=True)
    new_test.touch()

    plan = plan_changes([old_path, new_path], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == ["tests/test_gateway", new_path]
    assert plan["python_matrix"]["windows"] == ["gateway-sqlite"]
    assert "deleted_test_targeted" in plan["reason_codes"]


def test_cross_shard_test_helper_adds_importing_consumer_and_shard(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/test_skills_hash_consumers.py"
    _write_test_module(tmp_path, helper)
    _write_test_module(
        tmp_path,
        consumer,
        "from tests.test_skills.test_hub_management_service import FakeSource\n",
    )

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == [helper, consumer]
    assert plan["python_matrix"]["windows"] == ["core", "recovery-migration"]
    assert "test_dependency_closure" in plan["reason_codes"]


@pytest.mark.parametrize(
    "consumer_source",
    [
        (
            "from importlib import import_module as load_module\n"
            "helper = load_module('tests.test_skills.test_hub_management_service')\n"
        ),
        (
            "import importlib as loader\n"
            "helper = loader.import_module("
            "'tests.test_skills.test_hub_management_service')\n"
        ),
    ],
)
def test_dynamic_import_alias_adds_cross_shard_consumer(
    tmp_path: Path,
    suite_config: dict[str, Any],
    consumer_source: str,
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/test_skills_hash_consumers.py"
    _write_test_module(tmp_path, helper)
    _write_test_module(tmp_path, consumer, consumer_source)

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == [helper, consumer]
    assert plan["python_matrix"]["windows"] == ["core", "recovery-migration"]
    assert "test_dependency_closure" in plan["reason_codes"]


def test_pytest_plugins_adds_cross_shard_consumer(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/test_skills_hash_consumers.py"
    _write_test_module(tmp_path, helper)
    _write_test_module(
        tmp_path,
        consumer,
        "pytest_plugins = ['tests.test_skills.test_hub_management_service']\n",
    )

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == [helper, consumer]
    assert plan["python_matrix"]["windows"] == ["core", "recovery-migration"]


@pytest.mark.parametrize(
    "consumer_source",
    [
        (
            "from importlib import import_module\n"
            "helper = import_module(f'tests.test_skills.{module_name}')\n"
        ),
        "pytest_plugins = plugin_modules\n",
    ],
)
def test_uncertain_dynamic_test_loader_fails_closed(
    tmp_path: Path,
    suite_config: dict[str, Any],
    consumer_source: str,
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/test_skills_hash_consumers.py"
    _write_test_module(tmp_path, helper)
    _write_test_module(tmp_path, consumer, consumer_source)

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is True
    assert "test_dependency_analysis_uncertain" in plan["reason_codes"]


def test_test_helper_dependency_closure_is_recursive_and_cycle_safe(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    core = "tests/test_skills/test_hub_management_service.py"
    recovery = "tests/test_skills_hash_consumers.py"
    desktop = "tests/test_engine/test_runtime_meta_invoke_surfacing.py"
    _write_test_module(
        tmp_path,
        core,
        "from tests.test_engine.test_runtime_meta_invoke_surfacing import DesktopHelper\n",
    )
    _write_test_module(
        tmp_path,
        recovery,
        "from tests.test_skills.test_hub_management_service import CoreHelper\n",
    )
    _write_test_module(
        tmp_path,
        desktop,
        "from tests.test_skills_hash_consumers import RecoveryHelper\n",
    )

    plan = plan_changes([core], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == sorted([core, recovery, desktop])
    assert plan["python_matrix"]["windows"] == [
        "core",
        "desktop-installer-contracts",
        "recovery-migration",
    ]
    assert "test_dependency_closure" in plan["reason_codes"]


def test_ungoverned_test_module_consumer_fails_closed(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/unknown/test_helper_consumer.py"
    _write_test_module(tmp_path, helper)
    _write_test_module(
        tmp_path,
        consumer,
        "from tests.test_skills.test_hub_management_service import FakeSource\n",
    )

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is True
    assert plan["python_targets"] == ["tests"]
    assert "test_dependency_ungoverned" in plan["reason_codes"]
    assert "test_dependency_unsafe" in plan["reason_codes"]


def test_uncertain_test_module_parse_fails_closed(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    _write_test_module(tmp_path, helper, "def broken(:\n")

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is True
    assert plan["python_targets"] == ["tests"]
    assert "test_dependency_analysis_uncertain" in plan["reason_codes"]


def test_deleted_test_helper_keeps_cross_directory_consumer(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    helper = "tests/test_skills/test_hub_management_service.py"
    consumer = "tests/test_skills_hash_consumers.py"
    (tmp_path / "tests/test_skills").mkdir(parents=True)
    _write_test_module(
        tmp_path,
        consumer,
        "from tests.test_skills.test_hub_management_service import FakeSource\n",
    )

    plan = plan_changes([helper], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == ["tests/test_skills", consumer]
    assert plan["python_matrix"]["windows"] == ["core", "recovery-migration"]
    assert "deleted_test_targeted" in plan["reason_codes"]
    assert "test_dependency_closure" in plan["reason_codes"]


def test_deleted_governed_test_at_ref_uses_parent_tree(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    repo = tmp_path / "repo"
    test_dir = repo / "tests/test_gateway"
    test_dir.mkdir(parents=True)
    deleted_path = test_dir / "test_rpc_sessions.py"
    deleted_path.touch()
    (test_dir / "test_retained.py").touch()
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "CI Test"),
        ("config", "user.email", "ci@example.invalid"),
        ("add", "."),
        ("commit", "-m", "add tests"),
    ):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    deleted_path.unlink()
    subprocess.run(
        ["git", "add", "-u"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "delete test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    plan = plan_changes(
        ["tests/test_gateway/test_rpc_sessions.py"],
        repo=repo,
        config=suite_config,
        ref="HEAD",
    )

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == ["tests/test_gateway"]
    assert plan["python_matrix"]["windows"] == ["gateway-sqlite"]
    assert "deleted_test_targeted" in plan["reason_codes"]


def test_deleted_unregistered_test_still_fails_closed(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    path = "tests/unknown/test_removed_contract.py"
    (tmp_path / "tests/unknown").mkdir(parents=True)

    plan = plan_changes([path], repo=tmp_path, config=suite_config)

    assert plan["full_fallback"] is True
    assert "unknown_path" in plan["reason_codes"]


def test_shared_python_core_requests_complete_offline_python_only(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "src/opensquilla/engine/runtime.py")

    assert plan["full_fallback"] is False
    assert "python-full" in plan["required_suites"]
    assert "python-targeted" not in plan["required_suites"]
    assert "windows-compat" not in plan["required_suites"]
    assert plan["python_targets"] == ["tests"]
    assert plan["python_matrix"]["ubuntu"] == suite_config["full_python_matrix"][
        "ubuntu"
    ]
    assert plan["python_matrix"]["windows"] == []
    assert _platform_cells(plan, "python-full") == {
        ("ubuntu-latest", shard)
        for shard in suite_config["full_python_matrix"]["ubuntu"]
    }
    assert plan["reason_codes"] == ["python_shared_core"]


def test_generic_webui_change_does_not_wake_desktop_matrix(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "opensquilla-webui/src/views/SettingsView.vue")

    assert plan["full_fallback"] is False
    assert {"frontend", "webui-chat-recovery"} <= set(plan["required_suites"])
    assert "desktop-recovery-e2e" not in plan["required_suites"]
    assert plan["desktop_matrix"] == []
    assert plan["reason_codes"] == ["webui_changed"]


def test_gateway_change_runs_browser_recovery_without_native_desktop(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "src/opensquilla/gateway/app.py")

    assert {"frontend", "python-targeted", "webui-chat-recovery"} <= set(
        plan["required_suites"]
    )
    assert "desktop-recovery-e2e" not in plan["required_suites"]
    assert plan["desktop_matrix"] == []


def test_nested_opentui_source_selects_tui_suite_before_generic_python() -> None:
    config = load_config(CONFIG_PATH, repo=Path.cwd())
    plan = plan_changes(
        ["src/opensquilla/cli/tui/opentui/package/src/composer.mjs"],
        repo=Path.cwd(),
        config=config,
    )

    assert "tui" in plan["required_suites"]
    assert "python-targeted" not in plan["required_suites"]
    assert plan["reason_codes"] == ["tui_changed"]


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("src/opensquilla/session/store.py", "profiles"),
        ("src/opensquilla/process_tree.py", "ownership"),
        ("src/opensquilla/gateway/process_lifecycle.py", "ownership"),
        ("src/opensquilla/artifact_editor.py", "workbench"),
    ],
)
def test_python_native_risk_domains_select_only_corresponding_desktop_group(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
    group: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert {cell[1] for cell in _matrix(plan) if cell[0] == "ubuntu-latest"} == {group}
    if group == "profiles":
        assert "webui-chat-recovery" in plan["required_suites"]
    else:
        assert "webui-chat-recovery" not in plan["required_suites"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_gateway/test_desktop_ownership.py",
        "tests/test_gateway/test_rpc_sandbox_runtime.py",
        "tests/test_gateway/test_rpc_workbench_resources.py",
    ],
)
def test_test_only_domain_words_do_not_wake_native_desktop_e2e(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == [path]
    assert len(plan["python_matrix"]["windows"]) == 1
    assert plan["desktop_matrix"] == []
    assert "desktop-recovery-e2e" not in plan["required_suites"]
    assert "macos-recovery" not in plan["required_suites"]
    assert "test_only_targeted" in plan["reason_codes"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_recovery/test_atomic_and_locking.py",
        "tests/test_recovery/test_engine.py",
    ],
)
def test_darwin_recovery_test_keeps_macos_python_without_native_e2e(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert plan["python_targets"] == [path]
    assert "macos-recovery" in plan["required_suites"]
    assert _platform_cells(plan, "macos-recovery") == {
        ("macos-latest", "recovery")
    }
    assert "desktop-recovery-e2e" not in plan["required_suites"]
    assert plan["desktop_matrix"] == []
    assert "macos_recovery_test_changed" in plan["reason_codes"]


def test_macos_recovery_test_routing_is_covered_by_suite_digest(
    suite_config: dict[str, Any],
) -> None:
    assert set(suite_config["macos_recovery_test_inputs"]) <= set(
        suite_config["suites"]["macos-recovery"]["execution_inputs"]
    )


def test_macos_recovery_test_routing_without_digest_coverage_is_rejected(
    tmp_path: Path,
) -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["suites"]["macos-recovery"]["execution_inputs"].remove(
        "tests/test_recovery/**"
    )
    config_path = tmp_path / "suites.v1.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(PlanError, match="must be covered by the macos-recovery"):
        load_config(config_path)


def test_explicit_test_path_pattern_can_select_native_desktop_e2e(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    path = "tests/test_gateway/test_rpc_workbench_resources.py"
    suite_config["desktop_groups"]["workbench"]["path_patterns"].append(path)

    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert _matrix(plan) == {
        ("macos-latest", "workbench"),
        ("ubuntu-latest", "workbench"),
        ("windows-latest", "workbench"),
    }
    assert "desktop-recovery-e2e" in plan["required_suites"]
    assert "desktop_workbench_test_changed" in plan["reason_codes"]


@pytest.mark.parametrize(
    ("path", "windows_shard", "reason"),
    [
        (
            "desktop/electron/scripts/test-profile-import-flow.mjs",
            "profiles",
            "desktop_profiles_changed",
        ),
        (
            "desktop/electron/src/gateway-ownership.ts",
            "ownership",
            "desktop_ownership_changed",
        ),
        (
            "desktop/electron/src/native-workbench-surface.ts",
            "workbench",
            "desktop_workbench_changed",
        ),
    ],
)
def test_desktop_domain_selects_only_its_windows_shard(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
    windows_shard: str,
    reason: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert _matrix(plan) == {
        ("macos-latest", windows_shard),
        ("ubuntu-latest", windows_shard),
        ("windows-latest", windows_shard),
    }
    assert reason in plan["reason_codes"]


def test_full_desktop_matrix_keeps_macos_ownership_and_workbench_isolated(
    suite_config: dict[str, Any],
) -> None:
    macos_cells = {
        cell["shard"]
        for cell in suite_config["full_desktop_matrix"]
        if cell["os"] == "macos-latest"
    }

    assert macos_cells == {"profiles", "ownership", "workbench"}
    assert "ownership-workbench" not in macos_cells


def test_windows_specific_platform_change_stays_on_windows(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    plan = _plan(tmp_path, suite_config, "src/opensquilla/sandbox/windows_backend.py")

    assert plan["full_fallback"] is False
    assert "windows-high-risk" in plan["required_suites"]
    assert "macos-recovery" not in plan["required_suites"]
    assert _matrix(plan) == {("windows-latest", "ownership")}
    assert "windows_specific_changed" in plan["reason_codes"]


def test_toolchain_and_packaging_changes_select_dedicated_suites(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    toolchain = _plan(
        tmp_path, suite_config, "src/opensquilla/skills/toolchains/ffmpeg.py"
    )
    packaging = _plan(tmp_path, suite_config, "scripts/build_wheelhouse_zip.py")

    assert toolchain["full_fallback"] is False
    assert "managed-toolchain" in toolchain["required_suites"]
    assert "toolchain_changed" in toolchain["reason_codes"]
    assert packaging["full_fallback"] is False
    assert "release-packaging" in packaging["required_suites"]
    assert packaging["reason_codes"] == ["packaging_changed"]


@pytest.mark.parametrize(
    ("path", "domain_targets"),
    [
        (
            "src/opensquilla/skills/bundled/meta-paper-write/SKILL.md",
            {
                "tests/test_skills/test_meta_paper*.py",
                "tests/test_skills/test_paper_*.py",
            },
        ),
        (
            "src/opensquilla/skills/bundled/paper-quality-gate/scripts/audit.py",
            {
                "tests/test_skills/test_meta_paper*.py",
                "tests/test_skills/test_paper_*.py",
            },
        ),
        (
            "src/opensquilla/skills/bundled/meta-short-drama/SKILL.md",
            {"tests/test_skills/test_meta_short_drama*.py"},
        ),
        (
            "src/opensquilla/skills/bundled/subtitle-burner/scripts/burn.py",
            {"tests/test_skills/test_subtitle_burner.py"},
        ),
        (
            "src/opensquilla/skills/bundled/video-still-animator/scripts/animate.py",
            set(),
        ),
    ],
)
def test_bundled_managed_toolchain_domains_select_artifact_and_targeted_tests(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
    domain_targets: set[str],
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert {"managed-toolchain", "python-targeted", "windows-high-risk"} <= set(
        plan["required_suites"]
    )
    assert {
        "tests/test_skills/test_managed_toolchains.py",
        "tests/test_skills/test_toolchain_runtime_integration.py",
        "tests/test_skills/test_toolchain_state_scope.py",
        *domain_targets,
    } <= set(plan["python_targets"])
    assert "toolchain_changed" in plan["reason_codes"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_skills/test_managed_toolchains.py",
        "tests/test_skills/test_toolchain_runtime_integration.py",
        "tests/test_skills/test_meta_paper_write_e2e.py",
        "tests/test_skills/test_paper_quality_gate.py",
        "tests/test_skills/test_meta_short_drama_delivery_audit.py",
        "tests/test_skills/test_subtitle_burner.py",
    ],
)
def test_managed_toolchain_domain_tests_retain_the_artifact_e2e_suite(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert "managed-toolchain" in plan["required_suites"]
    assert path in plan["python_targets"]
    assert "toolchain_changed" in plan["reason_codes"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".github/workflows/ci.yml", "ci_policy_changed"),
        ("uv.lock", "dependency_changed"),
        ("new-product-surface/config.bin", "unknown_path"),
        ("tests/unknown/test_workbench.py", "unknown_path"),
    ],
)
def test_high_risk_changes_fail_closed_to_full_plan(
    tmp_path: Path, suite_config: dict[str, Any], path: str, reason: str
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is True
    assert plan["required_suites"] == sorted(suite_config["full_suites"])
    assert plan["python_targets"] == ["tests"]
    assert _matrix(plan) == {
        (cell["os"], cell["shard"])
        for cell in suite_config["full_desktop_matrix"]
    }
    assert plan["python_matrix"] == suite_config["full_python_matrix"]
    assert _platform_cells(plan, "windows-high-risk") == {
        ("windows-latest", shard)
        for shard in suite_config["full_python_matrix"]["windows"]
    }
    assert reason in plan["reason_codes"]


def test_windows_shard_metadata_does_not_invalidate_unrelated_suites(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    durations = _plan(
        tmp_path, suite_config, ".github/scripts/windows_test_durations.json"
    )
    assignments = _plan(
        tmp_path, suite_config, ".github/scripts/windows_test_assignments.json"
    )

    assert durations["full_fallback"] is False
    assert "python-targeted" in durations["required_suites"]
    assert durations["python_targets"] == ["tests/test_ci/test_windows_test_shards.py"]
    assert "scheduling_metadata_changed" in durations["reason_codes"]
    assert assignments["full_fallback"] is False
    assert "python-targeted" in assignments["required_suites"]
    assert "windows-high-risk" in assignments["required_suites"]
    assert "windows_shard_layout_changed" in assignments["reason_codes"]


@pytest.mark.parametrize(
    ("path", "expected_group"),
    [
        ("desktop/electron/scripts/test-profile-import-flow.mjs", "profiles"),
        (
            "desktop/electron/scripts/test-desktop-gateway-orphan-recovery-flow.mjs",
            "ownership",
        ),
        ("desktop/electron/scripts/test-unsafe-legacy-recovery-no-write.mjs", "workbench"),
    ],
)
def test_desktop_case_manifest_routes_each_known_case_to_its_executing_group(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
    expected_group: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert ("ubuntu-latest", expected_group) in _matrix(plan)
    assert not {
        cell
        for cell in _matrix(plan)
        if cell[0] == "ubuntu-latest" and cell[1] != expected_group
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/opensquilla/recovery/restore.py",
        "migrations/0001.sql",
    ],
)
def test_frontend_artifact_consumer_plan_always_includes_its_producer(
    tmp_path: Path, suite_config: dict[str, Any], path: str
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert "desktop-recovery-e2e" in plan["required_suites"]
    assert "frontend" in plan["required_suites"]


@pytest.mark.parametrize(
    ("paths", "reason"),
    [([], "empty_change_set"), (["../outside.py"], "invalid_changed_path")],
)
def test_missing_or_invalid_change_sets_fail_closed(
    tmp_path: Path,
    suite_config: dict[str, Any],
    paths: list[str],
    reason: str,
) -> None:
    plan = _plan(tmp_path, suite_config, *paths)

    assert plan["full_fallback"] is True
    assert reason in plan["reason_codes"]


def test_suite_execution_digest_tracks_matching_file_content(
    tmp_path: Path, suite_config: dict[str, Any]
) -> None:
    source = tmp_path / "src/opensquilla/provider/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = _plan(tmp_path, suite_config, source.relative_to(tmp_path).as_posix())

    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = _plan(tmp_path, suite_config, source.relative_to(tmp_path).as_posix())

    assert (
        first["suite_execution_digests"]["python-targeted"]
        != second["suite_execution_digests"]["python-targeted"]
    )
    assert first["plan_digest"] != second["plan_digest"]


def test_config_rejects_unknown_full_suite(tmp_path: Path) -> None:
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    value["full_suites"].append("missing-suite")
    path = tmp_path / "suites.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PlanError, match="unknown suites"):
        load_config(path)


def test_readme_locale_inputs_cover_the_executed_node_contract(
    suite_config: dict[str, Any],
) -> None:
    inputs = set(suite_config["suites"]["readme-locale"]["execution_inputs"])

    assert {
        "CONTRIBUTING.md",
        "README*.md",
        "RELEASES.md",
        "desktop/electron/README.md",
        "docs/README.md",
        "docs/quickstart.md",
        "docs/web-ui.md",
        "opensquilla-webui/.node-version",
        "opensquilla-webui/package.json",
        "opensquilla-webui/scripts/check-readme-locales.mjs",
        "opensquilla-webui/src/components/LanguageSwitcher.vue",
        "opensquilla-webui/src/i18n/index.ts",
    } <= inputs
    assert "scripts/check_readme_locale_parity.py" not in inputs


def test_managed_toolchain_inputs_cover_bundled_consumers_and_tests(
    suite_config: dict[str, Any],
) -> None:
    inputs = set(suite_config["suites"]["managed-toolchain"]["execution_inputs"])

    assert {
        "src/opensquilla/skills/bundled/meta-paper-write/**",
        "src/opensquilla/skills/bundled/meta-short-drama/**",
        "src/opensquilla/skills/bundled/paper-*/**",
        "src/opensquilla/skills/bundled/subtitle-burner/**",
        "src/opensquilla/skills/bundled/video-still-animator/**",
        "tests/test_skills/test_meta_paper*.py",
        "tests/test_skills/test_meta_short_drama*.py",
        "tests/test_skills/test_paper_*.py",
        "tests/test_skills/test_subtitle_burner.py",
    } <= inputs


@pytest.mark.parametrize(
    "missing_pattern",
    ["missing-ci-input.txt", "missing-ci-inputs/**", "missing-ci-inputs/*.json"],
)
def test_config_rejects_execution_input_patterns_without_repository_matches(
    tmp_path: Path,
    missing_pattern: str,
) -> None:
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    value["suites"]["readme-locale"]["execution_inputs"].append(missing_pattern)
    path = tmp_path / "suites.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        PlanError,
        match=r"execution_inputs match no repository files: .*missing-ci-input",
    ):
        load_config(path, repo=Path.cwd())


def test_config_accepts_repository_wide_recursive_wildcard(
    tmp_path: Path,
) -> None:
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    value["suites"]["python-full"]["execution_inputs"] = ["**"]
    path = tmp_path / "suites.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    loaded = load_config(path, repo=Path.cwd())

    assert loaded["suites"]["python-full"]["execution_inputs"] == ["**"]
