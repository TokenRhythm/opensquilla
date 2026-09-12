# Windows signed installer rehearsal

The Windows x64 installer handoff is enabled by default in builds containing
the activation change. `OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL=0` disables it
and restores the manual Show installer action. The previous opt-in value `1`
continues to work. Restart the client after changing the environment variable.
It keeps the full installer download and requires
the normal checksum, signature, cache, and shutdown gates before installation.
The existing `OPENSQUILLA_DESKTOP_ENABLE_WIN_UPDATE` switch exercises a separate
electron-updater path and must remain off during this rehearsal.

## Merge scope and default activation

The intended default Windows experience is **Check -> Download and verify ->
Quit and install -> visible NSIS wizard -> launch the installed version**.
PR #1584 introduced the implementation with the entry **off by default**.
Its update and lifecycle contracts, one signed native A-to-B cached-input flow
with retained-profile interactions, and PR/merge-queue CI do not certify general
availability or the entire release matrix.

Default activation is delivered by follow-up PR #1606 under the bounded
acceptance decision below. Record source SHAs, signed artifact hashes,
installation mode, network conditions and outcomes separately from CI results.
The earlier experimental merge alone did not authorize signing,
public channel changes, or a release. The
manual Show installer action remains available as a secondary action when the
handoff is enabled and as the primary action for shells without that capability.

### Activation acceptance scope

The maintainer waived Windows 10 native acceptance for this activation on
2026-09-10. Windows 10 remains in the supported Windows x64 scope; this waiver
does not establish a tested Windows 10 upgrade. Record Windows 10 as
**not tested / maintainer waived**,
never as passed or as an unsupported platform.

The maintainer subsequently authorized merging after the checks feasible on
the existing Windows 11 host, with unavailable native scenarios recorded as
unverified rather than blocking this activation. This supersedes the earlier
requirement to keep PR #1606 in Draft until every matrix cell passes; it does
not turn any missing evidence into a pass or waive required repository CI.

Host evidence includes the signed cached A-to-B custom per-user upgrade and
retained-profile interactions from PR #1584, three default-on/opt-out Electron
UI/lifecycle scenarios, 12 real Authenticode/cache checks, a direct signed NSIS
`--updated` cancellation before installation, and a real-registry refusal for
an executable outside the registered installation. The Electron scenarios use
explicit signature/registry and installer/Gateway fixtures. Direct NSIS
cancellation is not cancellation after application handoff or UAC cancellation.

Still unverified: Windows 10 native operation; a clean Windows certificate
cache; OS-enforced GitHub blocking with complete remote discovery/download;
the full default/custom path and per-user/per-machine matrix; ordinary-user
UAC acceptance/cancellation; and all post-handoff cancellation/retained-profile
matrix cells. Do not clear the daily-use host's certificate caches or alter its
firewall merely to make those cells appear complete. The fuller matrix below
remains the follow-up acceptance specification; its original default-activation
gates are deferred by this recorded maintainer decision.

The default-on source change alone is not evidence of a signed final release.
CI fixtures and warm-cache checks cannot substitute for the missing native
results. Existing installed clients are unaffected
until a build containing the activation is installed; the first upgrade from
an older manual client still follows that older client's UI.

## Development checks

From `desktop/electron`, build once and run:

```text
npm run build
node scripts/test-windows-update-security.mjs
node scripts/test-windows-update-cache.mjs
node scripts/test-windows-update-handoff.mjs
node scripts/test-windows-update-coordinator.mjs
node scripts/test-windows-update-integration.mjs
node scripts/test-windows-update-refresh.mjs
node scripts/test-windows-update-network.mjs
```

The ordinary `desktop-static` CI lane runs these deterministic contracts.
Windows `desktop-recovery-e2e` ownership cells also run the security and handoff
scripts so their Windows-specific verification and registry checks execute on
Windows. The handoff contracts also launch the real Node executable with
`--updated` (an unsupported argument that exits with code 9), and exercise a
real missing-executable spawn error. These checks prove OS spawn/error handling;
they do not run NSIS, sign a release, or install OpenSquilla.

Run `tests/test_ci/test_upgrade_baselines.py`,
`tests/test_ci/test_windows_signed_update_audit.py`, and
`tests/test_ci/test_plan_ci.py` when changing the rehearsal or CI routing.
The driver contracts use an in-memory desktop bridge and synthetic cache bytes.
The PowerShell orchestration contracts replace OS/process boundaries in a
temporary copy. They check dispatch, refusal, evidence scope and probe arguments;
they do not establish that a signed installer works.

### Local Windows process and signature checks

Install the WebUI dependencies as well as the Electron dependencies before the
rendered lifecycle test. From `desktop/electron`, run:

```powershell
npm run build
node scripts/test-windows-update-electron.mjs --output-dir "$env:TEMP\opensquilla-electron-evidence"
node scripts/test-windows-update-authenticode-local.mjs --installer C:\signed-audit\OpenSquilla-0.5.4-win-x64.exe --evidence-dir "$env:TEMP\opensquilla-signature-evidence"
```

The Electron fixture compiles the current production update components and uses
the production preload, extracted update IPC handlers and quit callback inside
a real Electron process. It checks failure recovery, repeated actions and quit
ordering with real child processes. Signature and installation-identity results
are injected; a Node process represents an owned Gateway, and the harmless Node
`--updated` launch represents the installer. This is process integration
evidence, not a packaged Gateway or NSIS upgrade. The Windows ownership CI cells
run this fixture and retain its reports.

The separate Authenticode check requires an existing signed installer and a new
evidence directory inside the OS temporary directory. It calls the production
Windows verifier and cache code, including a fresh-process cache read and
rejection of altered file copies. It
does not sign files, start installers, or use a verification bypass. A historical
signed installer can validate these file checks; it cannot stand in for baseline
A containing the new updater. Keep the report's source and artifact hashes with
the UI screenshots when recording local results.

Cached manual installers do not block update discovery. A check revalidates and
reuses the cache when the channel still advertises the same candidate. A changed
or withdrawn candidate clears the old ready path and persisted cache record;
the installer bytes are not executed or deleted. If discovery fails, a cache
that passes verification remains available, with a check error for an explicit
request. Download, reveal, installation and lifecycle ownership still prevent
concurrent candidate replacement. The refresh regression runs with the handoff
switch set to `0` so the emergency manual path is covered too.

### Restricted networks

The network regression runs the production discovery, source probing,
checksum fetching, streaming download, hashing, and cache flow against a
controlled transport. It rejects all non-OSS requests, including GitHub API
and release downloads. It covers both OSS-first operation and fallback from
GitHub, missing checksum metadata, changed bytes, and offline cache reuse.
Windows signature results are an explicit test seam in this deterministic
test; a passing result does not prove offline Authenticode verification.

The signed native download audit also accepts `DownloadSourceMode: "github-to-oss"`
in its JSON configuration (or `-DownloadSourceMode github-to-oss` when invoking
the signed helper directly). This mode launches the installed client with GitHub
as its preferred asset source and requires both discovery and the verified
download to report OSS with `fallbackUsed: true`. It refuses cached-input mode;
legacy manual and macOS drivers retain their original OSS-only assertions.

The audit still serves a controlled loopback channel and uses production asset
URLs. Its `sourceFallbackVerified` result proves application fallback, not that
GitHub was blocked at the OS network layer: an HTTP error can also cause fallback.
`networkIsolationVerified` and `remotePublicationVerified` remain false. Attach
independent guest network controls and negative/positive connection probes before
crediting the GitHub-unreachable native cell. Default remote channel discovery
requires its own evidence without a channel-root override. Neither this mode nor
a passing driver contract opens the default activation gate.

For the remaining native acceptance, test a disposable Windows 11 environment with GitHub
unreachable while the complete OSS release is reachable. Include the channel
JSON, `latest.yml`, installer, and `SHA256SUMS`; OSS promotion must occur only
after all versioned assets and checksums have been uploaded and read back.
Complete discovery, download, OS signature verification, cache reuse after
restart, and the visible installer handoff under that restriction.

Separately test a fresh Windows certificate cache with certificate-distribution
and revocation endpoints unreachable, then with normal access. Record the
system signature status, verifier elapsed time, user-visible error, retained
download, and retry outcome. Do not clear the host certificate cache or change
the host firewall for this test. The production verifier uses Windows system
trust and a bounded PowerShell process; it does not promise a network-free
certificate-chain check. Verification unavailable must remain distinct from
successful verification, and retry must not abandon the client or its data.
These checks also gate the new signature verification on the manual download
path: leaving Quit and install disabled does not exclude that path.

## Existing users: retain the manual upgrade gates

Official 0.5.3 and 0.5.4 clients do not contain the experimental handoff. Keep
their `manual` discovery/download/installer tests and the existing
`0.5.3 / 0.5.4` by `default / custom` installation matrix. Do not relabel the
historical installers as signed or replace their published bytes.

The first upgrade into a client with the new capability still uses the old
client's manual installer path. A separate signed A-to-B rehearsal proves the
new client's own update path.

## Signed A-to-B handoff

Use a disposable Windows account, or an explicitly authorized host whose
installation records have been checked and native Desktop profile is absent.
Keep other source checkouts and CLI/portable profiles outside the audit.
Preinstall a
previously verified signed baseline A that contains both the new shell and the
new update UI; stop A before this audit and leave the account's native
`AppData\Roaming\@opensquilla\desktop-electron` directory absent. This is
Electron's packaged application name, not the NSIS display name `OpenSquilla`.
The empty namespace parent `AppData\Roaming\@opensquilla` must already exist
in the disposable account; the audit does not create or move an existing profile.
Launch the audit from an ordinary desktop PowerShell shell opened outside
Codex or another MSIX app. Before plan creation, evidence writes, signature
cache warming, or profile seeding, the audit requires the launcher package API
to return `APPMODEL_ERROR_NO_PACKAGE` (15700). It then uses the same resolved
Node executable as the driver to check that the existing Roaming parent is
not redirected and the native OpenSquilla directory is absent. A detected
package identity or unexpected API/path failure is refused before seeding.
Those read-only checks alone cannot establish the write view. A second preflight
creates two new UUID siblings under Roaming through Node and the same frozen
Python executable used for seeding. They exchange nonce markers and must see
native paths. The probe checks actual paths, identities and marker contents
before deleting only its own markers and empty directories. Unexpected files
or uncertain cleanup preserve those paths and fail before evidence creation,
signature warming or seeding. It never creates or reuses `OpenSquilla`.
Passing establishes only those fresh writes in that launch context; future
redirection remains possible. The final cache guard still refuses redirected
paths. Launching outside the packaged app remains a prerequisite.
For ordinary-user/UAC cells, select `ProcessObservationMode:
standard-user-polling` and run the audit without elevation. This mode queries
the observed B process's path, command line and creation time; it refuses an
elevated launcher and does not claim a complete process-event trace. The
default `cim-trace` mode still requires `Win32_ProcessStartTrace` access and
fails before profile creation when subscription is denied. Elevating that
mode also elevates A; it cannot prove an ordinary-user UAC transition.
Prepare a newer signed stable candidate B and a rehearsal channel manifest
from its immutable release assets. Both must satisfy the production Windows
signing policy. No test certificate or verification bypass is accepted by the
client.

`HandoffInputMode` defaults to `download`, which retains the real candidate
download assertions below. The optional `verified-cache` mode instead stages
the pinned, locally available signed B artifact through the production cache
verifier, then checks two actual A cache restores, normal Quit/restart and the
visible installer handoff. See the [cached handoff fixture instructions](../desktop/electron/scripts/fixtures/packaged-cached-handoff/README.md)
for its prerequisites and evidence fields. It records `downloadVerified: false`
and `remotePublicationVerified: false`; it does not prove remote checksum
fetching, automatic refresh completion, GitHub/OSS fallback or cold-certificate
behavior. Both modes keep the aggregate native release gate closed.

Record build source SHAs, A's installed main EXE SHA256, B's installer SHA256,
and the baseline installation directory from the original signed artifacts.
The existing Windows upgrade entry accepts an independent signed parameter set.
Prepare a JSON configuration with exactly these fields (values below are
placeholders, not an executable release recipe):

```json
{
  "InstallRoot": "C:\\signed-audit\\installed-A",
  "UserDataDir": "C:\\Users\\audit-user\\AppData\\Roaming\\OpenSquilla",
  "EvidenceRoot": "C:\\Users\\audit-user\\AppData\\Local\\Temp\\signed-update-evidence",
  "BaselineVersion": "0.5.6",
  "BaselineExecutableSha256": "<64 lowercase hex characters>",
  "BaselineSourceSha": "<40 lowercase hex characters>",
  "CandidateInstaller": "C:\\signed-audit\\OpenSquilla-0.5.7-win-x64.exe",
  "CandidateInstallerSha256": "<64 lowercase hex characters>",
  "CandidateSourceSha": "<40 lowercase hex characters>",
  "ChannelManifest": "C:\\signed-audit\\channel.json",
  "ProcessObservationMode": "standard-user-polling",
  "InstallTimeoutSeconds": 600
}
```

From the repository root, run this only when a native installation audit is
authorized and an operator is present to complete the wizard:

```powershell
& .github/scripts/verify-release-windows-upgrade.ps1 `
  -SignedAuditConfigPath $absoluteConfigPath
```

The versions must be distinct canonical stable versions with B newer than A.
The script records supplied source provenance; local artifact hashes and the
production signature policy are verified independently. It cannot infer or
attest source identity from a binary. This mode rejects 0.5.3/0.5.4; the old
manual parameter set keeps its original baseline validation and behavior.

An approved branch can build Windows-only internal A/B artifacts through
`Release Assets` with an empty `tag` and `internal_windows_only: true`.
This keeps Web UI verification, production Windows signing and the original
four legacy upgrade cells, while skipping Python/macOS packaging and aggregate
publication. It does not publish a release or update OSS channels. Tagged
releases reject this option before signing. The existing signing environment's
branch restrictions and reviewer approval still apply; record each actual
run/source SHA and artifact hash separately from the PR CI result.

`EvidenceRoot` must be absent and inside the process temporary directory
(`RUNNER_TEMP`, otherwise the OS temporary directory). The profile must be the
account's actual native AppData directory and must not exist. The script seeds
only synthetic state there. It does not assume environment redirection or
`--user-data-dir` survives the NSIS shell-broker restart. Existing profiles are
refused; it does not delete them or clean up the installation afterward.

After the launcher checks, the outer helper verifies A's version/hash and A/B production signatures,
then launches the driver and observes process events or polls for B according
to the selected mode. This preflight warms certificate caches; a cold-cache
network cell requires a separate clean environment before any such verification.
Keep an
operator present throughout. Complete NSIS with Run OpenSquilla selected; do
not launch B manually. After a new B main process with `--updated` is observed,
confirm `FINISH-AUTOLAUNCH` only if B started from that Finish action. Electron
renderer/GPU processes are excluded. NSIS uses `ExecShellAsUser`, so B need not
have the installer as its parent; the result labels Finish causality as
operator-attested and leaves `automaticRestartVerified` false.

Next use B's actual tray Quit and confirm `QUIT`. The audit waits for B and its
captured descendants to exit, binding each PID to its creation time. Timeout
or a failed process query fails and preserves the scene; it never force-kills
them. `normalQuitObserved`
is scoped to that operator action and captured-process exit observation.

In the default `download` mode, the driver serves the selected channel manifest on loopback. Installer
downloads still use the manifest's versioned release source and the real
client's checks. The first discovery receives a controlled 503; the same
client must remain running and successfully retry. The driver then requires
`installMode: manual`, `canInstall: true`, and cached installer bytes matching
B's pinned SHA256. It then clicks the rendered update indicator and the
“Quit and install” action through Playwright. It disables mock updates and the
legacy native-update switch.

The result deliberately contains `ok: false`, `stage: installer-handoff`, and
`requiresPostInstallVerification: true`. A closed A process is not proof that B
installed or restarted. Preserve the reported `desktopLog` and installer
handoff diagnostics. The outer audit adds separately scoped postinstall evidence:

- B exists at A's original installation directory, has the exact expected PE
  product version, and passes the installer/installed executable signature
  checks, including Gateway, elevation helper, and uninstaller.
- `test-packaged-first-send-renderer.mjs` launches installed B with a separate
  absent `EvidenceRoot/first-send-new-profile`, verifying first send and owned
  Gateway readiness only for that new profile, using its synthetic provider.
- `test-packaged-retained-interaction.mjs` checks the credential hash captured
  before A's handoff, visits the original seeded sessions, and creates a new
  session for first send, a required `read_file` with an unpredictable sentinel,
  real UI Stop, a follow-up send, normal Quit and same-profile restart. It never
  rewrites the credential/config to make preservation pass. Three calls to the
  `verify-signed-retained` checker retain the exact old-history and external
  sentinel assertions. This signed-audit operation accepts only the exact seed
  config or the exact known migration; legacy manual checks are unchanged.
  Its independent report is bound to this audit ID,
  source SHA and installed B hash; every required proof must be JSON `true`.
  See the [probe contract](../desktop/electron/scripts/fixtures/packaged-retained-interaction/README.md).

Even if every automated stage passes, the outer script returns **2** and writes
`stage: postinstall-verified-with-gaps`, `ok: false`, and
`releaseGatePassed: false`. A refusal/failure returns **1**. The retained probe
can establish interaction on the upgraded synthetic profile with a loopback
provider; it does not establish NSIS/UAC cancellation, the full OS/path/scope
matrix, cold certificate-network behavior, machine-proven Finish causality,
or uninstall preservation. The former injected WebSocket recovery check is
also separate. Record the outstanding native release gates before enabling the feature by
default; do not reinterpret driver exit 0 or outer exit 2 as release success.

Run this handoff for default and custom baseline installation directories.
Reuse the original signed Release Assets artifact through the existing
Desktop Fault Injection workflow when an approved packaged audit is needed;
do not rebuild or re-sign solely to rerun verification.

## Release proof still required

### Native observations on September 10, 2026

Signed A `0.5.9001` (`d7a73311713a06ebd0c2b3f4de901e6ed0d34d0f`)
and B `0.5.9002` (`22e36dc491f35c16d7c4152c549a6e9fb81fbc2a`)
were exercised on Windows 11 in an existing per-user, non-default installation.
The production cache restored across two A launches. The rendered Quit and
install action stopped A and its owned Gateway and launched visible NSIS with
only `--updated`. NSIS detected the original installation; Finish/Run launched
B with the correct default profile, retained synthetic history and a connected
Gateway. Installed signatures and ordinary tray Quit were verified. A separate
fresh-profile first-send probe passed on the installed B.

The earlier attempt did **not complete its native acceptance cell**. Its failures
remain preserved, with the following findings:

- The outer run blocked in a lab stdin relay: redirected PowerShell suppressed
  the `Read-Host` prompt the relay expected. Subsequent checks were independently
  bound continuations, not a rewritten successful outer result.
- A deeply nested fresh test profile generated a 281-character ownership-file
  path and failed to start its packaged Gateway. The same first-send probe
  passed using a shorter evidence root. Long-profile-path support is unproven.
- The retained profile kept its exact seed config. Requiring a migrated config
  falsely rejected preservation; the signed-only checker now allows either
  exact fixture form while retaining all byte/hash/history checks.
- Retained first send then failed before reaching the synthetic Ollama service:
  its default 8K context budget could not accommodate the agent/tool request.
  Prepare adequate model capacity in the **next A baseline**, then rerun A-to-B;
  do not rewrite an upgraded profile or bypass request budgets to claim success.
  These observations alone did not verify retained tool use, Stop or restart.

The follow-up harness now seeds adequate synthetic model capacity before A,
emits explicit operator prompts even with redirected stdin, and recognizes the
production per-turn time context. An independent installed-B preflight then
passed first send, real sentinel `read_file`, actual Stop/stream cancellation,
follow-up and normal Quit. One attempt's restart was blocked by another
workspace's same-named CLI process; that process was preserved. A fresh retry
with no process conflict completed both launches, restored old and new history,
sent a message after restart, quit normally twice, and passed final config,
credential and profile checks (`os1584-cap-b720e1df`). This fresh synthetic
preflight is not upgraded-profile or integrated A-to-B evidence.

A later uninterrupted cached-input A-to-B run with audit code `2c40fa4c7`
completed the visible NSIS upgrade in the existing Windows 11 per-user custom
directory. Finish/Run launched B, the user used tray Quit, and the same outer
audit then passed fresh first send, retained first send, real sentinel tool
execution, Stop/stream cancellation, follow-up, same-profile restart and exact
credential/config/history preservation. The retained interaction report is
`ok: true`; the outer report intentionally remains `ok: false`, exits **2**,
and records `postinstall-verified-with-gaps` and `releaseGatePassed: false`.
Shell-brokered Finish/Run causality remains operator-attested. No rollback,
uninstall, remote-download or source-fallback claim is added.

Immutable local evidence digests (private profiles and logs are not committed):

- Outer result SHA256: `72e06f29d24499c471f8d2bf754bb14755f9fb9af01e400d69be0f321353f1c1`.
- Retained interaction SHA256: `8534a88295a47c66528a788fabefcbec8a39b9d62589b547e0fb458783c57560`.

The user's original profile has been restored with all 4913 files hash-verified;
the separate backup remains preserved.
Local evidence and private profile data are not repository artifacts. This
cached-input run does not verify download, public channels, blocked GitHub,
cold certificate-chain networking, UAC/cancellation, or the full OS/scope matrix.

The following native matrix remains required before default activation;
deterministic CI or the synthetic
PowerShell harness does not mark any row complete. Retain evidence per cell,
including source SHAs, artifact hashes, installation roots, logs and outcome.

| Native cell | Required observation |
| --- | --- |
| Legacy 0.5.3 / 0.5.4 × default / custom (four cells) | Original manual discovery/download/install assertions pass and retained profile survives. |
| Windows 10 / 11 × per-user default / custom path containing Chinese characters and spaces | Signed A-to-B GUI handoff, exact B version/signatures, Finish attestation, tray Quit, later retained-profile launch and data preservation. |
| Existing per-machine install and UAC | Correct installation root and privilege transition; no second per-user installation is silently created. |
| Conflicting per-user and per-machine installation records | Handoff is refused, the client remains usable and the manual installer entry remains available. |
| NSIS or UAC cancellation | No installed/restarted success claim; the client does not automatically launch the old version to disguise cancellation. Record the manual recovery outcome. |
| Invalid/untrusted signature or unavailable verification tool | No executable is handed off; refusal evidence identifies the verification failure. |
| Failure before handoff (download, cache, quiesce or spawn) | The existing client returns to a usable state where supported; no active writer is abandoned and profile state is preserved. |
| Installed B with failed Gateway readiness, stale version or retained-profile first send/tool/Stop failure | Release gate stays closed; preserve failure evidence and record the operator recovery steps. |

The experimental switch must not become the default until signed A-to-B
installation, restart, usability, preservation, and refusal-path evidence is
available. Test download interruption/corruption, cached-file replacement,
untrusted signer, installer launch refusal, locked installation, repeated
install/Quit, Gateway drain failure, stale installed version, and failed
post-install Gateway startup. Record a recovery outcome rather than marking
the update complete when installation or readiness is unverified.

The existing NSIS installer is not transactional after the old uninstaller
starts. Disk exhaustion, power loss, and extraction failure in that window do
not have an automatic rollback guarantee. A successful handoff test does not
close that gap or prove data-schema rollback compatibility. Consumer Windows
10/11 UAC, Defender, and SmartScreen behavior also needs disposable native
machine evidence beyond hosted Windows Server CI.
