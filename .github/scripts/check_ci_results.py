#!/usr/bin/env python3
"""Fail closed when the canonical CI plan or required job results are incomplete."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Final

PLANNER_RESULT: Final[tuple[str, str]] = ("RESULT_PLANNER", "Plan CI suites")
BASELINE_SUITES: Final[frozenset[str]] = frozenset(
    {"readme-locale", "workflow-lint"}
)

JOB_RESULT_LABELS: Final[dict[str, str]] = {
    "RESULT_WORKFLOW_LINT": "Workflow lint",
    "RESULT_README_LOCALE": "README locale parity",
    "RESULT_FRONTEND_ARTIFACT": "Frontend artifact",
    "RESULT_FRONTEND": "Frontend validation and wheel WebUI roundtrip",
    "RESULT_CONTRACT_WINDOWS": "Gateway Contract determinism on Windows",
    "RESULT_TUI": "OpenTUI package tests",
    "RESULT_DESKTOP": "Desktop Electron unit tests",
    "RESULT_UBUNTU": "Ubuntu quality gate",
    "RESULT_UBUNTU_FULL": "Ubuntu full test matrix",
    "RESULT_WINDOWS_FULL": "Windows high-risk matrix",
    "RESULT_MACOS_RECOVERY": "macOS profile recovery and native no-replace tests",
    "RESULT_DESKTOP_RECOVERY_E2E": "Desktop recovery E2E matrix",
    "RESULT_WEBUI_CHAT_RECOVERY": "WebUI chat recovery browser contracts",
    "RESULT_RELEASE": "Release packaging contracts",
    "RESULT_MANAGED_TOOLCHAIN_ARTIFACTS": "Managed Toolchain Artifact E2E",
    "RESULT_SKILL_HUB": "Skill Hub contract matrix",
}

KNOWN_SUITES: Final[frozenset[str]] = frozenset(
    {
        "desktop-recovery-e2e",
        "desktop-static",
        "frontend-artifact",
        "frontend-validation",
        "macos-recovery",
        "managed-toolchain",
        "python-full",
        "python-targeted",
        "readme-locale",
        "release-packaging",
        "skill-hub",
        "tui",
        "webui-chat-recovery",
        "wheel-webui-roundtrip",
        "windows-high-risk",
        "workflow-lint",
    }
)

# A suite may require more than one job, and multiple suites may share one job.
# Keep this mapping explicit so contract drift fails closed instead of silently
# accepting a planner suite that the aggregate gate does not understand.
SUITE_RESULT_REQUIREMENTS: Final[dict[str, tuple[str, ...]]] = {
    "desktop-recovery-e2e": ("RESULT_DESKTOP_RECOVERY_E2E",),
    "desktop-static": ("RESULT_DESKTOP",),
    "frontend-artifact": ("RESULT_FRONTEND_ARTIFACT",),
    "frontend-validation": ("RESULT_FRONTEND", "RESULT_CONTRACT_WINDOWS"),
    "macos-recovery": ("RESULT_MACOS_RECOVERY",),
    "managed-toolchain": ("RESULT_MANAGED_TOOLCHAIN_ARTIFACTS",),
    "python-full": ("RESULT_UBUNTU", "RESULT_UBUNTU_FULL"),
    "python-targeted": ("RESULT_UBUNTU",),
    "readme-locale": ("RESULT_README_LOCALE",),
    "release-packaging": ("RESULT_RELEASE",),
    "skill-hub": ("RESULT_SKILL_HUB",),
    "tui": ("RESULT_TUI",),
    "webui-chat-recovery": ("RESULT_WEBUI_CHAT_RECOVERY",),
    "wheel-webui-roundtrip": ("RESULT_FRONTEND",),
    "windows-high-risk": ("RESULT_WINDOWS_FULL",),
    "workflow-lint": ("RESULT_WORKFLOW_LINT",),
}


def _require_exact_result(
    env: Mapping[str, str],
    errors: list[str],
    variable: str,
    label: str,
    *,
    expected: str,
) -> None:
    result = env.get(variable, "")
    if result != expected:
        errors.append(f"{label} must be {expected}; got {result or 'missing'}.")


def _read_required_suites(env: Mapping[str, str], errors: list[str]) -> set[str]:
    raw = env.get("REQUIRED_SUITES")
    if raw is None:
        errors.append("Suite planner output required_suites is missing.")
        return set()
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
    required_suites = set(value)
    missing_baseline = sorted(BASELINE_SUITES - required_suites)
    if missing_baseline:
        errors.append(
            "Suite planner omitted baseline suites: " + ", ".join(missing_baseline)
        )
    return required_suites


def _validate_suite_result_contract(errors: list[str]) -> None:
    mapped_suites = set(SUITE_RESULT_REQUIREMENTS)
    missing_suites = sorted(KNOWN_SUITES - mapped_suites)
    if missing_suites:
        errors.append("CI result mappings are missing suites: " + ", ".join(missing_suites))

    unknown_suites = sorted(mapped_suites - KNOWN_SUITES)
    if unknown_suites:
        errors.append("CI result mappings contain unknown suites: " + ", ".join(unknown_suites))

    for suite, variables in sorted(SUITE_RESULT_REQUIREMENTS.items()):
        if not variables:
            errors.append(f"CI result mapping for {suite} must contain at least one job result.")
            continue
        unknown_variables = sorted(set(variables) - set(JOB_RESULT_LABELS))
        if unknown_variables:
            errors.append(
                f"CI result mapping for {suite} contains unknown job results: "
                + ", ".join(unknown_variables)
            )


def check_ci_results(env: Mapping[str, str]) -> list[str]:
    """Return gate errors; an empty list means the aggregate check may pass."""

    errors: list[str] = []
    planner_variable, planner_label = PLANNER_RESULT
    _require_exact_result(
        env,
        errors,
        planner_variable,
        planner_label,
        expected="success",
    )

    _validate_suite_result_contract(errors)
    required_suites = _read_required_suites(env, errors)

    required_results = {
        variable
        for suite in required_suites & KNOWN_SUITES
        for variable in SUITE_RESULT_REQUIREMENTS.get(suite, ())
    }
    for variable, label in JOB_RESULT_LABELS.items():
        _require_exact_result(
            env,
            errors,
            variable,
            label,
            expected="success" if variable in required_results else "skipped",
        )

    return errors


def main() -> int:
    errors = check_ci_results(os.environ)
    planner_variable, planner_label = PLANNER_RESULT
    print(f"{planner_label}: {os.environ.get(planner_variable, 'missing')}")
    print(f"Required suites: {os.environ.get('REQUIRED_SUITES', 'missing')}")
    for variable, label in JOB_RESULT_LABELS.items():
        print(f"{label}: {os.environ.get(variable, 'missing')}")
    if not errors:
        print("All planner-required CI results are complete and successful.")
        return 0
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
