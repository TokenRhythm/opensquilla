# Native quit acceptance fixture

Build `desktop/electron` with its pinned dependencies first. The fixture reads the
resulting `dist/main.js` and executes its actual close, quit, confirmation,
failure and translation functions. Confirmation windows use the production
preload and HTML builder; URL pages use the production
`NativeWorkbenchSurfaceManager` and real Chromium `beforeunload` handling.

From `desktop/electron`:

```powershell
node scripts/test-desktop-quit-native-acceptance.mjs --artifacts=C:\absolute\isolated\output
```

For manual native interaction, including the actual OS cleanup error box:

```powershell
node scripts/test-desktop-quit-native-acceptance.mjs --manual --manual-native-errors --artifacts=C:\absolute\isolated\output
```

If the pinned Electron executable is provided separately, set
`OPENSQUILLA_ACCEPTANCE_ELECTRON` to that executable's absolute path. The runner
creates a new profile below the output directory. It never uses the user's
OpenSquilla profile. The controller is titled **OpenSquilla PR1793 Acceptance**.
Its **Reveal app** and **Reset synthetic session** controls restore the visible
fixture after backgrounding or an accepted simulated exit.

Without `--artifacts`, CI runs save evidence under
`CI_REPORT_DIR/desktop-quit-native-acceptance` when that variable is set.
Windows and macOS exercise their real background/Ask close policy. Linux
exercises its real X-to-Quit policy and the common quit, page, task, keyboard,
writer and cleanup cases. The report records the actual platform and explains
which background/Ask choices are unavailable on Linux; those choices are never
reported as successful Linux tests.

The ticking task is an actual child process created and retained by this
fixture. Gateway activity, Gateway cleanup results, preferences, the tray-ready
flag, and the application's final exit boundary are fixture adapters. The host
stays open after that boundary so manual acceptance can reset it. This is not a
real Gateway, packaged-runtime, provider or tray test. Automated cleanup failure
records the production error-box copy without opening an OS modal;
`--manual-native-errors` enables the real OS modal for manual verification.

**Stop fixture** reaps the retained child and closes only this fixture. The
automated runner also does this in its finalizer. Screenshots and geometry
records are produced from actual confirmation BrowserWindows, not HTML mocks.
The runner also checks native visibility, focus, modal ownership, show events,
and placement inside the owner's display. Native bounds and event receipts are
saved alongside the rendered geometry, so a rendered screenshot alone cannot
satisfy the visibility checks.
