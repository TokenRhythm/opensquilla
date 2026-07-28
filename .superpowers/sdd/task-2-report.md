# Task 2 report: Desktop shortcut wiring and Workbench bounds refresh

## Scope completed

- Added the real Electron desktop-zoom fixture and extended the focused zoom test with source contracts plus end-to-end keyboard checks.
- Installed `installDesktopZoomShortcuts` when the main window is created, using its `webContents` for both input and zoom targets.
- Added `NativeWorkbenchSurfaceManager.refreshBounds(owner)` as the focused public bridge to the existing active-bounds reapplication behavior.

## TDD evidence

### RED

Command (run from `desktop/electron`):

```bash
npm run test:desktop-zoom
```

Relevant output:

```text
> @opensquilla/desktop-electron@0.5.0 build
> tsc -p tsconfig.json

AssertionError [ERR_ASSERTION]: The input did not match the regular expression /installDesktopZoomShortcuts\\(\\s*window\\.webContents,\\s*window\\.webContents,\\s*\\(\\) => nativeWorkbenchSurfaces\\.refreshBounds\\(window\\),?\\s*\\)/
```

The TypeScript build completed, then the newly added main-window source contract failed because the shortcut installation and Workbench refresh bridge did not yet exist.

### GREEN

Commands (run from `desktop/electron`):

```bash
npm run test:desktop-zoom
npm run test:desktop-workbench
```

Relevant output:

```text
> @opensquilla/desktop-electron@0.5.0 test:desktop-zoom
> npm run build && node scripts/test-desktop-zoom-shortcuts.mjs

> @opensquilla/desktop-electron@0.5.0 build
> tsc -p tsconfig.json
```

`test:desktop-zoom` exited with status 0 after its source contracts and real Electron keyboard sequence completed.

```text
> @opensquilla/desktop-electron@0.5.0 test:desktop-workbench
> npm run build && node scripts/test-native-workbench-surface.mjs && node scripts/test-native-workbench-surface-electron.mjs

> @opensquilla/desktop-electron@0.5.0 build
> tsc -p tsconfig.json

native Workbench surface contract checks passed
native Workbench real Electron smoke checks passed
```

## Files changed

- `desktop/electron/scripts/fixtures/desktop-zoom-shortcuts/package.json`
- `desktop/electron/scripts/fixtures/desktop-zoom-shortcuts/main.mjs`
- `desktop/electron/scripts/test-desktop-zoom-shortcuts.mjs`
- `desktop/electron/src/main.ts`
- `desktop/electron/src/native-workbench-surface.ts`

## Self-review

- The shortcut listener is installed immediately after `mainWindow = window`, as required.
- It uses `window.webContents` as both input and zoom contents and calls `nativeWorkbenchSurfaces.refreshBounds(window)` after a zoom operation.
- `refreshBounds` has no additional behavior; it delegates directly to the existing guarded `reapplyActiveBounds` logic.
- The fixture is a sandboxed, context-isolated Electron app and verifies zoom in, out, and reset with the platform primary modifier.
- `git diff --check` completed with no whitespace errors.

## Concerns

None. The focused zoom test's successful run printed the build output but not its final console line; its process exit status was 0, and the follow-up Workbench contract plus real-Electron smoke checks both printed their passing results.

## Commit

Included in the same Task 2 commit as the implementation and tests.
