# Project Workspace Sandbox Hardening Design

**Status:** Approved for written-spec review

**Date:** 2026-07-26

## Context

The project-workspace feature persists trusted directories, groups sessions under
those projects, and uses the selected directory as the session workspace. The
current implementation passes its focused happy-path tests, but review exposed
several gaps:

- a project created under an otherwise implicit configuration inherits Full Host
  Access instead of Codex-style workspace-write isolation;
- changing only the saved run mode to Standard would not fix that issue, because
  gateway boot currently installs a process-wide `NoopBackend` for the implicit
  Full default;
- existing project sessions do not revalidate their workspace record and
  canonical path before each turn, so a missing directory or a path replaced by
  a symlink/junction can silently change the effective authorization root;
- the non-`TaskRuntime` first-send path and project-history deletion are not
  atomic;
- the Web directory picker conflates navigation and selection state, starts from
  the gateway process working directory, and permits stale responses to replace
  newer navigation;
- the composer treats the pending project selection as the active project, so
  its project indicator disappears after the first successful send;
- protected metadata symlinks are canonicalized too early, losing the lexical
  `.git`, `.codex`, or `.agents` path that a backend must also protect; and
- six sandbox regressions currently fail consistently across Windows payload
  construction, sandbox-disabled stale context handling, and symlink-loop
  directory listing parity.

The local Codex source is the compatibility reference. In particular:

- `codex-rs/core/src/config/config_toml.rs` and
  `codex-rs/core/src/config/mod.rs` establish workspace-write plus on-request
  review as the safe project default;
- `codex-rs/core/src/session/turn_context.rs` keeps cwd and permissions in a
  per-turn context rather than using one process-global policy as the request
  policy; and
- `codex-rs/protocol/src/permissions.rs` and
  `codex-rs/sandboxing/src/policy_transforms.rs` preserve logical protected paths
  as well as canonical targets.

## Goals

1. Make an implicitly configured project session use a real Standard sandbox:
   the project is writable, protected metadata remains non-writable, and paths
   outside the project require the existing Standard approval flow.
2. Preserve the current default Full behavior for non-project sessions and
   preserve explicit operator choices, including explicit Full and explicit
   `sandbox=false`.
3. Treat a persisted project record as the only authoritative workspace for a
   bound session and validate it before every execution.
4. Make first-send persistence, project binding, ingress receipt creation, and
   history deletion transactionally correct.
5. Make project identity and availability visible and consistent across sidebar,
   composer, refresh, first-send handoff, removal, and recovery.
6. Give Web and Desktop users predictable, race-safe directory selection.
7. Restore the existing sandbox regression suite and add platform-specific
   coverage for the new invariants.

## Non-goals

- Changing the default mode of ordinary non-project sessions.
- Automatically granting access to a replacement directory.
- Deleting project files when project history or a project record is removed.
- Replacing Electron's native directory dialog with the Web picker.
- Adding a new database schema version solely for this hardening work.
- Silently falling back to host execution when a sandbox backend is unavailable.

## Chosen Approach

Use an integrated compatibility repair rather than a UI-only patch or a global
default-mode change.

The design separates process sandbox capability from the logical default mode of
an individual request. It then carries a validated workspace guard through turn
acceptance and reconstructs the active project state from authoritative session
metadata. This retains backwards-compatible Full behavior for ordinary tasks
while making a project task equivalent to Codex workspace-write unless the
operator explicitly selected another mode.

## Invariants

The implementation must preserve all of these invariants:

1. A project-bound session has exactly one authoritative workspace: the active,
   trusted `project_workspaces` row referenced by `sessions.workspace_id`.
2. A persisted `RunContext.workspace` is never sufficient authorization for a
   project-bound session.
3. Missing, removed, untrusted, inaccessible, or canonically changed project
   paths fail before the model or any tool starts. They never fall back to the
   agent default workspace.
4. A replay of an already accepted idempotent request returns its durable
   receipt before consulting mutable workspace state.
5. Non-replayed turn acceptance rechecks the workspace guard inside the same
   database write transaction that creates the session/message/receipt.
6. Implicit project mode is Standard; explicit Standard, Trusted, Full, and
   explicit sandbox disablement remain explicit.
7. A Standard project turn always uses a real backend or fails closed.
8. The UI does not send while the active project is unknown, unavailable,
   removed, or being resolved.
9. Navigation state and selected-directory state are distinct.
10. Protected metadata policy covers both the user-visible lexical path and its
    canonical target.

## Architecture

### 1. Sandbox Capability and Request Default Separation

Add two related concepts:

- `project_default_run_mode(config)` resolves the mode assigned to a new project
  session.
- `SandboxRuntime.default_run_mode` records the logical fallback for requests
  that do not carry a valid `ToolContext`.

For a bare/implicit configuration:

- `config_run_mode(config)` remains Full, so ordinary sessions retain their
  current behavior;
- `project_default_run_mode(config)` returns Standard; and
- gateway boot configures a real sandbox capability using Standard-compatible
  settings, while passing `default_run_mode=Full` into `configure_runtime`.

The project resolver returns the explicitly configured mode when the operator
set `sandbox.run_mode`, explicitly set `sandbox=false`, or selected
`permissions.default_mode=full`. Explicit Standard and Trusted are also
preserved.

Runtime code must stop deriving a request's fallback mode from the capability
settings. Contextless operations read `runtime.default_run_mode`. Operations
with a valid turn context read that context. A runtime whose effective sandbox
is disabled dominates stale restricted context and behaves as explicit Full
Host Access.

When backend setup is pending or unavailable, implicit project Standard fails
closed with the existing setup/backend error path. Ordinary implicit Full turns
may continue to use their explicit per-turn host bypass.

New project contexts persist mode provenance:

- `project_default` for the mode assigned during project task creation;
- `user` when changed through the run-mode UI/RPC; and
- `operator_default` when an explicit configuration supplied the default.

Legacy project sessions that contain Full without provenance are treated as an
old implicit project default when the current configuration is also implicit.
They are resolved as Standard. A user who intentionally needs Full can select it
again, which persists `user` provenance. Explicit global Full or explicit
`sandbox=false` continues to resolve legacy project sessions as Full.

Status payloads keep the existing ordinary `run_mode` meaning and add explicit
project/runtime-capability fields. They must not claim that the global default
changed to Standard.

### 2. Authoritative Project Workspace Validation

Introduce a focused project-workspace validation service with these values:

```python
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

`resolve_validated_project_workspace(storage, workspace_id)` verifies:

1. the record exists;
2. it is not removed;
3. it has a trust timestamp;
4. the stored path resolves strictly to a non-root directory;
5. the process can enumerate/traverse the directory;
6. the new canonical path and key exactly match the trusted stored identity.

A stored path replaced by a symlink or junction that resolves elsewhere returns
`canonical_changed`. Reopening that location through `workspaces.open` is the
only operation that can establish trust in the new target.

The service supplies the effective workspace for:

- new project sends;
- continuation, reset, and fork sends;
- `sessions.messages.subscribe` bootstrap metadata;
- sandbox run-context, mount, domain, and bundle RPCs;
- direct Web/CLI turn execution; and
- queued `TaskRuntime` execution immediately before `ToolContext` creation.

For a project-bound session, the validated path replaces any workspace stored in
session origin while retaining the session's run mode and approved grants.
Changing a project session's workspace through `sandbox.workspace.set` is
rejected; run mode and other grants remain mutable.

Availability projection reuses the same validator and returns a stable reason,
not a weaker `Path.is_dir()` check.

### 3. Idempotent and Atomic Turn Acceptance

Move durable ingress-receipt lookup ahead of all mutable workspace validation,
intent mutation, attachment ingestion, or task scheduling:

- same request ID and same fingerprint returns the original acceptance;
- same request ID and a different fingerprint returns the idempotency conflict;
- only a new request proceeds to live workspace validation.

Extend `SessionStorage.accept_turn` with:

```python
task_record: AgentTaskRecord | None
workspace_guard: ProjectWorkspaceGuard | None
```

Inside its existing write transaction, acceptance performs this order:

1. select and replay/conflict-check the ingress receipt;
2. for a new request, verify the guarded project row is still active, trusted,
   and has exactly the guarded path/key;
3. verify that the prepared or existing session binding matches the guard;
4. insert/update the session, transcript message, optional task, and receipt.

A bound project session without a guard fails closed. No foreign key is added:
removed projects intentionally retain their historical sessions.

The prepared intent/message path no longer depends on `TaskRuntime` presence.
Without `TaskRuntime`, it calls `accept_turn(task_record=None)` and schedules the
legacy runner only after a non-replayed commit. The nullable task ID already
supported by ingress receipts is used. Attachment validation failures therefore
occur before persistence and cannot leave an empty project task.

Queued runtime dispatch revalidates the project after dequeue and before
creating the tool context. This closes the filesystem race between acceptance
and actual execution.

### 4. Atomic Project-History Deletion

Refactor session deletion into:

- a connection-scoped helper that removes all database rows for one session;
- a post-commit helper that performs best-effort material/meta-run cleanup.

`delete_project_workspace_sessions` opens one write transaction, verifies the
project state, selects the complete ordered session snapshot, deletes all
dependent rows and sessions, and commits once. Post-commit cleanup runs for each
deleted session. A cleanup failure is logged but cannot misrepresent the
already-committed database result.

Database serialization defines concurrent behavior:

- an accepted turn committed before deletion is in the deleted snapshot;
- a turn accepted after deletion commits is new history and remains visible;
- project removal is different: acceptance after removal fails its workspace
  guard.

The project record and all project files remain untouched.

### 5. Protected Metadata and Symlink Semantics

Filesystem permission construction must preserve two views:

- the lexical absolute path under the writable workspace; and
- the canonical target used for actual access checks and backend mounts.

For `.git`, `.codex`, `.agents`, and every explicit non-write carveout under a
writable root, both views are protected. Effective-entry deduplication must not
discard the lexical entry merely because it canonicalizes to an existing
target. Direct filesystem/patch gates evaluate the original logical path before
following the final symlink. Linux bind mounts, macOS Seatbelt rules, and
Windows ACL/reparse-point plans receive enough information to prevent
unlinking, renaming, or replacing the protected symlink inode as well as writing
through it to the target.

This follows Codex's `canonicalize_preserving_symlinks` and writable-root
carveout behavior without copying its Rust representation into Python.

### 6. Stable Directory-Listing RPC

Evolve `sandbox.path.list` compatibly:

```json
{
  "currentPath": "/absolute/current",
  "path": "/absolute/current",
  "parentPath": "/absolute-or-null",
  "entries": []
}
```

`path` remains a temporary alias for existing clients. `parentPath` becomes the
actual parent and is `null` at a filesystem root. The synthetic `..` entry is
removed.

If `path` is omitted, the stable starting order is:

1. the validated workspace of an existing project session;
2. the session key's agent default workspace, when available;
3. the host user's home directory.

The gateway process cwd is never an implicit start or relative-path base.
Relative user input requires an absolute `basePath` and resolves against it.
Missing, inaccessible, or non-directory paths return an explicit RPC error
instead of an empty list. Workspace entries mark files non-selectable; the UI
renders only selectable directories.

### 7. Web and Desktop Picker State

The Web picker owns independent state:

- `currentDirectory`: directory represented by the entries;
- `selectedDirectory`: value returned by Choose;
- `locationDraft`: editable location text;
- `parentDirectory`;
- `phase`;
- `openEpoch` and `browseSequence`.

Single click changes selection only. Double click or Enter navigates. An
explicit parent control navigates to `parentDirectory`. A successful navigation
updates current location and selection consistently. Choose is disabled while
loading or when no selectable directory is selected.

Every request captures its dialog epoch and sequence. Only the newest request
from the current open instance may update state. A current-request failure keeps
the previous entries/selection and displays the error; stale successes and
failures are ignored.

Desktop continues to use Electron's native `openDirectory` dialog. Cancellation
closes normally. A rejected native call shows a Desktop-specific Retry/Cancel
state and never exposes an uninitialized Gateway-host Web picker. Results that
arrive after close/reopen are ignored by the same epoch rule.

### 8. Durable Active-Workspace UI

Separate:

- `pendingWorkspaceId`, used only on the first `chat.send`; and
- an authoritative active-workspace snapshot/state, used for display and send
  gating.

`sessions.messages.subscribe` already returns session metadata. Extend its
project projection with ID, name, canonical path, availability, removal state,
and availability reason. The subscription composable applies metadata only
when its existing session-key/attempt race guard still owns the response.

After first-send acceptance, pending state is cleared but active project state
remains. Refreshing or reopening a task reconstructs it from the subscription,
not from the first page of `sessions.list`.

The composer always shows the active project name and path:

- a blank project draft may close its project chip;
- a durable project task shows a read-only chip;
- resolving, unavailable, removed, or unknown projects display a state and
  block all send paths, including Enter, queued sends, and programmatic send;
- ordinary tasks remain unchanged.

Removing the project used by the current blank draft atomically replaces the
route with a default draft and clears its project ID. Removing a project that
owns the current durable task keeps the history visible but marks it unavailable
and blocks new sends. Sidebar creation controls are disabled for unavailable
projects.

### 9. Existing Sandbox Regression Repairs

The six reproduced failures are part of this work:

1. Windows payload construction uses a Windows-style PATH only when supplied by
   the Windows request/environment. A non-Windows host PATH is not treated as
   one Windows path. Tool-directory probing catches `OSError`.
2. Explicitly disabled sandbox runtime takes precedence over a stale restricted
   `ToolContext`.
3. Filesystem worker path verification preserves an `ELOOP`/symlink-loop
   classification so host and worker directory listings both report a broken
   symlink.

These are correctness repairs, not platform-test skips.

## Error Contract

Project validation uses stable internal reasons:

- `not_found`
- `removed`
- `untrusted`
- `unavailable`
- `canonical_changed`
- `guard_required`
- `binding_changed`

RPC mapping remains compact:

- missing, removed, or untrusted records map to `WORKSPACE_NOT_FOUND`;
- unavailable, canonical change, missing guard, or binding change map to
  `WORKSPACE_UNAVAILABLE` with `details.reason`;
- attempts to mutate the workspace of a bound project map to
  `PROJECT_WORKSPACE_FIXED`;
- sandbox capability/setup failures retain the existing setup/backend error
  contract.

No low-level filesystem path or backend traceback is exposed to a non-owner.

## Testing Strategy

### Backend and Storage

- mode matrix for implicit project Standard, ordinary implicit Full, explicit
  Standard/Trusted/Full, `sandbox=false`, and `permissions.default_mode=full`;
- capability-runtime boot tests proving implicit Full installs a real backend
  but retains Full as the request fallback;
- first-send, continue, reset, fork, bootstrap, and queued-dispatch workspace
  validation;
- missing directory, inaccessible directory, file/root path, symlink/junction
  retarget, tampered origin, removed record, and binding mismatch;
- receipt replay after removal/missing/retarget and conflict precedence;
- storage guard race between precheck and acceptance;
- no-`TaskRuntime` success, replay-once scheduling, attachment failure, and
  transaction rollback;
- atomic history deletion, injected mid-delete failure, cleanup failure, and
  concurrent accept/delete serialization.

### Sandbox

- project-internal write succeeds while a sibling write requires Standard user
  approval or is denied;
- ordinary Full continues to use host execution;
- explicit sandbox disablement dominates stale Standard context;
- backend unavailable/setup pending fails project Standard closed;
- `.git`, `.codex`, and `.agents` symlinks cannot be written through, unlinked,
  renamed, or replaced on supported backends;
- Windows PATH/ACL tests and host/worker symlink-loop parity pass.

### Web and Desktop

- omitted-path stable start, project-session start, parent/root contract,
  relative base handling, inaccessible-path errors, and owner gate;
- independent current/selected state, click versus navigation, parent control,
  non-selectable entries, keyboard behavior, and loading disablement;
- out-of-order request resolution, close/reopen stale result, retained listing
  on current error, and native cancel/reject/retry;
- first-send handoff retains the project indicator;
- refresh and session switch restore authoritative project metadata;
- unavailable/removed/unknown/resolving states block every send path;
- active draft removal routes to default while durable-task removal preserves
  history;
- a full project lifecycle E2E covers picker, trust, first send, reload,
  remove/reopen, and history deletion.

## Compatibility and Rollout

- No database migration is required for workspace guards, nullable task
  acceptance, or batch deletion.
- Existing RPC fields remain during the picker transition.
- Legacy project Full contexts without mode provenance are safety-migrated
  lazily as described above; explicit operator Full remains unchanged.
- Runtime/setup telemetry must distinguish logical request default, project
  default, effective backend capability, and setup readiness.
- Windows, macOS, and Linux failures remain fail-closed for Standard project
  turns.

## Acceptance Criteria

The work is complete only when:

1. a default project task demonstrably writes inside its project through a real
   sandbox and cannot write a sibling path without the Standard human approval
   flow;
2. an ordinary default task remains Full and explicit operator modes retain
   their semantics;
3. every project turn uses a freshly validated project record and rejects
   missing, removed, inaccessible, or retargeted directories before execution;
4. idempotent replay, first-send persistence, and history deletion satisfy the
   transaction rules above;
5. protected metadata symlinks are protected in logical and canonical form;
6. Web and Desktop picker behavior matches the state/error contract;
7. the active project remains visible and authoritative after send, reload, and
   removal;
8. all six reproduced sandbox failures pass without skips;
9. focused backend, full relevant sandbox, Web unit/type/architecture, Electron,
   and project-lifecycle E2E verification pass; and
10. the rebuilt Control UI and gateway health endpoints return HTTP 200.
