# OpenSquilla Web Guest / Windows Sandbox Security Review

Date: 2026-07-31
Scope: local branch `codex/web-guest-sandbox-local-upgrade`

## Result

The reviewed boundary now treats an unauthenticated or invalid-token remote Web
caller as Guest Safe mode using only server-derived authority. Desktop and valid
token callers retain their configured permissions. No known guest-to-host
execution or sensitive-path read bypass remains in the reviewed paths.

## Findings fixed

1. **Client-controlled source spoofing (high)**
   Guest classification previously also consulted `_source.caller_kind`.
   Classification now relies only on the authenticated server principal.

2. **Workspace junction aliases (high)**
   Windows path matching now resolves concrete junction/symlink targets before
   applying sensitive-path and authority-root rules.

3. **Retargeted guest scratch root (high)**
   `.opensquilla-guest` is rejected when its canonical location differs from
   the configured default workspace. RPC callers receive
   `GUEST_DEFAULT_WORKSPACE_UNSAFE`.

4. **Approval and host-escalation bypasses (critical)**
   Guest path requests cannot become temporary mounts through an approval ID,
   low-risk auto-mount, or `sandbox_permissions=require_escalated`. Guest Safe
   is also pinned to Safe mode when a caller forges Full mode or when the
   sandbox backend becomes unavailable.

5. **Anonymous approval authority (high)**
   Remote no-token/invalid-token callers no longer receive the global approvals
   scope. Debug mode does not upgrade transport-derived guest authority.

6. **Windows setup marker false positive (high)**
   Readiness now verifies the actual offline identity, persistent ACL state, and
   proxy boundary. A current marker with a stale password no longer reports
   Ready.

7. **Locked/stale offline account after an old install (high)**
   Automatic setup rotates the account password, unlocks the dedicated local
   account, and persists the matching DPAPI credential before ACL hardening.

8. **Legacy empty ACL / incomplete setup repair (high)**
   ACL repair now establishes the trusted root ACL before resetting children,
   checks all persistent state files, and fails on `icacls` errors instead of
   continuing past them.

9. **Cancellation-triggered restart loop (high availability)**
   A cancelled ACL transaction no longer tears down and rebuilds every retained
   read grant. Stale write grants are still revoked; retained read grants remain
   constrained by the request-specific restricting capability SIDs.

10. **Protected parent `.git` launch failure (medium)**
    Optional Git safe-directory discovery treats an inaccessible parent marker
    as absent instead of aborting the sandboxed process.

## Verification evidence

- Python focused security regression: **455 passed, 3 skipped**
- Desktop startup contract: **107 passed**
- Web UI unit tests: **2551 passed**
- Web UI architecture, i18n, and TypeScript checks: **passed**
- Native Windows process smoke: **4 passed**
- Native Windows boundary smoke: **9 passed**
- Ruff on all modified Python files: **passed**
- Web dependency audit: **0 vulnerabilities**
- Desktop production dependency audit: **0 vulnerabilities**
- Live Windows support probe:
  `setup_ready=true`, `identity_ready=true`, `storage_ready=true`,
  `proxy_allowlist_enforced=true`

## Build-only dependency residual

The full desktop development audit reports 16 high-severity transitive findings
under the latest stable `electron-builder` dependency tree. They are confined
to build tooling (`@electron/asar`, macOS DMG/universal tooling, Squirrel
tooling, glob/minimatch/ejs chains) and are absent from the production
dependency audit. `npm audit --omit=dev` reports zero vulnerabilities. No
risky downgrade or incompatible override was applied.
