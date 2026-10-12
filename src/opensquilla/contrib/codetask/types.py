"""Shared data types for the code-task harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskState(StrEnum):
    """Final outcome of a code-task run.

    The non-``verified``/``failed`` states capture the false-negative modes
    of the red→green→regression loop that a naive "tests must fail first"
    rule would misreport (codex design review finding #6).
    """

    VERIFIED = "verified"
    # The acceptance criteria already hold on the base commit: the agent's
    # acceptance test passed before any change (issue fixed upstream, or the
    # request is already satisfied). Not a failure, but no change was needed.
    ALREADY_SATISFIED = "already_satisfied"
    # The task cannot be expressed as an automated test (docs-only, config,
    # design discussion). Work may still be done, but not machine-verified.
    NOT_TESTABLE = "not_testable"
    # Dependency install / build / test harness could not be brought up, so
    # the loop could not run. The repo environment is the blocker.
    ENVIRONMENT_BLOCKED = "environment_blocked"
    # The agent emitted an acceptance manifest the runner could not validate
    # or run (missing command, malformed schema, test does not exist).
    INVALID_ACCEPTANCE_TEST = "invalid_acceptance_test"
    # The agent ran but the acceptance test did not reach green, or
    # regression introduced new failures.
    FAILED = "failed"


# States in which a non-empty change is expected to exist on the task branch.
PRODUCTIVE_STATES = frozenset({TaskState.VERIFIED, TaskState.NOT_TESTABLE})


@dataclass
class AcceptanceCheck:
    """One acceptance test from the agent's verification manifest."""

    name: str
    command: str  # shell command the runner runs from the repo root
    # Expected outcome the runner re-confirms after the agent finishes.
    expected: str = "pass"  # "pass" only, for v1
    # Runner-observed results.
    before: str | None = None  # pass | fail | error (red phase, if captured)
    after: str | None = None  # pass | fail | error (runner re-run)
    # Captured runner evidence (bounded), for the verify->fix retry loop + report.
    green_exit_code: int | None = None  # exit code of the post-change (green) run
    green_output_tail: str = ""  # bounded tail of the green run's output
    red_exit_code: int | None = None  # exit code of the base-worktree (red) run
    red_output_tail: str = ""  # bounded tail of the red run's output


@dataclass
class RegressionResult:
    """Outcome of the repo's existing test suite after the change."""

    command: str | None = None
    ran: bool = False
    passed: int | None = None
    failed: int | None = None
    new_failures: int | None = None
    raw_tail: str = ""  # last lines of output, for the report


@dataclass
class BuildCheck:
    """One runner-owned build-verification command and its observed result."""

    name: str  # npm_ci | build | package
    command: str
    ran: bool = False
    ok: bool = False
    exit_code: int | None = None
    duration_seconds: float | None = None
    raw_tail: str = ""  # last lines of output, for the report
    # True when the command was killed for exceeding its deadline (as opposed to
    # exiting non-zero). Distinguishes "the build hung / never finished" from
    # "the build ran and failed" so the report and retry loop can react.
    timed_out: bool = False


@dataclass
class BuildResult:
    """Outcome of build-mode verification (the fixed, runner-owned checklist)."""

    checks: list[BuildCheck] = field(default_factory=list)
    all_passed: bool = False
    installer_path: str | None = None  # produced .dmg on macOS deploys
    installer_paths: list[str] = field(default_factory=list)  # all .dmg deliverables


@dataclass
class AgentOutcome:
    """Structured result of the host agent subprocess."""

    success: bool
    timeout: bool
    exit_code: int
    finish_reason: str  # stop | timeout | error | empty
    duration_seconds: float = 0.0
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    # Provider/tool errors from the agent's JSON envelope (each {message, code});
    # the child exits 0 in --json mode even when a turn errored, so this is the
    # only channel carrying the real reason (e.g. code "no_provider"/"402").
    errors: list[dict] = field(default_factory=list)


@dataclass
class TaskResult:
    """Full outcome of a code-task run, serialized to result.json."""

    task_slug: str
    run_id: str
    state: TaskState
    repo: str
    base_ref: str
    branch: str
    source: str  # github-issue | inline | file
    verified: bool = False
    assumptions: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceCheck] = field(default_factory=list)
    regression: RegressionResult | None = None
    verification_kind: str = "red_green"  # red_green | build | scratch
    build: BuildResult | None = None
    persisted: bool = False  # build edit applied back to the source repo
    source_repo: str | None = None
    commits: int = 0
    files_changed: int = 0
    diffstat: str = ""
    artifact_dir: str | None = None
    patch_path: str | None = None
    duration_seconds: float | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    # Verify->fix retry loop metadata.
    attempts: int = 0
    max_attempts: int = 1
    retry_exhausted: bool = False
    relaunch_recommended: bool = False
    final_failure_reason: str | None = None
