# Standard Mode Human Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that Standard mode never automatically approves an elevation while preserving configured automatic review in Managed Execution.

**Architecture:** Introduce one run-mode-aware reviewer resolver and route every elevation producer and the automatic-review consumer through it. The active request/session run mode wins over process configuration, and Standard mode always resolves to the human reviewer.

**Tech Stack:** Python 3.12, asyncio, Pydantic sandbox settings, pytest.

## Global Constraints

- Standard mode must never automatically approve or consume an elevation.
- The active request/session run mode is authoritative.
- Trusted mode retains its configured reviewer.
- Existing automatic requests encountered in Standard mode become human-actionable and remain unresolved.
- No changes to Full Host Access behavior.

---

### Task 1: Central reviewer policy and exact tool elevation

**Files:**
- Modify: `src/opensquilla/sandbox/elevation.py`
- Modify: `tests/test_sandbox/test_approval_runtime.py`

**Interfaces:**
- Produces: `effective_approval_reviewer(configured: object, run_mode: object) -> ApprovalReviewerName`
- Consumes: `opensquilla.tools.run_mode.current_run_mode() -> str | None`

- [ ] **Step 1: Write failing tests**

Add tests that configure `approvals_reviewer="auto_review"`, set a Standard
`ToolContext`, call `gate_elevated_action`, and assert the queued record contains
`reviewer == "user"` and `humanActionable is True`. Add the trusted counterpart
asserting `reviewer == "auto_review"`.

- [ ] **Step 2: Verify the Standard test fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox/test_approval_runtime.py -q
```

Expected: the Standard assertion receives `auto_review`.

- [ ] **Step 3: Implement the minimal reviewer resolver**

Implement:

```python
def effective_approval_reviewer(configured: object, run_mode: object) -> ApprovalReviewerName:
    mode = getattr(run_mode, "value", run_mode)
    if str(mode or "").strip().lower() == "standard":
        return "user"
    return cast(
        "ApprovalReviewerName",
        configured if configured in {"user", "auto_review"} else "user",
    )
```

Use it in `gate_elevated_action` with `current_run_mode()`.

- [ ] **Step 4: Verify the focused tests pass**

Run the command from Step 2. Expected: PASS.

### Task 2: Network elevation policy

**Files:**
- Modify: `src/opensquilla/sandbox/integration.py`
- Modify: `src/opensquilla/sandbox/network_runtime.py`
- Modify: `tests/test_sandbox/test_network_runtime.py`
- Modify: `tests/test_sandbox/test_inprocess_managed_network.py`

**Interfaces:**
- Consumes: `effective_approval_reviewer(configured, run_mode)`
- Produces: user-owned network approval parameters in Standard mode.

- [ ] **Step 1: Write failing network tests**

Change/add Standard-mode tests so a runtime configured with `auto_review`
creates `reviewer="user"`, never calls `on_sandbox_auto_review`, and waits for
the manually resolved approval. Keep a Trusted-mode test proving automatic
review remains enabled.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox/test_network_runtime.py tests/test_sandbox/test_inprocess_managed_network.py -q
```

Expected: Standard requests still expose `reviewer="auto_review"`.

- [ ] **Step 3: Route network producers through the helper**

Pass the `RunContext.run_mode` or request run mode to the central resolver in
`NetworkApprovalService`, cached-network preflight, and package-bundle
preflight.

- [ ] **Step 4: Verify the network tests pass**

Run the command from Step 2. Expected: PASS.

### Task 3: Legacy queued-request backstop

**Files:**
- Modify: `src/opensquilla/engine/agent.py`
- Modify: `tests/test_engine/test_interactive_approval_retry.py`

**Interfaces:**
- Consumes: `effective_approval_reviewer("auto_review", current_run_mode())`
- Produces: a queued record updated to `reviewer="user"` and
  `humanActionable=True` without resolving it.

- [ ] **Step 1: Write the failing conversion test**

Queue an unresolved automatic elevation, activate a Standard `ToolContext`,
call `_review_pending_elevation_if_configured`, and assert the result is `None`,
the record is unresolved, and its reviewer is now `user`.

- [ ] **Step 2: Verify the test fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_engine/test_interactive_approval_retry.py -q
```

Expected: deterministic rules resolve the record automatically.

- [ ] **Step 3: Implement the conversion backstop**

Before deterministic review, resolve the effective reviewer. When it is
`user`, update the record to human-actionable and return without resolving it.

- [ ] **Step 4: Verify the interactive tests pass**

Run the command from Step 2. Expected: PASS.

### Task 4: Regression and runtime verification

**Files:**
- Verify only: sandbox, tool dispatch, and engine test suites.

**Interfaces:**
- Consumes: all completed policy changes.
- Produces: tested and running gateway behavior.

- [ ] **Step 1: Run focused regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox/test_approval_runtime.py tests/test_sandbox/test_network_runtime.py tests/test_sandbox/test_inprocess_managed_network.py tests/test_engine/test_interactive_approval_retry.py tests/test_tools/test_dispatch_envelope.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the complete sandbox suite and lint**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox -q
.\.venv\Scripts\python.exe -m ruff check src/opensquilla/sandbox/elevation.py src/opensquilla/sandbox/integration.py src/opensquilla/sandbox/network_runtime.py src/opensquilla/engine/agent.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 3: Run a real Standard-mode approval smoke**

Create an exact out-of-workspace write in Standard mode while configuration
requests automatic review. Assert the approval remains unresolved and the file
does not exist. Resolve the exact approval manually, replay once, and assert the
file is created and the approval consumed.

- [ ] **Step 4: Commit and restart**

Commit the code/tests, restart the exact gateway process listening on port
`19999`, then require HTTP `/health` status 200 and an accepted WebSocket
connection in the gateway log.

