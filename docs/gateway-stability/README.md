# Gateway connection recovery and evidence

Integration baseline: `c31abea3184938ff6877f4e5b92092138755922b` (`origin/main` verified on 2026-09-21). Historical experiments used `6750223bf72b418a257a2276c18e6edec07ef14f` plus an earlier working-tree candidate. Further frontend and backend review fixes followed those experiments. **The historical figures below do not validate the final implementation or its updated dependencies.**

## Current validation

The final candidate and c31 baseline completed 300 Vue-proxied browser relay trials, plus 210 production-writer application-fixture trials, with 30 trials per cell. Single-wake blackhole recovery improved from baseline p50 47.4431 s to 20.4167 s (p95 20.5288 s, max 20.5612 s). Repeated-wake baseline cases remained unrecovered at the 90-second observation limit; the candidate recovered in every trial. Both versions retained all scripted 13-second pauses. Final writer budgets and writer/close task counts were zero. Source/lock hashes, evidence identifiers, full tables and suite results are in [VALIDATION.zh-CN.md](VALIDATION.zh-CN.md).

The source Windows Electron check exposed and helped fix a Vue proxy identity bug missed by raw-client tests. Its final rerun passed: suspect UI at 15.938 s, successful replacement observed at 20.413 s, seven resume signals in one incident, one replacement, draft and renderer preserved. Physical sleep/remote-network and packaged Windows 30-cycle business gates remain open. The 20-second budget is provisional; controlled results do not establish production latency or a physical Windows wake fix.

## Mechanism

- The first wake signal starts one incident with a fixed deadline for the current connection generation. Further resume/pageshow/online signals cannot extend it. The implementation currently uses a 20-second candidate budget.
- Probe nonce, connection generation and pending-request identity fence recovery evidence. Stale or cancelled responses cannot restore health. Suspect calls fail through the existing transport-error path; retirement reuses the reconnect owner. UI projects suspect/reconnecting instead of advertising a healthy connection.
- Writer cleanup releases owned reservations on serialization failure, send failure and cancellation. Capacity and close fallbacks produce observable reasons. Direct send, recovery credit and queued physical send retain their separate 2/30/60-second resource boundaries. These are not a serial 92-second wait or a target UI response time.

## Reproduction

Use an isolated checkout/profile. Install the checkout's locked WebUI and Python dependencies and Playwright Chromium. The selected baseline Git object must be available. The browser harness loads actual `RpcClient` source; candidate variants change only `WAKE_INCIDENT_BUDGET_MS` to 10/15/20/30 seconds.

```powershell
npm --prefix opensquilla-webui ci
npm --prefix opensquilla-webui exec -- playwright install chromium
uv sync --frozen --extra dev

# Defaults: 30 trials per cell, at most 30 browser contexts.
# Output defaults to a fresh directory under the system temporary directory.
node scripts/gateway_wake_real_clock.mjs

# Pass the printed evidence directory; verification goes to a separate new
# temporary directory unless an EMPTY output directory is supplied second.
node scripts/verify_gateway_wake_real_clock.mjs C:\audit\wake-run

uv run --no-sync python scripts/gateway_writer_real_clock.py --repeats 30 --concurrency 96
```

For a smaller formal browser selection, set `OSQ_WAKE_VARIANTS=candidate20` and `OSQ_WAKE_SCENARIOS=single-wake-blackhole,repeat3-blackhole,repeat14-blackhole,repeat40-blackhole,buffer13-recovery`. Optional `OSQ_CURRENT_SOURCE` chooses a candidate source file; `OSQ_WAKE_EVIDENCE_DIR` chooses an empty output directory. The Python writer accepts `--output EMPTY_DIR`. Existing nonempty output directories are rejected so frozen evidence cannot be overwritten.

`OSQ_WAKE_BASELINE_SHA` selects a different baseline Git commit. The harness resolves and verifies it with `git rev-parse --verify <ref>^{commit}` before creating evidence, and records the full commit in `baselineSha`. The default remains historical `6750223bf72b418a257a2276c18e6edec07ef14f`, with variant name `baseline675`; other baselines use `baseline` plus the first eight commit characters. For the current integration baseline:

```powershell
$env:OSQ_WAKE_BASELINE_SHA = 'c31abea3184938ff6877f4e5b92092138755922b'
$env:OSQ_WAKE_VARIANTS = 'baselinec31abea3,candidate20'
$env:OSQ_WAKE_CLIENT_MODE = 'vue'
node scripts/gateway_wake_real_clock.mjs
```

`OSQ_WAKE_CLIENT_MODE=vue` wraps both variants in the installed Vue `ref()` and calls `notifyResume()` through that proxy, matching the store-owned Desktop resume path. The default raw-client mode dispatches `pageshow`. The report records the mode and Vue bundle hash; do not pool these modes. The harness captures its own hash before the run and reports whether it remained unchanged.

`scripts/summarize_gateway_wake_partial.mjs EVIDENCE_DIR [EMPTY_OUTPUT_DIR]` recovers the historical full five-variant matrix from per-trial files. It marks incomplete cells separately, writes outside the input directory, and cannot infer the process exit code or interruption cause. Retain the original terminal/process record for those facts.

Source Electron checks require a separately built WebUI, Electron and an isolated source Gateway runtime:

```powershell
npm --prefix opensquilla-webui run build:artifact
npm --prefix desktop/electron ci
npm --prefix desktop/electron run build
node desktop/electron/scripts/test-desktop-window-background-flow.mjs --flow-control --idle-send
node desktop/electron/scripts/test-desktop-window-background-flow.mjs --flow-control --wake-blackhole
```

These are manual acceptance tools, not automatically executed by adding them to the repository. Preserve each new run's source snapshots, dependency hashes, timestamps, per-trial records, full aggregate and verification output in a separate archive. Publish a compact manifest with archive SHA256 and retrieval information when sharing results. Raw historical archives and executable source snapshots are intentionally not part of this PR's source tree.

## Historical acceptance summary

| Evidence | Earlier candidate result | Scope |
|---|---|---|
| Browser relay: single wake blackhole, 30 trials/cell | 675 p50 47.3342 s; 20-second candidate p50 20.4860 s, p95 20.6046 s, max 20.6408 s | First successful synthetic echo RPC |
| Repeated wake at 3/14/40 s, 30 trials/cell | Baseline remained unrecovered at 90 s in all trials; candidate recovered in all trials at about 20.4 s | For 40 s, candidate retired before the first repeat |
| Scripted 13 s TCP buffering, 30 trials/cell | 10 s cut all original connections; 15/20/30 s preserved all | Controlled pause, not natural recovery distribution |
| Production writer application fixture, 7 × 30 trials | Direct/recovery/writer close p50 about 2/30/60 s; 5/13/20/30 s cooperative pauses recovered; end-of-run budgets/tasks zero | Event-blocked socket, Gateway privacy log bridge with NullHandler |
| Source Electron, one run each | 65 s background then accepted send in 948 ms; seven emitted resume signals shared one incident and recovered at 20.527 s | Source Windows Electron; renderer and draft retained |

Browser matrix accounting: 480 trials across 16 complete cells, 60 additional replication trials, and four explicitly excluded partial-cell trials from an interrupted run. The baseline's 90 censored trials and the 10-second candidate's 30 early retirements are outcomes, not acceptance passes. The first writer run suffered synchronous rich traceback rendering overhead; it remains archived and is not substituted for the Gateway-logging-bridge result.

The local 13-second test alone supports 15 seconds as the shortest passing candidate; it does not establish a production default. The 20-second choice remains provisional. Healthy wake and UI latency must be considered alongside tolerance for recoverable stalls.

## Evidence boundaries and remaining gates

The browser harness uses real Chromium/native WebSocket, clocks and loopback TCP relay, with a synthetic handshake/pong/echo upstream. It models an old-connection blackhole with healthy replacements, not a physical remote network or the full Python Gateway. The writer harness exercises production `WsConnection`/flow code with a cooperatively cancellable application socket fixture, not kernel backpressure. Parallel samples share a host and are not a population latency estimate.

Source Electron runs emit resume events; they are neither physical sleep nor packaged EXE tests. Browser recovery/Goal/steer/hydration E2E coverage is recorded separately from native acceptance. No result here establishes real Wi-Fi/VPN/NIC behavior, packaged 30-cycle reliability, or exactly-once mutation behavior under physical wake faults. Historical v0.5.4 and 8c7 results are not pooled into this matrix. Final-source controlled runs and the missing native/business gates remain distinct in the PR validation record.
