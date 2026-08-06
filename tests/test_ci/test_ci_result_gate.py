from __future__ import annotations

import runpy
from typing import Any

GATE_MODULE: dict[str, Any] = runpy.run_path(
    ".github/scripts/check_ci_results.py", run_name="check_ci_results"
)
BOOLEAN_FLAGS: tuple[str, ...] = GATE_MODULE["BOOLEAN_FLAGS"]
check_ci_results = GATE_MODULE["check_ci_results"]


def _flag_env(name: str) -> str:
    return f"FLAG_{name.upper()}"


def _base_env() -> dict[str, str]:
    env = {
        "RESULT_CLASSIFY": "success",
        "RESULT_WORKFLOW_LINT": "success",
        "RESULT_README_LOCALE": "success",
        "RESULT_FRONTEND": "skipped",
        "RESULT_TUI": "skipped",
        "RESULT_DESKTOP": "skipped",
        "RESULT_UBUNTU": "skipped",
        "RESULT_UBUNTU_FULL": "skipped",
        "RESULT_WINDOWS_SMOKE": "skipped",
        "RESULT_WINDOWS_FULL": "skipped",
        "RESULT_MACOS_RECOVERY": "skipped",
        "RESULT_DESKTOP_RECOVERY_E2E": "skipped",
        "RESULT_RELEASE": "skipped",
        "RESULT_MANAGED_TOOLCHAIN_ARTIFACTS": "skipped",
    }
    env.update({_flag_env(name): "false" for name in BOOLEAN_FLAGS})
    env[_flag_env("docs_only")] = "true"
    return env


def _full_env() -> dict[str, str]:
    env = _base_env()
    env.update({_flag_env(name): "true" for name in BOOLEAN_FLAGS})
    env[_flag_env("docs_only")] = "false"
    for key in tuple(env):
        if key.startswith("RESULT_"):
            env[key] = "success"
    return env


def test_ci_result_gate_accepts_intentional_docs_only_skips() -> None:
    assert check_ci_results(_base_env()) == []


def test_ci_result_gate_accepts_complete_full_matrix() -> None:
    assert check_ci_results(_full_env()) == []


def test_ci_result_gate_rejects_missing_or_invalid_classifier_outputs() -> None:
    missing = _base_env()
    missing.pop(_flag_env("windows_full_required"))
    invalid = _base_env()
    invalid[_flag_env("python_changed")] = "yes"

    assert any("windows_full_required" in error for error in check_ci_results(missing))
    assert any("python_changed" in error for error in check_ci_results(invalid))


def test_ci_result_gate_rejects_required_windows_matrix_skip() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("runtime_changed")] = "true"
    env[_flag_env("python_changed")] = "true"
    env[_flag_env("platform_sensitive_changed")] = "true"
    env[_flag_env("windows_full_required")] = "true"
    env[_flag_env("build_wheel_required")] = "true"
    env["RESULT_UBUNTU"] = "success"
    env["RESULT_WINDOWS_SMOKE"] = "success"

    errors = check_ci_results(env)

    assert any("Windows high-risk matrix" in error and "skipped" in error for error in errors)


def test_ci_result_gate_requires_ubuntu_full_matrix_only_for_full_ci() -> None:
    targeted = _base_env()
    targeted[_flag_env("docs_only")] = "false"
    targeted[_flag_env("runtime_changed")] = "true"
    targeted[_flag_env("python_changed")] = "true"
    targeted[_flag_env("build_wheel_required")] = "true"
    targeted["RESULT_FRONTEND"] = "success"
    targeted["RESULT_UBUNTU"] = "success"
    targeted["RESULT_WINDOWS_SMOKE"] = "success"
    assert check_ci_results(targeted) == []

    for result in ("skipped", "failure", "cancelled", ""):
        env = _full_env()
        env["RESULT_UBUNTU_FULL"] = result

        errors = check_ci_results(env)

        assert any("Ubuntu full test matrix" in error for error in errors)


def test_ci_result_gate_requires_verified_frontend_for_wheel_builds() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("runtime_changed")] = "true"
    env[_flag_env("python_changed")] = "true"
    env[_flag_env("build_wheel_required")] = "true"
    env["RESULT_UBUNTU"] = "success"
    env["RESULT_WINDOWS_SMOKE"] = "success"

    errors = check_ci_results(env)

    assert any("Frontend build, tests, and artifact" in error for error in errors)


def test_ci_result_gate_requires_real_toolchain_artifacts_when_classified() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("toolchain_artifact_changed")] = "true"

    errors = check_ci_results(env)

    assert any("Managed Toolchain Artifact E2E" in error and "skipped" in error for error in errors)


def test_ci_result_gate_accepts_successful_real_toolchain_artifacts() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("toolchain_artifact_changed")] = "true"
    env["RESULT_MANAGED_TOOLCHAIN_ARTIFACTS"] = "success"

    assert check_ci_results(env) == []


def test_ci_result_gate_rejects_failed_cancelled_or_missing_real_artifacts() -> None:
    for result in ("failure", "cancelled", ""):
        env = _base_env()
        env[_flag_env("docs_only")] = "false"
        env[_flag_env("toolchain_artifact_changed")] = "true"
        env["RESULT_MANAGED_TOOLCHAIN_ARTIFACTS"] = result

        errors = check_ci_results(env)

        assert any("Managed Toolchain Artifact E2E" in error for error in errors)


def test_ci_result_gate_allows_toolchain_artifacts_to_skip_when_unrelated() -> None:
    assert check_ci_results(_base_env()) == []


def test_ci_result_gate_rejects_failure_cancellation_and_missing_results() -> None:
    for result in ("failure", "cancelled", ""):
        env = _full_env()
        env["RESULT_WINDOWS_FULL"] = result

        errors = check_ci_results(env)

        assert any("Windows high-risk matrix" in error for error in errors)


def test_ci_result_gate_requires_macos_recovery_for_platform_sensitive_changes() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("runtime_changed")] = "true"
    env[_flag_env("python_changed")] = "true"
    env[_flag_env("platform_sensitive_changed")] = "true"
    env[_flag_env("windows_full_required")] = "true"
    env[_flag_env("build_wheel_required")] = "true"
    env["RESULT_UBUNTU"] = "success"
    env["RESULT_WINDOWS_SMOKE"] = "success"
    env["RESULT_WINDOWS_FULL"] = "success"

    errors = check_ci_results(env)

    assert any(
        "macOS profile recovery and native no-replace tests" in error
        and "skipped" in error
        for error in errors
    )


def test_ci_result_gate_rejects_failed_or_missing_required_macos_recovery() -> None:
    for result in ("failure", "cancelled", ""):
        env = _full_env()
        env["RESULT_MACOS_RECOVERY"] = result

        errors = check_ci_results(env)

        assert any(
            "macOS profile recovery and native no-replace tests" in error
            for error in errors
        )


def test_ci_result_gate_requires_desktop_recovery_e2e_for_desktop_changes() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("runtime_changed")] = "true"
    env[_flag_env("desktop_changed")] = "true"
    env[_flag_env("platform_sensitive_changed")] = "true"
    env[_flag_env("windows_full_required")] = "true"
    env[_flag_env("build_wheel_required")] = "true"
    env["RESULT_DESKTOP"] = "success"
    env["RESULT_UBUNTU"] = "success"
    env["RESULT_WINDOWS_SMOKE"] = "success"
    env["RESULT_WINDOWS_FULL"] = "success"
    env["RESULT_MACOS_RECOVERY"] = "success"

    errors = check_ci_results(env)

    assert any("Desktop recovery E2E matrix" in error and "skipped" in error for error in errors)


def test_ci_result_gate_requires_desktop_recovery_e2e_for_frontend_changes() -> None:
    env = _base_env()
    env[_flag_env("docs_only")] = "false"
    env[_flag_env("runtime_changed")] = "true"
    env[_flag_env("frontend_changed")] = "true"
    env[_flag_env("python_changed")] = "true"
    env[_flag_env("build_wheel_required")] = "true"
    env["RESULT_FRONTEND"] = "success"
    env["RESULT_UBUNTU"] = "success"
    env["RESULT_WINDOWS_SMOKE"] = "success"

    errors = check_ci_results(env)

    assert any("Desktop recovery E2E matrix" in error and "skipped" in error for error in errors)


def test_ci_result_gate_rejects_failed_or_missing_desktop_recovery_e2e() -> None:
    for result in ("failure", "cancelled", ""):
        env = _full_env()
        env["RESULT_DESKTOP_RECOVERY_E2E"] = result

        errors = check_ci_results(env)

        assert any("Desktop recovery E2E matrix" in error for error in errors)


def test_ci_result_gate_rejects_inconsistent_full_and_platform_flags() -> None:
    incomplete_full = _full_env()
    incomplete_full[_flag_env("release_changed")] = "false"
    unsafe_platform = _base_env()
    unsafe_platform[_flag_env("docs_only")] = "false"
    unsafe_platform[_flag_env("platform_sensitive_changed")] = "true"

    assert any("release_changed=true" in error for error in check_ci_results(incomplete_full))
    assert any("Platform-sensitive" in error for error in check_ci_results(unsafe_platform))
