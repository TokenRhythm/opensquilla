# Owner Default Full Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project workspaces use Full Access by default for local owners and channel administrators while retaining existing restrictions for ordinary remote-channel senders.

**Architecture:** Keep backend run-mode policy as the source of truth. Remove only the special implicit-Full-to-Standard project override; retain the independently prepared Standard sandbox capability so owners can still select a restricted mode, and retain principal coercion so non-owners cannot obtain Full Access.

**Tech Stack:** Python 3.12, Pydantic configuration models, pytest, Vue/TypeScript policy consumer.

## Global Constraints

- Local owners and Feishu channel administrators default to Full Access.
- Ordinary Feishu and other remote-channel senders remain non-owners.
- Explicit Standard and Managed selections continue to work.
- No database migration or persisted workspace rewrite.
- Do not stop or restart the user's running Gateway.
- Preserve unrelated working-tree changes and exclude them from task commits.

---

### Task 1: Make the backend project default follow the configured Full mode

**Files:**
- Modify: `src/opensquilla/sandbox/run_mode.py:153-158`
- Modify: `tests/test_sandbox/test_run_modes.py:181-259`
- Modify: `tests/test_sandbox/test_run_context.py:90-112`

**Interfaces:**
- Consumes: `config_run_mode(config: Any) -> RunMode`
- Produces: `project_default_run_mode(config: Any) -> RunMode`

- [ ] **Step 1: Change the policy tests to require Full**

Update the bare-config assertions:

```python
def test_bare_config_keeps_full_for_ordinary_and_project_execution() -> None:
    config = types.SimpleNamespace(
        sandbox=SandboxSettings(),
        permissions=PermissionsConfig(),
    )

    assert config_run_mode(config) is RunMode.FULL
    assert project_default_run_mode(config) is RunMode.FULL
    assert sandbox_runtime_capability_mode(config) is RunMode.STANDARD
```

Update the status assertion to require:

```python
assert payload["project_default_run_mode"] == "full"
assert payload["runtime_capability_run_mode"] == "standard"
```

Update the legacy context test to require:

```python
assert resolved.run_mode is RunMode.FULL
assert resolved.run_mode_source is None
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_sandbox/test_run_modes.py \
  tests/test_sandbox/test_run_context.py
```

Expected: the bare project-default and legacy implicit-Full assertions fail because current code returns Standard.

- [ ] **Step 3: Remove the project-only implicit downgrade**

Replace `project_default_run_mode` with:

```python
def project_default_run_mode(config: Any) -> RunMode:
    return config_run_mode(config)
```

Keep `sandbox_runtime_capability_mode` unchanged so Standard remains available when explicitly selected.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the Step 2 command.

Expected: all tests pass.

- [ ] **Step 5: Commit the policy change**

```bash
git add \
  src/opensquilla/sandbox/run_mode.py \
  tests/test_sandbox/test_run_modes.py \
  tests/test_sandbox/test_run_context.py
git commit -m "fix: keep owner projects on full access by default"
```

### Task 2: Prove real project and channel behavior

**Files:**
- Modify: `tests/test_gateway/test_project_workspace_execution.py:157-186`
- Modify: `tests/test_gateway/test_project_workspace_execution.py:812-852`
- Modify: `tests/test_gateway/test_project_workspace_execution.py:2432-2675`
- Test: `tests/test_gateway/test_channel_dispatch_realtime.py`
- Test: `tests/test_gateway/test_routing_interaction_mode.py`

**Interfaces:**
- Consumes: `project_default_run_mode(config) -> RunMode.FULL`
- Produces: regression coverage for new projects, upgraded implicit-Full projects, explicit Standard projects, and Feishu owner boundaries.

- [ ] **Step 1: Update new-project and upgrade expectations**

Require a new project to persist:

```python
assert saved_context["run_mode"] == "full"
assert saved_context["run_mode_source"] == "operator_default"
```

Require a legacy implicit-Full project to execute as:

```python
assert captured["tool_context"].run_mode == "full"
assert captured["tool_context"].sandbox_run_context.run_mode_source is None
```

- [ ] **Step 2: Preserve explicit Standard sandbox coverage**

For the native Standard and unavailable-backend tests, send an explicit choice:

```python
"_source": {
    "caller_kind": "web",
    "channel_kind": "webchat",
    "runMode": "standard",
},
```

Rename the native test to `test_explicit_standard_project_drives_real_sandbox_filesystem_runtime`.

- [ ] **Step 3: Add a default-Full host-path regression**

Add a project test that installs an unavailable sandbox backend, dispatches a project turn without a run-mode override, writes to a sibling path through `write_file`, and asserts:

```python
assert project_ctx.run_mode == "full"
assert full_host_access_for_context(project_ctx) is True
assert outside.read_text(encoding="utf-8") == "full-host"
```

This proves the default owner project does not depend on the previously unreliable sandbox backend.

- [ ] **Step 4: Run project and channel boundary tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_channel_dispatch_realtime.py \
  tests/test_gateway/test_routing_interaction_mode.py
```

Expected: all tests pass, with platform-native tests skipped only when their backend is unavailable.

- [ ] **Step 5: Run frontend and upgrade compatibility checks**

Run:

```bash
cd opensquilla-webui
npm run typecheck
npx vitest run \
  src/composables/chat/useChatRunModePreference.test.ts \
  src/components/chat/ChatComposer.workspace.test.ts
```

Then run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_recovery/test_historical_upgrades.py \
  tests/test_migration/test_legacy_config_fixtures.py \
  tests/test_migrations/test_v025_project_workspaces.py
```

Expected: all relevant checks pass.

- [ ] **Step 6: Commit only the owner-default changes**

Stage only the hunks created by this task from the already-dirty integration test, preserving all pre-existing uncommitted hunks.

```bash
git commit -m "test: cover full-access project upgrade default"
```
