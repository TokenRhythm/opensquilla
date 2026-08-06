#!/usr/bin/env python3
"""Fail closed when required CI jobs or classifier outputs are incomplete."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Final

BOOLEAN_FLAGS: Final[tuple[str, ...]] = (
    "docs_only",
    "runtime_changed",
    "test_changed",
    "ci_changed",
    "dependency_changed",
    "release_changed",
    "windows_full_required",
    "frontend_changed",
    "tui_changed",
    "desktop_changed",
    "python_changed",
    "platform_sensitive_changed",
    "build_wheel_required",
    "toolchain_artifact_changed",
    "full_required",
)

ALWAYS_REQUIRED_RESULTS: Final[tuple[tuple[str, str], ...]] = (
    ("RESULT_CLASSIFY", "Classify changed files"),
    ("RESULT_WORKFLOW_LINT", "Workflow lint"),
    ("RESULT_README_LOCALE", "README locale parity"),
)


def _flag_env(name: str) -> str:
    return f"FLAG_{name.upper()}"


def _read_flags(env: Mapping[str, str], errors: list[str]) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for name in BOOLEAN_FLAGS:
        raw = env.get(_flag_env(name), "")
        if raw not in {"true", "false"}:
            errors.append(f"Classifier output {name} must be exactly true or false; got {raw!r}.")
            continue
        flags[name] = raw == "true"
    return flags


def _require_result(
    env: Mapping[str, str],
    errors: list[str],
    variable: str,
    label: str,
    *,
    required: bool,
) -> None:
    result = env.get(variable, "")
    allowed = {"success"} if required else {"success", "skipped"}
    if result not in allowed:
        expectation = "succeed" if required else "be successful or intentionally skipped"
        errors.append(f"{label} must {expectation}; got {result or 'missing'}.")


def check_ci_results(env: Mapping[str, str]) -> list[str]:
    """Return gate errors; an empty list means the aggregate check may pass."""

    errors: list[str] = []
    flags = _read_flags(env, errors)

    for variable, label in ALWAYS_REQUIRED_RESULTS:
        _require_result(env, errors, variable, label, required=True)

    if len(flags) != len(BOOLEAN_FLAGS):
        return errors

    full = flags["full_required"]
    conditional_results = (
        (
            "RESULT_FRONTEND",
            "Frontend build, tests, and artifact",
            flags["frontend_changed"]
            or flags["build_wheel_required"]
            or flags["platform_sensitive_changed"]
            or flags["desktop_changed"]
            or full,
        ),
        ("RESULT_TUI", "OpenTUI package tests", flags["tui_changed"] or full),
        ("RESULT_DESKTOP", "Desktop Electron unit tests", flags["desktop_changed"] or full),
        ("RESULT_UBUNTU", "Ubuntu quality gate", flags["python_changed"] or full),
        ("RESULT_UBUNTU_FULL", "Ubuntu full test matrix", full),
        (
            "RESULT_WINDOWS_SMOKE",
            "Windows compatibility smoke tests",
            flags["python_changed"]
            or flags["platform_sensitive_changed"]
            or flags["dependency_changed"]
            or flags["release_changed"]
            or full,
        ),
        (
            "RESULT_WINDOWS_FULL",
            "Windows high-risk matrix",
            flags["windows_full_required"] or full,
        ),
        (
            "RESULT_MACOS_RECOVERY",
            "macOS profile recovery and native no-replace tests",
            flags["platform_sensitive_changed"] or flags["desktop_changed"] or full,
        ),
        (
            "RESULT_DESKTOP_RECOVERY_E2E",
            "Desktop recovery E2E matrix",
            flags["frontend_changed"]
            or flags["platform_sensitive_changed"]
            or flags["desktop_changed"]
            or full,
        ),
        (
            "RESULT_RELEASE",
            "Release packaging contracts",
            flags["release_changed"] or full,
        ),
        (
            "RESULT_MANAGED_TOOLCHAIN_ARTIFACTS",
            "Managed Toolchain Artifact E2E",
            flags["toolchain_artifact_changed"] or full,
        ),
    )
    for variable, label, required in conditional_results:
        _require_result(env, errors, variable, label, required=required)

    if flags["platform_sensitive_changed"] and not flags["windows_full_required"]:
        errors.append("Platform-sensitive changes must require the Windows high-risk matrix.")

    if full:
        if flags["docs_only"]:
            errors.append("A full CI run cannot be classified as docs-only.")
        for name in BOOLEAN_FLAGS:
            if name in {"docs_only", "full_required"}:
                continue
            if not flags[name]:
                errors.append(f"Full CI classification must set {name}=true.")

    if flags["docs_only"]:
        active = [
            name
            for name in BOOLEAN_FLAGS
            if name != "docs_only" and flags[name]
        ]
        if active:
            errors.append(
                "Docs-only classification cannot enable other flags: " + ", ".join(active)
            )

    return errors


def main() -> int:
    errors = check_ci_results(os.environ)
    for variable, label in ALWAYS_REQUIRED_RESULTS:
        print(f"{label}: {os.environ.get(variable, 'missing')}")
    print(
        "Classifier flags: "
        + " ".join(
            f"{name}={os.environ.get(_flag_env(name), 'missing')}" for name in BOOLEAN_FLAGS
        )
    )
    if not errors:
        print("All required CI results are complete and successful.")
        return 0
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
