from __future__ import annotations

import hashlib
import json
import runpy
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
    return plan_changes(paths, repo=tmp_path, config=suite_config)


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
    ("path", "group"),
    [
        ("tests/test_gateway/test_desktop_ownership.py", "ownership"),
        ("tests/test_gateway/test_rpc_sandbox_runtime.py", "ownership"),
        ("tests/test_gateway/test_rpc_workbench_resources.py", "workbench"),
    ],
)
def test_known_test_targets_retain_their_desktop_risk_domain(
    tmp_path: Path,
    suite_config: dict[str, Any],
    path: str,
    group: str,
) -> None:
    plan = _plan(tmp_path, suite_config, path)

    assert plan["full_fallback"] is False
    assert {cell[1] for cell in _matrix(plan) if cell[0] == "ubuntu-latest"} == {
        group
    }
    assert {
        "desktop-recovery-e2e",
        "macos-recovery",
        "python-targeted",
        "windows-high-risk",
    } <= set(plan["required_suites"])
    assert f"desktop_{group}_changed" in plan["reason_codes"]


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
    macos_shard = "profiles" if windows_shard == "profiles" else "ownership-workbench"
    assert _matrix(plan) == {
        ("macos-latest", macos_shard),
        ("ubuntu-latest", windows_shard),
        ("windows-latest", windows_shard),
    }
    assert reason in plan["reason_codes"]


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
        "tests/test_desktop/test_electron_startup_contract.py",
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
