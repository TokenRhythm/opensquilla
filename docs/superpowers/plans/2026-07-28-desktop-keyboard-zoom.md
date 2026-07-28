# Desktop Keyboard Zoom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-platform desktop keyboard shortcuts for zoom in, zoom out, and reset without enabling pinch zoom or a visible native menu.

**Architecture:** A new main-process helper converts Electron `Input` values into zoom commands, applies a bounded factor to a target `WebContents`, and notifies a caller after handled zoom changes. `main.ts` installs it on the Control UI window and asks the native Workbench manager to refresh active bounds.

**Tech Stack:** Electron 42, TypeScript 6, Node.js assertion scripts, Playwright Electron automation.

## Global Constraints

- Support `Command/Ctrl` + `+`, `Command/Ctrl` + `-`, and `Command/Ctrl` + `0`.
- Accept the unshifted `=` key and main-keyboard/numpad variants.
- Use `Command` on macOS and `Ctrl` on Windows/Linux.
- Clamp zoom factors to 0.5–3.0 and reset to exactly 1.0.
- Do not enable pinch-to-zoom, add a visible application menu, or persist zoom.
- Keep native Workbench bounds aligned after programmatic zoom.

---

### Task 1: Define and test the desktop zoom contract

**Files:**
- Create: `desktop/electron/src/desktop-zoom-shortcuts.ts`
- Create: `desktop/electron/scripts/test-desktop-zoom-shortcuts.mjs`
- Modify: `desktop/electron/package.json`

**Interfaces:**
- Produces: `desktopZoomCommandForInput(input, platform): DesktopZoomCommand | null`
- Produces: `desktopZoomFactor(currentFactor, command): number`
- Produces: `installDesktopZoomShortcuts(inputContents, zoomContents, onZoomApplied?): () => void`

- [ ] **Step 1: Write the failing contract test**

Create `scripts/test-desktop-zoom-shortcuts.mjs` importing the not-yet-created compiled module with this initial content:

```js
import assert from 'node:assert/strict'

import {
  desktopZoomCommandForInput,
  desktopZoomFactor,
} from '../dist/desktop-zoom-shortcuts.js'

function keyInput(overrides = {}) {
  return {
    type: 'keyDown',
    key: '',
    code: '',
    control: false,
    alt: false,
    meta: false,
    ...overrides,
  }
}

assert.equal(desktopZoomCommandForInput(keyInput({ meta: true, key: '=', code: 'Equal' }), 'darwin'), 'in')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '-', code: 'Minus' }), 'win32'), 'out')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '0', code: 'Digit0' }), 'linux'), 'reset')
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, alt: true, key: '=', code: 'Equal' }), 'linux'), null)
assert.equal(desktopZoomCommandForInput(keyInput({ control: true, key: '=', code: 'Equal', type: 'keyUp' }), 'linux'), null)
assert.equal(desktopZoomFactor(1, 'in'), 1.2)
assert.equal(desktopZoomFactor(1, 'out'), 1 / 1.2)
assert.equal(desktopZoomFactor(2, 'reset'), 1)
assert.equal(desktopZoomFactor(3, 'in'), 3)
assert.equal(desktopZoomFactor(0.5, 'out'), 0.5)

console.log('desktop keyboard zoom contract checks passed')
```

Add the following entry to the `scripts` object in `package.json`:

```json
"test:desktop-zoom": "npm run build && node scripts/test-desktop-zoom-shortcuts.mjs"
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
npm run test:desktop-zoom
```

Expected: fail because `dist/desktop-zoom-shortcuts.js` does not exist.

- [ ] **Step 3: Implement the minimal zoom helper**

Create `src/desktop-zoom-shortcuts.ts` with:

```ts
import type { Input, WebContents } from 'electron'

export const DESKTOP_ZOOM_MIN_FACTOR = 0.5
export const DESKTOP_ZOOM_MAX_FACTOR = 3
export const DESKTOP_ZOOM_STEP_FACTOR = 1.2

export type DesktopZoomCommand = 'in' | 'out' | 'reset'

type DesktopZoomInput = Pick<
  Input,
  'type' | 'key' | 'code' | 'control' | 'alt' | 'meta'
>

export function desktopZoomCommandForInput(
  input: DesktopZoomInput,
  platform: NodeJS.Platform = process.platform,
): DesktopZoomCommand | null {
  if (input.type !== 'keyDown' || input.alt) return null
  const primaryModifier = platform === 'darwin' ? input.meta : input.control
  if (!primaryModifier) return null
  if (input.key === '0' || input.code === 'Digit0' || input.code === 'Numpad0') return 'reset'
  if (input.key === '+' || input.key === '=' || input.code === 'Equal' || input.code === 'NumpadAdd') return 'in'
  if (input.key === '-' || input.key === '_' || input.code === 'Minus' || input.code === 'NumpadSubtract') return 'out'
  return null
}

export function desktopZoomFactor(
  currentFactor: number,
  command: DesktopZoomCommand,
): number {
  if (command === 'reset') return 1
  const candidate = command === 'in'
    ? currentFactor * DESKTOP_ZOOM_STEP_FACTOR
    : currentFactor / DESKTOP_ZOOM_STEP_FACTOR
  return Math.min(DESKTOP_ZOOM_MAX_FACTOR, Math.max(DESKTOP_ZOOM_MIN_FACTOR, candidate))
}

export function installDesktopZoomShortcuts(
  inputContents: WebContents,
  zoomContents: WebContents = inputContents,
  onZoomApplied: () => void = () => {},
): () => void {
  const listener = (event: Electron.Event, input: Input) => {
    const command = desktopZoomCommandForInput(input)
    if (!command) return
    event.preventDefault()
    zoomContents.setZoomFactor(desktopZoomFactor(zoomContents.getZoomFactor(), command))
    onZoomApplied()
  }
  inputContents.on('before-input-event', listener)
  return () => inputContents.removeListener('before-input-event', listener)
}
```

- [ ] **Step 4: Run the contract test to verify GREEN**

Run:

```bash
npm run test:desktop-zoom
```

Expected: all zoom contract assertions pass.

### Task 2: Wire shortcuts into the main window and refresh Workbench bounds

**Files:**
- Modify: `desktop/electron/src/main.ts`
- Modify: `desktop/electron/src/native-workbench-surface.ts`
- Modify: `desktop/electron/scripts/test-desktop-zoom-shortcuts.mjs`
- Create: `desktop/electron/scripts/fixtures/desktop-zoom-shortcuts/main.mjs`
- Create: `desktop/electron/scripts/fixtures/desktop-zoom-shortcuts/package.json`

**Interfaces:**
- Consumes: `installDesktopZoomShortcuts(inputContents, zoomContents, onZoomApplied)`
- Produces: `NativeWorkbenchSurfaceManager.refreshBounds(owner): void`

- [ ] **Step 1: Add a failing real-Electron shortcut test**

Create `scripts/fixtures/desktop-zoom-shortcuts/package.json`:

```json
{
  "name": "desktop-zoom-shortcuts-fixture",
  "private": true,
  "type": "module",
  "main": "main.mjs"
}
```

Create `scripts/fixtures/desktop-zoom-shortcuts/main.mjs`:

```js
import { app, BrowserWindow } from 'electron'

import { installDesktopZoomShortcuts } from '../../../dist/desktop-zoom-shortcuts.js'

await app.whenReady()
const window = new BrowserWindow({
  width: 640,
  height: 480,
  show: true,
  webPreferences: {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  },
})
installDesktopZoomShortcuts(window.webContents)
await window.loadURL('data:text/html;charset=utf-8,<title>Desktop zoom shortcuts</title><main>Zoom fixture</main>')
window.show()
window.focus()
```

Extend `test-desktop-zoom-shortcuts.mjs` with these imports and smoke checks:

```js
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const workbenchSource = readFileSync(
  new URL('../src/native-workbench-surface.ts', import.meta.url),
  'utf8',
)
assert.match(
  mainSource,
  /installDesktopZoomShortcuts\(\s*window\.webContents,\s*window\.webContents,\s*\(\) => nativeWorkbenchSurfaces\.refreshBounds\(window\),?\s*\)/,
)
assert.match(
  workbenchSource,
  /refreshBounds\(owner: BrowserWindow\): void \{\s*this\.reapplyActiveBounds\(owner\)\s*\}/,
)

const fixtureRoot = fileURLToPath(
  new URL('./fixtures/desktop-zoom-shortcuts', import.meta.url),
)
let desktopApp
try {
  desktopApp = await electron.launch({ args: [fixtureRoot] })
  const page = await desktopApp.firstWindow({ timeout: 30_000 })
  const platform = await desktopApp.evaluate(() => process.platform)
  const primaryModifier = platform === 'darwin' ? 'Meta' : 'Control'
  const zoomFactor = () => desktopApp.evaluate(({ BrowserWindow }) => (
    BrowserWindow.getAllWindows()[0]?.webContents.getZoomFactor()
  ))
  const waitForFactor = async expected => {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const actual = await zoomFactor()
      if (Math.abs(actual - expected) < 1e-6) return
      await new Promise(resolve => setTimeout(resolve, 20))
    }
    assert.ok(Math.abs((await zoomFactor()) - expected) < 1e-6)
  }
  const pressShortcut = async key => {
    await page.keyboard.down(primaryModifier)
    await page.keyboard.press(key)
    await page.keyboard.up(primaryModifier)
  }

  await pressShortcut('Equal')
  await waitForFactor(1.2)
  await pressShortcut('Minus')
  await waitForFactor(1)
  await pressShortcut('Equal')
  await waitForFactor(1.2)
  await pressShortcut('Digit0')
  await waitForFactor(1)
} finally {
  await desktopApp?.close().catch(() => {})
}
```

Add a source contract assertion that `main.ts` installs the helper with a callback calling `nativeWorkbenchSurfaces.refreshBounds(window)`.

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```bash
npm run test:desktop-zoom
```

Expected: pure contract assertions pass, but the source/integration assertions fail because main-window wiring and `refreshBounds` do not exist.

- [ ] **Step 3: Add main-window wiring and Workbench refresh**

Import the helper in `main.ts`:

```ts
import { installDesktopZoomShortcuts } from './desktop-zoom-shortcuts.js'
```

Immediately after assigning `mainWindow`, install it:

```ts
installDesktopZoomShortcuts(
  window.webContents,
  window.webContents,
  () => nativeWorkbenchSurfaces.refreshBounds(window),
)
```

Expose the focused manager method:

```ts
refreshBounds(owner: BrowserWindow): void {
  this.reapplyActiveBounds(owner)
}
```

- [ ] **Step 4: Run focused tests and Workbench regression tests**

Run:

```bash
npm run test:desktop-zoom
npm run test:desktop-workbench
```

Expected: zoom contract and real Electron shortcut checks pass; native Workbench contract and smoke checks pass.

### Task 3: Verify, review, and publish

**Files:**
- Review all changed files from `upstream/main...HEAD`.

**Interfaces:**
- Consumes: completed Tasks 1–2.
- Produces: a reviewed commit and upstream pull request.

- [ ] **Step 1: Run fresh verification**

Run:

```bash
npm run build
npm run test:desktop-zoom
npm run test:desktop-workbench
git diff --check
```

Expected: every command exits 0 with no test failures or whitespace errors.

- [ ] **Step 2: Review the complete diff**

Compare the changes with this plan, request a focused code review, and resolve all Critical and Important findings.

- [ ] **Step 3: Commit intended files**

Stage only the design, plan, zoom helper, main-process wiring, Workbench refresh, tests, fixture, and package metadata. Commit with:

```bash
git commit -m "Enable desktop keyboard zoom shortcuts"
```

- [ ] **Step 4: Push and open the upstream PR**

Push `agent/desktop-keyboard-zoom` to `origin`, then open a ready-for-review pull request against `opensquilla/opensquilla:main` using head `Liu-RK:agent/desktop-keyboard-zoom`.

- [ ] **Step 5: Monitor CI**

Watch required checks until terminal. If a GitHub Actions check fails, inspect its logs, make the smallest in-scope fix with a regression test where applicable, rerun local verification, commit, push, and monitor again until all required checks pass.
