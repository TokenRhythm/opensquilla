# CI optimization operations

The default `CI` workflow keeps the required check name `CI result` while assigning
different responsibilities to pull requests, merge queue entries, `main` pushes, and
the nightly run.

| Stage | Target | Responsibility |
| --- | --- | --- |
| Pull request | 8–15 minutes for ordinary changes | Run always-on contracts plus suites selected by the canonical planner. Registered Python, WebUI, Electron, and TUI dependency inputs receive ecosystem-specific coverage; unknown manifests and merge-critical CI policy inputs fail closed to the full matrix. |
| Merge queue | 1–3 minutes when evidence is reusable | Verify exact-tree, base, workflow-run, PR association, and policy attestations. Any mismatch runs the full queue matrix. |
| `main` push | 3–8 minutes in `enforce` | Build the WebUI and wheel, install in a clean environment, import the gateway, run the CLI, and exercise an offline provider/gateway canary. |
| Nightly | Full matrix | Exercise all supported test shards and platform contracts without change-based selection. |

Times are operational targets, not timeouts. High-risk pull requests intentionally remain
slower. Provider-live, credential, and external-network tests remain excluded from required
CI and retain their existing live markers.

## Parallel-runner timing experiment

The `integration/ci-parallel-timing` experiment keeps every existing check and
uses the same product revision as its baseline. Both branches run a complete
`workflow_dispatch` matrix. Failures remain failures and are recorded beside
execution time; there are no new assertion retries, skips, or relaxed gates.
A failed run that executes less work is not accepted as evidence of a speedup.

Ubuntu and Windows offline tests each use 20 physical partitions, preserving
responsibility families, marker filters, `ci_serial`, and per-runner worker
limits. Frontend type checks, four unit shards, and each Contract profile run
independently. Browser recovery uses four ordinary shards and separate clipboard,
virtualization and capacity runners. Desktop native/frozen/Docker probes run
independently of the Electron recovery matrix. Managed toolchain components
also use separate native VMs.

The NSIS candidate consumes the verified shared WebUI artifact; an independent
Windows job retains the complete native frontend build. Candidate identities are
recorded before consumers begin. Packaged dependency auditing, frozen Gateway
smoke and first interaction install and verify that exact installer on another
runner, concurrently with all upgrade cases and candidate probes. The final
acceptance requires every result. Gateway input hashing and freeze ordering are
unchanged.

Save each run API response as `run.json` and every jobs API page as `jobs.json`
(a JSON array of pages when paginated), then compare trigger-to-final-job time,
parallelism, candidate availability and failures with:

```sh
python .github/scripts/compare_ci_timings.py BASELINE_DIR CANDIDATE_DIR --output-dir OUTPUT_DIR
```

### First full-run observation (2026-10-17)

Baseline [38952186289](https://github.com/TokenRhythm/opensquilla/actions/runs/38952186289)
uses `3d6b8d01290674448406ada1303d98d98212cc9e`; candidate
[38953206278](https://github.com/TokenRhythm/opensquilla/actions/runs/38953206278)
uses `471280f59`. Both are first-attempt `workflow_dispatch` runs with the full
NSIS profile, unchanged application/dependency inputs and the same offline
markers. The dispatches started at 03:04:28 and 03:12:36 on October 17 in
Asia/Shanghai. Wall time is trigger to the final completed job, including setup
and aggregation. Suite finish times below use the same trigger-relative clock.

| Metric | Baseline | Parallel candidate |
| --- | ---: | ---: |
| Complete CI wall time | 24:32 | 17:45 |
| Peak / mean running jobs | 41 / 15.2 | 93 / 25.5 |
| Executed jobs | 69 | 128 |
| Summed runner time | 372:01 | 452:50 |
| NSIS candidate available | 15:12 | 9:20 |
| First NSIS acceptance starts | 15:14 | 9:22 |
| Last NSIS acceptance finishes | 24:15 | 17:23 |
| Ubuntu offline suite finishes | 11:54 | 6:06 |
| Windows offline suite finishes | 18:05 | 12:35 |
| Browser recovery/capacity finishes | 12:04 | 4:53 |

Observed wall time fell by **407 seconds (27.6%)**. Candidate availability moved
forward by 352 seconds. The retained baseline audit/first-send phase occupied
192 seconds on the candidate-production path; the new installed security
consumer completed successfully alongside upgrade tests. All NSIS jobs passed:
22 baseline and 25 candidate, including aggregation. Summed runner time rose
21.7%, reflecting the extra independent runner setup and duplicated preflights.

Both workflow conclusions are **failure**: baseline has one failed Python shard
plus aggregation; candidate has eight failed Python shards plus aggregation.
All nine failed Python shard logs contain completed parallel and serial phase
summaries; none ended at installation/preflight/collection or used dispatch
bulk `--maxfail`. Baseline failed a Unicode message-search assertion. Candidate
failures include stale CI structure assertions, a signing-cleanup test timeout,
and a compaction-summary assertion. No failure was hidden or rerun to replace
these samples. Normal dispatch-only skips are queue attestation and main canary.

Follow-up test-only changes update the moved smoke/Contract owners and NSIS cell
counts, and retain all 1,023 legacy partition-plan combinations in addition to
new boundaries (1,199 unique plans). These changes were locally verified after
the frozen timing run; they are not represented as a second remote measurement.
This is one paired observation, not an estimate of stable CI latency. Runner
images, hardware and cache contents were not experimentally fixed, and the
slower installation cases remain the final critical path.

### Overlapping baseline installation and independent startup checks

Upgrade VMs now begin alongside the NSIS producer. Each audit installs and
verifies the pinned old release, retained synthetic profile and registry state
on its own VM, then waits for the candidate without acquiring the fault lock.
The wait binds the artifact archive digest, installer and manifest to the
checked-out source, workflow, run and successful producer attempt. A failed-only
rerun may reuse its exact earlier successful producer; a failed current producer
cannot fall back. Producer failure fails the audit. The wait retains the existing
60-minute producer budget; the consumer job contains that wait plus its original
45-minute acceptance budget. Installer and interaction deadlines are unchanged.

Gateway and Desktop startup compatibility run on independent VMs. Both original
seven-configuration, two-start harnesses are unchanged. Each consumer retains
installation, candidate identity, optional signing verification and uninstallation.
Full/smoke evidence now requires 25/15 cells; omitting either startup cell fails
attestation. Fresh installs and all fourteen full-profile upgrade cases remain.

Runner durations now include explicit waiting for the candidate. Compare total
wall time and the interval from candidate availability to final acceptance, not
upgrade job duration alone. Remote timing for this second experiment is recorded
after the first complete run, including every failure.

### Second full-run observation (2026-10-17)

[38957580252](https://github.com/TokenRhythm/opensquilla/actions/runs/38957580252)
ran `8706650addedead88e83da4f3cd085c33f8a9906`, first-attempt full
`workflow_dispatch`, beginning at 03:48:07 Asia/Shanghai. Compare it with the
17:45 sample above, not a successful-only subset. Application source and locked
dependencies are unchanged. Both workflows concluded **failure**.

| Metric | First parallel run | Second parallel run |
| --- | ---: | ---: |
| Complete CI wall time | 17:45 | 17:30 |
| Executed / skipped jobs | 128 / 2 | 129 / 2 |
| Peak / mean running jobs | 93 / 25.5 | 109 / 30.2 |
| Summed runner time | 452:50 | 527:58 |
| NSIS candidate available | 9:20 | 9:08 |
| All 14 upgrade/fresh cases finish | 17:23 | 12:37 |
| Startup compatibility group finishes | 15:56 | 14:16 |
| NSIS aggregate finishes | 17:30 | 15:10 |
| Windows desktop-installer-contracts-1 finishes | 12:35 | 17:17 |

Total wall time improved by only **15 seconds (1.4%)**. The NSIS chain improved
by **2:20**, but the final critical path moved to Windows
`desktop-installer-contracts-1`. Its job grew from 11:48 to 16:42, although both
runs completed the same 1,569 parallel passes/14 skips and two serial passes.
The parallel phase grew from 549.95 to 820.23 seconds; serial time decreased
from 68.77 to 65.20 seconds. The slowest durations concentrate in
`tests/test_observability/test_bundle.py`; for example the `x-csrftoken` case
grew from 7.99 to 57.28 seconds. This identifies the observed slowdown, not its
root cause. Do not attribute it to the scheduling change or runner noise without
isolating it.

Both logs contain exactly the same 1,583 parallel `(result, nodeid)` pairs. All
66 bundle cases run sequentially on worker `gw0`; their first-start to last-finish
span grew from 526.03 to 799.47 seconds, accounting for the parallel-phase
increase. The bundle test, implementation, dependency manifests, partition map
and runner script have no changes between these samples. This is a concrete
next partitioning candidate, while the reason for the per-case slowdown remains
unverified.

All **26 NSIS jobs passed**. Logs show all 12 old installations completed at
2:43.5–4:42.7 while the candidate was still building; the producer itself took
7:53 versus 7:52 previously. All 14 consumers verified the same artifact and
producer attempt, then completed every expected phase and final `ok: true`.
All four readlock cells exercised real exit 2, same-candidate recovery and
retained interaction; both fresh cells completed first interaction. No timeout
or unexpected exit supplied the timing gain. Startup jobs took 3:05 (Gateway)
and 5:06 (Desktop), each retaining all seven configurations and two starts.
The new NSIS tail is packaged-security, which passed in 5:54 and finished at
15:05, followed by the aggregate at 15:10.

Summed runner occupancy increased **16.6%**. The early acceptance VMs spent a
combined **83:48** waiting for the candidate, which remains included in cost.
Old installation durations also varied between samples, so the entire upgrade
completion improvement cannot be attributed exclusively to overlap.

The three failed test jobs all completed parallel and serial phases: Ubuntu
and Windows desktop-installer-contracts-4 retain one stale artifact-download
count assertion each; Windows gateway-sqlite-5 has telemetry profile-writer
and SQLite-backend assertions. CI result correctly failed as the fourth failed
job. None was rerun or suppressed. The stale count was corrected and locally
retested after the frozen remote sample; that test-only correction is not a
new remote measurement. Local regression recorded 565 passes and 17 skips,
plus this corrected assertion's passing focused rerun; planner and candidate
identity/phase-order checks also passed.

Saved `round2-comparison/comparison.json`/`.md`, both paginated API snapshots,
and `round2/nsis-stage-report.json` under `.cache/ci-timing` preserve the raw
wall-time and per-case phase evidence.

### Third experiment: audited bundle case scheduling

The second run's longest successful job was Windows
`desktop-installer-contracts-1` (16:42, finishing at +17:17). Its 66
`tests/test_observability/test_bundle.py` cases occupied one `loadfile` worker
for 13:19, versus 8:46 in the first experiment. The source and case inventory
were unchanged; the reason for the per-case runtime variation is not established.

The bundle isolation audit found function-scoped synthetic config, home,
SQLite database and ZIP paths. Doctor collection uses a unique temporary
config copy, migration locks are keyed by database path, and the parent pytest
configuration already scopes runtime roots by run and worker. The tests that
patch `json.dumps`, doctor collection, or migration helpers must use separate
processes rather than concurrent threads. All real collectors, migrations,
assertions, parameters and the original 66 node IDs remain unchanged.

The shard runner explicitly loads an xdist scheduler that splits only this
audited file into individual case scopes. Every other file retains `loadfile`
grouping. Windows desktop still uses two workers and Ubuntu desktop four;
physical job counts, `ci_serial`, marker filters and failure gates are unchanged.
Run metadata records the selected `item_parallel_files`, and duration provenance
rejects pooling observations with different scheduling policies. Behavioral
contracts verify overlapping cases, whole-file grouping elsewhere, exact-once
execution and preservation of the serial phase after an injected failure.

The comparison baseline is the complete first attempt of run `38957580252`
(17:30). The candidate is the complete first attempt of
[run 39011651281](https://github.com/TokenRhythm/opensquilla/actions/runs/39011651281),
testing `067a6c66920244024bdab66028f0a2b48ab3d9d4` with `workflow_dispatch`.
It started at 2026-10-17 06:54:40 UTC and its last job completed at 07:11:27 UTC.

| Measurement | Second experiment | Third experiment |
| --- | ---: | ---: |
| Complete CI wall time | 17:30 | 16:47 |
| Executed / skipped jobs | 129 / 2 | 129 / 2 |
| Executed job conclusions | 125 success, 4 failure | 129 success |
| Peak running jobs | 109 | 105 |
| Summed runner time | 527:58 | 512:21 |
| Windows desktop1 job duration | 16:42 | 16:11 |
| Windows desktop1 parallel pytest phase | 820.23 s | 827.41 s |
| Windows desktop1 serial pytest phase | 65.20 s | 62.58 s |
| Windows bundle logged execution interval | 799.47 s | 684.76 s |
| Windows bundle cases per worker | 66 / 0 | 34 / 32 |
| Windows bundle cumulative JUnit case time | 797.205 s | 1345.202 s |
| Windows bundle median case time | 10.1275 s | 21.42 s |
| Ubuntu desktop1 parallel pytest phase | 73.11 s | 57.93 s |
| Ubuntu bundle logged execution interval | 19.59 s | 8.41 s |
| NSIS candidate ready | +9:08 | +9:00 |
| NSIS acceptance aggregate completes | +15:10 | +14:15 |

Both platform logs retain exactly the same 1,583 parallel and two serial
node IDs and results as their respective baselines, including all 66 passing
bundle cases with no duplicates. Windows has 1,569 passed / 14 skipped in its
parallel phase; Ubuntu has 1,576 passed / seven skipped. The two serial cases
pass on both platforms. The full workflow job-name multiset is also identical,
and all 26 NSIS jobs pass. The candidate includes the already recorded
`f16731a68` stale artifact-count contract fix; the all-green conclusion is not
evidence that the scheduler fixed prior failures.

**The observed 43-second (4.1%) workflow improvement does not establish a
Windows pytest speedup.** The critical job's API test step grows from 887 to
890 seconds. Preparation before that step falls from 109 to 74 seconds, the
job starts ten seconds earlier, cleanup adds one second, and the final result
gate saves two seconds. Those differences account for the overall reduction.
The unchanged setup steps, especially Node setup (26 to eight seconds), explain
most of the shorter job; they were not optimized by this commit.

The scheduler does distribute bundle cases, but xdist's existing largest-scope
ordering places the new singleton scopes behind larger files. The first bundle
case starts 22.33 seconds after the old test step begins versus 143.36 seconds
in the candidate. Its execution interval shrinks by 114.71 seconds, while that
later start and increased per-case time absorb the benefit. Cumulative case
time includes overlapping execution and must not be interpreted as wall time.
These two runs do not distinguish same-VM resource contention from other runner
variation; no CPU/disk profile was captured. Independent Windows runner shards
are the next useful controlled comparison, with exact-once case ownership and
the same real collectors and assertions retained.

Cached API snapshots, logs, JUnit artifacts and generated comparisons are under
`.cache/ci-timing/round3/` and `.cache/ci-timing/round3-comparison/`. The experiment
remains on its integration branch and is not merged into `main`.

### Fourth experiment: four independent Windows bundle runners

The 66 audited bundle cases now run on four independent `windows-latest` jobs,
directly after planning, with one serial pytest process per runner. Sorting the
complete nodeid inventory and selecting ordinal modulo four produces partitions
of 17 / 17 / 16 / 16. The original Windows owner delegates only this file when
the plan contains all four dedicated execution cells. Ubuntu retains its
existing execution policy. The test file, collectors and assertions are unchanged.

Every partition records the complete inventory, selected cases, pytest starts,
setup/call/teardown outcomes, JUnit nodeids and source/run/attempt identity. A
required coverage job verifies matching inventories and exactly-once execution
of all 66 cases; missing reports, duplicate execution, skips and interrupted
execution fail this check. Test failures retain their failing exit status and
are independently required by `CI result`. The planner, attestation matrix and
Windows duration collector include the four new execution cells. Duration
collection preserves the original logical file owner and validates all four
runner reports before accepting observations.

Reports are bound to one attempt. Use a full rerun when repeating this
experiment: rerunning only failed jobs cannot reuse successful siblings'
earlier-attempt reports and will fail the coverage check.

Local native Windows execution passed all four original partitions with no
missing, duplicate or skipped cases. The common inventory SHA256 is
`6535ae7967674409913f926191f538106e70ad2a937cc6dcb0eec187f7177c9f`.
Remote first-attempt timing is compared with run `39011651281` (16:47), using
run creation through the last executed job's completion, irrespective of failures.

The candidate is [run 39013705315](https://github.com/TokenRhythm/opensquilla/actions/runs/39013705315)
at `802b98244c48feed53dc295f39af9bf8a33ff720`, a full `workflow_dispatch`, attempt 1.
It ran from 2026-10-17 07:27:35 UTC through the final job at 07:42:16 UTC.

| Metric | Previous run | Four independent bundle jobs |
| --- | ---: | ---: |
| Complete CI wall time | 16:47 | 14:41 |
| Executed jobs / planned skips | 129 / 2 | 134 / 2 |
| Peak simultaneous jobs | 105 | 111 |
| Summed runner time | 512:21 | 472:59 |
| Median job-created to start | 2 s | 2 s |
| Original Windows owner job | 16:11 | 4:49 |
| Original owner parallel pytest | 827.41 s | 148.67 s |
| Original owner serial pytest | 62.58 s | 62.09 s |
| Bundle cumulative JUnit case time | 1345.202 s | 56.614 s |
| Bundle median JUnit case time | 21.42 s | 0.3755 s |
| NSIS candidate ready, from run creation | 9:00 | 6:20 |
| Last NSIS acceptance finishes | 14:10 | 10:46 |
| Windows profiles recovery job | 11:03, passed | 13:20, failed |

All four bundle jobs started at +0:27 on distinct Windows runners. Their job
durations were 53 / 63 / 61 / 65 seconds; recorded pytest execution durations
were 10.578 / 16.938 / 15.782 / 15.688 seconds. All four jobs finished by +1:33,
and the required coverage check passed at +1:45. All 66 cases passed exactly
once and their inventory matches the previous run. The original owner contains
zero bundle cases; its remaining 1,519 nodeids, outcomes and execution phases
match the previous run exactly (1,503 passed / 14 skipped parallel, 2 passed
serial). No test assertions or collectors were removed.

The final run has 136 jobs: 132 successful, two planned skips, and two failures.
All 26 NSIS jobs succeeded. The Windows profiles recovery job failed its last
`desktop-cleanup-flow` script, and `CI result` retained that failure. Its six
Desktop scripts were attempted sequentially; the cleanup helper reported
`cleanup_inventory_stale` and the test later timed out waiting for post-exit
completion. All six scripts ran; the final cleanup success condition was not
satisfied. The six-script step grew from 429 to 551 seconds, including cleanup
growing from 12.070 to 106.444 seconds. This experiment records the first failure
and does not retry or fix it.

The observed whole-run reduction is 126 seconds (12.5%), while the original
bundle-owning job shrank by 682 seconds (70.2%). These are different measures:
the new final bottleneck is Windows profiles recovery, which finishes at
+14:30 before the final result gate. NSIS also finished earlier without changes
to its implementation in this experiment. The failed candidate is therefore a
timing observation, not proof of a successful full regression run or a stable
12.5% speedup attributable solely to bundle partitioning.

The unexpectedly large per-case reduction is concentrated in the 55 tests that
call `_make_home()`: their cumulative JUnit time falls from 1344.576 to 56.519
seconds. That helper creates synthetic files and applies SQLite migrations;
the other 11 cases take 0.626 and 0.095 seconds respectively. Source, tests,
conftest, dependencies, Python 3.12.10 and Windows image 20261007.264.1 are
unchanged. Dedicated fresh processes, a single worker per runner, and explicit
pytest basetemp beneath `RUNNER_TEMP` changed together; the old default temp
path was not recorded. No profile isolates database I/O, storage placement or
same-VM contention, so the cause of the prior fixture-related long tail remains
unproven.

The remaining profiles job has two browser contracts and six sequential
Desktop scripts. They are the next candidates for runner-level separation,
retaining each script's internal import/reopen/recovery sequence. This round
does not change them. Cached API snapshots, execution reports, JUnit, logs and
comparisons are under `.cache/ci-timing/round4/` and
`.cache/ci-timing/round4-comparison/`. Results remain on the experiment branch;
no merge into `main` was performed.

### Fifth experiment: independent Windows profile scripts

The six Electron scripts formerly executed sequentially in Windows `profiles`
now run in six independent `desktop-recovery-e2e` matrix cells. Their names are
`profiles-onboarding-flow`, `profiles-onboarding-first-chat`,
`profiles-profile-consolidation-flow`, `profiles-primary-repair-accessibility`,
`profiles-profile-import-flow`, and `profiles-desktop-cleanup-flow`. Every cell
retains the original staged frontend, Python dependencies, compiled Desktop and
prepared Electron setup. The original Windows `profiles` cell retains both
browser contracts and its Router tests; the new cells do not duplicate those
checks. Linux and macOS selection is unchanged.

The six scripts and telemetry wrapper are unchanged. Each creates isolated
temporary profiles; some use fixed ports within the isolated runner. Internal
import, shutdown, restart and recovery sequences remain serial. No deadlines,
assertions, retries or failure handling are relaxed.

The planner closes every Windows profiles selection over all six execution
cells. A required coverage job verifies seven identity-bound reports: the
original owner executes zero of the six scripts, and each dedicated runner
executes its assigned script once. Reports preserve the script inventory and
hashes, run/SHA/attempt, telemetry status and exit result. Missing, duplicate,
skipped or inconsistent evidence fails the aggregate gate. Script failures
remain failures of the Desktop matrix, independently of complete invocation
coverage. This proves execution of each script, not completion of every
assertion inside a failing script. As with the bundle proof, a repeated
experiment needs all participating jobs from the same attempt.

The comparison baseline is run `39013705315` at
`802b98244c48feed53dc295f39af9bf8a33ff720`: 14:41 overall, including the retained
Windows profiles cleanup failure. The experiment uses a full remote
`workflow_dispatch` and preserves its first-attempt result regardless of failure.

The candidate is [run 39018293735](https://github.com/TokenRhythm/opensquilla/actions/runs/39018293735)
at `0b610ad969761405b57b9378be972183dd969531`, attempt 1, from
2026-10-17 08:41:14 UTC through the final job at 08:55:08 UTC.

| Metric | Previous run | Six independent profile jobs |
| --- | ---: | ---: |
| Complete CI wall time | 14:41 | 13:54 |
| Executed jobs / planned skips | 134 / 2 | 141 / 2 |
| Peak simultaneous jobs | 111 | 120 |
| Summed runner time | 472:59 | 523:51 |
| Median job-created to start | 2 s | 2 s |
| Original Windows profiles owner job | 13:20 | 2:49 |
| Longest job among the original owner and six scripts | 13:20 | 3:56 |
| All Windows profiles jobs finish, from run creation | 14:30 | 4:55 |
| First Electron script start through last script end | 550.776 s | 184.236 s |
| Cumulative six-script execution time | 550.275 s | 387.033 s |
| Summed Windows profiles job runner time, excluding coverage gate | 13:20 | 15:33 |
| NSIS build job | 5:11 | 7:57 |
| NSIS candidate ready, from run creation | 6:20 | 8:56 |
| Last NSIS acceptance finishes | 10:46 | 13:38 |
| Windows gateway-sqlite-5 job | 8:19, passed | 13:12, failed |

| Unchanged Electron script | Previous seconds / result | Candidate seconds / result | Candidate job |
| --- | ---: | ---: | ---: |
| onboarding-flow | 140.319 / passed | 90.391 / passed | 2:20 |
| onboarding-first-chat | 22.135 / passed | 22.599 / passed | 1:15 |
| profile-consolidation-flow | 92.824 / passed | 79.654 / passed | 2:29 |
| primary-repair-accessibility | 17.671 / passed | 15.579 / passed | 1:22 |
| profile-import-flow | 170.881 / passed | 164.961 / passed | 3:56 |
| desktop-cleanup-flow | 106.444 / failed | 13.848 / passed | 1:22 |

All six jobs started at +0:59 on six distinct Windows runners. Their actual
script execution intervals overlap with peak concurrency six, compared with
one previously. All six scripts passed exactly once. Downloaded manifests and
telemetry match the measured SHA/run/attempt, canonical LF hashes match all six
baseline script contents, and the original owner has zero Electron records.
The original browser session-hang, modern-session and Router steps all passed,
taking 76 / 12 / 13 seconds versus 107 / 19 / 21 seconds previously. The required
coverage job passed at +9:07 after the existing Desktop matrix completed.

The final 143 jobs comprise 139 successes, two planned skips and two failures.
NSIS passed all 26 jobs. The Windows `gateway-sqlite-5` parallel phase reported
one failed / 1,518 passed / 6 skipped in 631.25 seconds; its serial phase still
ran and passed all seven cases in 61.86 seconds. The failure was
`test_literal_unicode_code_fences_scope_and_utf16[aiosqlite]` in
`tests/test_session/test_message_search.py`. It was not retried or repaired;
the required `CI result` gate retained that failure. This is a completed-run
timing comparison, not a successful full-regression benchmark.

The observed overall reduction is 47 seconds (5.3%). Profiles is no longer on
the final path: all seven participating Windows jobs finish 575 seconds earlier
than their previous owner. The remaining tail is NSIS frozen audit / Gateway
smoke / first interaction finishing at +13:38, Windows gateway-sqlite-5 at
+13:39, NSIS aggregation at +13:43 and the final gate at +13:54. NSIS candidate
availability moved 156 seconds later despite unchanged build implementation.
These runs do not establish the cause of that variation. The old cleanup
failure versus the new pass also changes script work, separately from overlap.
Do not attribute the entire timing difference to the scheduler change.

Independent runners add preparation work: the seven profiles jobs consume
933 runner-seconds versus 800 previously, before the coverage gate. Total
runner time rose by 50:52 (10.8%), including unrelated NSIS and Python timing
changes. Raw API snapshots, seven execution reports, remote coverage proof,
SQLite failure log and comparisons are saved under `.cache/ci-timing/round5/`
and `.cache/ci-timing/round5-comparison/`. No merge into `main` was performed.

## Test cost and ownership

The ordinary offline suite excludes `stress`. The required `python-stress` suite
runs capacity/soak checks once, in a separate Ubuntu job without xdist or a
duplicate preflight. It runs for complete Python plans (including nightly,
manual full runs, dependency changes, and fail-closed queue fallback), the
Gateway/session/persistence/recovery/config/contract runtime domains, and the
stress test itself. Shared engine/application/safety changes already request
complete Python coverage. Unrelated frontend or documentation changes do not
request this suite. `CI result` requires success whenever it is planned;
failure, cancellation, or an unexpected skip cannot satisfy the gate.

The first capacity check is the 10,000-task terminal-cleanup soak. Its task count,
collection-size bounds, and 200 MiB memory ceiling remain intact. Its bounded
hang guard retains main's 60 seconds and reports completed-task progress on
timeout. Normal success, exception, queue handoff, and lock ownership
contracts stay in ordinary platform shards. The soak uses fake task storage,
an empty-approval port, and real in-process stream snapshots. Its replay cache
retains the two events needed for the synthetic turn; replay-history sizing and
approval expiry have separate regression owners. This avoids repeatedly scanning
unrelated history and issuing 10,000 empty approval queries. It does not establish
SQLite or packaged runtime capacity. Windows/native integration coverage remains
in the existing platform suites.

Full-shard preflights keep the environment identity probe and four per-runner
smoke files. Ubuntu no longer pre-runs the 24 regression files already owned by
that same full shard. Windows filters the historical family regression list
against the physical owners in the actual planned Windows matrix. A sibling's
regression is omitted only when that sibling must execute in this run; partial
matrices retain regressions whose owners were not selected. Missing, malformed,
duplicate or inconsistent matrices, and inventory commands that fail after
partial output, stop the job. The two passes remove 24 Ubuntu and 36 Windows
duplicate file invocations in a complete matrix (60 total). The four environment
smokes remain on every runner because another runner does not validate its setup.

Planner tests validate the unchanged repository configuration once per module,
then give each case a deep copy. This avoids repeating the repository-wide input
scan for every routing example while preserving isolation for nested mutations.
Invalid-configuration tests still call the real loader on their own input files.
Input validation uses exact membership for literal paths and shares wildcard
matches only within one invocation, so later calls observe repository changes.
Workflow tests cache YAML parsing by file content and return independent deep
copies; edits still invalidate the parse and one test cannot mutate another's
workflow data. These changes reduce harness work without removing invariants.
Ten routing examples that read the unchanged real repository also share one
dependency graph built by the real parser per pytest process. Only those cases
opt in; the fixture checks repository, revision and governed file set and gives
each call a fresh dictionary. Temporary repositories, historical revisions,
deletions, dynamic imports and parser rejection cases still exercise the real
builder without this snapshot. The production planner has no persistent cache.

### Browser behavior and capacity

Ordinary virtualization/recovery cases use `--grep-invert=@capacity`, while
the independently gated `webui-capacity` job uses `--grep=@capacity`. These
runners each retain one worker and zero retries and consume the same verified
production artifact. Capacity owns its browser installation and isolated VM;
its job result is required by `CI result`, so ordinary browser success cannot
hide planned capacity work that failed, was cancelled, or was skipped.

The long-list selection partitions into 52 ordinary cases and two capacity
cases: the 34,000-event flood and the 3,000-row sidebar. All assertions and
capacity thresholds remain unchanged. The ordinary phase retains both the
200-row and 1,000-row cases, including real pagination across 200-row boundaries,
as well as recovery, focus, anchoring, dragging and draft preservation.

All WebUI changes, Python dependency changes, full/manual/nightly plans and
unknown-input fallbacks require capacity. Backend/desktop-only recovery changes
still run the ordinary browser contracts without repeating these synthetic
renderer workloads. New capacity-tagged specs must be listed in the executable
capacity step and admitted by the planner. Local unfiltered Playwright commands
continue to run both tiers; no global default hides the capacity cases.
Every browser lane runs the capacity-inventory contract before browser
installation, using its already-installed Python dependencies. This admission
also runs for pure WebUI plans that do not select a Python test shard, so an
unregistered capacity tag fails before either browser phase can omit it.

```sh
# Ordinary regressions; stress is deselected by the repository default.
uv run pytest tests/test_gateway/test_task_runtime_terminal_cleanup.py
# Explicit capacity validation (same target and selection as CI).
uv run pytest tests/test_gateway/test_task_runtime_terminal_cleanup.py -m stress -n 0
# Include both tiers when deliberately validating the entire file.
uv run pytest tests/test_gateway/test_task_runtime_terminal_cleanup.py -m ""
```

When admitting a stress test, use `pytest.mark.stress`, register its file in the
dedicated job, and update the planner for its affected runtime paths. The CI
inventory contract compares marked files with executable targets and checks that
each test-file change selects the suite. This prevents a new marker from silently
removing a test from every lane. Custom pytest `-m` expressions override the
repository default; ordinary CI expressions therefore explicitly exclude stress.
The local `windows_test_shards.py run` helper defaults to the same `not stress`
selection. Explicit `-m stress` or `-m ""` overrides it in both its parallel and
serial phases.
Its metadata records the actual base filter and phase selector. The duration
builder accepts paired legacy or filtered markers, but still rejects different
execution configurations across sampled runs. Do not mix measurements from
before and after a selection change when recalibrating shard weights.

New regression tests should protect an observable contract or a reproduced
failure. Extend an existing behavior case when it already owns that contract;
delete obsolete or redundant assertions rather than retaining every generated
case. Prefer an event/barrier for real concurrency and a controlled clock for
deadline arithmetic. A bounded wait is a hang guard, not the assertion that an
operation must finish in an arbitrary number of milliseconds. Keep at least one
real thread/process test when that boundary is the contract. Do not replace
failure diagnosis with blanket retries, larger product timeouts, or more workers.
The Bun file runner preserves signal termination as `128 + signal` (SIGILL is
132); unknown termination remains a failure. The existing CI policy still only
retries SIGILL once, rather than retrying assertion failures or cancellation.

The initial removals preserve these owners:

| Removed duplicate | Retained contract owner |
| --- | --- |
| Onboarding coordinator private fields, comparator/Promise spelling, and internal assignment order | Compiled Electron single-flight, payload conflict/join, abandonment and import-admission behavior tests; TypeScript build; main IPC trust and writer/recovery wiring contracts |
| Update scheduler implementation and test-description strings | Existing fake-clock scheduler behavior tests, plus main startup/quit and interval configuration checks |
| Sleep-based watchdog recovery case | Existing controlled-clock recovery test now checks heartbeat sequence, age, duration, and observation span; the real sampling-thread smoke waits for a persisted sample |

Review cost using JUnit durations and first-attempt failures. Track queue wait,
execution wall time, summed job time, and unrelated failure rate separately.
Deleting source assertions reduces refactoring noise; it is not evidence of a
large wall-time saving. Measure the capacity split on actual CI runs before
claiming an overall speedup. Existing unknown-path and queue-evidence fallback
rules remain conservative.

### Portfolio review and deletion policy

The 2026-10-12 follow-up inventoried the tracked tests after the initial CI
changes: 1,590 Python test files with 20,966 static test-function definitions,
and 780 JS/TS test files. These are source counts, not pytest/Vitest collected
cases; parameter expansion and platform selection change execution counts.
The inventory covers all named test files. Semantic review focuses on duplicate
candidates, source-only contracts, expensive scenarios, and their retained
owners; it does not certify every assertion in the repository.

Use the following decisions when reviewing an existing or generated test:

| Finding | Decision | Required evidence |
| --- | --- | --- |
| Same product function, inputs, fixture semantics and assertions | Delete the duplicate; keep one named owner | Resolve imports, decorators, fixtures and platform markers before comparing test bodies |
| Test copies a product algorithm into the test and exercises only that copy | Delete the self-test | Retain direct tests of the real exported product function and any useful integration case |
| Test reads another test's source to check assertion names, prose or timeout spelling | Retire the implementation constraint | Keep executable harness admission and its real behavior assertions |
| Source strings freeze private fields, exact CSS spacing, gradient lengths or old class names | Retire the constraint when it has no independently valuable contract | Describe the intentionally removed restriction; do not pretend it has equivalent behavior coverage |
| Source inspection protects a unique architectural, IPC, accessibility, trust or packaging boundary | Retain, or replace only with adequate behavior coverage | A source-reading API alone is not evidence of low value |
| Similar test body with distinct malformed inputs, state transitions or native environments | Retain the cases | Parameterization is not redundant coverage |
| Capacity, heap or throughput scenario mixed into a short behavior suite | Separate execution frequency and responsibility | Keep a required planned owner; do not silently skip or lower the capacity threshold |

The follow-up removes duplicated Python contract cases and parameter rows,
desktop test-of-test and private-implementation checks, frontend CSS-spelling
sentinels, and a browser citation case that tested its own copied algorithm.
It preserves actual desktop/native flows, compiled behavior owners, recovery
fixtures, access scopes, and malformed-input coverage. One unique whitespace
input from duplicated CLI classification tests is transferred to the retained
owner instead of disappearing with its old test.
Follow-up review found one nonredundant CIDR boundary in the removed source
checks. Real `AuthConfig` construction now rejects public and overly broad
IPv4/IPv6 allowlists and accepts private-network boundaries. A negative control
that removes the registered validator makes all six rejection cases fail; the
old source-string constraint remains retired.

Two counterexamples matter. The CLI and Gateway URL normalizers have identical
test bodies but are different production implementations, so both remain.
Migration/recovery tests often read files to verify actual persisted output;
those reads are not source-code snapshots. Likewise, 38 identical Python body
groups in the inventory are review candidates, not 38 automatically deletable
groups.

Remaining work is prioritized by cost and independent failure detection:

- Keep real installed-wheel migration resolution even though it is expensive:
  inspecting ZIP membership does not exercise installation and runtime lookup.
- Investigate repeated source parsing, temporary Git subprocesses, and child
  pytest collection before deleting planner or architectural boundaries. The
  duration manifest includes provisional 0.01-second weights, so it is not a
  reliable current ranking for all recently introduced tests.
- Measure the two browser phases on hosted first-attempt runs. The TUI's three
  terminal-size performance combinations remain in their existing required
  owner until measurements justify a separate schedule. Do not claim savings
  for a browser spec absent from the required workflow merely because it was
  deleted, or assume that a new phase improves every PR's critical path.
- Treat historical physical-line ceilings as policy decisions. Retiring such
  a gate removes an architectural budget, not duplicate behavior coverage;
  this audit leaves those budgets and semantic architecture guards intact.

For new tests, first name the observable regression and search for its existing
owner. Extend that owner when the new input reaches a distinct boundary. A
private refactor, copied mock algorithm, renamed test, or changed CSS literal
does not by itself justify another regression case. Do not replace deleted
low-value assertions with a new test framework that costs more to maintain.

### Hosted measurement checkpoint (2026-10-12)

A read-only sample of the latest 20 completed runs created between 08:45 and
11:00 UTC used each run's **first attempt**, including a failed first attempt
whose later retry passed. It contained 10 successes, five failures and five
cancellations. This short convenience sample is not an estimate of the long-term
flake rate, and these runs do not contain the optimization candidate below.

Nine non-cancelled, full 70-job runs had a median elapsed time of 23m49s and
403.45 summed job minutes (not billed minutes). Windows desktop recovery
`profiles` was the largest critical-path candidate: ten successful jobs had a
20m33s median. Repeated Windows preflights also had material cost: 102 successful
steps totaled 163.15 job minutes, with a 95.5-second median. Investigate expensive
desktop phases and duplicate setup before adding more shards or removing native
recovery assertions.

The 17 executed WebUI jobs had 16 successes and one cancellation, with no observed
failure. Two representative logs reported about 1.3 minutes for the delta flood
and 3.5–3.6 seconds for the 3,000-row sidebar. This supports measuring the flood's
selection cost; it does not establish a hosted sidebar flake or a measured
speedup from the capacity split. A local browser failure still requires a
product-versus-harness diagnosis, with the same workload and thresholds.

Separate failure classes before remediation. The sample included real native
launch/goal predicate timeouts, exact dependency-version and attribution-string
mismatches, and an unexplained installed-desktop probe failure. A green retry
does not identify a cause. Preserve native/process boundaries, replace polling
with completion signals when that is the contract, and retain diagnostic
evidence for cases whose root cause is still unknown.

Pull-request path selection compares the PR head with its merge base, not directly with the
current base tree. A branch that is behind `main` therefore does not inherit base-only changes
as if the PR introduced them. This affects suite selection only: merge-queue reuse remains bound
to the exact current base and tested tree, so a base advance still triggers the full fail-closed
queue matrix. For an otherwise eligible non-policy PR, refreshing the branch and completing a
new green PR CI run can make a later queue entry exact; reuse is decided again at queue time.

### Windows profiles and PTY follow-up

Three representative successful Windows `profiles` jobs had 18 first-attempt
case records, all passed. Their median native case durations were 370.5 seconds
for onboarding, 240.2 for import, and 183.3 for consolidation. Desktop compilation
took only 1–2 seconds. The six scripts contain about 27 real Electron launches
with distinct first-run, restart, migration, credential, repair and deletion
contracts; sharing one warm profile would change those contracts.

Development Desktop now invokes the checkout's `gateway-entry.py` through
`uv run python`, matching the packaged dispatcher's existing lightweight offline
recovery path. It previously entered the complete CLI command tree for every
recovery child. Gateway, model probing and migration still dispatch to the same
CLI app; the public `opensquilla` console entry remains unchanged. The selected
checkout, real child processes, ownership gates, recovery checks and restart
assertions remain in place. A missing entry file fails before child launch.

The profiles browser step collects the same 24 scenarios (the former 1 + 23)
in one invocation, removing one managed Gateway startup. The four modern
recovery scenarios retain their separate invocation and output directory.
Workers remain one, retries zero, and first-failure traces remain inside the
uploaded report tree. Cleanup's two 30-second exit watchdogs now use the existing
abortable deadline helper; a successful exit no longer leaves their losing
timers keeping Node alive. The deadlines and post-exit deletion assertions are
unchanged.

The six profiles scripts and the orphan-recovery flow also isolate Windows
AppData and temporary directories. Changing HOME alone does not isolate the
OS user-state lock root or portable-profile discovery. Each fresh fixture owns
those directories; a deliberate restart continues to reuse the same fixture.
This prevents host profile candidates and lock state from becoming hidden test
inputs without replacing recovery with a mock or reusing warmed profiles.
The theme, window-background, unsafe-legacy recovery and mock-update scripts
use the same isolated AppData, temporary and user-state roots. Theme and
mock-update also discard inherited profile overrides before setting their own
launch configuration. Those four scripts set TMPDIR as well as TEMP/TMP so
their child processes use the same fixture on POSIX. Launch counts, native
behavior assertions and restart identities are unchanged.

The isolated native cleanup run exposed a separate Windows long-path defect:
quarantine renamed a readable backup directory to a longer path, and manifest
enumeration then failed at MAX_PATH and rolled the transaction back. Manifest
I/O now uses the existing native path conversion while retaining logical paths,
identity comparisons, no-follow traversal and rollback guards. The existing
long-path regression now covers directory enumeration after quarantine, not
only removal of a long file name; it failed before this fix and passed after it.

The isolated settings-import flow exposed another concrete failure behind a
window timeout: migration passed admission and preflight, but creating its
277-character staging receipt temporary file failed before publication. The
receipt JSON writer now uses native paths throughout its local atomic I/O,
without changing public paths or the commit protocol. The existing migration
behavior test reaches that boundary and fails on the old writer. Wider migration
regression also exposed the same boundary while copying real session media into
staging; snapshot destination I/O and failure cleanup use native paths while
source handles, identity and digest checks retain their contracts. These are
bounded fixes, not a claim that every migration path supports arbitrary Windows
path lengths. Import-flow failures retain the original window deadline and
include the settled migration result instead of discarding its cause. The
candidate metadata test uses a real Windows directory junction to exercise its
no-follow contract without requiring symlink creation privileges; POSIX keeps
its real symlink fixture.

Both sampled native launch timeout jobs were Windows runs with four pytest
workers. A local native reproduction found that registration failure and
cancellation could strand pywinpty's blocking output reader even after its
process and sockets were closed. Delaying reader creation alone did not repair
a later initialization failure. The adapter now starts its reader after owner
registration and uses a stoppable nonblocking native read loop: an empty read
does not imply EOF, socket backpressure is interrupted during close, and cleanup
joins the reader. Real PTY, process-tree and tail-output assertions are retained.
The launch test records safe phase/worker-stack diagnostics before its original
10-second guard expires. Fixing the reproduced leak does not establish it as the
sole cause of the historical hosted timeout; a new hosted first-attempt run is
still needed to assess that outcome and the profiles job's actual savings.

Local four-worker verification also recorded three generic controlled-launch
failures in native descendant-tree cases. A subsequent run with full exception
capture passed, as did one bounded old/new-backend comparison of the seven
native cases. The initial failures did not retain their inner exception and
remain unattributed; later passes do not prove a fix. Keep the first failures
and capture the cause chain on the next failure rather than adding retries or
changing the launch deadline.

Native consolidation validation also retained one 15-second Electron shutdown
failure: Gateway exited cleanly and Desktop recorded its committed phase, but
the wrapper/tree close was not proven and `taskkill` returned 128. A subsequent
observed close crossed that same boundary in 4.1 seconds with both owned PIDs
absent and normal wrapper stream closure. That does not identify the first
failure's cause. Preserve the strict process-tree fallback and original timeout;
do not treat a committed log entry or a later passing run as OS-exit proof.
The close helper captures the owned child before closing the Playwright
dispatcher and observes its exit, close and standard-stream lifecycle events.
Failure reports include the bounded event sequence before and after cleanup,
which separates process exit from inherited-pipe closure. Successful closes
remain silent, listeners are removed, and no stream contents or environment
values are collected. PID absence and taskkill 128 remain insufficient proof
of Windows process-tree cleanup.

### First published candidate and diagnostic follow-up

Run `38261184881`, attempt 1, tests commit
`cb6984b8eb0c6ccf7890b46b0d62bbd0dc5b5cf2`. Windows profiles passed in
634 seconds, with all six native cases passing on their first attempt and no
shutdown-failure or forced-cleanup event. The three earlier representative jobs
took 1,130, 1,510 and 1,241 seconds. The same ten Windows preflight cells totaled
329 seconds; ten earlier complete, non-cancelled first attempts had a median
total of 976.5 seconds. Event, runner, cache and revision differences remain, so
these observations do not establish a causal end-to-end CI speedup.

All 35 Windows PTY cases and the four affected Windows migration/cleanup
contracts passed. Two other required tests failed: an empty mutation observation
after a successful real file write, and a Node readiness-contract child that
timed out before its first synchronous phase log. Their direct implementations
were unchanged in that candidate. Neither failure is dismissed as harmless or
attributed to a specific root cause from the available logs.

The portable readiness contract now runs in the existing required Desktop Node
owner. All four terminal states, startup budget, error and redaction assertions
remain; the Python suite no longer starts another Node process just for this
contract. Real Git observation tests retain their deadlines and assertions,
with bounded failure-report sections for Git/snapshot states and elapsed times.
These reports contain no paths, command arguments or file contents.

Successful browser capacity runs upload only their two small performance JSON
reports through the existing diagnostic upload. Failures retain the full trace
tree. Diagnostic upload errors still warn without overriding the required test
result. First-attempt failures remain part of the evidence even when a later
candidate passes.

## Modes

Set the Actions repository variable `CI_OPTIMIZATION_MODE` to one of:

- `shadow`: compute and report whether merge-queue evidence is reusable without accepting it.
  The merge-group entry runs the full fail-closed matrix. Use this only as the emergency switch
  for disabling queue evidence reuse while keeping the merge queue active.
- `enforce` (default): reuse exact-base, exact-tree trusted PR evidence in the merge queue;
  otherwise run the full fail-closed queue matrix. Replace the normal `main` push matrix with the
  installation and offline gateway canary.
- `legacy`: emergency rollback mode. It deliberately runs full PR and queue CI and keeps
  the pre-enforcement `main` behavior.

An unset or empty variable resolves to `enforce`. Any non-empty unsupported value fails CI.
Changing modes does not modify code or persisted OpenSquilla configuration.

## Trust boundary

Reusable evidence is accepted only when all of these facts match authoritative GitHub data:

1. The successful source run was a `pull_request` run of `.github/workflows/ci.yml` in this
   repository and is associated with the attested PR head.
2. The PR merge preview and queue entry have the same Git tree and base commit.
3. The attested PR head is an ancestor of the queue commit.
4. Every merge-critical input declared in `.github/ci/trust-policy.v1.json` has the same digest
   as the queue base. The manifest includes itself, the suite contract, required workflow,
   planner, gate, attestation verifier, shard assignment policy, and local CI executors.
5. The planner digest, selected suites, platform cells, and per-suite execution digests match the
   tested tree. Scheduling-only duration data stays outside the trust root but remains covered by
   planner contracts and suite execution digests.

Missing, expired, malformed, stale, or unverifiable evidence never bypasses tests. The exact
merge-group entry runs the full matrix when evidence cannot be reused. Nightly health remains an
independent diagnostic and never changes a PR plan or queue decision. The required
branch-protection context remains `CI result`.

Before merging this rollout, record the current `protect-main` ruleset history version and
confirm that it is active on the default branch, has no bypass actors, requires `CI result` and
`Validate target branch` with strict status checks, and still enables the merge queue. Keep
`enforce` as a reviewed repository default; a CI-policy pull request still cannot reuse its own
evidence because its policy digest differs from the target branch. This CI workflow does not
modify repository governance settings.

## Validation and rollback

After a CI-policy rollout merges, validate reuse with an ordinary non-policy pull request based on
the new `main`. A successful queue fast path runs only evidence verification, the combined-tree
canary, and `CI result`. It does not publish derived evidence because only root PR evidence is a
queue trust source. Base/tree/policy mismatches and API or artifact failures must run the full
fallback matrix. Nightly failures must not affect either decision.

If evidence is accepted incorrectly or is not bound to the queue SHA, set
`CI_OPTIMIZATION_MODE=shadow`, keep the merge queue active, and revert the policy change. If the
planner omits a required suite, set `CI_OPTIMIZATION_MODE=legacy` so PR and queue entries run full
CI while the policy is reverted. Over-selection or a low reuse rate is safe and does not require a
mode change.

## Stability evidence

Keep a fixed UTC observation window and the original run, SHA, event, attempt,
job, failed step, and raw-log digest. Report first-attempt results independently
of the latest result; keep cancellations and authorization waits outside the
success/failure denominator. Exclude aggregate result gates from failure causes.
Count affected runs and distinct SHAs as well as matrix-job observations.

The attempts API can return a new job ID with an earlier attempt's execution
timestamps and log. Preserve that row, but do not count it as a new execution
when it completed before the current attempt began. Workflow reruns and retries
inside a job need separate coverage statements. A later green result, unchanged
test file, or repeated timeout does not establish a test race or infrastructure
root cause. Security-advisory changes can correctly fail unchanged source.

For concurrency changes, retain a controlled negative case at a fixed worker
count, the original deadline, and the first observed failure. The token RPC
tests notify the event loop directly instead of occupying the product executor
while waiting for it. The staging test drains owned work even when its entry
guard fails, preserving the original exception and adding cleanup diagnostics.
Install telemetry samples platform metadata outside the process lock while
keeping identity selection, privacy checks, and result merging protected.

These controls prove specific mechanisms; they do not identify every historical
timeout. After rollout, compare first-attempt failure and rerun rates by event,
OS, and executed job over the same observation window. Record the deployed SHA
and outstanding unknown signatures. A single green full matrix is acceptance
evidence, not a measured long-term stability improvement.

### Bounded subprocess contracts

Keep a subprocess deadline attached to the behavior it owns. The adversarial
source-symbol guard calls the same lightweight matcher used by the production
tool, without importing the entire tool registry and Gateway. Its original
30-second startup and 15-second parsing limits remain; tool-level Python and C
receipt tests cover integration. A regression to the earlier catastrophic C
regex must still fail the parsing guard.

Channel storage imports must not initialize adapters or the Gateway. Public
channel exports remain available through lazy lookup. The crash-recovery test
retains its 20-second deadline, abrupt exit, committed ingress, and ambiguous
outbox assertions. Preserve the same asynchronous pipe reader on timeout so
phase output already consumed before cancellation is not lost. On Windows,
parser, crash, and cold-import probes launch the base interpreter with the venv
launcher environment and assert its PID and prefix, so the watchdog owns the process
doing the work rather than a redirector that can exit before it.

The generic Windows managed-handle and pipe/EOF contracts use PowerShell
commands directly within the original five-second budget. Native Python argv,
quoting, exit-code, and stdin contracts remain in their dedicated tests. This
removes an unrelated interpreter startup from those generic contracts; it does
not establish which process stage caused historical failures without phase data.

The fresh-wheel migration test installs all base dependencies into a clean
environment from `uv.lock`, then installs the wheel with normal offline
dependency resolution. The install phases share the original 300-second budget.
Wheel contents in the cache do not imply that a second registry resolution has
cached metadata; preparing locked dependencies avoids that extra requirement.
The wheel's `Requires-Dist` must retain every declared base dependency, and the
installed environment must pass `uv pip check`.
Run the normal locked dependency sync first and preserve `UV_CACHE_DIR` when
isolating a local test profile. An unavailable cached dependency is an error,
never a reason to skip the test or retry online. The installed interpreter runs
with `-I` and must resolve migrations beneath its own environment prefix.

For queued real-storage RPC tests, readiness must identify the task it observed.
Clear the first handler's signal before releasing it, then observe and verify
the queued handler before starting its existing terminal-settlement budget.
Reuse the bounded durable-readiness guard; retain both two-second terminal
waits, replay identity, transcript, and reopen assertions. Timeout diagnostics
record the task identity, phase, task states, and owned drivers. Slow SQLite
holds show where time was spent, but do not by themselves identify the cause of
slow runner I/O or prove that a specific historical failure waited on that task.
Custom provider, recovery, and budget-error handlers use the same readiness
boundary before release. Their existing provider, retry, and terminal guards
start after durable activation, with the expected task identity checked first.
Review every custom handler and downstream wait in the module when fixing this
pattern, including paths whose activation happened to finish within the guard.

An unexplained post-exit Desktop cleanup timeout must fail its first attempt.
The timeout signature alone does not identify a transient environment failure.
Keep delete-all helper diagnostics outside the approved deletion targets so
that its output survives Electron exit and target removal. Regular-file output
avoids introducing an unread diagnostic pipe; retain the original stdin/EOF
approval protocol and the existing exit and cleanup deadlines. A successful
diagnostic run proves observability, not the cause of an earlier timeout.
