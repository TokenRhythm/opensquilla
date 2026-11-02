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

To run only the actual main-process exit check with injected incomplete cleanup,
independently of the dialog focus and geometry scenarios, add `--exit-only`.
Use `--themes-only` for the light/dark locale matrix, all custom palettes, and
the live native scheme change after returning to System. The runner puts the
isolated owner in the foreground before each user action; it never fixes a
production modal's focus after opening it.

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
flag, and most application exit boundaries are fixture adapters. The host
stays open after those boundaries so acceptance can reset it. The automated
runner finishes with an actual Electron process exit after injected incomplete
cleanup, and checks that the retained synthetic child is also gone. Ordinary
quit must not show a cleanup failure dialog or ask the user to quit again.
This is not a real Gateway, packaged-runtime, provider or tray test.
`--manual-native-errors` enables native error dialogs for the separate signal
shutdown policy.

**Stop fixture** reaps the retained child and closes only this fixture. The
automated runner also does this in its finalizer. Screenshots and geometry
records are produced from actual confirmation BrowserWindows, not HTML mocks.
The compact dialog is checked in English, Chinese, Japanese, French, German
and Spanish, in both basic themes, plus custom theme colors, long page counts
and dialogs without pages. Dialog colors are compared with the WebUI tokens,
including button hover and keyboard focus treatments.
Empty page warnings are omitted, including their accessibility references.
The runner also checks native visibility, focus, modal ownership, show events,
and placement inside the owner's display. Native bounds and event receipts are
saved alongside the rendered geometry, so a rendered screenshot alone cannot
satisfy the visibility checks.

The repeated-menu cancellation case injects a lost click acknowledgement only
after a real button click. Its result explicitly records
`lateClickAckFaultInjected`; this tests the exception branch, not a naturally
observed macOS protocol race. A target-closed click error is accepted only with
a fresh, matching decision IPC receipt from that exact dialog, while the host
remains alive. All cancellation and retained-draft assertions still run.
