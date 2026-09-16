# Signed cached installer handoff audit

This opt-in native mode tests an installed signed A restoring a locally staged,
signed B installer, quitting and restarting A with the same synthetic profile,
and using the actual renderer **Quit and install** action to open NSIS. It does
not change the installed A/B program bytes or the production updater.

Use the existing `.github/scripts/verify-release-windows-upgrade.ps1` entry
point with `-SignedAuditConfigPath <absolute JSON>`. Keep all existing signed
audit fields and set:

```json
{
  "HandoffInputMode": "verified-cache",
  "ProcessObservationMode": "standard-user-polling"
}
```

The excerpt is not a complete audit config. A must already be installed and
stopped. Pin its installed `OpenSquilla.exe` SHA256, both full build source SHAs,
the canonical B installer path/SHA256 and the local B channel manifest. Evidence
must be a new directory inside system Temp. The account's native
`AppData/Roaming/@opensquilla/desktop-electron` must not exist; NSIS Finish does not inherit a
Playwright `--user-data-dir`. The outer audit creates the synthetic retained
profile and a hash-bound `cached-handoff-audit.json` ownership marker. Run from
an ordinary desktop PowerShell shell opened outside Codex or another MSIX app,
without elevation for standard-user polling, with the repository Python on
PATH, `src` on PYTHONPATH, and the Electron harness built/dependencies installed.
The empty `AppData/Roaming/@opensquilla` namespace parent must already exist.
The native path follows Electron's packaged `name`, not NSIS `productName`.
An absent `AppData/Roaming/OpenSquilla` is not evidence that the actual native
profile is absent. Existing native profiles are refused before any seeding.
Before creating evidence, warming signature caches, or seeding the profile,
the outer audit requires `GetCurrentPackageFullName` to return
`APPMODEL_ERROR_NO_PACKAGE` (15700). The same resolved Node executable used for
the driver must see an existing, unredirected Roaming parent and an absent
native OpenSquilla directory. A detected package identity or unexpected API/path
failure stops before those writes. Package identity and the parent's realpath
alone cannot establish the write view: a second preflight creates two new UUID
siblings under Roaming, one each through Node and the frozen Python executable
also used for seeding. They exchange fixed nonce markers and must observe native
paths. Cleanup rechecks actual paths, file identities and nonce content, then
unlinks only each probe's marker and removes its empty directory. Unexpected
contents, redirection destinations, or uncertain cleanup preserve the probe
paths and fail before seeding. It never creates or reuses `OpenSquilla`.
Passing establishes only those fresh writes in that launch context; future
redirection remains possible. The final canonical cache-path guard remains
unchanged and can still refuse the newly seeded path.

The external harness validates the fixture through the production channel
parser, checks that B is newer and canonical, and verifies the Actions artifact
hash. It exclusively copies B into the synthetic profile's new
`update-downloads` directory. The default production cache verifier performs
SHA256 and real system PowerShell Authenticode, publisher, thumbprint and
timestamp checks before a canonical descriptor is persisted. Native staging
accepts no verification overrides. The signed application subsequently verifies
the same file when restoring the cache and before handing it to NSIS.

The driver serves only a loopback channel returning 503 if requested in this
mode. This cell does not assert that automatic refresh ran or completed, and
does not count as a network-failure test. It does not publish or impersonate a
public release. The first A process must show
a connected owned Gateway, restore the exact B cache, and Quit naturally with
clean Gateway/committed-exit evidence. A then launches again without rewriting
credentials. The second actual Electron PID and Gateway start identity must
differ; credential bytes and B cache identity must remain unchanged. A pending
verification is observed for a bounded time; an actual signature/integrity
error is preserved immediately, with no Download or verifier retry.

Only after those assertions does the real UI action initiate the normal signed
handoff. The harness never directly spawns the B installer. NSIS remains visible;
scope, directory, UAC, cancellation and Finish/Run must be observed separately.
Before clicking, the signed driver captures the actual Electron, Windows
Playwright wrapper, owned Gateway and any separate Gateway launcher identities.
This synthetic cell requires exactly one Gateway launch in the current A's log
segment; extra/restarted launches are rejected rather than omitted. A single
180-second deadline covers the UI click and independent exit/log observation.
The wrapper must exit naturally with code zero, every captured owned identity
must disappear (or be demonstrably replaced by a different creation identity),
and the new log suffix must contain this attempt's ordered verification, drain,
installer commit and matching B handoff records. Unknown or inaccessible
processes, recovered attempts, replaced logs and ordinary Quit records fail.

Playwright 1.60 on Windows emits ElectronApplication `close` through the wrapper's
ChildProcess `close`, which also waits for pipe EOF. A still-open NSIS wizard can
retain inherited handles after A and its wrapper have exited. Therefore this
event is diagnostic only. The driver first writes the independent handoff proof,
then releases only its own stdio references to the exited wrapper and waits at
most five seconds for transport cleanup. The handoff observer never invokes a
second Quit after requesting installation or kills a process tree, and never
counts closing its own streams as evidence of process exit or completed
installation. A real ChildProcess `close` tracker is registered immediately
after each managed launch; closed streams alone cannot stand in for the event
that removes Playwright's exit hook.

An unsuccessful signed audit writes `handoff.json.failure.json` with its original
error, `ok: false`, `handoffObserved: false`, and `operatorQuitRequired: true`.
The driver remains resident with a five-second observation interval until every
fixed process identity exits and its own transport actually closes. It does not
retry an already requested Quit, call a second `app.close`, terminate a process,
or change Playwright's hooks. Query errors, failed evidence writes, and late
transport errors cannot unwind this fence into Node exit. A failed wrapper exit
can be safely cleaned up after all recorded identities disappear, but retains
the original failure and never becomes a successful handoff.

An authenticated recovery Gateway is appended to the fixed identities; reused
PIDs do not replace prior records. Missing original identities or an unpinned
recovery launcher set `manualDiagnosisRequired: true` and
`automaticReleaseAvailable: false`. In that case, do not close the driver
terminal or retry: an operator must diagnose the identity gap, and normal Quit
alone cannot automatically clear it. This protection starts after Playwright
returns a managed app; Playwright's internal launch-initialization failure path
can still terminate its child and remains a separate dependency boundary.
The existing outer audit still verifies installed B, normal Quit, a fresh-profile
first-send probe and the retained-profile message/tool/Stop/Quit/restart probe.

`handoff.json` identifies `mode: signed-cached-handoff`, the local-fixture source,
source and file hashes, and the two actual A process observations. It explicitly
records **`downloadVerified: false` and `remotePublicationVerified: false`**.
The `.cache.json` sidecar and screenshots preserve intermediate states. Failure
evidence is separate from any already-written handoff observation; neither
record proves that the installer completed. The contract suite covers held
transport, missing/unknown/reused identities, failed writes and safe residence
using inert fixtures. Its inert Node grandchild does not reproduce NSIS handle
inheritance on Windows; native NSIS and failed-handoff interaction still need
their own acceptance evidence.

A successfully completed cell proves cache restoration and the real native
installation/interaction path. It cannot certify a prior real download, remote SHA256SUMS retrieval,
public channel publication, GitHub/OSS fallback or cold CA network behavior.
The signature preflight warms certificate caches. Existing download mode
(`HandoffInputMode: download`, the default), `signed-handoff`, and legacy manual
download assertions remain distinct. One successful native cell still exits 2
with the aggregate release gate closed.

Ordinary CI runs only `test-packaged-cached-handoff-contract.mjs`: inert temporary
bytes, production candidate/descriptor parsing, marker/hash/path validation,
exclusive copying, and pure observation assertions. Those tests do not stage a
ready unsigned installer, launch Electron/NSIS, or claim native acceptance.
An inert Node child/grandchild case exercises transport cleanup after a natural
OS exit; Node's Windows stdio behavior does not reproduce NSIS's inherited
native handles. The held-stream branch is covered separately with a controlled
event emitter, and the native signed A-to-B run remains the acceptance gate.
