# Packaged session-title startup comparison

This harness launches two actual packaged Electron clients and their bundled
frozen Gateways. It uses generated SQLite data, an unavailable loopback provider,
isolated profiles and environment directories, and the production startup and
60-second maintenance timers. It does not install either client or read a real
session database.

Build the candidate fully before testing. Record the baseline revision, candidate
changes, dependency lockfiles and build inputs separately; executable hashes alone
do not establish a fair comparison. The harness records hashes for Electron,
`app.asar`, the frozen Gateway and `gateway-build.json`.

Run from `desktop/electron` on native Windows with Node, Playwright, PowerShell 7
and a Python environment containing the repository's dependencies. On Windows,
Codex's restricted shell can block Python asyncio and Electron even for local
fixtures; use the approved native execution context. Stop other builds and
benchmarks before collecting timings.

Before formal timing, run a separate diagnostic pair for each fixture mix with
both executables. This exercises the freshly built packages and supplies
independent work counts; those diagnostic launches never enter timing results.
Formal timing checks common Python/Node build workers and native archive/package
workers (7-Zip, NSIS and app-builder) before each launch and
after its startup boundaries. A detected build fails the entire timing run.
These read-only checks run outside the measured interval and do not terminate
other tasks. They do not establish that all background host load is absent.

```powershell
node scripts/test-packaged-session-title-startup.mjs `
  --baseline-executable 'C:\path\baseline\OpenSquilla.exe' `
  --candidate-executable 'C:\path\candidate\OpenSquilla.exe' `
  --python 'C:\path\venv\Scripts\python.exe' `
  --workdir 'C:\short\title-ab-01' `
  --output 'C:\short\title-ab-01.json' `
  --pairs 6
```

The work directory and output file must be new. Prefer a short path to avoid
mixing Windows path-length failures into the benchmark. The script never removes
previous evidence. Each pair alternates A/B and B/A ordering, and each variant
gets its own identical database copy.

## Fixture and measurement scope

The default fixture has 50 sessions, each with `display_name = WebChat`, a valid
`derived_title`, and three small user messages. It uses the **current schema with
NULL `content_byte_length`**, representing the title-relevant state after a
historical database's additive migration. It is not a full historical schema
migration or a sanitized copy of a real user database. Configuration version 2
and a valid layout receipt keep unrelated configuration/layout migration out of
the intended scenario.

Each profile is launched twice:

1. `legacy-first`: a fresh process sees pending legacy content-length metadata.
2. `ready-restart`: after natural process exit, the fixture tool completes only
   the content-length metadata offline; the same profile then starts again.

The second case measures a normally indexed restart without spending 60 seconds
on every timing sample. It does not establish that live maintenance completed.
Both cases use fresh processes with uncontrolled, normally warm OS caches.
There is no reboot or cache purge, so these results are **not machine-cold-cache
measurements**.

Passive WebSocket observers record list request/response counts and title
correctness. A DOM observer records renderer connection, connected-and-editable composer,
first visible fixture title and first correct fixture title. The first visible
title is captured once. When Desktop preloads the document before Playwright's
launch handshake returns, the observer may attach to that document only before
connection and fixture-title visibility; otherwise the
sample fails. No reload is used to repair an incomplete observation.
The composer metric requires a connected Gateway as well as an enabled input.
It does not measure the earlier offline landing page's editable input.
The desktop's own `gateway_ready` log supplies the
Gateway readiness boundary. Startup profile-inspection/helper events, logs,
screenshots and process ownership/normal shutdown evidence are retained.
`desktopReadyElapsedMs` uses the desktop main process's own time origin; it is
not a Python Gateway internal startup measurement.

The baseline's first legacy response may contain `WebChat`; the candidate must
immediately return the saved titles. Startup list completion and correct-title
availability are separate measurements. Ordinary timing samples do not wait for
the baseline's delayed repair. When the first fixture has a saved title, the
candidate's first visible sidebar title must already be correct. Both variants'
indexed restarts must also render the correct title on the first visible frame.

## Live maintenance and refresh contract

Use a separate run to exercise the unchanged real 60-second delay:

```powershell
node scripts/test-packaged-session-title-startup.mjs `
  --baseline-executable 'C:\path\baseline\OpenSquilla.exe' `
  --candidate-executable 'C:\path\candidate\OpenSquilla.exe' `
  --python 'C:\path\venv\Scripts\python.exe' `
  --workdir 'C:\short\title-maintenance-01' `
  --output 'C:\short\title-maintenance-01.json' `
  --pairs 0 --verify-backfill
```

This uses five saved-title and five genuinely untitled sessions. Each variant's
first launch waits through live maintenance; the next launch reuses the actual
maintenance result and observes the no-pending-work case. The candidate must
emit one directory change and cause one list refresh after changed rows, then
emit zero changes and cause zero maintenance refreshes on the second launch.
All visible fixture titles must be correct after maintenance, including at
least one genuinely untitled fixture; an RPC-only repair cannot pass this gate.
No focus change or manual reload is used. No timer is shortened. Four launches
take several minutes.

## Independent work-count evidence

Read-path diagnostics add logging overhead. Enable them only for a separate
functional comparison:

```powershell
node scripts/test-packaged-session-title-startup.mjs `
  --baseline-executable 'C:\path\baseline\OpenSquilla.exe' `
  --candidate-executable 'C:\path\candidate\OpenSquilla.exe' `
  --python 'C:\path\venv\Scripts\python.exe' `
  --workdir 'C:\short\title-counts-01' `
  --output 'C:\short\title-counts-01.json' `
  --pairs 1 --read-diagnostics --named 45 --unnamed 5
```

Use `--named 50 --unnamed 0`, `--named 45 --unnamed 5` and
`--named 0 --unnamed 50` for all-named, mixed and all-untitled comparisons.
The total must be between 1 and 50. Existing production diagnostics supply
title-candidate SELECT count, title-content SELECT count and returned body rows.
The baseline must contain candidate-query diagnostics; an absent or incorrectly
parsed log cannot count as zero-read optimization evidence.

Diagnostic reports have `timingEligible: false`, no performance summary and no
startup performance gate. They establish reduced work, not faster wall time.

## Interpreting the result

The default observed regression allowance is the larger of **250 ms or 5%** of
the corresponding baseline percentile, checked separately for p50 and p95.
Set these limits before a run using `--max-regression-ms` and
`--max-regression-ratio`; do not loosen them afterward to make a result pass.
Percentiles use nearest rank, and raw per-boot observations remain in the report.
With six samples per variant and phase, p95 is the largest observed sample,
not a reliable estimate of the long-run tail.

This finite-sample noise allowance is not an absolute guarantee of zero
regression on all machines, profiles or cache states. Report the raw comparisons,
sample count, fixture limits and any failed gate. Reduced SELECT/body-read counts
are an independent positive result even when startup timing differences are
inside the noise allowance.

## Isolation and cleanup

Every profile has isolated `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`,
temporary directories and OpenSquilla user state. Provider secrets and inherited
OpenSquilla overrides are scrubbed. Automatic updates and network observability
are disabled. Proxy environment variables point to an unavailable local proxy;
this is not a network sandbox, and Gateway clients may ignore those variables.

Packaged Electron automatically registers its `opensquilla://` protocol.
Environment-directory isolation does not isolate HKCU. The guard snapshots the
exact `HKCU\Software\Classes\opensquilla` subtree before launch, restores it after
startup timing is captured, and verifies it again after shutdown and at the end.
It restores only when the current command belongs to one of the two explicitly
supplied test executables; an unexpected concurrent change causes failure.
Before/after snapshots preserve value names, kinds and data.

The harness closes its own Electron application and verifies its Gateway
ownership identity, normal Gateway exit and ownership-record removal. It does
not terminate unrelated clients or processes. Logs and synthetic profiles remain
available for inspection.
