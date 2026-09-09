# Retained synthetic profile interaction probe

The native entry point is `../../test-packaged-retained-interaction.mjs`. It is
invoked by the signed-update acceptance audit after B has been installed and
the prior client has exited. It is not an installer or a signing gate. Ordinary
CI runs only `../../test-packaged-retained-interaction-contract.mjs`.

The native command, from `desktop/electron`, is:

```powershell
node scripts/test-packaged-retained-interaction.mjs --audit-manifest C:\audit-profile\retained-interaction-audit.json --output-dir C:\audit-evidence\retained-interaction
```

The outer audit must write a marker with `schemaVersion: 1`,
`purpose: "opensquilla-synthetic-signed-update-audit"`, a 32-character lowercase
hex `auditId`, `seedLabel`, absolute `userDataDir`, absolute `executablePath`,
stable `expectedVersion`, 40-character `sourceSha`, and SHA-256 hashes in
`executableSha256`, `credentialSha256`, and `configSha256`. It also supplies
`externalSentinelsDir` to retain the external tool sentinel checks. The marker
must reside directly in the selected synthetic userData directory. PowerShell
5.1 UTF-8 BOMs are accepted without normalizing the bytes being hashed.

The caller proves that the credential hash is unchanged from A before creating
this marker. The probe verifies that pinned hash before, between, and after its
launches; it cannot independently establish an earlier A hash. It never invokes
the shared launcher that writes synthetic credentials. The existing pinned
Ollama credential must have no secrets, use direct routing with a named audit
model, and point at an explicit `127.0.0.1` HTTP port. A port already in use
fails the run. Evidence must use a new directory separate from both the install
and profile directories. Redirected input paths and any running OpenSquilla
instance are rejected.

The probe invokes the unchanged Python `verify-runtime` preservation checker
before launch, between restarts, and after the final Quit, including
`--external-root` when supplied. Its exact 320-message old-session assertions
remain in force. Real rendered UI navigation also visits the two seeded
sessions, then creates an independent new session for these actions:

1. A first message receives an answer from the loopback synthetic provider.
2. The actual Gateway must execute `read_file` for a new random sentinel inside
   this synthetic workspace. The provider receives only the expected nonce
   hash, never the nonce itself, and accepts a matching tool result only.
3. The provider holds a response without sending a terminal chunk. The actual
   Stop button must issue exactly one task-scoped `chat.abort` with
   `source: webui_stop`; a matching cancelled task event and a provider stream
   close must arrive before fixture cleanup. A follow-up message must work.
4. Playwright calls the real Electron `app.quit()` path. Success requires a
   natural zero process exit, the identity-matched Gateway's exit, and desktop
   logs showing clean Gateway shutdown and a committed desktop exit without
   hard termination.
5. B starts again with the exact same profile/credentials. Both seeded sessions
   and the new session's prior answers must be visible. A new send succeeds,
   followed by another normal Quit and preservation check.

`report.json` begins with `ok: false, status: running`. Only all completed
assertions produce `ok: true, status: passed` and exit 0. The report includes
probe source hashes, pinned input hashes, actual process/Gateway identities,
observed RPC metadata, provider counters, and screenshot paths/hashes. It makes
no claim about NSIS, Authenticode, UAC, external providers, or the former
session-recovery script's injected WebSocket recovery scenarios.

On failure it records `ok: false, status: failed` and exits nonzero after
cleanup settles. It closes only its own provider and asks only its own observed
application for a normal Quit. If normal Quit cannot be proved, the report sets
`operatorQuitRequired`; the process may remain attached until the operator
quits that exact synthetic instance. No forced termination passes this gate.
The synthetic workspace sentinel and all evidence are preserved for inspection.

The contract test uses inert temporary executable bytes, a VM socket fixture,
and real loopback HTTP connections. Its tool-result input and connection abort
are controlled test inputs. Passing it proves auxiliary validation behavior,
not a packaged UI interaction or native A-to-B upgrade.
