# Project Workspace Sandbox Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project workspaces use a real Codex-compatible Standard sandbox by default, enforce their trusted path on every turn, make lifecycle writes atomic, and repair the Web/Desktop workspace selection experience.

**Architecture:** Keep the ordinary request default and process sandbox capability as separate runtime concepts. Resolve every project-bound turn through one canonical project-workspace validator and carry a transaction guard into turn acceptance. Expose the validated project snapshot to the UI, which keeps navigation, selection, pending binding, and durable active-workspace state independent.

**Tech Stack:** Python 3.12, asyncio, Pydantic, aiosqlite, pytest/pytest-asyncio, Vue 3 Composition API, TypeScript, Vitest/happy-dom, Electron, Playwright.

## Global Constraints

- The authoritative design is `docs/superpowers/specs/2026-07-26-project-workspace-sandbox-hardening-design.md`.
- A bare configuration keeps ordinary non-project sessions at Full Host Access.
- A bare configuration gives project sessions Standard mode backed by a real sandbox.
- Explicit Standard, Trusted, Full, `sandbox=false`, and `permissions.default_mode=full` remain explicit.
- A Standard project turn must use a real backend or fail closed.
- A bound session uses only its active, trusted `project_workspaces` row as its workspace; persisted origin text is not authorization.
- Missing, removed, inaccessible, or canonically retargeted projects fail before model/tool execution and never fall back.
- Same-fingerprint ingress replay precedes mutable project checks; new acceptance rechecks the project inside the write transaction.
- Project history deletion never deletes project files or the project record.
- Protected `.git`, `.codex`, and `.agents` symlinks are protected in lexical and canonical form.
- Web picker navigation and selection are independent; no relative path resolves against gateway process cwd.
- No database schema migration is introduced for this hardening.
- Local-only Web build artifacts, process ownership, and gateway restart state are
  repaired and verified locally but never staged.
- Pre-existing failures outside the workspace feature are recorded as a baseline,
  not repaired opportunistically; every staged production file must be a causal
  dependency of workspace sandboxing, project lifecycle, picker, or active-project UI.
- Every production change follows red-green-refactor and ends with focused verification plus an intentional commit.

---

## File Responsibility Map

### Python runtime and workspace authority

- `src/opensquilla/sandbox/run_mode.py`: ordinary/project mode resolution and explicitness detection.
- `src/opensquilla/sandbox/integration.py`: process capability runtime and request-default fallback.
- `src/opensquilla/gateway/boot.py`: capability-oriented runtime boot and queued-turn revalidation.
- `src/opensquilla/project_workspaces.py`: canonical project validation, guard types, availability projection.
- `src/opensquilla/gateway/project_workspace_runtime.py`: session-bound project resolution and authoritative RunContext composition.
- `src/opensquilla/sandbox/run_context.py`: persisted run-mode provenance.
- `src/opensquilla/gateway/rpc_sessions.py`: receipt-first ingress, prepared acceptance, project snapshot bootstrap.
- `src/opensquilla/gateway/rpc_sandbox.py`: project-aware sandbox RPCs and stable path listing.
- `src/opensquilla/session/storage.py`: guarded turn transaction and atomic project-history deletion.

### Sandbox parity

- `src/opensquilla/sandbox/permissions.py`: lexical/canonical protected metadata representation.
- `src/opensquilla/sandbox/backend/linux_permissions.py`: Linux logical carveout plan.
- `src/opensquilla/sandbox/backend/seatbelt.py`: macOS lexical/canonical rules.
- `src/opensquilla/sandbox/backend/windows_default.py`: Windows ACL/reparse protected-path variants.
- `src/opensquilla/sandbox/filesystem_worker.py`: logical/canonical permission serialization parity.
- `src/opensquilla/tools/run_mode.py`: disabled-runtime precedence.
- `src/opensquilla/tools/builtin/filesystem.py`, `patch.py`, `shell.py`: logical protected-path gates.

### Web and Desktop

- `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue`: picker state machine.
- `opensquilla-webui/src/composables/useActiveProjectWorkspace.ts`: pending versus durable project state.
- `opensquilla-webui/src/composables/chat/useChatSessionSubscription.ts`: authoritative project snapshot handoff.
- `opensquilla-webui/src/composables/chat/useChatSend.ts`: fail-closed project send guard.
- `opensquilla-webui/src/views/ChatView.vue`: active-project wiring.
- `opensquilla-webui/src/components/chat/ChatComposer.vue`: durable project indicator and blocked state.
- `opensquilla-webui/src/components/SidebarConversations.vue`, `opensquilla-webui/src/App.vue`: unavailable/removal behavior.
- `opensquilla-webui/src/types/rpc.ts`: path-list and project snapshot contracts.
- `opensquilla-webui/src/locales/*.json`: picker and unavailable-project copy.
- `opensquilla-webui/e2e/project-workspaces.spec.ts`: complete lifecycle.

---

### Task 1: Separate Sandbox Capability From Request Defaults

**Files:**
- Modify: `src/opensquilla/sandbox/run_mode.py`
- Modify: `src/opensquilla/sandbox/integration.py`
- Modify: `src/opensquilla/gateway/boot.py`
- Modify: `src/opensquilla/tools/run_mode.py`
- Modify: `src/opensquilla/sandbox/status.py`
- Test: `tests/test_sandbox/test_run_modes.py`
- Test: `tests/test_sandbox/test_run_mode_routing.py`
- Test: `tests/test_sandbox/test_windows_default_request_context.py`
- Test: `tests/test_sandbox/test_cli_run_modes.py`
- Test: `tests/test_gateway/test_router_boot.py`
- Test: `tests/test_tools/test_filesystem_read_workspace.py`

**Interfaces:**
- Produces: `project_default_run_mode(config: Any) -> RunMode`
- Produces: `sandbox_runtime_capability_mode(config: Any) -> RunMode`
- Produces: `SandboxRuntime.default_run_mode: RunMode`
- Changes: `configure_runtime(settings: SandboxSettings, *, approval_queue: _ApprovalQueueLike | None = None, stale_cache: StaleOutputCache | None = None, workspace: Path | None = None, default_run_mode: RunMode | str | None = None) -> SandboxRuntime`
- Consumes: existing `config_run_mode`, `run_mode_config_patch`, and Pydantic `model_fields_set`

- [ ] **Step 1: Add the failing mode-resolution matrix**

```python
def test_bare_config_keeps_ordinary_full_but_project_is_standard() -> None:
    config = SimpleNamespace(
        sandbox=SandboxSettings(),
        permissions=PermissionsConfig(),
    )
    assert config_run_mode(config) is RunMode.FULL
    assert project_default_run_mode(config) is RunMode.STANDARD
    assert sandbox_runtime_capability_mode(config) is RunMode.STANDARD


@pytest.mark.parametrize(
    ("sandbox", "permissions", "expected"),
    [
        (SandboxSettings(run_mode="full"), PermissionsConfig(), RunMode.FULL),
        (
            SandboxSettings(sandbox=False, security_grading=False),
            PermissionsConfig(),
            RunMode.FULL,
        ),
        (
            SandboxSettings(),
            PermissionsConfig(default_mode="full"),
            RunMode.FULL,
        ),
        (SandboxSettings(run_mode="standard"), PermissionsConfig(), RunMode.STANDARD),
        (SandboxSettings(run_mode="trusted"), PermissionsConfig(), RunMode.TRUSTED),
    ],
)
def test_project_mode_preserves_explicit_operator_choice(
    sandbox: SandboxSettings,
    permissions: PermissionsConfig,
    expected: RunMode,
) -> None:
    config = SimpleNamespace(sandbox=sandbox, permissions=permissions)
    assert project_default_run_mode(config) is expected
```

- [ ] **Step 2: Run the new mode tests and observe missing helper failures**

Run:

```bash
uv run pytest tests/test_sandbox/test_run_modes.py -q
```

Expected: FAIL because `project_default_run_mode` and
`sandbox_runtime_capability_mode` do not exist.

- [ ] **Step 3: Implement explicitness-aware mode helpers**

```python
def _field_was_set(model: Any, field_name: str) -> bool:
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", None)
    return field_name in fields_set if fields_set is not None else False


def _full_mode_is_explicit(config: Any) -> bool:
    sandbox = getattr(config, "sandbox", None)
    permissions = getattr(config, "permissions", None)
    if getattr(sandbox, "run_mode", None) is not None:
        return normalize_run_mode(sandbox.run_mode) is RunMode.FULL
    if _field_was_set(sandbox, "sandbox") and not bool(sandbox.sandbox):
        return True
    return str(getattr(permissions, "default_mode", "")).strip().lower() == "full"


def project_default_run_mode(config: Any) -> RunMode:
    configured = config_run_mode(config)
    if configured is not RunMode.FULL or _full_mode_is_explicit(config):
        return configured
    return RunMode.STANDARD


def sandbox_runtime_capability_mode(config: Any) -> RunMode:
    configured = config_run_mode(config)
    if configured is RunMode.FULL and not _full_mode_is_explicit(config):
        return RunMode.STANDARD
    return configured
```

Export both helpers.

- [ ] **Step 4: Add failing hybrid-runtime tests**

```python
@pytest.mark.asyncio
async def test_bare_full_default_boots_standard_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[tuple[SandboxSettings, RunMode]] = []

    def fake_configure(settings: SandboxSettings, **kwargs: Any) -> Any:
        captured.append((settings, kwargs["default_run_mode"]))
        return SimpleNamespace(
            effective=SimpleNamespace(
                sandbox_enabled=True,
                as_dict=lambda: {"sandbox_enabled": True},
            )
        )

    monkeypatch.setattr("opensquilla.sandbox.integration.configure_runtime", fake_configure)
    services = await build_services(
        config=GatewayConfig(
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            sandbox={"auto_setup": False},
            control_ui={"enabled": False},
            channels={"channels": []},
            mcp={"enabled": False},
            memory={"flush_enabled": False},
        ),
        session_db_path=str(tmp_path / "sessions.sqlite"),
        seed_agent_workspaces=False,
    )
    try:
        settings, default_mode = captured[0]
        assert settings.run_mode == "standard"
        assert settings.sandbox is True
        assert settings.security_grading is True
        assert settings.network_default == "proxy_allowlist"
        assert default_mode is RunMode.FULL
    finally:
        await services.close()
```

Also add:

```python
def test_hybrid_runtime_uses_full_without_context_and_standard_with_context(
    tmp_path: Path,
) -> None:
    try:
        runtime = configure_runtime(
            SandboxSettings(
                run_mode="standard",
                backend="noop",
                allow_legacy_mode=True,
            ),
            default_run_mode=RunMode.FULL,
            workspace=tmp_path,
        )
        assert runtime.default_run_mode is RunMode.FULL
        policy = build_policy(
            SecurityLevel.STANDARD,
            "shell.exec",
            tmp_path,
            runtime.settings,
            trusted=True,
        )
        request = build_request(
            action_kind="shell.exec",
            argv=("cmd", "/c", "echo ok"),
            cwd=tmp_path,
            policy=policy,
        )
        assert request.run_mode == RunMode.FULL.value
        assert full_host_access_for_context(None) is True
        standard_ctx = ToolContext(run_mode="standard")
        assert full_host_access_for_context(standard_ctx) is False
        token = current_tool_context.set(standard_ctx)
        try:
            standard_request = build_request(
                action_kind="shell.exec",
                argv=("cmd", "/c", "echo standard"),
                cwd=tmp_path,
                policy=policy,
            )
        finally:
            current_tool_context.reset(token)
        assert standard_request.run_mode == RunMode.STANDARD.value
    finally:
        reset_runtime()
```

Update the two contradictory legacy tests rather than merely adding coverage:

- rename/update
  `test_build_services_normalizes_default_full_host_access_for_sandbox_runtime`
  so bare config asserts Standard capability settings but a Full request
  default;
- rename/update
  `test_explicit_standard_context_disables_global_full_fallback` so a disabled
  runtime makes the stale Standard context resolve to Full, while the new
  enabled-runtime companion proves a valid Standard context still wins.

Use real `SandboxSettings` and `PermissionsConfig` instances for explicitness
tests. In particular, the explicit `sandbox=false` case must exercise
Pydantic's `model_fields_set`; a `SimpleNamespace` cannot prove that behavior.

- [ ] **Step 5: Run hybrid-runtime tests and observe current Full/Noop behavior**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_router_boot.py::test_bare_full_default_boots_standard_capability \
  tests/test_sandbox/test_run_mode_routing.py \
  tests/test_sandbox/test_windows_default_request_context.py \
  tests/test_tools/test_filesystem_read_workspace.py::test_sandbox_disabled_write_ignores_stale_restricted_tool_context \
  -q
```

Expected: FAIL because boot turns implicit Full into `sandbox=False`,
`SandboxRuntime` has no request default, and stale context wins.

- [ ] **Step 6: Add the runtime default and use capability settings at boot**

Implement:

```python
@dataclass
class SandboxRuntime:
    settings: SandboxSettings
    effective: EffectiveMode
    backend: Backend
    gate: ApprovalGate
    ledger: DenialLedger
    cache: StaleOutputCache
    workspace: Path
    approval_queue: Any
    default_run_mode: RunMode


def configure_runtime(
    settings: SandboxSettings,
    *,
    approval_queue: _ApprovalQueueLike | None = None,
    stale_cache: StaleOutputCache | None = None,
    workspace: Path | None = None,
    default_run_mode: RunMode | str | None = None,
) -> SandboxRuntime:
    request_default = (
        normalize_run_mode(default_run_mode)
        if default_run_mode is not None
        else normalize_run_mode(settings.run_mode)
    )
    # Existing backend/gate construction remains unchanged until the final
    # runtime construction:
    _runtime = SandboxRuntime(
        settings=settings,
        effective=effective,
        backend=backend,
        gate=gate,
        ledger=ledger,
        cache=cache,
        workspace=ws,
        approval_queue=queue,
        default_run_mode=request_default,
    )
```

In `boot.py`, preserve an explicit Standard/Trusted `config.sandbox` object so
its legacy grading fields are not overwritten. For Full, patch from
`sandbox_runtime_capability_mode(config)`: explicit Full stays Full, while only
implicit/bare Full is copied to Standard capability settings.

```python
def _sandbox_settings_for_runtime(config: GatewayConfig) -> SandboxSettings:
    configured = config_run_mode(config)
    if configured in {RunMode.STANDARD, RunMode.TRUSTED}:
        return config.sandbox
    patch = run_mode_config_patch(sandbox_runtime_capability_mode(config))
    return config.sandbox.model_copy(
        update={
            "run_mode": patch.run_mode.value,
            "sandbox": patch.sandbox,
            "security_grading": patch.security_grading,
            "network_default": patch.network_default,
        }
    )
```

Always call:

```python
configure_runtime(
    runtime_settings,
    workspace=Path(config.workspace_dir) if config.workspace_dir else None,
    default_run_mode=config_run_mode(config),
)
```

Replace all three contextless/request-policy reads of
`runtime.settings.run_mode` in `integration.py` with
`runtime.default_run_mode`: `_resolve_request_run_mode`, `gate_action`'s
`configured_mode`, and `_runtime_is_full_host_access`. Keep capability/backend
construction reading `settings.run_mode`. Verify the distinction with:

```bash
rg '(runtime|rt)\.settings.*run_mode|settings.*run_mode' \
  src/opensquilla/sandbox/integration.py
```

- [ ] **Step 7: Make disabled runtime dominate stale context and extend status**

Implement at the top of `full_host_access_for_context`:

```python
runtime = None
try:
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
except Exception:
    pass
if runtime is not None and not runtime.effective.sandbox_enabled:
    return True
```

When no usable context exists, return whether `runtime.default_run_mode` is
Full. Add `project_default_run_mode`, `runtime_capability_run_mode`, and
`runtime_sandbox_required` fields to `status_payload` while preserving the
existing ordinary `run_mode`. Do not infer backend/setup readiness from the
synchronous config-only payload; existing asynchronous `sandbox.setup.status`
remains the source of readiness. Add status tests for bare config and explicit
Full/Standard: bare asserts ordinary Full, project Standard, capability
Standard, `runtime_sandbox_required=true`, and the existing ordinary
`execution_target=host`; explicit Full reports Full/Full/Full/false; explicit
Standard reports Standard/Standard/Standard/true.

Rename/update
`test_explicit_standard_context_disables_global_full_fallback`: a runtime whose
effective sandbox is disabled makes that Standard context stale and therefore
returns Full. Add the complementary enabled-runtime assertion proving a valid
Standard context still wins over `default_run_mode=Full`.

- [ ] **Step 8: Run focused and regression tests**

Run:

```bash
uv run pytest \
  tests/test_sandbox/test_run_modes.py \
  tests/test_sandbox/test_run_mode_routing.py \
  tests/test_sandbox/test_windows_default_request_context.py \
  tests/test_sandbox/test_cli_run_modes.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_tools/test_filesystem_read_workspace.py::test_sandbox_disabled_write_ignores_stale_restricted_tool_context \
  -q
```

Expected: PASS.

- [ ] **Step 9: Commit the runtime separation**

```bash
git add \
  src/opensquilla/sandbox/run_mode.py \
  src/opensquilla/sandbox/integration.py \
  src/opensquilla/gateway/boot.py \
  src/opensquilla/tools/run_mode.py \
  src/opensquilla/sandbox/status.py \
  tests/test_sandbox/test_run_modes.py \
  tests/test_sandbox/test_run_mode_routing.py \
  tests/test_sandbox/test_windows_default_request_context.py \
  tests/test_sandbox/test_cli_run_modes.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_tools/test_filesystem_read_workspace.py
git commit -m "fix: prepare sandbox capability for project turns"
```

---

### Task 2: Validate Project Identity and Persist Mode Provenance

**Files:**
- Modify: `src/opensquilla/project_workspaces.py`
- Modify: `src/opensquilla/sandbox/run_context.py`
- Modify: `src/opensquilla/sandbox/escalation.py`
- Modify: `src/opensquilla/gateway/rpc_workspaces.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Test: `tests/test_gateway/test_rpc_workspaces.py`
- Test: `tests/test_gateway/test_project_workspace_execution.py`
- Test: `tests/test_sandbox/test_run_context.py`
- Test: `tests/test_sandbox/test_run_context_grants.py`

**Interfaces:**
- Consumes: `project_default_run_mode(config)` from Task 1
- Produces: `ProjectWorkspaceGuard`
- Produces: `ValidatedProjectWorkspace`
- Produces: `ProjectWorkspaceStateError.reason`
- Produces: `resolve_validated_project_workspace(storage, workspace_id)`
- Adds: `RunContext.run_mode_source: str | None`

- [ ] **Step 1: Write failing canonical-identity and availability tests**

```python
@pytest.mark.asyncio
async def test_validated_workspace_rejects_symlink_retarget(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    trusted.mkdir()
    replacement.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(trusted), "trusted": True},
        ctx,
    )
    moved = tmp_path / "trusted-old"
    trusted.rename(moved)
    trusted.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(
            storage,
            opened["workspace"]["id"],
        )
    assert raised.value.reason == "canonical_changed"


@pytest.mark.asyncio
async def test_workspace_payload_uses_strict_validator_for_availability(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, _storage = workspace_ctx
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    trusted.mkdir()
    replacement.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(trusted), "trusted": True},
        ctx,
    )
    trusted.rename(tmp_path / "trusted-old")
    trusted.symlink_to(replacement, target_is_directory=True)
    listed = await _handle_workspaces_list(None, ctx)
    row = next(
        item
        for item in listed["workspaces"]
        if item["id"] == opened["workspace"]["id"]
    )
    assert row["available"] is False
    assert row["availabilityReason"] == "canonical_changed"
```

Add named validator tests for `not_found`, `removed`, `untrusted`, missing
directory, file path, filesystem root, POSIX inaccessible directory (restore
mode in `finally`), POSIX symlink retarget, and Windows junction retarget when
available. Each test asserts the exact stable reason and the workspace-list
availability projection.

- [ ] **Step 2: Run project validation tests and observe current `is_dir` acceptance**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_rpc_workspaces.py \
  tests/test_gateway/test_project_workspace_execution.py \
  -q
```

Expected: new retarget/inaccessible tests FAIL.

- [ ] **Step 3: Implement validator types and stable error reasons**

```python
ProjectWorkspaceStateReason = Literal[
    "not_found",
    "removed",
    "untrusted",
    "unavailable",
    "canonical_changed",
    "guard_required",
    "binding_changed",
]


class ProjectWorkspaceStateError(RuntimeError):
    def __init__(self, reason: ProjectWorkspaceStateReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProjectWorkspaceGuard:
    workspace_id: str
    path: str
    path_key: str


@dataclass(frozen=True)
class ValidatedProjectWorkspace:
    workspace: ProjectWorkspace
    canonical_path: str
    guard: ProjectWorkspaceGuard
```

Implement a synchronous filesystem probe used through `asyncio.to_thread`:

```python
def _validate_stored_project_path(workspace: ProjectWorkspace) -> str:
    try:
        candidate = Path(workspace.path).expanduser().resolve(strict=True)
        if not candidate.is_dir() or candidate.parent == candidate:
            raise ProjectWorkspaceStateError("unavailable")
        with os.scandir(candidate):
            pass
    except ProjectWorkspaceStateError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectWorkspaceStateError("unavailable") from exc
    canonical = _normalized_path(candidate)
    if project_path_key(candidate, strict=True) != workspace.path_key:
        raise ProjectWorkspaceStateError("canonical_changed")
    if canonical != workspace.path:
        raise ProjectWorkspaceStateError("canonical_changed")
    return canonical
```

`resolve_validated_project_workspace` checks row/trust state, runs the probe,
and returns the guard. Use the same result for `available` and
`availabilityReason`.

- [ ] **Step 4: Write failing mode-provenance tests**

```python
def test_run_context_round_trips_mode_source() -> None:
    context = RunContext(
        run_mode=RunMode.STANDARD,
        workspace="/tmp/project",
        run_mode_source="project_default",
    )
    restored = run_context_from_origin_payload(context.to_origin_payload())
    assert restored is not None
    assert restored.run_mode_source == "project_default"


@pytest.mark.asyncio
async def test_new_project_uses_standard_with_project_default_provenance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        key = "agent:main:webchat:project-provenance"
        response = await get_dispatcher().dispatch(
            "project-provenance",
            "chat.send",
            {
                "sessionKey": key,
                "message": "pwd",
                "workspaceId": project.workspace_id,
                "clientRequestId": "project-provenance-request",
            },
            stack.context,
        )
        assert response.ok is True
        session = await stack.storage.get_session(key)
        assert session is not None and session.origin is not None
        payload = session.origin[RUN_CONTEXT_ORIGIN_KEY]
        assert payload["run_mode"] == "standard"
        assert payload["run_mode_source"] == "project_default"
```

Add
`test_explicit_full_project_uses_operator_default_provenance`,
`test_explicit_standard_and_trusted_project_modes_round_trip`, and
`test_sandbox_run_mode_set_persists_user_provenance`. Each rehydrates the
stored origin and asserts both the exact mode and source.

- [ ] **Step 5: Run provenance tests and observe missing field/default Full**

Run:

```bash
uv run pytest \
  tests/test_sandbox/test_run_context.py \
  tests/test_gateway/test_project_workspace_execution.py::test_new_project_uses_standard_with_project_default_provenance \
  -q
```

Expected: FAIL.

- [ ] **Step 6: Add provenance and use the project default on creation**

Add:

```python
@dataclass(frozen=True)
class RunContext:
    run_mode: RunMode
    workspace: str | None = None
    # Existing grant fields remain in place.
    run_mode_source: str | None = None
    source: str = "default"
```

Serialize as `"run_mode_source"` and accept only
`{"project_default", "operator_default", "user"}` when hydrating.
`set_run_mode` writes `run_mode_source="user"`.

Audit every `RunContext(...)` copy/reconstruction in `run_context.py`,
`escalation.py`, and gateway session creation so it copies
`run_mode_source`. Add a regression that selects user Full, materializes a
mount/domain/bundle grant, rehydrates and merges an overlay, and still resolves
to Full with `run_mode_source="user"` under an otherwise implicit config.

When creating a project session:

```python
mode = project_default_run_mode(ctx.config)
mode_source = (
    "project_default"
    if mode is RunMode.STANDARD and config_run_mode(ctx.config) is RunMode.FULL
    else "operator_default"
)
RunContext(
    run_mode=mode,
    workspace=selected_workspace.path,
    run_mode_source=mode_source,
    source="project_workspace",
)
```

- [ ] **Step 7: Add safe legacy-project resolution**

Add the pure helper to `sandbox/run_context.py`; the Task 4 project runtime
boundary imports it. Legacy project contexts without provenance are interpreted
as Standard only when the current project default is implicit Standard:

```python
def effective_project_run_mode(context: RunContext, config: Any) -> RunContext:
    if (
        context.run_mode is RunMode.FULL
        and context.run_mode_source is None
        and config_run_mode(config) is RunMode.FULL
        and project_default_run_mode(config) is RunMode.STANDARD
    ):
        return replace(
            context,
            run_mode=RunMode.STANDARD,
            run_mode_source="project_default",
        )
    return context
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_rpc_workspaces.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_sandbox/test_run_context.py \
  tests/test_sandbox/test_run_context_grants.py \
  -q
```

Expected: PASS.

- [ ] **Step 9: Commit project validation and provenance**

```bash
git add \
  src/opensquilla/project_workspaces.py \
  src/opensquilla/sandbox/run_context.py \
  src/opensquilla/sandbox/escalation.py \
  src/opensquilla/gateway/rpc_workspaces.py \
  src/opensquilla/gateway/rpc_sessions.py \
  tests/test_gateway/test_rpc_workspaces.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_sandbox/test_run_context.py \
  tests/test_sandbox/test_run_context_grants.py
git commit -m "fix: validate trusted project workspace identity"
```

---

### Task 3: Guard and Atomically Accept Project Turns

**Files:**
- Modify: `src/opensquilla/session/storage.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/gateway/channel_dispatch.py`
- Test: `tests/test_session/test_turn_acceptance_storage.py`
- Test: `tests/test_gateway/test_turn_ingress_rpc.py`
- Test: `tests/test_gateway/test_channel_turn_ingress.py`
- Test: `tests/test_gateway/test_project_workspace_execution.py`

**Interfaces:**
- Consumes: `ProjectWorkspaceGuard` and `ProjectWorkspaceStateError`
- Changes: `SessionStorage.accept_turn(entry: TranscriptEntry, *, expected_epoch: int, updated_at: int, task_record: AgentTaskRecord | None, source_scope: str, request_session_key: str, client_request_id: str, request_fingerprint: str, session_node: SessionNode | None = None, reset_from_session_id: str | None = None, initial_transcript_entries: tuple[TranscriptEntry, ...] = (), session_updates: dict[str, Any] | None = None, merge_into_task: bool = False, workspace_guard: ProjectWorkspaceGuard | None = None) -> TurnAcceptanceResult`
- Produces: a prepared, atomic no-`TaskRuntime` acceptance path

- [ ] **Step 1: Write failing storage-guard tests**

```python
@pytest.mark.asyncio
async def test_accept_turn_rechecks_project_guard_in_transaction(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        await storage.remove_project_workspace(project.workspace_id)

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("guarded-message"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-guarded",
                request_fingerprint="sha256:guarded",
                session_node=node,
                workspace_guard=guard,
            )
        assert raised.value.reason == "removed"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-guarded",
        ) is None
    finally:
        await storage.close()
```

Add four named tests by adapting the same fixture:

- `test_accept_turn_rejects_project_binding_mismatch` passes a guard for a
  second project and asserts `binding_changed` with no writes.
- `test_accept_turn_requires_guard_for_bound_session` passes no guard and
  asserts `guard_required` with no writes.
- `test_accept_turn_without_task_persists_nullable_receipt` accepts an active
  guard with `task_record=None`, then asserts one message and a receipt whose
  `task_id` is null.
- `test_accept_turn_replays_before_removed_project_guard` first accepts, then
  removes the project and repeats the same request; it asserts `replayed=True`
  and the original receipt ID. Reusing that request ID with a changed
  fingerprint still raises `TurnIngressConflictError` before the removed guard.

- [ ] **Step 2: Run storage tests and observe signature/atomicity failures**

Run:

```bash
uv run pytest tests/test_session/test_turn_acceptance_storage.py -q
```

Expected: FAIL because `task_record` is required and no guard is checked.

- [ ] **Step 3: Implement guard verification inside `accept_turn`**

After receipt replay/conflict and before session writes:

```python
async def _verify_project_workspace_guard(
    conn: aiosqlite.Connection,
    *,
    session_node: SessionNode | None,
    entry_session_key: str,
    workspace_guard: ProjectWorkspaceGuard | None,
) -> None:
    async with conn.execute(
        "SELECT workspace_id FROM sessions WHERE session_key = ?",
        (entry_session_key,),
    ) as cursor:
        session_row = await cursor.fetchone()
    persisted_bound_id = (
        session_row["workspace_id"] if session_row is not None else None
    )
    prepared_bound_id = (
        session_node.workspace_id
        if session_node is not None
        else persisted_bound_id
    )
    if (
        session_row is not None
        and session_node is not None
        and persisted_bound_id != prepared_bound_id
    ):
        raise ProjectWorkspaceStateError("binding_changed")
    bound_id = prepared_bound_id
    if bound_id is None:
        if workspace_guard is not None:
            raise ProjectWorkspaceStateError("binding_changed")
        return
    if workspace_guard is None:
        raise ProjectWorkspaceStateError("guard_required")
    if workspace_guard.workspace_id != bound_id:
        raise ProjectWorkspaceStateError("binding_changed")
    async with conn.execute(
        """
        SELECT workspace_id, path, path_key, removed_at, trusted_at
        FROM project_workspaces
        WHERE workspace_id = ?
        """,
        (bound_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ProjectWorkspaceStateError("not_found")
    if row["removed_at"] is not None:
        raise ProjectWorkspaceStateError("removed")
    if row["trusted_at"] is None:
        raise ProjectWorkspaceStateError("untrusted")
    if row["path"] != workspace_guard.path or row["path_key"] != workspace_guard.path_key:
        raise ProjectWorkspaceStateError("binding_changed")
```

Allow `task_record=None`, skip task validation/insert, and persist a receipt
with a null task ID. Keep `merge_into_task=True` invalid without a task.

- [ ] **Step 4: Write failing receipt-first RPC tests**

```python
@pytest.mark.asyncio
async def test_replay_survives_project_removal_and_missing_directory(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        params = {
            "sessionKey": "agent:main:webchat:workspace-replay",
            "message": "pwd",
            "workspaceId": project.workspace_id,
            "clientRequestId": "stable-project-request",
        }
        first = await get_dispatcher().dispatch(
            "workspace-replay-first",
            "chat.send",
            params,
            stack.context,
        )
        assert first.ok is True
        await stack.storage.remove_project_workspace(project.workspace_id)
        project_path.rmdir()
        replay = await get_dispatcher().dispatch(
            "workspace-replay-second",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["task_id"] == first.payload["task_id"]


@pytest.mark.asyncio
async def test_replay_conflict_precedes_workspace_unavailable(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        params = {
            "sessionKey": "agent:main:webchat:workspace-conflict",
            "message": "first",
            "workspaceId": project.workspace_id,
            "clientRequestId": "stable-conflict-request",
        }
        first = await get_dispatcher().dispatch(
            "workspace-conflict-first",
            "chat.send",
            params,
            stack.context,
        )
        assert first.ok is True
        await stack.storage.remove_project_workspace(project.workspace_id)
        conflict = await get_dispatcher().dispatch(
            "workspace-conflict-second",
            "chat.send",
            {**params, "message": "changed"},
            stack.context,
        )
        assert conflict.ok is False
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
```

- [ ] **Step 5: Run RPC tests and observe current `WORKSPACE_*` precedence**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_turn_ingress_rpc.py \
  tests/test_gateway/test_project_workspace_execution.py \
  -q
```

Expected: new tests FAIL because workspace state is checked before the receipt.

- [ ] **Step 6: Reorder ingress and decouple prepared acceptance from TaskRuntime**

In `rpc_sessions.py`:

1. validate syntactic params and compute `request_identity`;
2. query/replay/conflict-check the durable receipt;
3. resolve the existing/new project and produce a guard;
4. ingest attachments;
5. prepare intent and message without writes;
6. call `accept_turn` with an optional task;
7. activate `TaskRuntime` or schedule the direct runner only for a new commit.

Split the current boolean:

```python
supports_prepared_acceptance = all(
    callable(value)
    for value in (
        prepare_intent,
        getattr(ctx.session_manager, "prepare_message", None),
        getattr(storage, "accept_turn", None),
    )
)
supports_task_runtime_activation = (
    supports_prepared_acceptance
    and task_runtime_candidate is not None
    and callable(getattr(task_runtime_candidate, "reserve", None))
    and callable(getattr(task_runtime_candidate, "activate", None))
    and callable(getattr(task_runtime_candidate, "abort_reservation", None))
)
```

The prepared direct-runner path calls
`accept_turn(task_record=None, workspace_guard=guard)` and schedules `_run`
after a non-replayed commit.

- [ ] **Step 7: Pass guards from every acceptance caller**

Keep `_accept_channel_runtime_turn`'s durable
`get_turn_ingress_receipt`/fingerprint check as the first stateful operation.
Only after that preflight returns no replay does `channel_dispatch.py` resolve
the bound project and supply its guard to `accept_turn`. Ordinary sessions pass
`None`. A project-bound new acceptance that cannot resolve a guard returns the
same workspace error and does not persist. Add channel tests proving a
same-fingerprint replay still succeeds after removal and a changed fingerprint
still returns the conflict before workspace validation.

- [ ] **Step 8: Add no-TaskRuntime rollback/replay tests**

```python
@pytest.mark.asyncio
async def test_project_first_send_without_task_runtime_is_atomic(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        ran = asyncio.Event()

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                ran.set()
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        key = "agent:main:webchat:direct-project"
        accepted = await get_dispatcher().dispatch(
            "direct-project",
            "chat.send",
            {
                "sessionKey": key,
                "message": "pwd",
                "workspaceId": project.workspace_id,
                "clientRequestId": "direct-1",
            },
            stack.context,
        )
        assert accepted.ok is True
        await asyncio.wait_for(ran.wait(), timeout=2)
        session = await stack.storage.get_session(key)
        assert session is not None
        assert len(await stack.storage.get_transcript(session.session_id)) == 1
        async with stack.storage.conn.execute(
            "SELECT task_id FROM turn_ingress_receipts WHERE accepted_session_key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row["task_id"] is None


@pytest.mark.asyncio
async def test_attachment_failure_without_task_runtime_leaves_no_project_session(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        stack.context.task_runtime = None
        key = "agent:main:webchat:missing-attachment-project"
        failed = await get_dispatcher().dispatch(
            "missing-attachment-project",
            "chat.send",
            {
                "sessionKey": key,
                "message": "inspect",
                "workspaceId": project.workspace_id,
                "clientRequestId": "missing-attachment-request",
                "attachments": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "name": "missing.txt",
                        "file_uuid": "missing-upload",
                    }
                ],
            },
            stack.context,
        )
        assert failed.ok is False
        assert await stack.storage.get_session(key) is None
```

Call the same accepted request twice and assert the direct runner starts once.

- [ ] **Step 9: Run acceptance suites**

Run:

```bash
uv run pytest \
  tests/test_session/test_turn_acceptance_storage.py \
  tests/test_gateway/test_turn_ingress_rpc.py \
  tests/test_gateway/test_channel_turn_ingress.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_turn_ingress_intents.py \
  -q
```

Expected: PASS.

- [ ] **Step 10: Commit guarded acceptance**

```bash
git add \
  src/opensquilla/session/storage.py \
  src/opensquilla/gateway/rpc_sessions.py \
  src/opensquilla/gateway/channel_dispatch.py \
  tests/test_session/test_turn_acceptance_storage.py \
  tests/test_gateway/test_turn_ingress_rpc.py \
  tests/test_gateway/test_channel_turn_ingress.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_turn_ingress_intents.py
git commit -m "fix: atomically guard project turn acceptance"
```

---

### Task 4: Revalidate Project Workspace on Every Execution and Sandbox RPC

**Files:**
- Create: `src/opensquilla/gateway/project_workspace_runtime.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Modify: `src/opensquilla/gateway/boot.py`
- Modify: `src/opensquilla/gateway/channel_dispatch.py`
- Modify: `src/opensquilla/cli/agent_cmd.py`
- Modify: `src/opensquilla/cli/tui/standalone_runtime.py`
- Test: `tests/test_gateway/test_project_workspace_execution.py`
- Test: `tests/test_gateway/test_rpc_sessions.py`
- Test: `tests/test_sandbox/test_rpc_sandbox_access.py`
- Test: `tests/test_gateway/test_router_boot.py`
- Test: `tests/test_gateway/test_channel_dispatch_realtime.py`
- Test: `tests/test_cli/test_agent_cmd.py`
- Test: `tests/unit/cli/tui/test_runtime_adapters.py`

**Interfaces:**
- Consumes: Task 2 validator and `effective_project_run_mode`
- Produces: `resolve_session_project_workspace(storage, session) -> ValidatedProjectWorkspace | None`
- Produces: `authoritative_project_run_context(*, storage: SessionStorage, session_manager: Any, session: SessionNode, config: Any, default_workspace: str | None) -> tuple[RunContext, ProjectWorkspaceGuard | None]`
- Produces: `project_workspace_snapshot(storage: SessionStorage, session: SessionNode) -> dict[str, Any] | None`
- Produces: `map_project_workspace_error(error: ProjectWorkspaceStateError, *, owner: bool) -> RpcHandlerError`

- [ ] **Step 1: Add failing continuation, queued-run, and tampered-origin tests**

```python
@pytest.mark.asyncio
async def test_continue_rejects_retargeted_project_before_runner_starts(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        key = "agent:main:webchat:retargeted-continue"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "standard",
                        "workspace": project.path,
                    }
                },
            )
        )
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        project_path.rename(tmp_path / "project-old")
        project_path.symlink_to(replacement, target_is_directory=True)

        class Runner:
            calls: list[dict[str, Any]] = []

            async def run(self, message: str, session_key: str, **kwargs: Any):
                self.calls.append(kwargs)
                yield DoneEvent()

        runner = Runner()
        stack.context.task_runtime = None
        stack.context.turn_runner = runner
        result = await get_dispatcher().dispatch(
            "retargeted-continue",
            "chat.send",
            {"sessionKey": key, "message": "pwd", "intent": "continue"},
            stack.context,
        )
        assert result.ok is False
        assert result.error.code == "WORKSPACE_UNAVAILABLE"
        assert result.error.details["reason"] == "canonical_changed"
        assert runner.calls == []


@pytest.mark.asyncio
async def test_origin_workspace_tamper_cannot_change_project_tool_context(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        outside = tmp_path / "outside"
        outside.mkdir()
        key = "agent:main:webchat:tampered-origin"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "standard",
                        "workspace": str(outside),
                    }
                },
            )
        )
        captured: dict[str, Any] = {}

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        result = await get_dispatcher().dispatch(
            "tampered-origin",
            "chat.send",
            {"sessionKey": key, "message": "pwd", "intent": "continue"},
            stack.context,
        )
        assert result.ok is True
        assert captured["tool_context"].workspace_dir == project.path


@pytest.mark.asyncio
async def test_queued_turn_revalidates_project_before_tool_context(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        key = "agent:main:webchat:queued-retarget"
        await stack.storage.upsert_session(
            SessionNode(session_key=key, workspace_id=project.workspace_id)
        )
        envelope = build_cli_route_envelope(session_key=key, agent_id="main")
        envelope.metadata["sandbox_run_context"] = {
            "run_mode": "standard",
            "workspace": project.path,
        }
        object.__setattr__(envelope, "sandbox_run_context_fresh", True)
        run = SimpleNamespace(
            agent_id="main",
            task_id="queued-retarget-task",
            session_key=key,
            message="pwd",
            envelope=envelope,
            attachments=[],
            input_provenance={},
            run_kind="interactive",
            no_memory_capture=False,
            ingress_pipeline_steps=[],
            semantic_message=None,
            stream_event_sink=None,
        )
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        project_path.rename(tmp_path / "project-old")
        project_path.symlink_to(replacement, target_is_directory=True)

        class RecordingTurnRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def run(
                self,
                message: str,
                session_key: str,
                **kwargs: Any,
            ):
                self.calls.append(kwargs)
                yield DoneEvent()

        turn_runner = RecordingTurnRunner()
        with pytest.raises(ProjectWorkspaceStateError):
            await dispatch_task_runtime_turn(
                run,
                config=stack.context.config,
                session_manager=stack.manager,
                turn_runner=turn_runner,
                event_emitter=AsyncMock(),
            )
        assert turn_runner.calls == []
```

Add named execution tests for reset, fork, subscription bootstrap, direct Web
runner, CLI direct runner, and channel/queued runtime. For each path, mutate
the project after acceptance but before the runner barrier releases and assert
the runner/tool is never called. Cover missing directory, file replacement,
root path, POSIX symlink retarget, and Windows junction retarget where the host
supports it. The corresponding valid-path tests assert the authoritative
project path overwrites a tampered origin.

For the concrete direct-entry tests:

- pass `session_manager` into `channel_dispatch._run_turn_with_streaming`;
  after channel acceptance but immediately before `tool_context_from_envelope`,
  load the bound row through `authoritative_project_run_context`. A barrier
  retarget test in `test_channel_dispatch_realtime.py` proves neither streaming
  nor batch runner starts on failure;
- in `cli.agent_cmd.run_agent_once`, resolve an existing session through the
  same helper after transcript preparation and before `ToolContext` creation;
- in `cli.tui.standalone_runtime`, resolve the active session afresh for every
  `_dispatch_input`, rather than capturing one agent workspace when the REPL
  starts.

The CLI commands cannot create a new project binding, but they can resume an
already-bound project session, so they are workspace-capable execution paths.
Scheduler/cron session keys cannot select or resume a project workspace and are
out of this boundary. Add one valid and one retargeted bound-session test for
each CLI path.

- [ ] **Step 2: Run project execution suites and observe stale-origin execution**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_gateway/test_channel_dispatch_realtime.py \
  tests/test_cli/test_agent_cmd.py \
  tests/unit/cli/tui/test_runtime_adapters.py \
  -q
```

Expected: new tests FAIL.

- [ ] **Step 3: Implement the project runtime boundary**

Create `project_workspace_runtime.py` with:

```python
async def resolve_session_project_workspace(
    storage: SessionStorage,
    session: SessionNode,
) -> ValidatedProjectWorkspace | None:
    workspace_id = getattr(session, "workspace_id", None)
    if not workspace_id:
        return None
    return await resolve_validated_project_workspace(storage, workspace_id)


async def authoritative_project_run_context(
    *,
    storage: SessionStorage,
    session_manager: Any,
    session: SessionNode,
    config: Any,
    default_workspace: str | None,
) -> tuple[RunContext, ProjectWorkspaceGuard | None]:
    context = await get_run_context(
        session_manager,
        session.session_key,
        config=config,
        workspace=default_workspace,
        session_node=session,
    )
    validated = await resolve_session_project_workspace(storage, session)
    if validated is None:
        return context, None
    return (
        replace(
            effective_project_run_mode(context, config),
            workspace=validated.canonical_path,
        ),
        validated.guard,
    )
```

`project_workspace_snapshot` is display projection, not an execution guard. It
first reads `session.workspace_id` and the bound row directly. A missing bound
row returns an unavailable snapshot with the bound ID and reason `not_found`;
a removed row returns its retained name/path with `removed=true`,
`available=false`, and reason `removed`. For an active row, call the strict
validator and project any failure into `available=false` plus its stable
reason. Never propagate those state errors out of message subscription, because
durable history must remain readable.

Add one error mapper that produces owner-safe `RpcHandlerError` values. Unit
test every stable reason: `not_found`, `removed`, and `untrusted` map to
`WORKSPACE_NOT_FOUND`; the other four map to `WORKSPACE_UNAVAILABLE` and expose
only `details.reason`. A non-owner assertion verifies neither the project path
nor a low-level exception string appears in the message/details.

- [ ] **Step 4: Use the boundary at send, bootstrap, and queued dispatch**

Replace direct `get_run_context` calls for a resolved session in
`rpc_sessions.py` with `authoritative_project_run_context`.

In `dispatch_task_runtime_turn`, load the session and storage first, resolve the
authoritative context, replace the envelope's sandbox context/workspace, then
build `ToolContext`. If validation fails, emit a terminal workspace error and
do not call the runner.

The no-`TaskRuntime` `_run` closure and every direct CLI/channel execution path
must perform the same authoritative resolution after acceptance and
immediately before `ToolContext` creation. Add a barrier test that accepts a
turn, retargets the directory while execution is paused, releases the barrier,
and asserts no model/tool call occurs. TaskRuntime-backed channels rely on the
queued-dispatch check above, not the acceptance-time snapshot.

In `sessions.messages.subscribe`, load its concrete row and add both the binding
and projection to the existing return dictionary:

```python
session = await storage.get_session(key) if storage is not None else None
workspace_id = session.workspace_id if session is not None else None
project_snapshot = (
    await project_workspace_snapshot(storage, session)
    if storage is not None and session is not None
    else None
)

return {
    "subscribed": subscription_mgr is not None,
    "key": key,
    "workspaceId": workspace_id,
    "projectWorkspace": project_snapshot,
    "current_stream_seq": replay.current_stream_seq,
    "replay_complete": replay.replay_complete,
    "replay_gap_reason": replay.gap_reason,
    "replayed_count": replayed_count,
    "active_task_group_ids": active_task_group_ids,
    **task_state,
}
```

The snapshot includes `id`, `name`, `path`, `available`, `removed`, and
`availabilityReason`.

- [ ] **Step 5: Add failing project-aware sandbox RPC tests**

```python
@pytest.mark.asyncio
async def test_sandbox_context_get_uses_bound_project_not_agent_default(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    session = await manager.create(
        "agent:main:webchat:project-sandbox-context",
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    ctx = RpcContext(
        conn_id="project-sandbox-context",
        principal=OWNER,
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )
    try:
        payload = await _handle_sandbox_run_context_get(
            {"sessionKey": session.session_key},
            ctx,
        )
        assert payload["workspace"] == project.path
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_bound_project_workspace_cannot_be_changed(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    ctx, project_session, _project = project_sandbox_ctx
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_sandbox_workspace_set(
            {
                "sessionKey": project_session.session_key,
                "workspace": str(other),
            },
            ctx,
        )
    assert raised.value.code == "PROJECT_WORKSPACE_FIXED"
```

Add this reusable fixture, importing `AsyncIterator`, `pytest_asyncio`,
`GatewayConfig`, `Principal`, `ProjectWorkspace`, `SessionManager`, and
`SessionStorage`:

```python
@pytest_asyncio.fixture
async def project_sandbox_ctx(
    tmp_path: Path,
) -> AsyncIterator[tuple[RpcContext, SessionNode, ProjectWorkspace]]:
    storage = await SessionStorage.open(str(tmp_path / "sandbox-project.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    session = await manager.create(
        "agent:main:webchat:project-sandbox-fixture",
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    ctx = RpcContext(
        conn_id="project-sandbox-fixture",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read", "operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(workspace_dir=str(tmp_path / "agent-default")),
        session_manager=manager,
    )
    try:
        yield ctx, session, project
    finally:
        await storage.close()
```

Use it for named tests
`test_project_mount_validation_is_relative_to_authoritative_workspace` and
`test_project_sandbox_rpc_fails_when_workspace_becomes_unavailable`.

- [ ] **Step 6: Route sandbox RPCs through the authoritative boundary**

Replace `_workspace_for_session` with a helper returning session, context, and
guard. `run_context.get/set`, mount/domain/bundle methods use the validated
project workspace. `sandbox.workspace.set` rejects a non-null
`session.workspace_id`.

- [ ] **Step 7: Run all focused execution/RPC tests**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_rpc_sessions.py \
  tests/test_sandbox/test_rpc_sandbox_access.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_gateway/test_channel_dispatch_realtime.py \
  tests/test_cli/test_agent_cmd.py \
  tests/unit/cli/tui/test_runtime_adapters.py \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit authoritative per-turn resolution**

```bash
git add \
  src/opensquilla/gateway/project_workspace_runtime.py \
  src/opensquilla/gateway/rpc_sessions.py \
  src/opensquilla/gateway/rpc_sandbox.py \
  src/opensquilla/gateway/boot.py \
  src/opensquilla/gateway/channel_dispatch.py \
  src/opensquilla/cli/agent_cmd.py \
  src/opensquilla/cli/tui/standalone_runtime.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_rpc_sessions.py \
  tests/test_sandbox/test_rpc_sandbox_access.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_gateway/test_channel_dispatch_realtime.py \
  tests/test_cli/test_agent_cmd.py \
  tests/unit/cli/tui/test_runtime_adapters.py
git commit -m "fix: enforce project workspace on every turn"
```

---

### Task 5: Delete Project History in One Database Transaction

**Files:**
- Modify: `src/opensquilla/session/storage.py`
- Modify: `src/opensquilla/gateway/rpc_workspaces.py`
- Test: `tests/test_session/test_project_workspace_storage.py`
- Test: `tests/test_session/test_turn_acceptance_storage.py`
- Test: `tests/test_gateway/test_rpc_workspaces.py`

**Interfaces:**
- Produces: `_delete_session_rows(conn, session) -> None`
- Produces: `_cleanup_deleted_session(session) -> None`
- Changes: `delete_project_workspace_sessions` to one write transaction

- [ ] **Step 1: Write failing rollback and serialization tests**

```python
@pytest.mark.asyncio
async def test_project_history_delete_rolls_back_all_sessions_on_mid_delete_failure(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        first = SessionNode(
            session_key="agent:main:webchat:history-first",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        second = SessionNode(
            session_key="agent:main:webchat:history-second",
            workspace_id=project.workspace_id,
            created_at=200,
            updated_at=200,
        )
        await storage.upsert_session(first)
        await storage.upsert_session(second)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_second_session_delete
            BEFORE DELETE ON sessions
            WHEN OLD.session_key = 'agent:main:webchat:history-second'
            BEGIN
                SELECT RAISE(ABORT, 'injected delete failure');
            END
            """
        )
        await storage.conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="injected delete failure"):
            await storage.delete_project_workspace_sessions(project.workspace_id)

        assert await storage.get_session(first.session_key) is not None
        assert await storage.get_session(second.session_key) is not None
        assert await storage.get_project_workspace(project.workspace_id) == project
        assert project_path.is_dir()
    finally:
        await storage.close()
```

Extend `test_workspace_binding_and_history_delete_leave_project` to create two
sessions and seed one transcript row, context-state row, task, ingress receipt,
memory receipt, router decision, and turn error through their existing storage
APIs. After deletion, query each corresponding table and assert every count is
zero, while the `project_workspaces` row and `project_path` still exist. Assert
the returned keys are ordered by `(created_at, session_key)`.

Add a serialization test to `test_turn_acceptance_storage.py`:

```python
@pytest.mark.asyncio
async def test_project_accept_after_history_delete_commit_remains_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        old = SessionNode(
            session_key="agent:main:webchat:history-old",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        await storage.upsert_session(old)
        new = SessionNode(
            session_key="agent:main:webchat:history-new",
            workspace_id=project.workspace_id,
            created_at=200,
            updated_at=200,
        )
        entry = TranscriptEntry(
            session_id=new.session_id,
            session_key=new.session_key,
            message_id="history-new-message",
            role="user",
            content="new history",
            created_at=200,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        delete_entered = asyncio.Event()
        release_delete = asyncio.Event()
        original_delete_rows = storage._delete_session_rows

        async def paused_delete_rows(
            conn: aiosqlite.Connection,
            session: SessionNode,
        ) -> None:
            delete_entered.set()
            await release_delete.wait()
            await original_delete_rows(conn, session)

        monkeypatch.setattr(storage, "_delete_session_rows", paused_delete_rows)
        deleting = asyncio.create_task(
            storage.delete_project_workspace_sessions(project.workspace_id)
        )
        await asyncio.wait_for(delete_entered.wait(), timeout=2)
        accepting = asyncio.create_task(
            storage.accept_turn(
                entry,
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=new.session_key,
                client_request_id="history-new-request",
                request_fingerprint="sha256:history-new-request",
                session_node=new,
                workspace_guard=guard,
            )
        )
        await asyncio.sleep(0)
        assert accepting.done() is False
        release_delete.set()

        assert await deleting == [old.session_key]
        await accepting
        assert await storage.get_session(old.session_key) is None
        assert await storage.get_session(new.session_key) is not None
    finally:
        await storage.close()
```

Add the converse
`test_project_accept_committed_before_history_delete_is_in_deleted_snapshot`:
accept the guarded session first, call history deletion second, and assert both
the session and its receipt rows are deleted.

Add `test_project_remove_commit_serializes_before_accept_guard`: pause
`remove_project_workspace` after its row update while it holds the write
transaction, start guarded acceptance and assert it is waiting, release the
removal commit, then assert acceptance raises reason `removed` with no
session/message/receipt rows.

- [ ] **Step 2: Run storage tests and observe partial-delete behavior**

Run:

```bash
uv run pytest tests/test_session/test_project_workspace_storage.py -q
```

Expected: rollback test FAIL because each session uses a separate transaction.

- [ ] **Step 3: Extract connection-scoped row deletion**

Move the SQL portion of `delete_session` into:

```python
async def _delete_session_rows(
    self,
    conn: aiosqlite.Connection,
    session: SessionNode,
) -> None:
    for table in (
        "transcript_entries",
        "compacted_transcript_entries",
        "session_summaries",
    ):
        await conn.execute(
            f"DELETE FROM {table} WHERE session_id = ?",
            (session.session_id,),
        )
    await conn.execute(
        "DELETE FROM session_context_states WHERE session_id = ?",
        (session.session_id,),
    )
    for table in ("router_decisions", "turn_errors"):
        async with conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ) as cursor:
            exists = await cursor.fetchone() is not None
        if exists:
            await conn.execute(
                f"DELETE FROM {table} WHERE session_key = ?",
                (session.session_key,),
            )
    for table in ("agent_tasks", "memory_durable_receipts"):
        await conn.execute(
            f"DELETE FROM {table} WHERE session_key = ?",
            (session.session_key,),
        )
    await conn.execute(
        "DELETE FROM turn_ingress_receipts WHERE accepted_session_key = ?",
        (session.session_key,),
    )
    await conn.execute(
        "DELETE FROM sessions WHERE session_key = ?",
        (session.session_key,),
    )
```

Extract current material/meta-run cleanup into `_cleanup_deleted_session`.
`delete_session` calls the row helper inside one transaction and cleanup after.

- [ ] **Step 4: Implement batch deletion**

```python
async def delete_project_workspace_sessions(self, workspace_id: str) -> list[str]:
    deleted: list[SessionNode] = []
    async with self._write_transaction("delete_project_workspace_sessions") as conn:
        async with conn.execute(
            "SELECT removed_at FROM project_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ) as cursor:
            workspace_row = await cursor.fetchone()
        if workspace_row is None or workspace_row["removed_at"] is not None:
            raise KeyError(f"Project workspace not found: {workspace_id}")
        async with conn.execute(
            """
            SELECT * FROM sessions
            WHERE workspace_id = ?
            ORDER BY created_at ASC, session_key ASC
            """,
            (workspace_id,),
        ) as cursor:
            deleted = [
                SessionNode(**_deserialize_row(dict(row)))
                for row in await cursor.fetchall()
            ]
        for session in deleted:
            await self._delete_session_rows(conn, session)
    for session in deleted:
        try:
            await self._cleanup_deleted_session(session)
        except Exception:  # noqa: BLE001 - database commit is authoritative.
            log.warning(
                "project_workspace.session_material_cleanup_failed",
                workspace_id=workspace_id,
                session_key=session.session_key,
                exc_info=True,
            )
    return [session.session_key for session in deleted]
```

Have the RPC rely on this transaction instead of a separate active precheck.

- [ ] **Step 5: Test post-commit cleanup failure semantics**

Monkeypatch cleanup so the first session raises and the second records a call.
Assert database deletion remains complete, both cleanup attempts occurred, the
RPC reports the committed delete, and project files remain.

- [ ] **Step 6: Run storage and RPC history tests**

Run:

```bash
uv run pytest \
  tests/test_session/test_project_workspace_storage.py \
  tests/test_session/test_turn_acceptance_storage.py \
  tests/test_gateway/test_rpc_workspaces.py \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit atomic history deletion**

```bash
git add \
  src/opensquilla/session/storage.py \
  src/opensquilla/gateway/rpc_workspaces.py \
  tests/test_session/test_project_workspace_storage.py \
  tests/test_session/test_turn_acceptance_storage.py \
  tests/test_gateway/test_rpc_workspaces.py
git commit -m "fix: delete project history atomically"
```

---

### Task 6: Preserve Lexical Protected-Metadata Symlinks

**Files:**
- Modify: `src/opensquilla/sandbox/permissions.py`
- Modify: `src/opensquilla/sandbox/types.py`
- Modify: `src/opensquilla/sandbox/backend/linux_permissions.py`
- Modify: `src/opensquilla/sandbox/backend/linux_payload.py`
- Modify: `src/opensquilla/sandbox/backend/linux_helper.py`
- Modify: `src/opensquilla/sandbox/backend/seatbelt.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Modify: `src/opensquilla/sandbox/filesystem_worker.py`
- Modify: `src/opensquilla/sandbox/path_validation.py`
- Modify: `src/opensquilla/tools/builtin/filesystem.py`
- Modify: `src/opensquilla/tools/builtin/patch.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Test: `tests/test_sandbox/test_permission_profiles.py`
- Test: `tests/test_sandbox/test_linux_bwrap.py`
- Test: `tests/test_sandbox/test_linux_payload.py`
- Test: `tests/test_sandbox/test_linux_helper.py`
- Test: `tests/test_sandbox/test_filesystem_worker_policy.py`
- Test: `tests/test_sandbox/test_seatbelt_backend.py`
- Test: `tests/test_sandbox/test_windows_default_backend.py`
- Test: `tests/test_sandbox/test_path_access.py`

**Interfaces:**
- Produces: `FileSystemPermissionEntry.logical_path`
- Produces: `FileSystemPermissionProfile.protected_path_variants(path)`
- Preserves: canonical resolution for actual target access

- [ ] **Step 1: Add failing profile tests for lexical and canonical variants**

```python
@pytest.mark.parametrize("name", PROTECTED_METADATA_NAMES)
def test_protected_metadata_symlink_preserves_lexical_and_target(
    tmp_path: Path,
    name: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "metadata-target"
    workspace.mkdir()
    target.mkdir()
    (workspace / name).symlink_to(target, target_is_directory=True)
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)

    variants = profile.protected_path_variants(workspace / name)
    assert workspace / name in variants
    assert target in variants
    assert profile.resolve(workspace / name / "config") is FileSystemAccess.READ
    assert profile.resolve(target / "config") is FileSystemAccess.READ
```

Add a test proving effective-entry dedupe retains the lexical entry, plus a
symlinked explicit READ/DENY carveout under a writable root. Use dotted input
such as `workspace / "src" / ".." / ".git" / "config"` to prove lexical
normalization removes `..` without resolving the symlink. Add direct-tool
cases using relative `.git/config` while the process cwd is outside the
project: filesystem resolves relative to `_workspace_root()`, while
`apply_patch` resolves relative to its explicit `root`.

- [ ] **Step 2: Run permission tests and observe lexical path loss**

Run:

```bash
uv run pytest tests/test_sandbox/test_permission_profiles.py -q
```

Expected: new tests FAIL because entries are canonicalized and deduped.

- [ ] **Step 3: Represent both path views**

Extend entries:

```python
@dataclass(frozen=True)
class FileSystemPermissionEntry:
    path: PurePath
    access: FileSystemAccess
    logical_path: PurePath | None = None

    @property
    def lexical_path(self) -> PurePath:
        return self.logical_path or self.path
```

Change the existing `_lexical_absolute` implementation and expose the same
logic as `logical_absolute_path` for direct tools:

```python
def logical_absolute_path(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        return Path(os.path.abspath(os.fspath(path.expanduser())))
    return path


def _lexical_absolute(path: PurePath) -> PurePath:
    return logical_absolute_path(path)
```

Unlike `Path.absolute()`, this collapses `.`/`..`; unlike `resolve()`, it keeps
nested symlink components. When constructing default protected entries:

```python
logical = logical_absolute_path(Path(writable_root) / name)
canonical = _canonical(logical)
FileSystemPermissionEntry(
    path=canonical,
    logical_path=logical,
    access=FileSystemAccess.READ,
)
```

Apply the same two-view entry construction to every explicit non-WRITE
carveout below a writable root. Deduplicate by
`(lexical_key, canonical_key, access)` rather than canonical target alone.
`resolve`, `protected_metadata_root`, and `read_only_subpaths` compare both
logical and canonical candidates, choosing the most restrictive matching
access.

- [ ] **Step 4: Add failing backend-plan tests**

For Linux, Seatbelt, and Windows, construct `.git -> outside-target` and assert
the emitted policy includes both:

```python
assert str(workspace / ".git") in protected_paths
assert str(outside_target) in protected_paths
```

For Windows, assert deny-write paths include lexical reparse ancestors and the
target variants. For Seatbelt, assert both deny-write regex/subpath rules.
Add direct-tool and native-backend operations that attempt write-through,
unlink, rename, and replacement of a protected metadata symlink; each must
fail while an ordinary file next to it remains writable.

- [ ] **Step 5: Run backend tests and observe missing logical masks**

Run:

```bash
uv run pytest \
  tests/test_sandbox/test_linux_bwrap.py \
  tests/test_sandbox/test_seatbelt_backend.py \
  tests/test_sandbox/test_windows_default_backend.py \
  -q
```

Expected: new lexical-mask assertions FAIL.

The four known Windows PATH/probe baseline tests also remain red in this
full-file invocation; compare node IDs and do not treat them as Task 6
implementation targets.

- [ ] **Step 6: Thread variants through platform policies**

- Linux permission payloads emit lexical read-only carveouts and canonical
  targets without normalizing the lexical path away.
- Seatbelt adds literal/subpath deny-write rules for both variants.
- Windows `_effective_raw_profile_entries` keys by lexical and canonical
  variant, and `_acl_path_variants` returns both even when the final component
  is a symlink/junction.
- `SandboxPolicy.summary`, Linux payload/helper, and filesystem-worker profile
  serialization round-trip `logicalPath` in addition to `path` and `access`;
  old payloads without it remain valid.

Use one shared profile method rather than recreating symlink semantics per
backend.

- [ ] **Step 7: Gate direct tools on the logical path**

Before resolving the final path, filesystem and patch tools call:

```python
def logical_tool_path(raw_path: str | Path, *, base: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(logical_absolute_path(candidate))


# Filesystem: relative paths use the active project workspace, never process cwd.
base = _workspace_root() or Path.cwd()
logical = logical_tool_path(original_path, base=base)

# Patch: each op.path uses the apply_patch root supplied by the caller.
patch_logical = logical_tool_path(op.path, base=root)

protected = profile.protected_metadata_root(logical)
if protected is not None and operation_writes:
    return protected_metadata_block(logical, protected)

patch_protected = profile.protected_metadata_root(patch_logical)
if patch_protected is not None and operation_writes:
    return protected_metadata_block(patch_logical, patch_protected)
```

Shell command profiling checks lexical command targets and their canonical
targets relative to the command's effective cwd. Full Host Access retains its
explicit bypass. Absolute inputs stay absolute; none of these lexical joins
follow a symlink.

- [ ] **Step 8: Run profile, backend, and direct-tool tests**

Run:

```bash
uv run pytest \
  tests/test_sandbox/test_permission_profiles.py \
  tests/test_sandbox/test_linux_bwrap.py \
  tests/test_sandbox/test_linux_payload.py \
  tests/test_sandbox/test_linux_helper.py \
  tests/test_sandbox/test_filesystem_worker_policy.py \
  tests/test_sandbox/test_seatbelt_backend.py \
  tests/test_sandbox/test_windows_default_backend.py \
  tests/test_sandbox/test_path_access.py \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_payload_does_not_acl_grant_windows_platform_roots \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_trusted_non_sensitive_expansion_auto_grants \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_missing_expansion_roots_are_ignored \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_standard_non_sensitive_expansion_requires_approval \
  -q
```

Expected: PASS on the host platform after deselecting only the four explicitly
recorded, unrelated Windows baseline nodes; all new protected-path
payload/unit tests pass everywhere.

- [ ] **Step 9: Commit protected metadata parity**

```bash
git add \
  src/opensquilla/sandbox/permissions.py \
  src/opensquilla/sandbox/types.py \
  src/opensquilla/sandbox/backend/linux_permissions.py \
  src/opensquilla/sandbox/backend/linux_payload.py \
  src/opensquilla/sandbox/backend/linux_helper.py \
  src/opensquilla/sandbox/backend/seatbelt.py \
  src/opensquilla/sandbox/backend/windows_default.py \
  src/opensquilla/sandbox/filesystem_worker.py \
  src/opensquilla/sandbox/path_validation.py \
  src/opensquilla/tools/builtin/filesystem.py \
  src/opensquilla/tools/builtin/patch.py \
  src/opensquilla/tools/builtin/shell.py \
  tests/test_sandbox/test_permission_profiles.py \
  tests/test_sandbox/test_linux_bwrap.py \
  tests/test_sandbox/test_linux_payload.py \
  tests/test_sandbox/test_linux_helper.py \
  tests/test_sandbox/test_filesystem_worker_policy.py \
  tests/test_sandbox/test_seatbelt_backend.py \
  tests/test_sandbox/test_windows_default_backend.py \
  tests/test_sandbox/test_path_access.py
git commit -m "fix: preserve protected metadata symlink paths"
```

---

### Task 7: Preserve the Pre-existing Baseline and Enforce Workspace-only Scope

**Files:**
- Read only: `tests/test_sandbox/test_windows_default_backend.py`
- Read only: `tests/test_tools/test_filesystem_read_workspace.py`
- Modify: none

**Interfaces:**
- Preserves: the user's requested workspace-only submission scope
- Produces: an exact before/after failure-node comparison
- Forbids: opportunistic Windows PATH/probe and symlink-loop repairs

- [ ] **Step 1: Record the exact six pre-existing failures**

Run:

```bash
uv run pytest \
  tests/test_sandbox/test_windows_default_backend.py::test_payload_does_not_acl_grant_windows_platform_roots \
  tests/test_sandbox/test_windows_default_backend.py::test_trusted_non_sensitive_expansion_auto_grants \
  tests/test_sandbox/test_windows_default_backend.py::test_missing_expansion_roots_are_ignored \
  tests/test_sandbox/test_windows_default_backend.py::test_standard_non_sensitive_expansion_requires_approval \
  tests/test_tools/test_filesystem_read_workspace.py::test_sandbox_disabled_write_ignores_stale_restricted_tool_context \
  tests/test_tools/test_filesystem_read_workspace.py::test_list_dir_symlink_loop_matches_worker_output \
  -q
```

Expected before implementation: six failures. If Task 1 already fixed the stale
context failure, expect five failures and record that evidence.

- [ ] **Step 2: Classify the overlap before touching code**

The stale disabled-runtime case may turn green only through Task 1's required
ordinary-Full/request-default separation. The four Windows PATH/probe failures
and the worker symlink-loop failure are not causal dependencies of project
workspace hardening. Do not edit production or tests to chase those five.

- [ ] **Step 3: Re-run the baseline after implementation**

Run the exact command from Step 1. Compare failing node IDs, not only the count:

- no new failure node is allowed;
- the five unrelated nodes may remain unchanged and are reported explicitly;
- a failure disappearing incidentally is acceptable only when the responsible
  diff is already justified by a workspace requirement.

- [ ] **Step 4: Audit the diff for forbidden scope**

```bash
test -z "$(git diff --name-only 213cd366..HEAD -- \
  src/opensquilla/sandbox/directory_listing.py)"
git diff --name-only 213cd366..HEAD -- \
  src/opensquilla/sandbox/backend/windows_default.py \
  src/opensquilla/sandbox/filesystem_worker.py \
  src/opensquilla/tools/run_mode.py
```

Any listed file must map to its existing Task 1 or Task 6 workspace-specific
responsibility. `windows_default.py` and `filesystem_worker.py` may change only
for protected lexical/canonical paths; `tools/run_mode.py` may change only for
capability/request-default separation. There is no Task 7 commit.

---

### Task 8: Stabilize the Gateway Directory-Listing Contract

**Files:**
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Modify: `opensquilla-webui/src/types/rpc.ts`
- Test: `tests/test_sandbox/test_rpc_sandbox_access.py`

**Interfaces:**
- Produces: `SandboxPathListResponse`
- Changes: `sandbox.path.list` accepts omitted `path` and optional absolute `basePath`
- Returns: `currentPath`, compatibility `path`, real `parentPath`, selectable entries

- [ ] **Step 1: Write failing RPC contract tests**

```python
@pytest.mark.asyncio
async def test_path_list_omitted_path_uses_agent_workspace_not_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    agent_workspace = tmp_path / "agent-workspace"
    unrelated_cwd = tmp_path / "gateway-cwd"
    agent_workspace.mkdir()
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(agent_workspace)
    result = await _handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "kind": "workspace"},
        ctx,
    )
    assert result["currentPath"] == str(agent_workspace.resolve())
    assert result["path"] == result["currentPath"]


@pytest.mark.asyncio
async def test_path_list_parent_and_selectability_contract(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    root = tmp_path / "root"
    child = root / "child"
    nested = child / "nested"
    file_entry = child / "notes.txt"
    nested.mkdir(parents=True)
    file_entry.write_text("notes", encoding="utf-8")

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": str(child),
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result["currentPath"] == str(child.resolve())
    assert result["path"] == result["currentPath"]
    assert result["parentPath"] == str(root.resolve())
    assert all(row["name"] != ".." for row in result["entries"])
    file_row = next(row for row in result["entries"] if row["kind"] == "file")
    assert file_row["path"] == str(file_entry.resolve())
    assert file_row["selectable"] is False
    directory_row = next(
        row for row in result["entries"] if row["kind"] == "directory"
    )
    assert directory_row["path"] == str(nested.resolve())
    assert directory_row["selectable"] is True
```

Add named tests
`test_path_list_omitted_path_uses_validated_project_session`,
`test_path_list_falls_back_to_home_when_agent_workspace_is_missing`,
`test_path_list_root_has_null_parent`,
`test_path_list_relative_path_requires_absolute_base`,
`test_path_list_relative_path_resolves_against_base`,
`test_path_list_missing_or_inaccessible_directory_is_an_error`, and retain the
existing owner-gate test. Update the two legacy listing tests so they assert
the removed synthetic `..` row, the real parent, and non-selectable files.

- [ ] **Step 2: Run RPC path tests and observe current cwd/parent behavior**

Run:

```bash
uv run pytest tests/test_sandbox/test_rpc_sandbox_access.py -q
```

Expected: new contract tests FAIL.

- [ ] **Step 3: Implement deterministic start and relative-base handling**

Add:

```python
async def _path_list_start(
    params: dict[str, Any],
    ctx: RpcContext,
    session_key: str,
) -> Path:
    raw = params.get("path")
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            base_raw = params.get("basePath")
            if not isinstance(base_raw, str) or not Path(base_raw).expanduser().is_absolute():
                raise ValueError("relative path requires absolute basePath")
            candidate = Path(base_raw).expanduser() / candidate
        return candidate.resolve(strict=True)
    manager = _require_session_manager(ctx)
    session = await _session_for_key(manager, session_key)
    if session is not None and getattr(session, "workspace_id", None):
        storage = get_session_storage(manager)
        if storage is None:
            raise RpcUnavailableError("Session storage is not configured")
        validated = await resolve_session_project_workspace(storage, session)
        assert validated is not None
        return Path(validated.canonical_path)
    workspace = resolve_agent_workspace_dir(parse_agent_id(session_key), ctx.config)
    if workspace is not None and Path(workspace).is_dir():
        return Path(workspace).resolve(strict=True)
    return Path.home().resolve(strict=True)
```

List the directory itself, do not swallow `OSError`, set real parent semantics,
and mark only directories selectable for `kind="workspace"`.

- [ ] **Step 4: Define TypeScript contracts**

```typescript
export interface SandboxPathEntry {
  name: string
  path: string
  kind: 'directory' | 'file'
  selectable: boolean
  hidden?: boolean
}

export interface SandboxPathListResponse {
  currentPath: string
  path: string
  parentPath: string | null
  entries: SandboxPathEntry[]
}
```

- [ ] **Step 5: Run RPC and Python typing/lint checks**

Run:

```bash
uv run pytest tests/test_sandbox/test_rpc_sandbox_access.py -q
uv run ruff check src/opensquilla/gateway/rpc_sandbox.py tests/test_sandbox/test_rpc_sandbox_access.py
```

Expected: PASS.

- [ ] **Step 6: Commit stable path contract**

```bash
git add \
  src/opensquilla/gateway/rpc_sandbox.py \
  opensquilla-webui/src/types/rpc.ts \
  tests/test_sandbox/test_rpc_sandbox_access.py
git commit -m "fix: make project path browsing deterministic"
```

---

### Task 9: Replace Picker State Conflation With a Race-Safe State Machine

**Files:**
- Modify: `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue`
- Modify: `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.test.ts`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/platform/desktop.ts`
- Modify: `desktop/electron/scripts/test-project-workspace-picker.mjs`

**Interfaces:**
- Consumes: `SandboxPathListResponse` from Task 8
- Emits: existing `choose(path)` and `close`
- Adds no Web fallback after a native Desktop error

- [ ] **Step 1: Rewrite component tests around separate state**

Add tests with deferred promises:

```typescript
const PICKER_KEY = 'agent:main:webchat:picker'

function pathResult(currentPath: string, children: string[]): SandboxPathListResponse {
  const parent = currentPath === '/' ? null : currentPath.replace(/\/[^/]+$/, '') || '/'
  return {
    currentPath,
    path: currentPath,
    parentPath: parent,
    entries: children.map(path => ({
      name: path.split('/').at(-1) || path,
      path,
      kind: 'directory',
      selectable: true,
    })),
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok
    reject = fail
  })
  return { promise, resolve, reject }
}

async function flushPromises() {
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

function locationInput(): HTMLInputElement {
  return document.querySelector<HTMLInputElement>('[aria-label="Project path"]')!
}

function setLocation(value: string) {
  const input = locationInput()
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

function clickButton(label: string) {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(candidate => candidate.textContent?.trim() === label)
  if (!button) throw new Error(`Missing button: ${label}`)
  button.click()
}

it('selects on click and browses only on double click', async () => {
  mocks.rpcCall
    .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
    .mockResolvedValueOnce(pathResult('/repos/a', []))
  await mountPicker()
  await flushPromises()

  const option = document.querySelector<HTMLButtonElement>('[role="option"]')!
  option.click()
  await nextTick()
  expect(mocks.rpcCall).toHaveBeenCalledTimes(1)
  expect(option.getAttribute('aria-selected')).toBe('true')

  option.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
  await flushPromises()
  expect(mocks.rpcCall).toHaveBeenLastCalledWith('sandbox.path.list', {
    sessionKey: PICKER_KEY,
    path: '/repos/a',
    kind: 'workspace',
  })
})


it('ignores an older browse response that resolves last', async () => {
  const first = deferred<SandboxPathListResponse>()
  const second = deferred<SandboxPathListResponse>()
  mocks.rpcCall.mockResolvedValueOnce(pathResult('/repos', ['/repos/a', '/repos/b']))
  await mountPicker()
  await flushPromises()
  mocks.rpcCall.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  setLocation('/repos/a')
  clickButton('Browse')
  await nextTick()
  setLocation('/repos/b')
  clickButton('Browse')
  second.resolve(pathResult('/repos/b', []))
  first.resolve(pathResult('/repos/a', []))
  await flushPromises()
  expect(locationInput().value).toBe('/repos/b')
})
```

Add tests for omitted initial path, parent, non-selectable entries, latest error
retaining entries, close/reopen stale response, keyboard navigation, native
cancel, native reject Retry/Cancel, and retry success. Explicitly assert
successful navigation synchronizes `currentDirectory`, `locationDraft`, and
`selectedDirectory`; Choose is disabled during loading and whenever selection
is empty/non-selectable.

- [ ] **Step 2: Run picker tests and observe current conflated/racy behavior**

Run:

```bash
(
  cd opensquilla-webui
  npm run test:unit -- src/components/ProjectWorkspacePickerDialog.test.ts
)
```

Expected: new tests FAIL.

- [ ] **Step 3: Implement explicit picker state**

Use:

```typescript
type PickerPhase =
  | 'closed'
  | 'native-picking'
  | 'desktop-error'
  | 'web-loading'
  | 'web-ready'
  | 'web-error'

const phase = ref<PickerPhase>('closed')
const currentDirectory = ref('')
const selectedDirectory = ref('')
const locationDraft = ref('')
const parentDirectory = ref<string | null>(null)
const entries = ref<SandboxPathEntry[]>([])
let openEpoch = 0
let browseSequence = 0
```

`browse` captures `{epoch, sequence}` and applies success/error/finally only if
both still own the dialog. Omit `path` on first Web browse without
`initialPath`; pass `basePath=currentDirectory` for relative location input.
Current failure leaves prior entries and selection. On successful navigation,
set all three of `currentDirectory`, `locationDraft`, and
`selectedDirectory` to `response.currentPath`.

Filter with:

```typescript
const directories = computed(() =>
  entries.value.filter(entry => entry.kind === 'directory' && entry.selectable),
)
```

Single click updates `selectedDirectory`; double click/Enter calls `browse`.
The parent control browses `parentDirectory`. Choose emits only
`selectedDirectory`.

- [ ] **Step 4: Handle native rejection without Web fallback**

Guard the Desktop bridge with `typeof` in `platform/desktop.ts`. In the
component, catch rejection and set `phase='desktop-error'`, retaining the error
for Retry/Cancel. Do not call `sandbox.path.list` from that state. Increment
`openEpoch` on close and reopen.

- [ ] **Step 5: Add localized labels**

Add exact semantic keys in all six locale files:

- `workspaces.goToPath`
- `workspaces.parentDirectory`
- `workspaces.retryDirectoryPicker`
- `workspaces.directoryPickerFailed`
- `workspaces.chooseSelectedDirectory`

Use natural translations consistent with each existing locale; do not leave
English fallback text in non-English files.

- [ ] **Step 6: Strengthen the Electron contract test**

Assert the IPC handler still uses trusted sender validation,
`properties: ['openDirectory']`, and returns `null` on cancel. Add a static
assertion that the renderer capability is type-guarded and the component has a
native rejection branch.

- [ ] **Step 7: Run picker, type, architecture, and Electron tests**

Run:

```bash
(
  cd opensquilla-webui
  npm run test:unit -- src/components/ProjectWorkspacePickerDialog.test.ts
  npm run typecheck
  npm run check:architecture
)
(
  cd desktop/electron
  npm run test:project-workspace-picker
)
```

Expected: PASS.

- [ ] **Step 8: Commit picker state repair**

```bash
git add \
  opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue \
  opensquilla-webui/src/components/ProjectWorkspacePickerDialog.test.ts \
  opensquilla-webui/src/locales/en.json \
  opensquilla-webui/src/locales/zh-Hans.json \
  opensquilla-webui/src/locales/de.json \
  opensquilla-webui/src/locales/es.json \
  opensquilla-webui/src/locales/fr.json \
  opensquilla-webui/src/locales/ja.json \
  opensquilla-webui/src/platform/desktop.ts \
  desktop/electron/scripts/test-project-workspace-picker.mjs
git commit -m "fix: make project directory selection predictable"
```

---

### Task 10: Keep the Durable Active Project Visible and Fail Closed

**Files:**
- Create: `opensquilla-webui/src/composables/useActiveProjectWorkspace.ts`
- Create: `opensquilla-webui/src/composables/useActiveProjectWorkspace.test.ts`
- Modify: `opensquilla-webui/src/composables/useProjectWorkspaces.ts`
- Modify: `opensquilla-webui/src/composables/useProjectWorkspaces.test.ts`
- Modify: `opensquilla-webui/src/types/rpc.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSessionSubscription.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSessionSubscription.test.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSend.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSend.attachments.test.ts`
- Modify: `opensquilla-webui/src/views/ChatView.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposer.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposer.workspace.test.ts`
- Modify: `opensquilla-webui/src/components/SidebarConversations.vue`
- Modify: `opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts`
- Modify: `opensquilla-webui/src/App.vue`
- Modify: `opensquilla-webui/src/locales/*.json`

**Interfaces:**
- Consumes: `session.projectWorkspace` snapshot from Task 4
- Produces: `ActiveProjectWorkspaceState`
- Produces: `activeWorkspace`, `sendBlockedReason`, `beginProjectDraft`,
  `acceptPendingBinding`, `beginSessionResolution`, `applySessionSnapshot`,
  `failSessionResolution`, `applyWorkspaceRefresh`, `clearDraft`
- Keeps: `pendingWorkspaceId` only for first-send RPC binding

- [ ] **Step 1: Add the active-project state composable tests**

```typescript
function project(
  id: string,
  available: boolean,
): ActiveProjectWorkspaceSnapshot {
  return {
    id,
    name: `Project ${id}`,
    path: `/repos/${id}`,
    available,
    removed: false,
  }
}

it('clears pending binding after acceptance but keeps the active snapshot', () => {
  const state = useActiveProjectWorkspace()
  state.beginProjectDraft(project('p1', true))
  expect(state.pendingWorkspaceId.value).toBe('p1')
  state.acceptPendingBinding('p1')
  expect(state.pendingWorkspaceId.value).toBeNull()
  expect(state.activeWorkspace.value?.id).toBe('p1')
  expect(state.status.value).toBe('ready')
})


it('blocks resolving, unavailable, removed, and failed project states', () => {
  const resolving = useActiveProjectWorkspace()
  resolving.beginSessionResolution('session-a')
  expect(resolving.status.value).toBe('resolving')
  expect(resolving.sendBlockedReason.value).toBeTruthy()

  const unavailable = useActiveProjectWorkspace()
  unavailable.beginProjectDraft(project('p2', false))
  expect(unavailable.status.value).toBe('unavailable')
  expect(unavailable.sendBlockedReason.value).toBeTruthy()

  const removed = useActiveProjectWorkspace()
  removed.beginProjectDraft({ ...project('p3', false), removed: true })
  expect(removed.status.value).toBe('removed')
  expect(removed.sendBlockedReason.value).toBeTruthy()

  const failed = useActiveProjectWorkspace()
  const generation = failed.beginSessionResolution('session-b')
  failed.failSessionResolution('session-b', generation)
  expect(failed.status.value).toBe('error')
  expect(failed.sendBlockedReason.value).toBeTruthy()

  const unknown = useActiveProjectWorkspace()
  const unknownGeneration = unknown.beginSessionResolution('session-c')
  unknown.applySessionSnapshot('session-c', unknownGeneration, {
    workspaceId: 'missing-project-row',
    projectWorkspace: null,
  })
  expect(unknown.status.value).toBe('unknown')
  expect(unknown.sendBlockedReason.value).toBeTruthy()
})
```

Add stale-session snapshot rejection and recovery-to-ready tests.

- [ ] **Step 2: Run the new composable test and observe missing module**

Run:

```bash
(
  cd opensquilla-webui
  npm run test:unit -- src/composables/useActiveProjectWorkspace.test.ts
)
```

Expected: FAIL because the composable does not exist.

- [ ] **Step 3: Implement the state machine**

```typescript
export type ActiveProjectWorkspaceStatus =
  | 'none'
  | 'resolving'
  | 'ready'
  | 'unavailable'
  | 'removed'
  | 'unknown'
  | 'error'

export interface ActiveProjectWorkspaceSnapshot {
  id: string
  name: string
  path: string
  available: boolean
  removed: boolean
  availabilityReason?: string
}

export function useActiveProjectWorkspace() {
  const pendingWorkspaceId = ref<string | null>(null)
  const boundWorkspaceId = ref<string | null>(null)
  const activeWorkspace = ref<ActiveProjectWorkspaceSnapshot | null>(null)
  const status = ref<ActiveProjectWorkspaceStatus>('none')
  let sessionGeneration = 0
  let resolvingSessionKey: string | null = null

  function clearActiveProject() {
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    status.value = 'none'
  }

  function clearDraft() {
    pendingWorkspaceId.value = null
    clearActiveProject()
  }

  function beginProjectDraft(workspace: ActiveProjectWorkspaceSnapshot) {
    sessionGeneration += 1
    pendingWorkspaceId.value = workspace.id
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    status.value = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
  }

  function acceptPendingBinding(workspaceId: string | null) {
    if (pendingWorkspaceId.value === workspaceId) pendingWorkspaceId.value = null
  }

  function beginSessionResolution(
    sessionKey: string,
  ): number {
    sessionGeneration += 1
    resolvingSessionKey = sessionKey
    status.value = 'resolving'
    return sessionGeneration
  }

  function applySessionSnapshot(
    sessionKey: string,
    generation: number,
    metadata: {
      workspaceId?: string
      projectWorkspace?: ActiveProjectWorkspaceSnapshot | null
    },
  ): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    const workspace = metadata.projectWorkspace || null
    boundWorkspaceId.value = metadata.workspaceId || workspace?.id || null
    activeWorkspace.value = workspace
    if (workspace?.removed) status.value = 'removed'
    else if (workspace?.available) status.value = 'ready'
    else if (workspace) status.value = 'unavailable'
    else if (metadata.workspaceId) status.value = 'unknown'
    else status.value = 'none'
    return true
  }

  function failSessionResolution(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    status.value = 'error'
    return true
  }

  function applyWorkspaceRefresh(
    workspace: ActiveProjectWorkspaceSnapshot | null,
  ): void {
    if (!boundWorkspaceId.value) return
    if (!workspace) {
      if (activeWorkspace.value) {
        activeWorkspace.value = {
          ...activeWorkspace.value,
          available: false,
          removed: true,
          availabilityReason: 'removed',
        }
      }
      status.value = 'removed'
      return
    }
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    status.value = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
  }

  const sendBlockedReason = computed(() =>
    status.value === 'none' || status.value === 'ready'
      ? null
      : status.value,
  )

  return {
    pendingWorkspaceId,
    boundWorkspaceId,
    activeWorkspace,
    status,
    sendBlockedReason,
    beginProjectDraft,
    acceptPendingBinding,
    beginSessionResolution,
    applySessionSnapshot,
    failSessionResolution,
    applyWorkspaceRefresh,
    clearDraft,
  }
}
```

Import `computed` and `ref`. The status value is an internal blocking reason;
ChatView maps it to localized copy. `none` does not block ordinary sessions.

- [ ] **Step 4: Type and apply authoritative subscription metadata**

Extend the response:

```typescript
export interface SessionProjectWorkspaceSnapshot {
  id: string
  name: string
  path: string
  available: boolean
  removed: boolean
  availabilityReason?: string
}

export interface SessionMessagesSubscribeResponse extends SessionEventPayload {
  workspaceId?: string
  projectWorkspace?: SessionProjectWorkspaceSnapshot | null
}
```

Extend the existing `SessionMessagesSubscribeResponse` directly rather than
introducing a parallel unused response type. Add
`beginSessionMetadataResolution?: (key) => number`,
`onSessionMetadata?: (key, generation, metadata) => void`, and
`onSessionMetadataError?: (key, generation) => void` to
`UseChatSessionSubscriptionOptions`. At the start of one subscription attempt,
call the begin hook and retain the returned active-workspace generation. Invoke
the success/error hook with that same generation only after the existing
attempt and session-key race checks. Do not substitute the subscription attempt
counter for the active-workspace generation.

Add `availabilityReason?: string` to `ProjectWorkspaceItem` and preserve it in
`useProjectWorkspaces.normalizeWorkspace`.

- [ ] **Step 5: Add failing first-send/reload/send-gate tests**

```typescript
it.each(['resolving', 'unavailable', 'removed', 'unknown', 'error'])(
  'does not mutate or call chat.send when project preflight returns %s',
  async reason => {
    const validateActiveProjectBeforeSend = vi.fn(async () => reason)
    const { api, options, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
    })

    await api.onSend()

    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
    expect(rpc.call).not.toHaveBeenCalledWith(
      'chat.send',
      expect.anything(),
    )
    expect(options.inputText.value).toBe('hello')
    expect(options.messages.value).toEqual([])
  },
)

it('sends only after project preflight confirms ready', async () => {
  const validateActiveProjectBeforeSend = vi.fn(async () => null)
  const { api, rpc } = makeOptions({
    validateActiveProjectBeforeSend,
  })

  await api.onSend()

  expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
  expect(rpc.call).toHaveBeenCalledWith(
    'chat.send',
    expect.objectContaining({ message: 'hello' }),
  )
})
```

Cover project reload from subscription, stale A snapshot after switching to B,
durable chip without close, blank-draft chip with close, removal, and recovery.
Add two send-preflight tests:

- after a ready subscription snapshot, `workspaces.list` reports the same ID
  unavailable; assert `chat.send` is not called and the draft remains;
- immediately after `workspaces.remove`, the active durable task changes to
  removed without reload and every send entry point remains blocked.

For unavailable, removed, unknown, resolving, and error states, parameterize
the send suite over button/programmatic send, Enter-key send, queued follow-up,
and retry-send; assert none reaches `chat.send` and none clears the draft.

- [ ] **Step 6: Run UI tests and observe pending/active conflation**

Run:

```bash
(
  cd opensquilla-webui
  npm run test:unit -- \
    src/composables/useActiveProjectWorkspace.test.ts \
    src/composables/chat/useChatSessionSubscription.test.ts \
    src/composables/chat/useChatSend.attachments.test.ts \
    src/components/chat/ChatComposer.workspace.test.ts
)
```

Expected: new tests FAIL.

- [ ] **Step 7: Wire ChatView, send guard, and composer**

- Replace the local pending-only computed workspace with the new composable.
- `useChatSend` receives `sendBlockedReason` plus
  `validateActiveProjectBeforeSend`. Before any optimistic message/input
  mutation, the validator performs a fresh `workspaces.list`, finds the active
  ID, and updates the state: missing means removed, `available=false` means
  unavailable, RPC failure means error, and available means ready. It returns
  a blocking reason unless ready. The backend guard remains authoritative for
  the race after this preflight.
- First-send acceptance calls `acceptPendingBinding`, not a full active clear.
- Subscription metadata calls `applySessionSnapshot`.
- `ChatComposer` always receives the active snapshot/status. It shows a close
  action only for a blank draft and renders an unavailable/removed message for
  durable tasks.
- Generalize the existing blocked-message DOM ID/prop so it is not image-only.

- [ ] **Step 8: Enforce sidebar and removal behavior**

`SidebarConversations` disables the new-task pencil/action for
`available=false` and exposes an accessible reason.

Watch the shared `useProjectWorkspaces.byId` map in `ChatView`. Once
`hasLoaded=true`, if the active durable workspace ID disappears, mark the local
active state removed immediately. This is the removal notification path after
`App.onProjectRemove`; it does not wait for another subscription response.

After `App.onProjectRemove`:

```typescript
if (
  route.path === '/chat/new'
  && String(route.query.project || '') === workspaceId
) {
  freshTaskDraft.requestFreshTask('main', null)
  await router.replace({ path: '/chat/new', query: { agent: 'main' } })
}
```

A durable task remains on screen; its subscription/list snapshot marks the
project removed and blocks new sends.

- [ ] **Step 9: Add localized unavailable/removal copy**

Add translations in every locale for:

- active project unavailable;
- active project removed;
- project state resolving;
- project blocks sending;
- unavailable project cannot start a task.

- [ ] **Step 10: Run focused Web tests, typecheck, and architecture checks**

Run:

```bash
(
  cd opensquilla-webui
  npm run test:unit -- \
    src/composables/useActiveProjectWorkspace.test.ts \
    src/composables/chat/useChatSessionSubscription.test.ts \
    src/composables/chat/useChatSend.attachments.test.ts \
    src/components/chat/ChatComposer.workspace.test.ts \
    src/components/SidebarConversations.workspaces.test.ts \
    src/composables/useProjectWorkspaces.test.ts
  npm run typecheck
  npm run check:architecture
)
```

Expected: PASS.

- [ ] **Step 11: Commit durable active-workspace UI**

```bash
git add \
  opensquilla-webui/src/composables/useActiveProjectWorkspace.ts \
  opensquilla-webui/src/composables/useActiveProjectWorkspace.test.ts \
  opensquilla-webui/src/composables/useProjectWorkspaces.ts \
  opensquilla-webui/src/composables/useProjectWorkspaces.test.ts \
  opensquilla-webui/src/types/rpc.ts \
  opensquilla-webui/src/composables/chat/useChatSessionSubscription.ts \
  opensquilla-webui/src/composables/chat/useChatSessionSubscription.test.ts \
  opensquilla-webui/src/composables/chat/useChatSend.ts \
  opensquilla-webui/src/composables/chat/useChatSend.attachments.test.ts \
  opensquilla-webui/src/views/ChatView.vue \
  opensquilla-webui/src/components/chat/ChatComposer.vue \
  opensquilla-webui/src/components/chat/ChatComposer.workspace.test.ts \
  opensquilla-webui/src/components/SidebarConversations.vue \
  opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts \
  opensquilla-webui/src/App.vue \
  opensquilla-webui/src/locales/en.json \
  opensquilla-webui/src/locales/zh-Hans.json \
  opensquilla-webui/src/locales/de.json \
  opensquilla-webui/src/locales/es.json \
  opensquilla-webui/src/locales/fr.json \
  opensquilla-webui/src/locales/ja.json
git commit -m "fix: keep active project state authoritative"
```

---

### Task 11: Add Full Project Lifecycle and Sandbox Compatibility E2E

**Files:**
- Modify: `opensquilla-webui/e2e/project-workspaces.spec.ts`
- Modify: `tests/test_gateway/test_project_workspace_execution.py`
- Verify only: `tests/test_sandbox/test_path_access.py`
- Verify only: `tests/test_sandbox/test_filesystem_profile_integration.py`
- Verify only: `desktop/electron/scripts/test-project-workspace-picker.mjs`

**Interfaces:**
- Consumes all prior tasks
- Produces end-to-end evidence for the design acceptance criteria

- [ ] **Step 1: Add a failing browser lifecycle scenario**

The test must execute this sequence against a deterministic RPC stub. Add the
fixture in the same file so the scenario has no hidden/global helper
dependencies:

```typescript
type RpcParams = Record<string, unknown>

interface ProjectLifecycleState {
  sessionKey: string
  pathListRequests: RpcParams[]
  sends: RpcParams[]
  historyDeleteRequests: RpcParams[]
  postDeleteWorkspaceLists: number
  postDeleteSessionLists: number
  projectPresent: boolean
  removed: boolean
  sent: boolean
  historyDeleted: boolean
}

async function installProjectLifecycleRpc(
  page: Page,
): Promise<ProjectLifecycleState> {
  const state: ProjectLifecycleState = {
    sessionKey: 'agent:main:webchat:project-demo-task',
    pathListRequests: [],
    sends: [],
    historyDeleteRequests: [],
    postDeleteWorkspaceLists: 0,
    postDeleteSessionLists: 0,
    projectPresent: false,
    removed: false,
    sent: false,
    historyDeleted: false,
  }
  const workspace = () => ({
    id: 'project-demo',
    name: 'demo',
    path: '/repos/demo',
    taskCount: state.sent ? 1 : 0,
    pinned: false,
    available: true,
    removed: false,
  })
  const session = () => ({
    key: state.sessionKey,
    title: 'pwd',
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    effectiveAgentId: 'main',
    updatedAt: 1_753_500_000,
    messageCount: 1,
    status: 'ok',
    runStatus: 'idle',
    workspaceId: 'project-demo',
    workspace: '/repos/demo',
  })

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({
      type: 'res',
      id,
      ok: true,
      payload,
    }))
    ws.onMessage(raw => {
      let frame: {
        type?: string
        id?: unknown
        method?: string
        params?: RpcParams
      }
      try {
        frame = JSON.parse(String(raw))
      } catch {
        return
      }
      if (frame.type !== 'req' || frame.id === undefined) return
      const params = frame.params || {}
      switch (frame.method) {
        case 'connect':
          ws.send(JSON.stringify({
            protocol: 3,
            policy: { tick_interval_ms: 30_000 },
          }))
          return
        case 'sandbox.path.list':
          state.pathListRequests.push(params)
          respond(frame.id, {
            currentPath: '/repos',
            path: '/repos',
            parentPath: '/',
            entries: [{
              name: 'demo',
              path: '/repos/demo',
              kind: 'directory',
              selectable: true,
            }],
          })
          return
        case 'workspaces.open':
          expect(params).toMatchObject({ path: '/repos/demo', trusted: true })
          state.projectPresent = true
          state.removed = false
          respond(frame.id, { workspace: workspace() })
          return
        case 'workspaces.list':
          if (state.historyDeleted) state.postDeleteWorkspaceLists += 1
          respond(frame.id, {
            workspaces: state.projectPresent ? [workspace()] : [],
          })
          return
        case 'chat.send':
          state.sends.push(params)
          state.sent = true
          respond(frame.id, {
            sessionKey: state.sessionKey,
            status: 'accepted',
            task_id: 'project-demo-task',
            message_id: 'project-demo-user-message',
          })
          return
        case 'chat.history':
          respond(frame.id, {
            messages: state.sent
              ? [{
                  role: 'user',
                  text: 'pwd',
                  message_id: 'project-demo-user-message',
                  timestamp: '2026-07-26T00:00:00.000Z',
                }]
              : [],
            has_more: false,
          })
          return
        case 'sessions.list':
          if (state.historyDeleted) state.postDeleteSessionLists += 1
          respond(frame.id, {
            sessions: state.sent ? [session()] : [],
            has_more: false,
          })
          return
        case 'sessions.messages.subscribe':
          respond(frame.id, {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
            workspaceId: state.sent ? 'project-demo' : undefined,
            projectWorkspace: state.sent
              ? state.removed
                ? {
                    ...workspace(),
                    available: false,
                    removed: true,
                    availabilityReason: 'removed',
                  }
                : workspace()
              : null,
          })
          return
        case 'workspaces.remove':
          expect(params).toEqual({ workspaceId: 'project-demo' })
          state.projectPresent = false
          state.removed = true
          respond(frame.id, { workspaceId: 'project-demo' })
          return
        case 'workspaces.history.delete':
          expect(params).toEqual({ workspaceId: 'project-demo' })
          state.historyDeleteRequests.push(params)
          state.historyDeleted = true
          state.sent = false
          respond(frame.id, {
            workspaceId: 'project-demo',
            deletedTaskCount: 1,
            deletedSessionKeys: [state.sessionKey],
          })
          return
        default: {
          const payloads: Record<string, unknown> = {
            'agents.list': { agents: [] },
            'commands.list_for_surface': { commands: [] },
            'config.get': {
              squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
              permissions: {},
              skills: {},
            },
            'onboarding.status': { audioConfigured: false },
            'usage.status': { sessions: [] },
          }
          respond(frame.id, payloads[String(frame.method)] ?? {})
        }
      }
    })
    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: {},
    }))
  })
  return state
}

test('project picker, trust, first send, reload, remove, reopen, and history delete', async ({ page }) => {
  const state = await installProjectLifecycleRpc(page)
  await openControl(page)

  await page
    .locator('.sidebar-actions')
    .getByRole('button', { name: 'Choose project' })
    .click()
  await expect.poll(() => state.pathListRequests.length).toBe(1)
  expect(state.pathListRequests[0]).not.toHaveProperty('path')
  expect(state.pathListRequests[0]).toMatchObject({
    kind: 'workspace',
  })
  expect(state.pathListRequests[0].sessionKey).toEqual(expect.any(String))
  const picker = page.getByRole('dialog', { name: 'Choose project' })
  await picker.getByRole('option', { name: 'demo' }).click()
  await picker.getByRole('button', { name: 'Choose selected directory' }).click()
  await page.getByRole('button', { name: 'Trust and open' }).click()
  await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=project-demo$/)
  await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')

  await page.getByRole('textbox', { name: 'Message to send' }).fill('pwd')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect.poll(() => state.sends.length).toBe(1)
  expect(state.sends[0]).toMatchObject({
    message: 'pwd',
    workspaceId: 'project-demo',
  })
  await expect(page).toHaveURL(/\/chat\?session=/)
  await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')

  await page.reload()
  await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')
  const projectRow = page.locator('.sidebar-history-row--workspace').first()
  await projectRow.getByTestId('project-workspace-more').click()
  await page.getByRole('menuitem', { name: 'Remove project' }).click()
  await page.getByRole('button', { name: 'Remove project' }).click()
  await expect.poll(() => state.removed).toBe(true)
  await expect(page.locator('.chat-project-chip')).toContainText('/repos/demo')
  const blockedSend = page.getByRole('button', { name: 'Send' })
  await page.getByRole('textbox', { name: 'Message to send' }).fill('must stay')
  await expect(blockedSend).toBeDisabled()
  expect(state.sends).toHaveLength(1)

  await page
    .locator('.sidebar-actions')
    .getByRole('button', { name: 'Choose project' })
    .click()
  const reopenedPicker = page.getByRole('dialog', { name: 'Choose project' })
  await reopenedPicker.getByRole('option', { name: 'demo' }).click()
  await reopenedPicker
    .getByRole('button', { name: 'Choose selected directory' })
    .click()
  await page.getByRole('button', { name: 'Trust and open' }).click()
  await expect.poll(() => state.projectPresent).toBe(true)

  const reopenedRow = page.locator('.sidebar-history-row--workspace').first()
  await reopenedRow.getByTestId('project-workspace-more').click()
  await page
    .getByRole('menuitem', { name: 'Delete project task history' })
    .click()
  await page.getByRole('button', { name: 'Delete history' }).click()
  await expect.poll(() => state.historyDeleted).toBe(true)
  await expect.poll(() => state.postDeleteWorkspaceLists).toBeGreaterThan(0)
  await expect.poll(() => state.postDeleteSessionLists).toBeGreaterThan(0)
  expect(state.historyDeleteRequests).toEqual([{ workspaceId: 'project-demo' }])
  await expect(page.locator(`[data-session-key="${state.sessionKey}"]`)).toHaveCount(0)
  await expect(page.locator('.sidebar-workspace-empty')).toHaveText('No tasks')
})
```

The fixture's authoritative `workspaceId` and `projectWorkspace` subscription
fields are what restore the durable chip after reload. Client-side completion
is proven by post-delete workspace/session refreshes and the converged no-task
UI. Task 5's real backend tests remain the proof that history deletion leaves
project files untouched.

- [ ] **Step 2: Run the E2E test and observe missing lifecycle behavior**

Run:

```bash
(
  cd opensquilla-webui
  npx playwright test e2e/project-workspaces.spec.ts
)
```

Expected: new scenario FAIL before prior tasks are integrated.

- [ ] **Step 3: Add backend sandbox integration proof**

Add a gateway-to-production-tool-to-native-backend proof. Extend the existing
imports with `json`, `sys`, `from dataclasses import replace`,
`ApprovalQueue`, `BubblewrapBackend`, `SeatbeltBackend`,
`UnavailableBackend`, `SandboxSettings`, `configure_runtime`,
`reset_runtime`, `RunMode`, `SandboxBackendError`, `filesystem as fs`,
`current_tool_context`, and `full_host_access_for_context`. Do not hand-build a
policy and call the backend separately: the `ToolContext` produced by the
gateway must drive the real `write_file` implementation and its
`SandboxOperationRuntime`.

Use `from opensquilla.sandbox.backend.unavailable import UnavailableBackend`
and `from opensquilla.sandbox.types import SandboxBackendError`.

```python
@pytest.mark.asyncio
async def test_default_project_drives_real_standard_filesystem_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform.startswith("linux"):
        probe = BubblewrapBackend()
    elif sys.platform == "darwin":
        probe = SeatbeltBackend()
    else:
        pytest.skip("native project sandbox proof is POSIX-only")
    if not probe.available():
        pytest.skip(f"{probe.name} is unavailable")

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        async with open_stack(tmp_path / "sessions.db") as stack:
            project_path = tmp_path / "project"
            sibling = tmp_path / "sibling"
            sibling.mkdir()
            project = await add_project(stack, project_path)
            assert project is not None
            inside = project_path / "inside.txt"
            outside = sibling / "outside.txt"
            outcomes: dict[str, Any] = {}
            completed = asyncio.Event()

            runtime = configure_runtime(
                SandboxSettings(
                    run_mode="standard",
                    backend=probe.name,
                    network_default="none",
                    exclude_slash_tmp=True,
                    exclude_tmpdir_env_var=True,
                ),
                approval_queue=queue,
                workspace=project_path,
                default_run_mode=RunMode.FULL,
            )
            assert runtime.backend.name == probe.name
            native_operations: list[Any] = []
            native_run_operation = runtime.backend.run_operation

            async def counted_native_operation(operation: Any) -> Any:
                native_operations.append(operation)
                return await native_run_operation(operation)

            monkeypatch.setattr(
                runtime.backend,
                "run_operation",
                counted_native_operation,
            )

            class Runner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    project_ctx = kwargs["tool_context"]
                    outcomes["tool_context"] = project_ctx
                    token = current_tool_context.set(project_ctx)
                    try:
                        outcomes["inside"] = await fs.write_file(
                            str(inside),
                            "inside",
                        )
                        outcomes["outside"] = json.loads(
                            await fs.write_file(str(outside), "outside")
                        )
                        outcomes["outside_after_standard"] = outside.exists()
                    except BaseException as exc:  # surfaced to the test task
                        outcomes["error"] = exc
                    else:
                        yield DoneEvent()
                    finally:
                        current_tool_context.reset(token)
                        completed.set()

            stack.context.task_runtime = None
            stack.context.turn_runner = Runner()
            response = await get_dispatcher().dispatch(
                "project-standard-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:project-standard-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "workspaceId": project.workspace_id,
                    "clientRequestId": "project-standard-proof-1",
                },
                stack.context,
            )
            await asyncio.wait_for(completed.wait(), timeout=10.0)
            if "error" in outcomes:
                raise outcomes["error"]

            assert response.ok is True
            tool_ctx = outcomes["tool_context"]
            assert tool_ctx.run_mode == "standard"
            assert tool_ctx.workspace_dir == str(project_path.resolve())
            assert full_host_access_for_context(tool_ctx) is False
            assert inside.read_text(encoding="utf-8") == "inside"
            outside_result = outcomes["outside"]
            assert outside_result["status"] == "elevation_required"
            assert outside_result["reason"] == "mount_requires_write_access"
            assert outside_result["path"] == str(outside.resolve())
            assert outside_result["access"] == "rw"
            assert outcomes["outside_after_standard"] is False
            assert [operation.kind for operation in native_operations] == [
                "write_text"
            ]

            full_completed = asyncio.Event()

            class FullRunner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    full_ctx = kwargs["tool_context"]
                    outcomes["full_tool_context"] = full_ctx
                    token = current_tool_context.set(full_ctx)
                    try:
                        await fs.write_file(str(outside), "full-host")
                    except BaseException as exc:
                        outcomes["full_error"] = exc
                    else:
                        yield DoneEvent()
                    finally:
                        current_tool_context.reset(token)
                        full_completed.set()

            stack.context.turn_runner = FullRunner()
            full_response = await get_dispatcher().dispatch(
                "ordinary-full-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:ordinary-full-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "clientRequestId": "ordinary-full-proof-1",
                },
                stack.context,
            )
            await asyncio.wait_for(full_completed.wait(), timeout=10.0)
            if "full_error" in outcomes:
                raise outcomes["full_error"]

            assert full_response.ok is True
            assert outcomes["full_tool_context"].run_mode == "full"
            assert full_host_access_for_context(
                outcomes["full_tool_context"]
            ) is True
            assert outside.read_text(encoding="utf-8") == "full-host"
            assert len(native_operations) == 1
    finally:
        reset_runtime()
        queue.close()
```

Add `test_project_standard_fails_closed_when_native_backend_is_unavailable`
with the same gateway-produced project context. Configure the hybrid runtime,
replace only `runtime.backend` with
`UnavailableBackend("test unavailable")`, set `current_tool_context` to the
project context, and call `fs.write_file` for a path inside the project. Assert
`SandboxBackendError` and that the target was not created. Then clone that same
context with `replace(project_ctx, run_mode="full")`, call the identical tool
for an outside target, and assert the host write succeeds. Always reset the
context/runtime and close the queue in `finally`. This proves fail-closed
behavior through the production tool path, not merely through a direct backend
unit call.

- [ ] **Step 4: Run lifecycle and compatibility tests**

Run:

```bash
uv run pytest \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_sandbox/test_path_access.py \
  tests/test_sandbox/test_filesystem_profile_integration.py \
  -q
(
  cd opensquilla-webui
  npx playwright test e2e/project-workspaces.spec.ts
)
(
  cd desktop/electron
  npm run test:project-workspace-picker
)
```

Expected: PASS.

- [ ] **Step 5: Commit E2E proof**

```bash
git add \
  opensquilla-webui/e2e/project-workspaces.spec.ts \
  tests/test_gateway/test_project_workspace_execution.py
git commit -m "test: cover project workspace sandbox lifecycle"
```

---

### Task 12: Full Review, Build, Runtime Restart, and Completion Audit

**Files:**
- Modify only files required by review findings
- Verify: all files changed since `213cd366`

**Interfaces:**
- Consumes all implementation commits
- Produces final review evidence and a running Control UI

- [ ] **Step 1: Run Python formatting/static checks on changed Python files**

```bash
if git diff --quiet 213cd366..HEAD -- '*.py'; then
  echo "No changed Python files"
else
  git diff --name-only -z 213cd366..HEAD -- '*.py' \
    | xargs -0 uv run ruff check
  git diff --name-only -z 213cd366..HEAD -- '*.py' \
    | xargs -0 uv run ruff format --check
fi
```

Expected: PASS. The guard prevents Ruff from receiving an empty path list.

- [ ] **Step 2: Run focused backend/project suites**

```bash
uv run pytest \
  tests/test_migrations/test_v025_project_workspaces.py \
  tests/test_session/test_project_workspace_storage.py \
  tests/test_session/test_turn_acceptance_storage.py \
  tests/test_gateway/test_rpc_workspaces.py \
  tests/test_gateway/test_project_workspace_execution.py \
  tests/test_gateway/test_turn_ingress_rpc.py \
  tests/test_gateway/test_channel_turn_ingress.py \
  tests/test_gateway/test_turn_ingress_intents.py \
  tests/test_gateway/test_rpc_sessions.py \
  tests/test_gateway/test_router_boot.py \
  tests/test_gateway/test_channel_dispatch_realtime.py \
  tests/test_cli/test_agent_cmd.py \
  tests/unit/cli/tui/test_runtime_adapters.py \
  tests/test_sandbox/test_rpc_sandbox_access.py \
  tests/test_sandbox/test_run_context.py \
  tests/test_sandbox/test_run_context_grants.py \
  tests/test_sandbox/test_run_modes.py \
  tests/test_sandbox/test_run_mode_routing.py \
  tests/test_sandbox/test_windows_default_request_context.py \
  tests/test_sandbox/test_cli_run_modes.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run the complete relevant sandbox matrix**

```bash
uv run pytest \
  tests/test_sandbox/test_approval_runtime.py \
  tests/test_sandbox/test_permission_profiles.py \
  tests/test_sandbox/test_platform_permissions.py \
  tests/test_sandbox/test_path_access.py \
  tests/test_sandbox/test_filesystem_profile_integration.py \
  tests/test_sandbox/test_filesystem_worker_policy.py \
  tests/test_sandbox/test_linux_bwrap.py \
  tests/test_sandbox/test_linux_payload.py \
  tests/test_sandbox/test_linux_helper.py \
  tests/test_sandbox/test_seatbelt_backend.py \
  tests/test_sandbox/test_windows_default_backend.py \
  tests/test_sandbox/test_windows_shell_process_runtime.py \
  tests/test_tools/test_filesystem_read_workspace.py \
  tests/test_tools/test_tool_failure_envelope.py \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_payload_does_not_acl_grant_windows_platform_roots \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_trusted_non_sensitive_expansion_auto_grants \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_missing_expansion_roots_are_ignored \
  --deselect tests/test_sandbox/test_windows_default_backend.py::test_standard_non_sensitive_expansion_requires_approval \
  --deselect tests/test_tools/test_filesystem_read_workspace.py::test_list_dir_symlink_loop_matches_worker_output \
  -q
```

Expected: zero failures in the workspace-scoped matrix. Platform-unavailable
integration cases may retain their existing explicit skips; none of the new
unit/payload tests may be skipped. Task 7 separately re-runs and records the
five explicitly deselected pre-existing failures.

- [ ] **Step 4: Run all Web unit, type, architecture, and E2E checks**

```bash
(
  cd opensquilla-webui
  npm run test:unit
  npm run typecheck
  npm run check:architecture
  npx playwright test e2e/project-workspaces.spec.ts
  npm run build
)
```

Expected: PASS and a fresh `dist/`.

- [ ] **Step 5: Run Electron picker checks**

```bash
(
  cd desktop/electron
  npm run test:project-workspace-picker
)
```

Expected: PASS.

- [ ] **Step 6: Review the entire implementation diff**

Run:

```bash
git diff --check 213cd366..HEAD
git diff --stat 213cd366..HEAD
git log --oneline 213cd366..HEAD
```

Inspect every changed production file against all ten acceptance criteria in the
design. Record each criterion's proving test or runtime observation. Fix any
scope gap with a new failing test before changing production code.

Also prove the submission boundary:

```bash
git status --short
git diff --name-only 213cd366..HEAD
test -z "$(git ls-files src/opensquilla/gateway/static/dist)"
```

Classify every production file as a direct dependency of workspace authority,
project sandboxing, lifecycle, picker, or active-project UI. Remove any
unrelated repair rather than rationalizing it after the fact. Ignored static
assets may exist locally for runtime verification but must remain untracked.

- [ ] **Step 7: Request two-stage code review**

Use `superpowers:requesting-code-review` for:

1. specification compliance against the design and this plan;
2. code quality, concurrency, sandbox security, and cross-platform behavior.

Address every confirmed high/medium issue with TDD and rerun the affected suite.

- [ ] **Step 8: Rebuild static assets used by the running gateway**

The Web build writes the ignored distribution consumed at
`src/opensquilla/gateway/static/dist`. Confirm:

```bash
test -f src/opensquilla/gateway/static/dist/index.html
find src/opensquilla/gateway/static/dist -type f | wc -l
```

Expected: `index.html` exists and the file count is nonzero.

- [ ] **Step 9: Restart only if Python/runtime code is not hot-loaded**

Resolve ownership, listener, command, and cwd first:

```bash
uv run opensquilla gateway status --json
lsof -nP -iTCP:18791 -sTCP:LISTEN
ps -axo pid=,ppid=,command= \
  | rg 'opensquilla gateway (run|start)'
```

If status reports `managed=true`, run:

```bash
uv run opensquilla gateway restart --json
uv run opensquilla gateway status --json
```

If status reports `managed=false`, validate and stop only the exact listener:

```bash
listener_pid="$(lsof -t -iTCP:18791 -sTCP:LISTEN | head -n 1)"
test -n "$listener_pid"
listener_command="$(ps -p "$listener_pid" -o command=)"
listener_cwd="$(lsof -a -p "$listener_pid" -d cwd -Fn | sed -n 's/^n//p')"
test "$listener_cwd" = "/Users/liurunke/opensquilla"
case "$listener_command" in
  *"/Users/liurunke/opensquilla/"*"opensquilla gateway run"*) ;;
  *) echo "Refusing to stop unrelated listener: $listener_command" >&2; exit 1 ;;
esac
kill -TERM "$listener_pid"
for attempt in {1..100}; do
  if ! lsof -t -iTCP:18791 -sTCP:LISTEN >/dev/null; then
    break
  fi
  sleep 0.1
done
test -z "$(lsof -t -iTCP:18791 -sTCP:LISTEN)"
uv run opensquilla gateway start --json
uv run opensquilla gateway status --json
```

Do not signal a process whose command/cwd validation fails. If a development
supervisor already loaded the new Python code, retain it and only run status.

- [ ] **Step 10: Verify live Control UI and health**

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18791/control/
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18791/health
```

Expected:

```text
200
200
```

Fetch the built entry JS/CSS paths from `/control/` and verify each also returns
HTTP 200:

```bash
uv run python - <<'PY'
import re
import urllib.parse
import urllib.request

root = "http://127.0.0.1:18791/control/"
with urllib.request.urlopen(root, timeout=10) as response:
    assert response.status == 200
    html = response.read().decode("utf-8")
assets = {
    urllib.parse.urljoin(root, path)
    for path in re.findall(r'(?:src|href)="([^"]+)"', html)
    if path.endswith((".js", ".css"))
}
assert assets
for asset in sorted(assets):
    with urllib.request.urlopen(asset, timeout=10) as response:
        assert response.status == 200, asset
        print(response.status, asset)
PY
```

- [ ] **Step 11: Commit any final review-only repairs**

If review required changes:

```bash
git status --short
git diff --check
git diff --cached --name-only
git commit -m "fix: address workspace hardening review"
```

Stage each exact file named by the review finding before running the cached
name check; do not use a directory-wide add. If review found no changes, do not
create an empty commit.

- [ ] **Step 12: Produce the completion audit**

Report:

- commit list and files changed;
- project/non-project run-mode evidence;
- canonical retarget/missing/removal evidence;
- transaction/idempotency evidence;
- protected metadata cross-platform evidence;
- pre-existing-baseline delta and workspace-only scope evidence;
- picker/UI/E2E evidence;
- live HTTP evidence;
- any platform test skipped because the backend is genuinely unavailable.

Do not mark the goal complete until every design acceptance criterion has direct
evidence.
