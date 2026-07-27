# L3 Approved Write Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a user-approved L3 shell or Python operation execute exactly once through the existing host path instead of rerunning inside an unwritable L3 sandbox.

**Architecture:** `ApprovalGate` will return a process-local `ApprovedHostExecution` value only after an L3 approval is resolved and atomically consumed. Shell and code tools will recognize that value and use their existing host-execution branches; ordinary `ALLOW`, denials, backend-denial elevation, and all platform backends remain unchanged.

**Tech Stack:** Python 3.11+, asyncio, pytest, OpenSquilla approval queue and sandbox runtime.

## Global Constraints

- Do not change operation classification or L1/L2/L3 baseline policies.
- Do not change macOS, Linux, or Windows sandbox backends.
- The grant applies only to the waiting original tool call and is never model-visible.
- Consume the approval before starting the side effect.
- Do not emit a second approval request.
- Preserve existing Trusted and Full behavior.
- Write and observe failing tests before every production change.

---

### Task 1: Represent and consume an approved L3 host grant

**Files:**
- Modify: `src/opensquilla/sandbox/types.py`
- Modify: `src/opensquilla/sandbox/governance.py`
- Modify: `src/opensquilla/sandbox/__init__.py`
- Test: `tests/test_sandbox/test_approval_gate_enqueue.py`

**Interfaces:**
- Produces: `ApprovedHostExecution(approval_id: str, action_fingerprint: str, level: SecurityLevel)`.
- Produces: `ApprovalDecision = _AllowSentinel | ApprovedHostExecution | DenialResult`.
- Consumes: approval queues implementing `consume(approval_id: str) -> None`.

- [ ] **Step 1: Write the failing approval-gate tests**

Add an L3 policy helper and a resolving queue that records `consume` calls:

```python
class _ResolvingConsumableQueue(_RecordingQueue):
    def __init__(self, *, approved: bool = True) -> None:
        super().__init__()
        self.approved = approved
        self.consumed: list[str] = []

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return self.approved

    def consume(self, approval_id: str) -> None:
        self.consumed.append(approval_id)


@pytest.mark.asyncio
async def test_locked_approval_returns_consumed_host_execution_grant(tmp_path: Path) -> None:
    policy = dataclasses.replace(_policy(tmp_path), level=SecurityLevel.LOCKED)
    request = SandboxRequest(
        argv=("shell.exec", f"rm {tmp_path / 'x'}"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
    )
    queue = _ResolvingConsumableQueue()

    decision = await ApprovalGate(queue).gate(request, policy, session_id="s1")

    assert isinstance(decision, ApprovedHostExecution)
    assert decision.approval_id == "approval-1"
    assert decision.action_fingerprint == action_fingerprint(request)
    assert decision.level is SecurityLevel.LOCKED
    assert queue.consumed == ["approval-1"]


@pytest.mark.asyncio
async def test_rejected_locked_approval_does_not_consume(tmp_path: Path) -> None:
    policy = dataclasses.replace(_policy(tmp_path), level=SecurityLevel.LOCKED)
    request = SandboxRequest(
        argv=("shell.exec", f"rm {tmp_path / 'x'}"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
    )
    queue = _ResolvingConsumableQueue(approved=False)

    decision = await ApprovalGate(queue).gate(request, policy, session_id="s1")

    assert isinstance(decision, DenialResult)
    assert queue.consumed == []
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_sandbox/test_approval_gate_enqueue.py::test_locked_approval_returns_consumed_host_execution_grant \
  tests/test_sandbox/test_approval_gate_enqueue.py::test_rejected_locked_approval_does_not_consume
```

Expected: FAIL because `ApprovedHostExecution` does not exist and approved L3 currently returns `ALLOW`.

- [ ] **Step 3: Add the grant type and consume it in the gate**

Add to `sandbox/types.py`:

```python
@dataclass(frozen=True)
class ApprovedHostExecution:
    approval_id: str
    action_fingerprint: str
    level: SecurityLevel


ApprovalDecision = _AllowSentinel | ApprovedHostExecution | DenialResult
```

Extend `_ApprovalQueueLike` in `sandbox/governance.py`:

```python
def consume(self, approval_id: str) -> None: ...
```

In `ApprovalGate.gate`, replace the approved L3 return:

```python
if approved:
    if policy.level is SecurityLevel.LOCKED:
        try:
            self._queue.consume(approval_id)
        except (KeyError, ValueError) as exc:
            return DenialResult(
                reason=DenialReason.POLICY_DENIED,
                suggested_next_step=SuggestedNextStep.ASK_USER,
                level=policy.level,
                action_fingerprint=fingerprint,
                message=f"Approved L3 execution grant could not be consumed: {exc}",
                retryable=False,
            )
        return ApprovedHostExecution(
            approval_id=approval_id,
            action_fingerprint=fingerprint,
            level=policy.level,
        )
    return ALLOW
```

Export the type from `sandbox/types.py` and `sandbox/__init__.py`.

- [ ] **Step 4: Run focused and governance regression tests**

Run:

```bash
pytest -q \
  tests/test_sandbox/test_approval_gate_enqueue.py \
  tests/test_sandbox/test_governance.py
```

Expected: PASS.

- [ ] **Step 5: Commit the grant state-machine change**

```bash
git add \
  src/opensquilla/sandbox/types.py \
  src/opensquilla/sandbox/governance.py \
  src/opensquilla/sandbox/__init__.py \
  tests/test_sandbox/test_approval_gate_enqueue.py
git commit -m "fix: consume approved L3 execution grants"
```

### Task 2: Route approved L3 shell calls to the existing host path

**Files:**
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Test: `tests/test_tools/test_shell_approval_policy.py`

**Interfaces:**
- Consumes: `ApprovedHostExecution` from Task 1.
- Produces: `exec_command` and `background_process` set `host_execution=True` only for that returned grant.

- [ ] **Step 1: Write failing shell routing tests**

Patch `gate_action` to return `ApprovedHostExecution`, patch the sandbox backend to fail if invoked, and patch `_run_host_shell_command` to record the exact command:

```python
@pytest.mark.asyncio
async def test_exec_command_approved_locked_action_uses_host_once(monkeypatch, tmp_path):
    seen: list[str] = []
    grant = ApprovedHostExecution(
        approval_id="approval-1",
        action_fingerprint="fingerprint",
        level=SecurityLevel.LOCKED,
    )

    async def fake_gate_action(**kwargs):
        return grant, _locked_policy(tmp_path), _request(tmp_path, kwargs)

    async def fail_backend(*args, **kwargs):
        raise AssertionError("approved L3 call must not run under the backend")

    async def fake_host(command: str, **kwargs):
        seen.append(command)
        return "exit_code=0\n"

    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", fail_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)

    result = await shell.exec_command("rm -f exact-probe", workdir=str(tmp_path))

    assert result == "exit_code=0\n"
    assert seen == ["rm -f exact-probe"]
```

Add the equivalent assertion for `background_process`, using its existing host-process test helper.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_tools/test_shell_approval_policy.py -k "approved_locked"
```

Expected: FAIL because shell currently treats every non-denial decision as ordinary sandbox `ALLOW`.

- [ ] **Step 3: Implement the minimal shell branch**

Import `ApprovedHostExecution`. Immediately after each `gate_action` denial check in `exec_command` and `background_process`, add:

```python
if isinstance(decision, ApprovedHostExecution):
    host_execution = True
    backend_retry_granted = True
```

Only run `consume_backend_denial_retry` and construct a backend request when `host_execution` remains false.

- [ ] **Step 4: Run focused shell and sandbox tests**

Run:

```bash
pytest -q \
  tests/test_tools/test_shell_approval_policy.py \
  tests/test_sandbox/test_trusted_sandbox_execution.py \
  tests/test_sandbox/test_windows_shell_process_runtime.py
```

Expected: PASS.

- [ ] **Step 5: Commit the shell routing change**

```bash
git add \
  src/opensquilla/tools/builtin/shell.py \
  tests/test_tools/test_shell_approval_policy.py
git commit -m "fix: run approved L3 shell calls once on host"
```

### Task 3: Route approved L3 Python calls to the existing host path

**Files:**
- Modify: `src/opensquilla/tools/builtin/code_exec.py`
- Test: `tests/test_sandbox/test_trusted_sandbox_execution.py`

**Interfaces:**
- Consumes: `ApprovedHostExecution` from Task 1.
- Produces: approved L3 `execute_code` calls set `host_execution=True`, `sandbox_enabled=False`, and `elevated_code_execution=True`.

- [ ] **Step 1: Write the failing Python routing test**

```python
@pytest.mark.asyncio
async def test_execute_code_approved_locked_action_uses_host_once(monkeypatch, tmp_path):
    target = tmp_path / "probe.txt"
    target.write_text("probe")
    grant = ApprovedHostExecution(
        approval_id="approval-1",
        action_fingerprint="fingerprint",
        level=SecurityLevel.LOCKED,
    )

    async def fake_gate_action(**kwargs):
        return grant, _locked_policy(tmp_path), _request(tmp_path, kwargs)

    async def fail_backend(*args, **kwargs):
        raise AssertionError("approved L3 code must not run under the backend")

    monkeypatch.setattr(code_exec, "gate_action", fake_gate_action)
    monkeypatch.setattr(code_exec, "_run_backend_with_managed_network_if_needed", fail_backend)

    result = json.loads(
        await code_exec.execute_code(
            f"from pathlib import Path; Path({str(target)!r}).unlink()",
            workdir=str(tmp_path),
        )
    )

    assert result["exit_code"] == 0
    assert target.exists() is False
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
pytest -q \
  tests/test_sandbox/test_trusted_sandbox_execution.py \
  -k "approved_locked_action"
```

Expected: FAIL because `execute_code` still builds an L3 backend request.

- [ ] **Step 3: Implement the minimal code-execution branch**

Import `ApprovedHostExecution`. Immediately after the `DenialResult` check:

```python
if isinstance(decision, ApprovedHostExecution):
    host_execution = True
    sandbox_enabled = False
    elevated_code_execution = True
```

Only run backend-denial retry consumption and construct the backend request when `host_execution` remains false.

- [ ] **Step 4: Run focused code and approval tests**

Run:

```bash
pytest -q \
  tests/test_sandbox/test_trusted_sandbox_execution.py \
  tests/test_sandbox/test_escalate_backend_denial.py \
  tests/test_sandbox/test_approval_runtime.py
```

Expected: PASS.

- [ ] **Step 5: Commit the Python routing change**

```bash
git add \
  src/opensquilla/tools/builtin/code_exec.py \
  tests/test_sandbox/test_trusted_sandbox_execution.py
git commit -m "fix: run approved L3 Python calls once on host"
```

### Task 4: Verify end-to-end behavior and user-visible approval count

**Files:**
- Modify: `tests/test_engine/test_interactive_approval_retry.py`

**Interfaces:**
- Verifies the Tasks 1-3 contract without introducing a new UI approval kind.

- [ ] **Step 1: Add the engine regression test**

Assert that one L3 approval request yields one final tool result and no second `approval_required` or `approval_pending` payload:

```python
assert approval_request_count == 1
assert host_execution_count == 1
assert pending_payload_count == 0
assert final_result.execution_status["status"] == "success"
```

- [ ] **Step 2: Run the engine test and verify RED**

```bash
pytest -q tests/test_engine/test_interactive_approval_retry.py
```

Expected: FAIL because the approved legacy L3 decision currently carries no host-execution authority.

- [ ] **Step 3: Make the minimal glue correction required by the failing engine test**

Do not add a new approval kind or WebUI component. Keep the approval ID internal and deliver the host result as the original tool result.

- [ ] **Step 4: Run the complete targeted matrix**

```bash
pytest -q \
  tests/test_sandbox/test_approval_gate_enqueue.py \
  tests/test_sandbox/test_governance.py \
  tests/test_tools/test_shell_approval_policy.py \
  tests/test_sandbox/test_trusted_sandbox_execution.py \
  tests/test_sandbox/test_escalate_backend_denial.py \
  tests/test_sandbox/test_approval_runtime.py \
  tests/test_engine/test_interactive_approval_retry.py
```

Expected: PASS with no warnings or errors.

- [ ] **Step 5: Run formatting and static checks on changed Python files**

```bash
ruff check \
  src/opensquilla/sandbox/types.py \
  src/opensquilla/sandbox/governance.py \
  src/opensquilla/tools/builtin/shell.py \
  src/opensquilla/tools/builtin/code_exec.py \
  tests/test_sandbox/test_approval_gate_enqueue.py \
  tests/test_tools/test_shell_approval_policy.py \
  tests/test_sandbox/test_trusted_sandbox_execution.py
```

Expected: PASS.

- [ ] **Step 6: Commit any final engine-only regression coverage**

```bash
git add tests/test_engine/test_interactive_approval_retry.py
git commit -m "test: cover one-click L3 host execution"
```
