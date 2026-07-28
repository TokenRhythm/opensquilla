# Desktop Keyboard Zoom Design

## Goal

Make the OpenSquilla desktop Control UI respond consistently to:

- `Command/Ctrl` + `+` (or the unshifted `=` key) to zoom in.
- `Command/Ctrl` + `-` to zoom out.
- `Command/Ctrl` + `0` to reset to 100%.

The behavior must work on macOS, Windows, and Linux without enabling pinch-to-zoom or adding a visible application menu.

## Architecture

Add a focused desktop-main-process module that maps Electron keyboard input to a zoom command, computes a bounded zoom factor, and installs a `before-input-event` listener on the main Control UI `webContents`. `main.ts` installs that listener immediately after creating the main window.

The zoom target is the main Control UI only. The handler uses `Command` on macOS and `Ctrl` on other platforms, ignores combinations containing `Alt`, accepts both the main keyboard and numpad forms, and prevents the handled key event from reaching the renderer.

## Zoom Behavior

Zoom changes by Chromium's documented 1.2 factor per step. The result is clamped to 0.5–3.0, and reset always sets the exact factor to 1.0. Zoom state remains scoped to the running Electron session; no preference or disk persistence is added.

The existing menu roles remain unchanged. Explicit input handling is used so shortcuts also work on Windows and Linux, where OpenSquilla intentionally removes the native application menu.

## Native Workbench Coordination

Programmatic `setZoomFactor` calls are not treated as an implicit geometry notification. After a handled command changes or resets zoom, `main.ts` asks `NativeWorkbenchSurfaceManager` to reapply the active native surface bounds for the owner window. The existing CSS-pixel-to-DIP conversion continues to use the owner's current zoom factor.

## Testing

Add:

- Pure contract coverage for modifier selection, supported key variants, ignored inputs, reset behavior, step behavior, and 0.5–3.0 clamping.
- A real Electron smoke fixture that sends the platform-appropriate keyboard shortcuts and inspects the main window's actual `webContents` zoom factor.
- A package script for the focused zoom test.

Run the focused zoom test, the existing native Workbench tests, and the desktop TypeScript build before publishing.

## Non-Goals

- Trackpad or touchscreen pinch-to-zoom.
- A renderer/Vue keyboard listener.
- A zoom percentage control in the UI.
- Persisting zoom across app restarts.
- Zooming the onboarding window or artifact preview contents independently.
