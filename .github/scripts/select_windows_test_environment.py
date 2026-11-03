"""Choose the Windows test package target while preserving the UAT gate."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def select_environment(
    requested: str, event_name: str, ref: str, default_branch: str = "main"
) -> str:
    if requested not in ("auto", "uat", "production"):
        raise ValueError("REQUESTED_ACCOUNT_ENVIRONMENT must be auto, uat, or production")
    if requested != "auto":
        return requested
    if event_name == "workflow_dispatch" and ref == f"refs/heads/{default_branch}":
        return "production"
    if event_name == "push" and ref.startswith("refs/tags/test-windows-production-"):
        return "production"
    return "uat"


def main() -> None:
    environment = select_environment(
        os.environ.get("REQUESTED_ACCOUNT_ENVIRONMENT", "auto"),
        os.environ.get("GITHUB_EVENT_NAME", ""),
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("DEFAULT_BRANCH", "main"),
    )
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"account_environment={environment}\n")
    gate = (
        "Build and verify UAT first; build the production test installer only after "
        "the UAT stage succeeds."
        if environment == "production"
        else "Build and verify UAT only; production packaging is skipped."
    )
    summary = (
        "### Windows test installer target\n\n"
        f"- Account/API environment: `{environment}`\n"
        f"- UAT gate: {gate}\n"
    )
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write(summary)
    print(summary)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
