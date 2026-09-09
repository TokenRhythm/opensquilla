# Client connection stability

Implementation started from `bbd0c429e106babeeec70a6c6d493b563696d576`
(`origin/main` pinned by the approved 2026-09-09 plan).

The local integration branch subsequently merged fetched `origin/main`
`e4eb36b2deb8dcbb6572aa9a310492a28c8c01da` (image previews and archived
experiment cleanup). Upstream functionality is retained; this change has not
been pushed or published. Most existing generated-contract diffs are generator
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
.venv/Scripts/python.exe scripts/contracts/generate_gateway_contracts.py --check
.venv/Scripts/python.exe scripts/contracts/generate_gateway_contracts.py --verify-determinism
```

From `opensquilla-webui`: `npm run test:unit`, `npm run typecheck`, `npm run build`.

From `desktop/electron` (after building WebUI and `npm run build`):

```powershell
node scripts/test-desktop-window-background-flow.mjs
node scripts/test-desktop-window-background-flow.mjs --connection-faults --flow-control
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
