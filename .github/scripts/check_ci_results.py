#!/usr/bin/env python3
"""Fail closed when required CI jobs or classifier outputs are incomplete."""

from __future__ import annotations

import json
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
    "python_full_required",
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
KNOWN_SUITES: Final[frozenset[str]] = frozenset(
    {
        "desktop-recovery-e2e",
        "desktop-static",
        "frontend",
        "macos-recovery",
        "managed-toolchain",
        "python-full",
        "python-targeted",
        "readme-locale",
        "release-packaging",
        "tui",
        "webui-chat-recovery",
        "windows-compat",
        "windows-high-risk",
        "workflow-lint",
    }
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


def _read_required_suites(env: Mapping[str, str], errors: list[str]) -> set[str] | None:
    raw = env.get("REQUIRED_SUITES")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        errors.append("Suite planner output required_suites must be valid JSON.")
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append("Suite planner output required_suites must be a list of strings.")
        return set()
    if value != sorted(set(value)):
        errors.append("Suite planner output required_suites must be sorted and duplicate-free.")
        return set()
    unknown = sorted(set(value) - KNOWN_SUITES)
    if unknown:
        errors.append("Suite planner selected unknown suites: " + ", ".join(unknown))
    return set(value)


def check_ci_results(env: Mapping[str, str]) -> list[str]:
    """Return gate errors; an empty list means the aggregate check may pass."""

    errors: list[str] = []
    flags = _read_flags(env, errors)
    required_suites = _read_required_suites(env, errors)

    for variable, label in ALWAYS_REQUIRED_RESULTS:
        _require_result(env, errors, variable, label, required=True)

    if len(flags) != len(BOOLEAN_FLAGS):
        return errors

    full = flags["full_required"]
    windows_full_required = flags["windows_full_required"] or full
    windows_smoke_relevant = (
        flags["python_changed"]
        or flags["platform_sensitive_changed"]
        or flags["dependency_changed"]
        or flags["release_changed"]
    )
    if required_suites is None:
        suite_required = {
            "frontend": (
                flags["frontend_changed"]
                or flags["build_wheel_required"]
                or flags["platform_sensitive_changed"]
                or flags["desktop_changed"]
                or full
            ),
            "tui": flags["tui_changed"] or full,
            "desktop-static": flags["desktop_changed"] or full,
            "python-targeted": flags["python_changed"] or full,
            "python-full": flags["python_full_required"] or full,
            "windows-compat": windows_smoke_relevant and not windows_full_required,
            "windows-high-risk": windows_full_required,
            "macos-recovery": (
                flags["platform_sensitive_changed"] or flags["desktop_changed"] or full
            ),
            "desktop-recovery-e2e": (
                flags["platform_sensitive_changed"] or flags["desktop_changed"] or full
            ),
            "webui-chat-recovery": (
                flags["frontend_changed"] or flags["platform_sensitive_changed"] or full
            ),
            "release-packaging": flags["release_changed"] or full,
            "managed-toolchain": flags["toolchain_artifact_changed"] or full,
        }
    else:
        suite_required = {suite: suite in required_suites for suite in KNOWN_SUITES}
    conditional_results = (
        (
            "RESULT_FRONTEND",
            "Frontend tests and package validation",
            suite_required["frontend"],
        ),
        ("RESULT_TUI", "OpenTUI package tests", suite_required["tui"]),
        ("RESULT_DESKTOP", "Desktop Electron unit tests", suite_required["desktop-static"]),
        (
            "RESULT_UBUNTU",
            "Ubuntu quality gate",
            suite_required["python-targeted"] or suite_required["python-full"],
        ),
        (
            "RESULT_UBUNTU_FULL",
            "Ubuntu full test matrix",
            suite_required["python-full"],
        ),
        (
            "RESULT_WINDOWS_SMOKE",
            "Windows compatibility smoke tests",
            suite_required["windows-compat"],
        ),
        (
            "RESULT_WINDOWS_FULL",
            "Windows high-risk matrix",
            suite_required["windows-high-risk"],
        ),
        (
            "RESULT_MACOS_RECOVERY",
            "macOS profile recovery and native no-replace tests",
            suite_required["macos-recovery"],
        ),
        (
            "RESULT_DESKTOP_RECOVERY_E2E",
            "Desktop recovery E2E matrix",
            suite_required["desktop-recovery-e2e"],
        ),
        (
            "RESULT_WEBUI_CHAT_RECOVERY",
            "WebUI chat recovery browser contracts",
            suite_required["webui-chat-recovery"],
        ),
        (
            "RESULT_RELEASE",
            "Release packaging contracts",
            suite_required["release-packaging"],
        ),
        (
            "RESULT_MANAGED_TOOLCHAIN_ARTIFACTS",
            "Managed Toolchain Artifact E2E",
            suite_required["managed-toolchain"],
        ),
    )
    for variable, label, required in conditional_results:
        _require_result(env, errors, variable, label, required=required)

    if flags["platform_sensitive_changed"] and not flags["windows_full_required"]:
        errors.append("Platform-sensitive changes must require the Windows high-risk matrix.")

    if flags["python_full_required"] and not flags["python_changed"]:
        errors.append("Python full-matrix changes must also be classified as Python changes.")

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
