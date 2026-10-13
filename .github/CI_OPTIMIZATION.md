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
hang guard is 300 seconds and reports completed-task progress on timeout; the
former 60-second guard was not a product throughput SLO. Normal success,
exception, queue handoff, and lock ownership
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

The existing browser job runs ordinary virtualization/recovery cases with
`--grep-invert=@capacity`, and a separately gated `webui-capacity` phase runs
`--grep=@capacity`. Both phases keep one worker and zero retries and share the
already-installed browser and verified production artifact. No additional
runner/dependency installation is introduced. The capacity step's own outcome
is exported to `CI result`: a green browser job cannot hide a planned capacity
step that failed, was cancelled, or was skipped.

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
The browser job runs the existing capacity-inventory contract before browser
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
