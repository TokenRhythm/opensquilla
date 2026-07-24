# Codex Standard Sandbox Parity Implementation Plan

> Execute with test-driven development: add each failing assertion first, run the focused
> test to observe the intended failure, implement the minimum coherent production change,
> then rerun the focused test.

## Task 1: Make Windows host-readability a true baseline

Files:

- Modify: `src/opensquilla/sandbox/permissions.py`
- Modify: `src/opensquilla/sandbox/platform_permissions.py`
- Test: `tests/test_sandbox/test_platform_permissions.py`
- Test: `tests/test_sandbox/test_permission_profiles.py`

Steps:

1. Add Windows profile tests proving `C:\outside` and `D:\outside` resolve to `READ`,
   the workspace resolves to `WRITE`, and explicit deny entries still win.
2. Run:
   `uv run pytest -q tests/test_sandbox/test_platform_permissions.py tests/test_sandbox/test_permission_profiles.py`
   and confirm the new cross-drive assertion fails.
3. Represent Windows `host_root_readonly` as `default_access=READ` while retaining specific
   entries for writes and carve-outs.
4. Remove obsolete selective Windows root expansion from the standard baseline without
   changing explicit helper/readable roots.
5. Rerun the focused tests.

## Task 2: Remove read approvals from standard-mode tools

Files:

- Modify: `src/opensquilla/tools/builtin/filesystem.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Test: `tests/test_sandbox/test_path_access.py`

Steps:

1. Replace the old tests that expect standard-mode read approvals with tests that expect the
   backend to run immediately and no approval row to be created.
2. Add both simple-command and PowerShell path coverage.
3. Run the focused tests and confirm failure before production changes.
4. Make tool preflight trust the active filesystem profile's Windows read baseline.
5. Rerun the focused tests.

## Task 3: Prevent drive-root ACL mutation

Files:

- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Test: `tests/test_sandbox/test_windows_default_backend.py`

Steps:

1. Add a test with a Windows drive-root `READ` entry and assert it is absent from
   `windowsAclPlan.autoGrants`.
2. Add a workspace profile test asserting only exact writable/runtime roots receive grants.
3. Run the focused tests and confirm the drive-root test fails.
4. Skip policy RX grants for filesystem roots and for implicit full-read baselines.
5. Keep explicit deny paths and writable-root grants unchanged.
6. Rerun the focused tests.

## Task 4: Harden approval continuation

Files:

- Modify: `src/opensquilla/sandbox/escalation.py`
- Modify: `src/opensquilla/sandbox/approval_runtime.py` if the state machine requires it
- Modify: `src/opensquilla/engine/agent.py` if result suppression requires it
- Test: `tests/test_engine/test_interactive_approval_retry.py`
- Test: `tests/test_sandbox/test_path_access.py`
- Test: `tests/test_tools/test_approval_unification.py`

Steps:

1. Add an integration test for an outside write that emits one approval, blocks the original
   call, resumes the same `ToolCall`, executes once, and marks the grant consumed.
2. Add denial and timeout assertions proving zero side effects and no fallback provider call.
3. Run the tests and confirm any lifecycle gap.
4. Validate approval identity/session/action on replay and consume exact elevation authority
   immediately before execution.
5. Ensure intermediate approval payloads are UI events only and the model receives only the
   final result.
6. Rerun the focused tests.

## Task 5: Stabilize Windows worker environment

Files:

- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default_runner.py` if normalization belongs
  in the child
- Test: `tests/test_sandbox/test_filesystem_worker_policy.py`
- Test: `tests/test_sandbox/test_windows_default_backend.py`

Steps:

1. Add a worker-launch test with missing home variables and assert the child receives a valid
   home tuple.
2. Run the focused test and confirm failure.
3. Normalize `USERPROFILE`, `HOME`, `HOMEDRIVE`, and `HOMEPATH` before launch.
4. Rerun the focused tests.

## Task 6: Bound and recover the Windows execution lease

Files:

- Modify: `src/opensquilla/sandbox/backend/windows_default_runner.py`
- Test: `tests/test_sandbox/test_windows_default_runner.py`

Steps:

1. Add a contention test proving acquisition fails within a bounded interval with a typed,
   actionable message.
2. Add a recovery test proving a later acquisition succeeds after release.
3. Run the focused tests and confirm the current blocking/deadlock behavior fails.
4. Implement non-blocking retry with monotonic deadline and guaranteed unlock/close.
5. Translate Windows lock errors into the sandbox backend's structured failure protocol.
6. Rerun the focused tests.

## Task 7: Stop infrastructure failure storms

Files:

- Modify: `src/opensquilla/tools/policy/finalize.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Modify: `src/opensquilla/sandbox/operation_runtime.py`
- Test: `tests/test_sandbox/test_operation_runtime.py`
- Test: `tests/test_tools/test_shell_approval_policy.py`
- Test: `tests/test_engine/test_interactive_approval_retry.py`

Steps:

1. Add tests distinguishing sandbox denial from backend infrastructure failure.
2. Assert infrastructure failures are non-retryable, create no approval, and produce one tool
   result.
3. Run the focused tests and confirm current misclassification.
4. Centralize the classification and remove retry hints for backend setup/ACL/lease failures.
5. Rerun the focused tests.

## Task 8: Regression and real runtime verification

Steps:

1. Run focused sandbox and approval suites.
2. Run:
   `uv run ruff check` on changed Python files.
3. Run the broader relevant suite:
   `uv run pytest -q tests/test_sandbox tests/test_engine/test_interactive_approval_retry.py tests/test_tools/test_approval_unification.py`
4. Restart the source gateway.
5. In standard mode, verify `list_dir D:\lrk` succeeds with no approval.
6. Select an OpenSquilla project workspace, request a write to a disposable outside directory,
   approve once, and verify the same operation succeeds.
7. Inspect logs and the approval database to confirm no root ACL attempt, no unconsumed exact
   elevation grant, and no repeated fallback tools.
