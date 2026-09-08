# Windows signed installer rehearsal

The controlled Windows installer handoff is experimental and off by default.
`OPENSQUILLA_DESKTOP_ENABLE_WIN_INSTALL=1` enables it in a packaged client that
contains the implementation. It keeps the full installer download and requires
the normal checksum, signature, cache, and shutdown gates before installation.
The existing `OPENSQUILLA_DESKTOP_ENABLE_WIN_UPDATE` switch exercises a separate
electron-updater path and must remain off during this rehearsal.

## Development checks

From `desktop/electron`, build once and run:

```text
npm run build
node scripts/test-windows-update-security.mjs
node scripts/test-windows-update-cache.mjs
node scripts/test-windows-update-handoff.mjs
node scripts/test-windows-update-coordinator.mjs
node scripts/test-windows-update-integration.mjs
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

## Existing users: retain the manual upgrade gates

Official 0.5.3 and 0.5.4 clients do not contain the experimental handoff. Keep
their `manual` discovery/download/installer tests and the existing
`0.5.3 / 0.5.4` by `default / custom` installation matrix. Do not relabel the
historical installers as signed or replace their published bytes.

The first upgrade into a client with the new capability still uses the old
client's manual installer path. A separate signed A-to-B rehearsal proves the
new client's own update path.

## Signed A-to-B handoff

Use a disposable Windows account on a disposable Windows machine. Preinstall a
previously verified signed baseline A that contains both the new shell and the
new update UI; stop A before this audit and leave the account's native
`AppData\Roaming\OpenSquilla` directory absent.
The same disposable account must be permitted to subscribe to
`Win32_ProcessStartTrace`; on machines that deny it, use an elevated shell for
that same account. Do not switch to another administrator's profile. The script
registers the observer before seeding the profile and fails on access denial.
Prepare a newer signed stable candidate B and a rehearsal channel manifest
from its immutable release assets. Both must satisfy the production Windows
signing policy. No test certificate or verification bypass is accepted by the
client.

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

`EvidenceRoot` must be absent and inside the process temporary directory
(`RUNNER_TEMP`, otherwise the OS temporary directory). The profile must be the
account's actual native AppData directory and must not exist. The script seeds
only synthetic state there. It does not assume environment redirection or
`--user-data-dir` survives the NSIS shell-broker restart. Existing profiles are
refused; it does not delete them or clean up the installation afterward.

The outer helper first verifies A's version/hash and A/B production signatures,
then launches the driver and captures process starts continuously. Keep an
operator present throughout. Complete NSIS with Run OpenSquilla selected; do
not launch B manually. After a new B main process with `--updated` is observed,
confirm `FINISH-AUTOLAUNCH` only if B started from that Finish action. Electron
renderer/GPU processes are excluded. NSIS uses `ExecShellAsUser`, so B need not
have the installer as its parent; the result labels Finish causality as
operator-attested and leaves `automaticRestartVerified` false.

Next use B's actual tray Quit and confirm `QUIT`. The audit waits for B and its
captured descendants to exit, binding each PID to its creation time. Timeout
fails and preserves the scene; it never force-kills them. `normalQuitObserved`
is scoped to that operator action and captured-process exit observation.

The driver serves only the selected channel manifest on loopback. Installer
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
- `test-packaged-session-recovery.mjs` reopens the retained A/B profile with
  seed-matching session keys and label, then
  `verify-release-profile-preservation.py verify-runtime` checks the retained
  synthetic state and external sentinels.

Even if every automated stage passes, the outer script returns **2** and writes
`stage: postinstall-verified-with-gaps`, `ok: false`, and
`releaseGatePassed: false`. A refusal/failure returns **1**. The evidence does
not prove first send or a necessary tool call on the retained upgraded profile,
chat Stop/restart, machine-proven Finish causality, or uninstall preservation.
Record those separate native release gates before enabling the feature by
default; do not reinterpret driver exit 0 or outer exit 2 as release success.

Run this handoff for default and custom baseline installation directories.
Reuse the original signed Release Assets artifact through the existing
Desktop Fault Injection workflow when an approved packaged audit is needed;
do not rebuild or re-sign solely to rerun verification.

## Release proof still required

The following native matrix remains required; deterministic CI or the synthetic
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
