# Project Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent Codex-style project workspaces while preserving OpenSquilla's existing default workspace for ordinary tasks.

**Architecture:** A new `project_workspaces` table owns stable project identity, path, name, ordering, pin, removal, and trust state; sessions reference it through an optional `workspace_id`. Gateway RPCs own path validation and project lifecycle, while the first `chat.send` atomically creates a session with both `workspace_id` and the matching sandbox run context. The WebUI renders persisted project rows independently of sessions, and Electron supplies a trusted native directory picker.

**Tech Stack:** Python 3.11+, SQLModel/SQLite/aiosqlite, yoyo migrations, pytest, Vue 3/Pinia/TypeScript/Vitest, Electron/TypeScript.

## Global Constraints

- The default landing has no selected project; ordinary tasks continue to use the existing OpenSquilla default workspace.
- Project rows render above ordinary tasks; ordinary tasks have no workspace header.
- Selecting a folder creates/restores the project immediately, but no task is persisted until the first message.
- Clicking a project name only expands/collapses it; clicking a task changes the active task/workspace.
- Project order never changes from task activity. New projects lead the unpinned region; newly pinned projects lead the pinned region.
- Project task buttons use existing `info`, `pencil`, and `moreHorizontal` icons; pencil/more are hidden until row hover or keyboard focus.
- Removing a project hides it but preserves history and disk files. Deleting project history permanently deletes sessions but never disk files.
- A project has one primary directory. Out-of-workspace access remains owned by the existing sandbox approval/auto-approval system.
- The backend is authoritative for canonical paths and effective workspaces.
- Preserve the pre-existing untracked `docs/sandbox-deep-dive.html`.

---

## File Structure

### Backend

- Create `migrations/V025__project_workspaces.py`: additive workspace table and session binding migration.
- Modify `src/opensquilla/session/models.py`: `ProjectWorkspace` model and `SessionNode.workspace_id`.
- Modify `src/opensquilla/session/storage.py`: fresh-schema DDL and workspace/session-binding CRUD.
- Create `src/opensquilla/project_workspaces.py`: path canonicalization, validation, response projection, and legacy-session adoption.
- Create `src/opensquilla/gateway/rpc_workspaces.py`: owner-gated project lifecycle RPCs.
- Modify `src/opensquilla/gateway/rpc/__init__.py` and `src/opensquilla/gateway/scopes.py`: register/classify RPCs.
- Modify `src/opensquilla/gateway/rpc_sessions.py`: atomic project binding and effective workspace propagation.
- Modify `src/opensquilla/session/manager.py`: fork inheritance of `workspace_id`.

### Desktop and WebUI

- Modify `desktop/electron/src/main.ts` and `desktop/electron/src/preload.cts`: trusted native project directory picker.
- Modify `opensquilla-webui/src/platform/types.ts`, `platform/desktop.ts`, `platform/web.ts`, and `vite-env.d.ts`: platform picker contract.
- Create `opensquilla-webui/src/composables/useProjectWorkspaces.ts`: authoritative project list and mutations.
- Create `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue`: Gateway-host directory browser for Web.
- Create `opensquilla-webui/src/components/ProjectWorkspaceEditDialog.vue`: name-only editor with read-only path.
- Modify `opensquilla-webui/src/composables/useSessions.ts`: merge persisted projects with session/task rows.
- Modify `opensquilla-webui/src/components/SidebarConversations.vue`: project disclosure rows, info popover, hidden actions, and menu.
- Modify `opensquilla-webui/src/App.vue`: orchestration, confirmations, routing, and picker entry points.
- Modify `opensquilla-webui/src/composables/chat/useChatSessionRoute.ts`, `useChatSend.ts`, `views/ChatView.vue`, and `components/chat/ChatComposer.vue`: project draft state and first-send binding.
- Modify all six locale JSON files and relevant scoped/base CSS.

---

### Task 1: Persist Project Workspaces

**Files:**
- Create: `migrations/V025__project_workspaces.py`
- Modify: `src/opensquilla/session/models.py`
- Modify: `src/opensquilla/session/storage.py`
- Create: `tests/test_migrations/test_v025_project_workspaces.py`
- Create: `tests/test_session/test_project_workspace_storage.py`

**Interfaces:**
- Produces: `ProjectWorkspace`, `SessionNode.workspace_id`, and `SessionStorage` methods `create_or_restore_project_workspace`, `list_project_workspaces`, `get_project_workspace`, `update_project_workspace`, `set_project_workspace_pin`, `remove_project_workspace`, `bind_session_workspace`, and `delete_project_workspace_sessions`.

- [ ] **Step 1: Write migration and storage tests**

```python
async def test_workspace_crud_preserves_fixed_order(storage):
    first = await storage.create_or_restore_project_workspace(path="/repo/a", path_key="/repo/a", display_name="a")
    second = await storage.create_or_restore_project_workspace(path="/repo/b", path_key="/repo/b", display_name="b")
    assert [row.workspace_id for row in await storage.list_project_workspaces()] == [
        second.workspace_id,
        first.workspace_id,
    ]

async def test_remove_and_restore_keeps_history(storage):
    workspace = await storage.create_or_restore_project_workspace(path="/repo/a", path_key="/repo/a", display_name="a")
    await storage.remove_project_workspace(workspace.workspace_id)
    restored = await storage.create_or_restore_project_workspace(path="/repo/a", path_key="/repo/a", display_name="a")
    assert restored.workspace_id == workspace.workspace_id
    assert restored.removed_at is None
```

- [ ] **Step 2: Run tests and confirm the missing schema/API failures**

Run:

```text
uv run pytest tests/test_migrations/test_v025_project_workspaces.py tests/test_session/test_project_workspace_storage.py -q
```

Expected: failures because V025, `ProjectWorkspace`, and storage CRUD do not exist.

- [ ] **Step 3: Add the model and migration**

```python
class ProjectWorkspace(SQLModel, table=True):
    __tablename__ = "project_workspaces"
    workspace_id: str = Field(default_factory=_new_uuid, primary_key=True)
    path: str
    path_key: str = Field(unique=True)
    display_name: str
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    position_at: int = Field(default_factory=_now_ms)
    pinned_at: int | None = None
    removed_at: int | None = None
    trusted_at: int | None = None
```

V025 creates the table and indexes, then adds nullable `sessions.workspace_id`. Fresh-schema DDL mirrors the migrated schema and `SCHEMA_VERSION` increments from 11 to 12.

- [ ] **Step 4: Implement serialized workspace CRUD**

Use explicit SQL and the existing `_write_transaction`/`_serialized_read` conventions. `list_project_workspaces` orders pinned rows by `pinned_at DESC`, then unpinned rows by `position_at DESC`; name edits do not modify either order field. `delete_project_workspace_sessions` deletes every bound session through the same durable cleanup path used by `delete_session`.

- [ ] **Step 5: Run focused tests**

Run:

```text
uv run pytest tests/test_migrations/test_v025_project_workspaces.py tests/test_session/test_project_workspace_storage.py tests/test_migrations/test_migrations_lint.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```text
git add migrations/V025__project_workspaces.py src/opensquilla/session/models.py src/opensquilla/session/storage.py tests/test_migrations/test_v025_project_workspaces.py tests/test_session/test_project_workspace_storage.py
git commit -m "feat: persist project workspaces"
```

### Task 2: Add Workspace Domain Service and RPCs

**Files:**
- Create: `src/opensquilla/project_workspaces.py`
- Create: `src/opensquilla/gateway/rpc_workspaces.py`
- Modify: `src/opensquilla/gateway/rpc/__init__.py`
- Modify: `src/opensquilla/gateway/scopes.py`
- Create: `tests/test_gateway/test_rpc_workspaces.py`

**Interfaces:**
- Consumes: Task 1 storage methods.
- Produces RPCs: `workspaces.list`, `workspaces.open`, `workspaces.update`, `workspaces.pin`, `workspaces.remove`, and `workspaces.history.delete`.
- Produces projection: `{id, name, path, taskCount, pinned, available}`.

- [ ] **Step 1: Write failing RPC tests**

```python
async def test_open_workspace_requires_owner_and_existing_directory(rpc_ctx, tmp_path):
    result = await dispatch("workspaces.open", {"path": str(tmp_path), "trusted": True}, owner_ctx)
    assert result["workspace"]["path"] == str(tmp_path.resolve())

async def test_empty_workspace_is_returned_with_zero_tasks(owner_ctx, tmp_path):
    opened = await dispatch("workspaces.open", {"path": str(tmp_path), "trusted": True}, owner_ctx)
    listed = await dispatch("workspaces.list", None, owner_ctx)
    assert listed["workspaces"][0]["id"] == opened["workspace"]["id"]
    assert listed["workspaces"][0]["taskCount"] == 0
```

Cover duplicate normalized paths, non-directory paths, untrusted requests, pin/unpin order, rename, remove/restore, permanent history deletion, and non-owner rejection.

- [ ] **Step 2: Run the RPC tests and confirm METHOD_NOT_FOUND**

Run:

```text
uv run pytest tests/test_gateway/test_rpc_workspaces.py -q
```

Expected: failures because workspace RPCs are not registered.

- [ ] **Step 3: Implement canonical path validation**

```python
def resolve_project_path(value: str) -> ResolvedProjectPath:
    candidate = Path(value).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("workspace_not_directory")
    normalized = unicodedata.normalize("NFC", str(candidate))
    path_key = os.path.normcase(normalized).replace("\\", "/")
    return ResolvedProjectPath(path=normalized, path_key=path_key, name=candidate.name or normalized)
```

Reject filesystem roots. Preserve missing projects in list responses with `available=False`; never silently fall back to the default workspace.

- [ ] **Step 4: Implement RPC handlers and legacy adoption**

`workspaces.open` requires owner and `trusted is True`, creates/restores by `path_key`, and does not reorder an already-visible project. `workspaces.list` idempotently adopts legacy sessions whose saved run-context workspace differs from their Agent default, binds them to a generated project, and returns empty projects too.

- [ ] **Step 5: Run focused tests**

Run:

```text
uv run pytest tests/test_gateway/test_rpc_workspaces.py tests/test_gateway/test_rpc_sessions.py tests/test_gateway/test_rpc_sessions_fork.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```text
git add src/opensquilla/project_workspaces.py src/opensquilla/gateway/rpc_workspaces.py src/opensquilla/gateway/rpc/__init__.py src/opensquilla/gateway/scopes.py tests/test_gateway/test_rpc_workspaces.py
git commit -m "feat: expose project workspace lifecycle"
```

### Task 3: Bind First Sends Atomically and Fix Effective Workspace

**Files:**
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/session/manager.py`
- Modify: `tests/test_gateway/test_rpc_sessions.py`
- Modify: `tests/test_gateway/test_rpc_sessions_fork.py`
- Create: `tests/test_gateway/test_project_workspace_execution.py`

**Interfaces:**
- Consumes: `workspaceId` from `chat.send`/`sessions.send`.
- Produces: sessions with `workspace_id` and matching `origin.sandbox_run_context.workspace`.

- [ ] **Step 1: Write failing atomic-binding and execution tests**

```python
async def test_new_chat_workspace_binding_is_atomic(owner_ctx, project):
    result = await send_new_chat(owner_ctx, workspaceId=project.workspace_id, message="pwd")
    session = await storage.get_session(result["sessionKey"])
    assert session.workspace_id == project.workspace_id
    assert session.origin["sandbox_run_context"]["workspace"] == project.path

async def test_tool_context_uses_saved_project_workspace(owner_ctx, project):
    tool_context = await capture_tool_context_for_new_chat(owner_ctx, project.workspace_id)
    assert tool_context.workspace_dir == project.path
```

Also cover: workspaceId rejected on `continue`, removed/missing project rejection, non-owner rejection, idempotency conflict when the same request id changes workspace, fork inheritance, bootstrap returning the project path, and no durable session when acceptance fails.

- [ ] **Step 2: Run focused tests and observe wrong/default workspace failures**

Run:

```text
uv run pytest tests/test_gateway/test_project_workspace_execution.py tests/test_gateway/test_rpc_sessions_fork.py -q
```

Expected: fail because workspaceId is ignored and `ToolContext` still receives the Agent default.

- [ ] **Step 3: Add workspace binding to prepared session creation**

For `SessionIntent.NEW_CHAT`, resolve `workspaceId`, require owner, and add both fields to `create_kwargs`:

```python
create_kwargs["workspace_id"] = project.workspace_id
create_kwargs["origin"] = {
    RUN_CONTEXT_ORIGIN_KEY: RunContext(
        run_mode=config_run_mode(ctx.config),
        workspace=project.path,
        source="project_workspace",
    ).to_origin_payload(),
}
```

Reject workspaceId for continuing sessions. Because `chat.send` fingerprints all send parameters, the workspace identity becomes part of idempotency automatically.

- [ ] **Step 4: Use the saved run context everywhere**

Immediately after `get_run_context`, set:

```python
workspace_dir = run_context.workspace or workspace_dir
```

Use that value for strictness and `tool_context_from_envelope`. `sessions.bootstrap` resolves the saved run context instead of returning the Agent default. Forked sessions copy `workspace_id` in addition to the branch-safe origin.

- [ ] **Step 5: Run focused tests**

Run:

```text
uv run pytest tests/test_gateway/test_project_workspace_execution.py tests/test_gateway/test_rpc_sessions.py tests/test_gateway/test_rpc_sessions_fork.py tests/test_sandbox/test_run_context_grants.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```text
git add src/opensquilla/gateway/rpc_sessions.py src/opensquilla/session/manager.py tests/test_gateway/test_project_workspace_execution.py tests/test_gateway/test_rpc_sessions.py tests/test_gateway/test_rpc_sessions_fork.py
git commit -m "feat: bind tasks to project workspaces"
```

### Task 4: Add the Trusted Desktop Directory Picker

**Files:**
- Modify: `desktop/electron/src/main.ts`
- Modify: `desktop/electron/src/preload.cts`
- Modify: `opensquilla-webui/src/vite-env.d.ts`
- Modify: `opensquilla-webui/src/platform/types.ts`
- Modify: `opensquilla-webui/src/platform/desktop.ts`
- Modify: `opensquilla-webui/src/platform/web.ts`
- Create: `desktop/electron/scripts/test-project-workspace-picker.mjs`
- Modify: `desktop/electron/package.json`

**Interfaces:**
- Produces: `platform.files.chooseProjectDirectory(): Promise<{path: string} | null>`.

- [ ] **Step 1: Write a failing desktop contract test**

The script asserts the preload bridge invokes `desktop:workspace:choose-directory`, the main handler checks `trustedControlUiIpc`, uses `openDirectory`, and cancellation returns `null` rather than an error.

- [ ] **Step 2: Run and confirm failure**

Run:

```text
npm --prefix desktop/electron run test:project-workspace-picker
```

Expected: failure because the bridge and handler do not exist.

- [ ] **Step 3: Implement the handler and platform contract**

```typescript
ipcMain.handle('desktop:workspace:choose-directory', async event => {
  if (!trustedControlUiIpc(event)) return null
  const choice = await dialog.showOpenDialog(currentMainWindow()!, {
    title: 'Choose a project',
    properties: ['openDirectory'],
  })
  return choice.canceled || choice.filePaths.length !== 1
    ? null
    : { path: resolve(choice.filePaths[0]!) }
})
```

Expose it through preload and the platform abstraction. Web leaves the native picker undefined and uses the Gateway browser in Task 6.

- [ ] **Step 4: Run desktop verification**

Run:

```text
npm --prefix desktop/electron run test:project-workspace-picker
npm --prefix desktop/electron run build
```

Expected: both pass.

- [ ] **Step 5: Commit**

```text
git add desktop/electron/src/main.ts desktop/electron/src/preload.cts desktop/electron/scripts/test-project-workspace-picker.mjs desktop/electron/package.json opensquilla-webui/src/vite-env.d.ts opensquilla-webui/src/platform/types.ts opensquilla-webui/src/platform/desktop.ts opensquilla-webui/src/platform/web.ts
git commit -m "feat: choose project directories on desktop"
```

### Task 5: Build the Frontend Workspace State and Sidebar Arrangement

**Files:**
- Modify: `opensquilla-webui/src/types/rpc.ts`
- Create: `opensquilla-webui/src/composables/useProjectWorkspaces.ts`
- Modify: `opensquilla-webui/src/composables/useSessions.ts`
- Create: `opensquilla-webui/src/composables/useProjectWorkspaces.test.ts`
- Modify: `opensquilla-webui/src/composables/useSessions.sections.test.ts`

**Interfaces:**
- Produces: `ProjectWorkspaceItem`, `loadWorkspaces`, `openWorkspace`, `renameWorkspace`, `setPinned`, `removeWorkspace`, and `deleteWorkspaceHistory`.
- Produces sidebar rows: `workspace`, `workspace-empty`, and `session`, each carrying `workspaceId` where applicable.

- [ ] **Step 1: Write failing state and arrangement tests**

```typescript
it('places fixed project groups before recency-sorted ordinary tasks', () => {
  const sections = arrangeSidebarSections(tasks, projects)
  expect(sections[0].rows.map(row => row.rowKind)).toEqual([
    'workspace', 'workspace-empty', 'workspace', 'session', 'session',
  ])
})

it('does not reorder projects when a child task becomes active', () => {
  expect(projectKeys(arrangeSidebarSections(updatedTasks, projects))).toEqual(['project:b', 'project:a'])
})
```

Cover pinned/unpinned order, empty projects, removed projects omitted by the RPC, direct task counts, subtask indentation, and ordinary tasks at the bottom.

- [ ] **Step 2: Run tests and confirm the old session-derived grouping fails**

Run:

```text
npm --prefix opensquilla-webui run test:unit -- src/composables/useProjectWorkspaces.test.ts src/composables/useSessions.sections.test.ts
```

Expected: fail because projects are not independent inputs.

- [ ] **Step 3: Implement the typed project store and pure arranger**

`arrangeSidebarSections(items, projects)` emits projects exactly in backend order, inserts an empty row when a project has no tasks, recency-sorts only child/default tasks, and keeps channels/automations unchanged.

- [ ] **Step 4: Run focused tests**

Run:

```text
npm --prefix opensquilla-webui run test:unit -- src/composables/useProjectWorkspaces.test.ts src/composables/useSessions.sections.test.ts
```

Expected: all pass.

- [ ] **Step 5: Commit**

```text
git add opensquilla-webui/src/types/rpc.ts opensquilla-webui/src/composables/useProjectWorkspaces.ts opensquilla-webui/src/composables/useProjectWorkspaces.test.ts opensquilla-webui/src/composables/useSessions.ts opensquilla-webui/src/composables/useSessions.sections.test.ts
git commit -m "feat: model project workspaces in the web ui"
```

### Task 6: Implement Project Picking, Sidebar Actions, and Project Drafts

**Files:**
- Create: `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue`
- Create: `opensquilla-webui/src/components/ProjectWorkspaceEditDialog.vue`
- Modify: `opensquilla-webui/src/components/SidebarConversations.vue`
- Modify: `opensquilla-webui/src/App.vue`
- Modify: `opensquilla-webui/src/composables/chat/useChatSessionRoute.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSend.ts`
- Modify: `opensquilla-webui/src/views/ChatView.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposer.vue`
- Create: `opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts`
- Create: `opensquilla-webui/src/components/ProjectWorkspacePickerDialog.test.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatSend.test.ts`

**Interfaces:**
- Draft route uses only the non-sensitive project id: `/chat/new?project=<workspace-id>`.
- First send adds `workspaceId` only while `intent === "new_chat"`.

- [ ] **Step 1: Write failing component and send tests**

Cover:

- project name toggles disclosure but emits no task selection;
- info icon is always rendered with path/task-count popover;
- pencil/more controls are present but CSS-hidden until hover/focus;
- pencil emits `new-project-task`;
- menu emits pin/edit/delete-history/remove;
- delete confirmation includes top-level task count and danger styling;
- desktop cancel creates nothing;
- Web path browser calls `sandbox.path.list`;
- first send includes workspaceId and retry preserves it;
- closing the project chip routes to a default draft.

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```text
npm --prefix opensquilla-webui run test:unit -- src/components/SidebarConversations.workspaces.test.ts src/components/ProjectWorkspacePickerDialog.test.ts src/composables/chat/useChatSend.test.ts
```

Expected: failures because project controls and draft workspace identity do not exist.

- [ ] **Step 3: Implement the selection flow**

Desktop uses `platform.files.chooseProjectDirectory`. Web opens the Gateway-host browser and clearly labels that path scope. Before `workspaces.open`, show a trust confirmation stating that OpenSquilla may read, modify, and execute content in the selected folder.

- [ ] **Step 4: Implement sidebar actions and dialogs**

Use existing icons and confirmation infrastructure. Project names only toggle a project-specific collapsed key. Info popovers open on hover/focus. Menus close on outside click/Escape. Rename validates a non-empty trimmed name and displays the read-only path.

- [ ] **Step 5: Implement project draft propagation**

`useChatSessionRoute` reads the project id, `ChatView` loads its workspace record, `ChatComposer` renders the project chip and close control, and `useChatSend` captures the project id inside `SendAttempt`:

```typescript
if (intent === 'new_chat' && options.pendingWorkspaceId.value) {
  params.workspaceId = options.pendingWorkspaceId.value
}
```

Retry restoration retains the same workspace id. Successful handoff clears the draft identity because the durable session is now authoritative.

- [ ] **Step 6: Run focused tests**

Run:

```text
npm --prefix opensquilla-webui run test:unit -- src/components/SidebarConversations.workspaces.test.ts src/components/ProjectWorkspacePickerDialog.test.ts src/composables/chat/useChatSend.test.ts src/composables/useSessions.sections.test.ts
```

Expected: all pass.

- [ ] **Step 7: Commit**

```text
git add opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue opensquilla-webui/src/components/ProjectWorkspaceEditDialog.vue opensquilla-webui/src/components/SidebarConversations.vue opensquilla-webui/src/App.vue opensquilla-webui/src/composables/chat/useChatSessionRoute.ts opensquilla-webui/src/composables/chat/useChatSend.ts opensquilla-webui/src/views/ChatView.vue opensquilla-webui/src/components/chat/ChatComposer.vue opensquilla-webui/src/components/SidebarConversations.workspaces.test.ts opensquilla-webui/src/components/ProjectWorkspacePickerDialog.test.ts opensquilla-webui/src/composables/chat/useChatSend.test.ts
git commit -m "feat: add project workspace interactions"
```

### Task 7: Finish Copy, Styling, Migration Coverage, and End-to-End Verification

**Files:**
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`
- Modify: `opensquilla-webui/src/assets/base.css`
- Create: `opensquilla-webui/e2e/project-workspaces.spec.ts`

**Interfaces:**
- Produces complete translated task/project vocabulary and an end-to-end acceptance test.

- [ ] **Step 1: Add all locale keys and workspace styling**

Use “task” consistently for both project and default records. Preserve existing design tokens; no new colors, radius scale, or icon artwork. Ensure hidden row actions become visible on both `:hover` and `:focus-within`.

- [ ] **Step 2: Add the E2E acceptance test**

The test stubs workspace RPCs and verifies: empty project appears immediately; project name only collapses; pencil opens a project draft; first send contains the workspace id; ordinary task remains below projects; removing/reopening restores history; deleting history does not request filesystem deletion.

- [ ] **Step 3: Run backend verification**

Run:

```text
uv run pytest tests/test_migrations/test_v025_project_workspaces.py tests/test_session/test_project_workspace_storage.py tests/test_gateway/test_rpc_workspaces.py tests/test_gateway/test_project_workspace_execution.py tests/test_gateway/test_rpc_sessions.py tests/test_gateway/test_rpc_sessions_fork.py tests/test_sandbox/test_rpc_sandbox_access.py tests/test_sandbox/test_run_context_grants.py -q
```

Expected: all pass.

- [ ] **Step 4: Run WebUI verification**

Run:

```text
npm --prefix opensquilla-webui run test:unit
npm --prefix opensquilla-webui run typecheck
npm --prefix opensquilla-webui run build
npm --prefix opensquilla-webui run test:e2e -- project-workspaces.spec.ts
```

Expected: all pass.

- [ ] **Step 5: Run desktop verification**

Run:

```text
npm --prefix desktop/electron run test:project-workspace-picker
npm --prefix desktop/electron run build
```

Expected: all pass.

- [ ] **Step 6: Perform the requirement audit**

Check every acceptance criterion in `docs/superpowers/specs/2026-07-24-project-workspaces-design.md` against a passing test or direct runtime evidence. Confirm `git status --short` contains only the pre-existing `docs/sandbox-deep-dive.html` plus intentional committed changes.

- [ ] **Step 7: Commit**

```text
git add opensquilla-webui/src/locales opensquilla-webui/src/assets/base.css opensquilla-webui/e2e/project-workspaces.spec.ts
git commit -m "test: verify project workspace experience"
```
