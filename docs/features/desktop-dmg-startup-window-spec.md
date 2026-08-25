# Desktop DMG Startup Window Recovery Spec

## Background

Users reported two macOS DMG installation issues:

1. After installing the latest DMG, the first launch shows no window.
2. In an earlier DMG, closing the client window with the red close button leaves the OpenSquilla app running in the Dock, but clicking the Dock icon does not reopen any window.

This spec covers the desktop startup and packaging fix. It is separate from the gateway/yoyo migration lock recovery spec, although both can surface during first-run gateway startup.

## Evidence

Local packaged logs showed the bundled gateway crashing during first launch:

```text
AttributeError: '_AsyncConnection' object has no attribute 'create_function'
```

The crash path is:

```text
opensquilla/session/storage.py -> SessionStorage.connect()
```

The packaged runtime under the built app was missing `create_function` in `opensquilla.compat.aiosqlite._AsyncConnection`, while current source has the compatibility method.

The packaged Electron app also still had this startup order:

```ts
const gateway = await startGateway()
const window = await createMainWindow()
```

That means any gateway crash or long wait can prevent the first visible desktop window from being created.

For the Dock behavior, macOS intentionally keeps the application process alive after all windows are closed. The existing behavior is acceptable only if `activate` reliably recreates or focuses the main window and reuses the already-running gateway instead of trying to perform a full duplicate startup.

## Goals

- Show a desktop window immediately on launch and Dock activation, before gateway startup can block or fail.
- Surface gateway startup failure inside the desktop UI instead of leaving the user with no visible window.
- Ensure the packaged gateway runtime contains the aiosqlite compatibility API needed by session storage.
- Reuse an already-owned, healthy gateway on Dock activation instead of spawning a duplicate process.
- Prevent duplicate gateway startup while a startup attempt is already in progress.
- Make the release validation catch stale packaged runtime contents before distributing a DMG.

## Non-Goals

- Do not change macOS close-button semantics by default. Closing the last window may keep the app running unless product decides to switch to quit-on-close.
- Do not disable yoyo migration locking.
- Do not change gateway state directory, auth token, or persistence layout.
- Do not treat a previously built DMG as fixed unless it is rebuilt from the fixed source and revalidated.
- Do not treat unsigned local validation as equivalent to a distributable release validation.

## Root Causes

### 1. Packaged Gateway Runtime Was Stale

The built app included a runtime copy of `opensquilla.compat.aiosqlite` without `_AsyncConnection.create_function`. Source had moved forward, but the packaged runtime did not contain that fix.

When session storage calls `create_function`, the packaged gateway crashes before becoming healthy.

### 2. Electron Created the Window After Gateway Startup

The app waited for `startGateway()` before calling `createMainWindow()`. If gateway startup crashes, hangs, or waits on a lock, the desktop app can remain running without any visible window.

### 3. macOS Activation Path Did Not Reliably Resume UI

On macOS, `window-all-closed` does not quit the app. After the red close button, the Dock dot remains because the app process and gateway may still be alive.

Clicking the Dock icon must focus an existing window or create a new one and attach it to the existing gateway state. If activation re-enters a full startup path, it can wait unnecessarily, hit duplicate startup guards, or fail silently.

## Design

### Electron Startup

`bootDesktopApp()` must create the main window before gateway startup:

```ts
const window = await createMainWindow()
const gateway = await startGateway()
```

The initial window loads the local Desktop renderer. Gateway startup runs behind it and publishes a revisioned connection descriptor to the trusted main frame, including an instance-scoped token for Desktop-owned runtimes. The renderer connects its RPC client only after `/readyz` succeeds. If Gateway startup fails, the same local renderer stays visible with log and retry actions; it is never replaced by a dead Gateway origin.

### Idempotent Gateway Startup

`startGateway()` should become explicitly idempotent:

- If an owned gateway process is already healthy, return the existing `gatewayState`.
- If startup is already in progress, do not spawn another gateway. Focus or keep the local renderer visible.
- If the owned process exited, clear stale owned state before starting a new process.
- If a gateway URL is present, verify `/readyz` before deciding to reuse it.

This prevents Dock activation, second-instance activation, and retry actions from racing into duplicate gateway processes.

### Dock Activation

The `activate` handler should call a single helper that:

1. Focuses an existing non-destroyed main window, or creates a new local renderer window.
2. If `gatewayState.status === "ready"` and `/readyz` passes, republishes the current descriptor without navigating.
3. If startup is in progress, keeps the local renderer visible.
4. Otherwise starts Gateway once and publishes either a ready descriptor or a recoverable error state.

The same helper should be used by `second-instance` handling so a second launch focuses the original instance and does not start another gateway against the same state directory.

### Window Close Semantics

The macOS red close button can keep the process alive, but the implementation must clear stale window references on `closed` and make activation recreate the window.

If product later chooses quit-on-close, that should be a separate product decision. It changes expected macOS app behavior and gateway lifetime, so it should not be bundled into this bug fix.

### Gateway Runtime Compatibility

The compatibility layer must provide `create_function` on both the protocol and wrapper implementation:

```py
class Connection(Protocol):
    async def create_function(...)

class _AsyncConnection:
    async def create_function(...)
```

The wrapper should delegate to the underlying sqlite connection in the same thread-safe style as the other compatibility methods.

### Packaging Guard

Before building a distributable DMG:

1. Rebuild the gateway runtime from current source.
2. Fail the release if `desktop/electron/runtime/gateway` is empty or stale.
3. Build the Electron app from current source.
4. Inspect the packaged runtime and assert `create_function` exists.
5. Inspect packaged `app.asar` and assert the main window is created before gateway startup.
6. Sign, notarize, staple, and validate the final DMG.

The previously generated DMG in `dist/desktop-electron` should be treated as stale for this bug because it was built before the latest desktop/runtime fixes.

### Unsigned Functional Testing

It is acceptable to build an unsigned or ad-hoc-signed DMG first for fast local functional testing. This phase should verify application behavior only:

- first launch shows a window immediately;
- advanced configuration can start the gateway;
- closing the window with the red close button and clicking the Dock icon reopens the UI;
- no duplicate gateway process is created;
- logs do not contain the `create_function` crash or duplicate lock failures.

Code signing and notarization do not rewrite Electron or Python business logic, so they should not change the intended desktop/gateway behavior. However, they can change the effective macOS runtime environment through Gatekeeper, quarantine handling, Hardened Runtime, entitlements, nested executable signing, dynamic library loading, and child process launch policy.

Therefore, unsigned validation is only a pre-release smoke test. The final artifact sent to external users must still be rebuilt, signed, notarized, stapled, and validated end to end.

The final release must not sign only the outer `.dmg`. The `.app` bundle and nested executables must be signed before creating/signing/notarizing the DMG.

## Validation Plan

### Automated

- Run gateway compatibility tests covering `_AsyncConnection.create_function`.
- Run gateway startup/lock tests to ensure duplicate startup and stale yoyo lock handling still work.
- Add or keep an Electron-side regression check for startup order, ideally asserting `createMainWindow()` runs before `startGateway()` in the desktop boot path.
- Add an Electron-side test or factored helper test for activation reuse:
  - ready gateway state is reused;
  - startup-in-progress does not spawn a second gateway;
  - exited owned process is cleared before restart.

### Manual DMG Smoke Test

For the first local pass, this test may use an unsigned or ad-hoc-signed DMG. For external distribution, repeat the same test with a freshly built, signed, notarized, and stapled DMG.

1. Remove or move aside existing user data for a clean first-run test.
2. Install the app from the DMG.
3. Launch from `/Applications`; a window must appear immediately.
4. Complete or enter advanced configuration; the gateway should start and the UI should load.
5. Close the window with the red close button.
6. Confirm the Dock dot remains.
7. Click the Dock icon; the window must reopen and load the existing gateway UI.
8. Confirm only one `opensquilla-gateway` process is running.
9. Confirm logs do not contain the `create_function` AttributeError.
10. Confirm logs do not show duplicate gateway state-dir lock failures during Dock activation.

### Release Validation

Release validation applies only to the final signed and notarized artifact.

Run these checks on the final artifact:

```bash
spctl --assess --type open --context context:primary-signature -v dist/desktop-electron/OpenSquilla-*.dmg
spctl --assess --type execute -v dist/desktop-electron/mac-arm64/OpenSquilla.app
xcrun stapler validate dist/desktop-electron/OpenSquilla-*.dmg
hdiutil verify dist/desktop-electron/OpenSquilla-*.dmg
```

Also mount the DMG and inspect the installation window layout.

## Acceptance Criteria

- First launch from a clean DMG install always shows a desktop window before gateway health checks finish.
- Gateway startup failures are visible in the app window with actionable error UI.
- Packaged gateway runtime contains `_AsyncConnection.create_function`.
- Dock activation after red-window-close reopens the UI and reuses the existing healthy gateway.
- Second launch/focus behavior does not spawn a duplicate gateway process.
- Final DMG is signed, notarized, stapled, and passes Gatekeeper validation.
