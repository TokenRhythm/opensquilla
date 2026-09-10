# Client connection stability

Implementation started from `bbd0c429e106babeeec70a6c6d493b563696d576`
(`origin/main` pinned by the approved 2026-09-09 plan).

The local integration branch subsequently merged fetched `origin/main`
`e4eb36b2deb8dcbb6572aa9a310492a28c8c01da` (image previews and archived
experiment cleanup), then integrated the 2026-09-10 pinned upstream
`021063bc7327670dddd1bde1c83ceb742044c670`. The latter includes #1585 strict
handshake/directory validation, #1595 parallel contract generation, #1598 Router
replay isolation, and the upstream skill-source and retired-experiment changes.
The PR preparation additionally integrates `9486415af7188335d9f18f45143bfe33ce97e890`
(#1597 questionnaire waiting and task-terminal recovery).
The final upstream refresh is `d44e7936be0f60c82a854cbeb8a66b491f395fec`
(#1602 entering a new draft before optional project hydration).
Retired-experiment file removals are unchanged from that upstream pin and remain
recoverable from Git history; this adaptation does not introduce engine cleanup.
Upstream functionality is retained. Most existing generated-contract diffs are generator
fingerprint changes caused by the explicitly added production validation roles,
not changes to those methods' schemas.

The contract is continuity of work, not an immortal TCP connection: a healthy
Gateway stays usable; interrupted communication is repaired internally without
discarding a draft, navigating the page, replaying a mutation, or restarting the
client/Gateway. A persistent service or identity failure remains visible and
must not be represented as a successful operation.

## Implemented boundaries

1. **One connection owner.** RPC cancellation and deadlines reject their own
   waits. They do not recycle the shared socket. Handshake deadlines, replacing
   credentials, health checks and reconnect timers belong to `RpcClient`.
   Network recovery has no attempt limit, caps jittered backoff at 15 seconds,
   and resets attempts only after 30 seconds of stability. Logout/stop still
   stops it. A matching healthy connection descriptor is a no-op; explicit
   connect can retry a blocked identity after the underlying problem is fixed.
   Complete Hello shape validation and matching-connect response admission run
   before identity/intent validation; business events cannot enter the sequence
   or application before Hello. Identity, capabilities and flow policy are
   installed before readiness, with generation checks after callbacks. An
   explicit unsupported protocol or rejected identity blocks automatic retries;
   malformed handshakes retire only their current generation and back off.
2. **Control is independent of slow business work.** The Gateway reader admits
   ordinary requests into a byte-bounded FIFO (8 waiting requests, one worker).
   Probe and flow controls are processed by the reader. Queue rejection means
   `accepted=false`, never a fabricated successful mutation. Existing supervised
   accepted work and receipt semantics remain in place after transport loss.
3. **Original read lease, original page.** Gaps schedule a coalesced in-place
   reconcile. Live state, persisted history, task metadata and approval status
   use their own authoritative reads. A read during which another gap occurs
   cannot prove the later gap repaired. Session identity, stream generation and
   read/consumer revisions fence old work. A bounded overflow becomes explicit
   dirty state, not a truncated replay declared complete.
4. **Bounded consumption flow (candidate-only).** A slow client pauses its own
   replayable session events. Other connections progress. Dirty intent replaces
   unlimited pending deltas. No consumer, parsing success or elapsed 100ms alone
   can ACK an event. The 100ms observation requests a version-fenced recovery;
   explicit domain responsibility or application is required before credit is
   returned. Unknown session B is never acknowledged by successful recovery of A.
5. **Quiet UX.** Recovery keeps the component, draft and route mounted. One
   nonblocking notice appears after two seconds; a blocked operation explains
   itself immediately. There is no new offline auto-send queue, manual recovery
   dependency, `restart`, page reload or SQLite migration in this feature.

## Negotiation and rollout

`transport.probe.v1` enables nonce-based two-way application probes. Its absence
uses the compatible legacy probe. Protocol-level `websockets` keepalive is
explicit: ping 20s, timeout 120s, inbound frame maximum 25MiB. Authenticated
application idle eviction defaults to disabled. Foreground/system-resume signals
grant grace and inspect the existing socket; they do not themselves retire it.

The server switch is **off by default**:

```text
OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED=true
```

Enable it only in an isolated candidate environment until compatibility and
long-duration gates pass. Both peers must negotiate `transport.flow.v1` and
support the new methods. Existing clients receive the existing event format.
Turning the switch off affects newly established connections and does not
remove request isolation or sustained reconnect. With flow disabled, the legacy
512-frame resource protection can still close an overloaded client; the
candidate flow improvement must not be claimed as the default release behavior.

## Wire and memory contract

| Limit | Value / scope |
| --- | --- |
| Ordinary request backlog | 8, plus encoded-byte admission |
| Writer queue | 512, with bounded control reserve |
| Unacknowledged session-event window | 128 frames or 4MiB |
| Encoded buffer accounting | 50MiB per connection, 256MiB per Gateway |
| Legal large event | One at a time when the ordinary window is empty |
| Snapshot | 25MiB, 192KiB raw segments, one frozen transfer per connection |
| Snapshot expiration | 120s idle |
| Snapshot encoding | Bounded frozen JSON tree; incremental escaping/encoding with yields |
| Snapshot traversal safety | At most 250,000 nodes and depth 128 |
| Delivery confirmation | At 32 completions or 50ms from the first pending ACK |

Accounting covers encoded queue, delivery and snapshot buffers, not total RSS,
parsed object graphs, history caches or model runtime memory. Those must be
measured independently in the soak gates. Frozen-tree capture is synchronous
and bounded; subsequent encoding yields instead of whole-snapshot `JSON.stringify`
followed by slicing. Excessive snapshots fail locally and preserve the page.

`transport.flow.update` uses a connection-specific delivery epoch and cumulative
`ack_delivery_id`. `staged_delivery_ids` contains at most one separately confirmed
snapshot segment. This independent recovery credit avoids deadlock behind an
unowned ordinary event. A segment is validated and either staged or explicitly
discarded by an obsolete read before confirmation; it does not advance the
semantic stream cursor. The client retains a bounded confirmation queue if an
ACK reply is lost. Validated same-generation orphan snapshot replies also return
their credit, without installing abandoned content.

`sessions.messages.snapshot.read` captures one immutable snapshot and reads its
segments using `key`, `snapshot_id`, `sync_revision` and `segment_index`. Only
complete validated installation can request `resume`. Each update carries at
most one resume, so current persistent session identity is checked immediately
before applying the installation with no intervening await. Same-lease retries
retain their own identity and are idempotent, including after another session
uses the transfer slot. A reset/delete-recreate at stream cursor zero cannot
validate an old snapshot.

Global dirty state has a revision per active server read lease. Every affected
lease must install an appropriate snapshot (or be legitimately unsubscribed)
before the barrier clears. Frontend recovery expands its real read-admission
registry and asks each owner for explicit coverage. Only successful remote
unsubscribe creates bounded retired-read responsibility; a new read always
bootstraps and invalidates the retired record.

Old snapshot RPC remains available. An old Gateway can use the existing snapshot
and read contracts without understanding consumption feedback. Ordinary RPC
response envelopes have no new delivery-ACK protocol. Side-effecting sends keep
their existing client request IDs and receipt reconciliation; flow retry never
replays such a send.

## Reproducible validation

`RpcClient` emits bounded metadata-only `_transport` observations: phase,
connection generation, retry attempt, close reason/initiator and loop-lag
measurements. Valid Hello reports `recoveryMs` from the first confirmed failure,
without resetting it on each retry. Initial startup/user wait is not silently
classified as recovery. Flow diagnostics expose frozen queue/confirmation
counts, not keys, delivery epochs, credentials or message content. These are
observations, not permission to terminate an otherwise healthy connection.

All commands run from the repository root unless a directory is specified.
Use the isolated development environment, never a real profile.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_gateway/test_connection_stability_socket.py tests/test_gateway/test_transport_flow.py tests/test_gateway/test_websocket_connection_stability.py tests/test_gateway/test_snapshot_transfer.py tests/test_gateway/test_snapshot_transfer_rpc.py -q
.venv/Scripts/python.exe scripts/contracts/generate_gateway_contracts.py --check-determinism --jobs 4
```

From `opensquilla-webui`: `npm run test:unit`, `npm run typecheck`, `npm run build`.

Full contract gates also include `npm run test:contract-tooling`, the complete
`tests/contracts` suite with `OPENSQUILLA_RUN_CONTRACT_TOOLCHAIN_INTEGRATION=1`,
and separate production/verification deterministic generation. The combined
policy contains 201 targets / 218 production roles and 886 verification roles.
Generating clean bytes alone does not prove the profile/count tests passed.

The browser recovery matrix runs from `opensquilla-webui`, once for each
explicit `OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED=false/true` value:

```powershell
npx playwright test assistant-activity.spec.ts composer-paste.spec.ts history-hydration.spec.ts session-created-card.spec.ts session-switch-transport.spec.ts new-task-ensemble-race.spec.ts goal-mode.spec.ts plan-questionnaire-lifecycle.spec.ts queue-steer.spec.ts share.spec.ts --project=chromium --workers=2 --retries=0
```

Run its managed production Gateway with isolated state/config, HOME,
USERPROFILE, APPDATA and LOCALAPPDATA, filtering inherited credentials and
OpenSquilla overrides. Use a short temporary output path on Windows to avoid
fixture setup exceeding MAX_PATH. The isolated deterministic Goal Gateway
forwards only the normalized flow flag and asserts the actual Hello policy;
continuation/lifecycle cases also require consumption feedback when enabled.
Legacy mocked peers intentionally do not negotiate flow in either run.

From `desktop/electron` (after building WebUI and `npm run build`):

```powershell
node scripts/test-desktop-window-background-flow.mjs
node scripts/test-desktop-window-background-flow.mjs --connection-faults
node scripts/test-desktop-window-background-flow.mjs --connection-faults --flow-control
node scripts/test-desktop-window-background-flow.mjs --connection-faults --outage-ms=120000
node scripts/test-desktop-window-background-flow.mjs --connection-faults --flow-control --outage-ms=120000
node scripts/test-desktop-window-background-flow.mjs --connection-faults --flow-control --outage-ms=600000
```

The native fixture creates a disposable keyless profile and isolates HOME,
USERPROFILE, APPDATA and LOCALAPPDATA. It filters inherited credentials and
OpenSquilla attachment/profile overrides. It checks draft, focus, renderer and
healthy-socket continuity across simulated resume, minimizing and tray return.
Optional routed-WebSocket faults close the measured transport and reject
connections temporarily; recovery must be automatic. A setup-only reload
installs test interception; no reload is allowed during the measured recovery.
The fixture does not suspend the operator's computer or alter system networking.

The real socket fixture uses loopback Uvicorn with the production reader,
dispatcher and Python WebSocket peer. Unit counter tests are not presented as
Windows packaged evidence, nor as proof of final business content in all domains.

### Recorded development evidence (2026-09-09)

- After the upstream integration: 441 WebUI unit-test files / 5,689 cases passed;
  TypeScript checking and the production build passed. The combined Gateway,
  existing session/guest, snapshot, flow, diagnostics, real-socket and RPC
  architecture regression passed 461 cases. Six upstream Uvicorn/websockets
  deprecation warnings remain visible; they are not suppressed.
- Windows shard discovery/governance passed and includes the new connection
  tests; it is not a substitute for running the entire Windows CI matrix.
- Full generated-contract check passed. Full independent generation determinism
  passed before the final one-resume constraint; that changed contract was then
  independently regenerated twice and the complete tree checked again.
- Native Windows Electron **source/development** client, negotiated flow enabled:

| Injected unavailable interval | Backoff attempts during fault | Signal-to-operation available |
| --- | --- | --- |
| 5 seconds | 3 | 221ms |
| 2 minutes | 14 | 204ms |
| 10 minutes | 59 | 218ms |

Each run preserved the same page, original composer, unsent draft and focus;
healthy resume did not close the shared socket. These are single-run samples on
an empty keyless test conversation, **not P95 measurements**, model-execution
continuity proof, real network-adapter failure or packaged-candidate soak proof.
They were collected before the final upstream rebuild. After the final upstream
rebuild, the 5-second fault fixture passed again with negotiated flow enabled
(3 backoff attempts, 217ms signal-to-operation available) and disabled
(4 backoff attempts, 216ms). Both final runs preserved page/composer identity,
draft and focus, retained a healthy socket on resume, and passed minimize/tray
return checks. No real user profile or working conversation was used.

### Upstream integration validation (2026-09-10)

Local environment: Windows, Python 3.12.13, Node 24.15.0. The repository pins
Node 22.12.0 for CI; these local runs do not substitute for that exact CI runtime.

This integration deliberately combines #1585 validation with the stability
client rather than choosing either complete `rpc.ts`. Strict Hello fixtures
now cover local `auth:none` ownership, explicit-token rejection, generation
replacement during callbacks, malformed-handshake recovery, and both flow modes.
Malformed `sessions.list` responses preserve the existing complete directory,
leave pagination retryable, and cannot retire the shared RPC connection.
Router replay tests also combine buffered recovery with per-attempt card identity.

The browser gate exposed a real history-only recovery race: publishing the
reactive error phase before storing its failure result could wake the retry
watcher with no retryable evidence, leaving the session stalled. A regression
test failed before the fix and passes after result-before-phase publication;
no socket transition or unrelated live update is required to resume history.
Old browser fixtures were also updated to provide valid history/approval data
and assert automatic, same-socket recovery instead of timeout-driven redial or
manual retry. Their transcript, draft, focus and send-readiness checks remain.

The earlier development pass did not exercise every contract profile/count
gate. Several assertions still described the old inventory. This integration
corrects those exact counts, explicitly checks the new flow/snapshot roles,
and runs the complete tooling rather than treating generation as sufficient.

- Full WebUI suite: 441 files / 5,760 tests passed with `--maxWorkers=4`,
  including a complete rerun after the history recovery race fix.
  The initial higher-concurrency run exposed two new-test assertion mistakes
  (corrected to the actual `SessionDirectoryError` contract) and two unchanged
  Channels-view timeouts. The final complete rerun kept all assertions and
  default test deadlines; no product timeout was increased.
- Complete Python contract suite with the real Python/TypeScript/Ajv toolchain:
  405 passed / 18 existing conditional skips. Seventeen require Windows symlink
  privileges unavailable here; one parameterized case lacks a result fixture.
- JavaScript contract tooling: 16 passed. Production `--check-determinism
  --jobs 4` and isolated verification `--write-determinism --jobs 4` passed.
  Full-profile comparison covered 886 roles, with 218 production roles compared
  over 115,837 inputs and positive seeds for every role. This finite corpus is
  not a formal equivalence proof. Windows hash manifests cover 1,091 production
  artifacts and 1,147 verification artifacts; no Linux cross-OS comparison ran.
- Gateway/session/guest/snapshot/flow/real-WebSocket and RPC architecture
  regression: 461 passed, with six visible upstream deprecation warnings.
- Complete eight-spec Chromium recovery matrix: flow OFF 58/58 passed (54.9s),
  flow ON 58/58 passed (57.0s), both with two workers and zero test retries.
  All three real Goal cases asserted the negotiated policy; continuation and
  lifecycle also asserted actual `transport.flow.update` traffic when enabled.
  Legacy mocked peers remain compatibility-path coverage, not 58 negotiated-flow
  scenarios. Both final runs used identical frozen fixtures and build.
  Earlier fixture failures were corrected at their causes: required approval
  fields, Windows output-path length, and cursor-driven history pagination with
  UI/anchor readiness before the next reader scroll. All 320 seeded rows, bounded
  pages, transcript, draft, focus and automatic recovery assertions remain.
- Full production WebUI build, Electron TypeScript build, focused Ruff and
  whitespace/conflict checks passed.

That production WebUI build (including the history-only recovery fix)
also passed four native Windows Electron source/development recovery runs:

| Negotiated flow | Injected unavailable interval | Backoff attempts during fault | Signal-to-operation available |
| --- | --- | --- | --- |
| On | 5 seconds | 3 | 217ms |
| Off | 5 seconds | 4 | 216ms |
| On | 2 minutes | 14 | 218ms |
| Off | 2 minutes | 14 | 219ms |

All four kept renderer/page/composer identity, the unsent draft and focus;
healthy resume retained the socket, and minimize, tray return and deep-link
activation retained the original window. Both modes asserted actual negotiated
policy, not only an environment flag. These are isolated keyless profiles and
routed transport faults, not real working conversations or physical Windows
sleep. The four timings are individual samples after the recovery signal,
not outage-detection measurements, a P95 distribution, or packaged evidence.

### PR preparation against #1597 (2026-09-10)

The next pinned upstream is `9486415af7188335d9f18f45143bfe33ce97e890`.
Its questionnaire and Plan lifecycle changes are additive to connection
recovery, not a reason to replace the transport implementation:

- In-place recovery supplies the completed snapshot's cursor as the lower
  bound for pending-input hydration. A legacy peer without snapshot support
  retains the confirmed subscription bound. The parameter stays required,
  and the Goal cursor remains a separate domain.
- A questionnaire arriving while metadata is in flight survives a late empty
  response. A subsequent newer authoritative empty snapshot can still expire
  an old questionnaire. Session, epoch and stream fencing remain in effect.
- `onTaskSettled` updates the owning Plan alongside `onRecoveryRequired`.
  Buffered terminal events are applied only after their recovery completes;
  superseded recovery and old-epoch events cannot settle the current task.
- The combined schema retains `USER_INPUT_EXPIRED` with `accepted=false` and
  `retryable=false`. All generated merge conflicts are resolved by generation
  from that schema, not by selecting one branch's generated artifacts.
- Browser recovery coverage now includes the questionnaire lifecycle spec in
  both flow modes. All profiles remain isolated and credential-free.

Validation on this merged implementation:

- Full WebUI unit suite: 442 files / 5,841 tests passed with four workers,
  including a complete final rerun after replacing a test-only `Array.at()`
  call with indexed access for the repository's existing TypeScript target.
  No compiler target or test timeout was relaxed.
- Gateway/session/snapshot/flow/real-WebSocket regressions, RPC and CI workflow
  architecture checks, Plan and clarification RPCs, and mid-turn input tests:
  601 passed. Six upstream Uvicorn/websockets deprecation warnings remain.
- Complete Python contract suite with the real toolchain enabled: 405 passed,
  18 unchanged conditional skips (17 Windows symlink-privilege limitations,
  one existing parameter case without a result fixture). JavaScript tooling:
  16 passed. Both production and verification deterministic generation passed;
  finite differential validation covered all 886 roles, including 218 production
  roles compared over 115,837 inputs, with no role missing a positive seed.
- WebUI production build, artifact verification/staging, Electron TypeScript
  build, focused Ruff and whitespace/conflict checks passed.

### Draft-navigation update from #1602

Upstream `d44e7936be0f60c82a854cbeb8a66b491f395fec` was incorporated during
final PR preparation. Its draft transition retires the previous session before
optional project hydration, while retaining the existing recovery-generation
and draft-hydration fences. It changes no transport schemas or Gateway runtime
code. The final browser matrix also includes its New Task/Ensemble race case.

The full frontend suite passed again on this source (442 files / 5,841 tests),
and the updated CI workflow tests passed 79 cases. Byte comparison confirms
the Gateway/engine and all contract source/generated trees are unchanged from
the #1597 validation above; its 601 backend and 405 contract results therefore
cover the same runtime/contract code. The added browser fixture uses canonical
`session.event.thinking` and the questionnaire fixture uses `task.timeout`,
matching actual Gateway events; no production validator was broadened to admit
the mistyped fixture aliases.

## Release gates still required

- Windows packaged candidate installation and 72-hour soak; 24-hour development
  soak. Run against isolated profiles only.
- Actual lock/sleep/wake across Windows power states (not just a resume signal).
- Browser and packaged long-output matrix: 10,000 deltas with text/tool/approval/
  terminal equality, one fast and one paused consumer, replay overflow and maximum
  snapshots. Measure parsed/cache memory as well as encoded accounting.
- Repeated warm-recovery distribution: separately record outage discovery and
  signal-to-usable recovery; P95 <=5s is a target, not a conclusion from one run.
- Old/new peer combinations, authenticated token rotation and profile boundaries
  in packaged builds before enabling flow in a release.

Do not use a continuously green connection indicator as an acceptance result.
