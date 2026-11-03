from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(".github/scripts/select_windows_test_environment.py")
SPEC = importlib.util.spec_from_file_location("select_windows_test_environment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    ("event", "ref", "default_branch", "expected"),
    [
        ("workflow_dispatch", "refs/heads/main", "main", "production"),
        ("workflow_dispatch", "refs/heads/stable", "stable", "production"),
        ("workflow_dispatch", "refs/heads/main", "stable", "uat"),
        ("workflow_dispatch", "refs/heads/fix/example", "main", "uat"),
        ("workflow_dispatch", "refs/tags/main", "main", "uat"),
        ("workflow_dispatch", "refs/heads/main-extra", "main", "uat"),
        ("push", "refs/heads/main", "main", "uat"),
        ("push", "refs/tags/test-windows-production-20261103", "main", "production"),
        ("push", "refs/heads/test-windows-production-20261103", "main", "uat"),
        ("push", "refs/tags/test-windows-20261103", "main", "uat"),
        ("push", "refs/tags/main", "main", "uat"),
        ("workflow_dispatch", "refs/tags/test-windows-production-20261103", "main", "uat"),
        ("pull_request", "refs/heads/main", "main", "uat"),
        ("", "", "main", "uat"),
    ],
)
def test_auto_selects_only_the_intended_event_and_ref(event, ref, default_branch, expected):
    assert MODULE.select_environment("auto", event, ref, default_branch) == expected


@pytest.mark.parametrize("requested", ["uat", "production"])
@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("workflow_dispatch", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/feature/example"),
        ("push", "refs/tags/test-windows-production-20261103"),
    ],
)
def test_explicit_selection_takes_precedence(requested, event, ref):
    assert MODULE.select_environment(requested, event, ref) == requested


@pytest.mark.parametrize("requested", ["", "prod", "UAT", "production\nother=value"])
def test_rejects_unknown_or_multiline_environment(requested):
    with pytest.raises(ValueError, match="must be auto, uat, or production"):
        MODULE.select_environment(requested, "workflow_dispatch", "refs/heads/main")


def cli_env(tmp_path):
    env = os.environ.copy()
    for key in (
        "REQUESTED_ACCOUNT_ENVIRONMENT",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "DEFAULT_BRANCH",
    ):
        env.pop(key, None)
    env["GITHUB_OUTPUT"] = str(tmp_path / "output")
    env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary")
    return env


@pytest.mark.parametrize(
    ("overrides", "expected", "gate"),
    [
        (
            {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"},
            "production",
            "only after the UAT stage succeeds",
        ),
        (
            {
                "DEFAULT_BRANCH": "stable",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/stable",
            },
            "production",
            "only after the UAT stage succeeds",
        ),
        (
            {
                "REQUESTED_ACCOUNT_ENVIRONMENT": "uat",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
            },
            "uat",
            "production packaging is skipped",
        ),
    ],
)
def test_cli_appends_selected_environment_and_gate_summary(tmp_path, overrides, expected, gate):
    env = cli_env(tmp_path)
    env.update(overrides)
    output = Path(env["GITHUB_OUTPUT"])
    summary = Path(env["GITHUB_STEP_SUMMARY"])
    output.write_text("existing=value\n", encoding="utf-8")
    summary.write_text("Existing summary\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=True
    )

    assert output.read_text(encoding="utf-8") == (
        f"existing=value\naccount_environment={expected}\n"
    )
    appended_summary = summary.read_text(encoding="utf-8").removeprefix("Existing summary\n")
    assert appended_summary == result.stdout.rstrip("\n") + "\n"
    assert f"Account/API environment: `{expected}`" in appended_summary
    assert gate in appended_summary
    assert not result.stderr


def test_cli_invalid_input_fails_before_writing_outputs_and_does_not_echo_input(tmp_path):
    env = cli_env(tmp_path)
    invalid = "invalid-sensitive-value"
    env["REQUESTED_ACCOUNT_ENVIRONMENT"] = invalid

    result = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert "must be auto, uat, or production" in result.stderr
    assert invalid not in result.stdout + result.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert not Path(env["GITHUB_STEP_SUMMARY"]).exists()
