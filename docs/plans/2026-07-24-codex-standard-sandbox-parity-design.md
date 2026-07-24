# Codex Standard Sandbox Parity Design

Date: 2026-07-24

## Goal

Make OpenSquilla standard mode behave like Codex workspace-write mode:

- the host filesystem is readable subject to the host OS and explicit deny rules;
- the selected workspace and declared writable roots are writable;
- writes outside those roots require one approval;
- the original tool call waits at the approval boundary and resumes exactly once;
- Windows never tries to make a drive root readable by mutating its ACL;
- infrastructure failures are reported once and are not converted into repeated approval or fallback attempts.

## Current failure

The current Windows path model says that standard mode is host-readable, but its Windows
special-root expansion enumerates selected directories instead of expressing a full read
baseline. Reads on another drive therefore become `sandbox_path` approvals.

After approval, the selected path is added as a read-only mount. The Windows ACL compiler
then treats broad read entries such as `D:\` as capability ACL grants. Applying that grant
requires changing the drive-root ACL and fails with `ERROR_ACCESS_DENIED`. The approval has
already resolved, so the user sees an approved request followed by a tool failure. The model
then tries alternative tools and produces a visible failure storm.

The filesystem worker also lacks a stable user-home environment in some restricted launches,
and the global Windows ACL execution lease can surface a low-level deadlock error after an
interrupted or overlapping run.

## Codex reference behavior

Codex represents workspace-write as a root read rule plus specific write roots. Its Windows
restricted-token backend requires the policy to retain filesystem-root read access, projects
only writable roots into Windows capability grants, and keeps an approval request connected
to the in-flight call through a one-shot continuation.

OpenSquilla will preserve its existing queue persistence for UI and cross-process delivery,
but the queue remains a decision channel, not a replacement for the suspended call.

## Design

### Permission model

`FileSystemPermissionProfile.workspace()` and `.read_only()` will express Windows
`host_root_readonly=True` as an implicit `READ` default. Specific `WRITE`, `READ`, and `DENY`
entries continue to override that baseline by longest-path precedence.

On POSIX, the existing explicit `/` read entry remains unchanged so mount compilers continue
to receive the representation they already support.

Consequences:

- `C:\...`, `D:\...`, and other local paths resolve to `READ` without an approval;
- configured deny-read paths still resolve to `DENY`;
- the workspace resolves to `WRITE`;
- protected metadata under writable roots remains read-only;
- host OS permissions are still authoritative, so unreadable system objects fail normally.

### Windows ACL compilation

An implicit full-disk read baseline does not generate ACL grants. The restricted process keeps
the host identity's existing read access. Capability ACL grants are reserved for explicit
writable roots and narrowly required runtime paths.

As defense in depth, the compiler will reject/skip read-only ACL grants whose target is a
filesystem root. A drive root must never appear in `autoGrants`.

Explicit deny-read and deny-write carve-outs remain enforced through the existing deny ACL
desired-state layer.

### Approval lifecycle

The lifecycle remains:

1. execute the original tool call;
2. if an out-of-policy write needs approval, emit one approval event;
3. keep the turn and original `ToolCall` suspended;
4. receive the decision;
5. on approval, attach the approval continuation and execute the same object once;
6. consume exact elevation authority before the side effect;
7. send only the final tool result back to the model.

Read operations in standard mode never enter this lifecycle.

A denial returns one canonical denial result. A timeout leaves the turn suspended/expired
without asking the model to improvise a fallback.

### Worker environment and execution lease

Every Windows worker launch will carry a valid `USERPROFILE`, `HOME`, `HOMEDRIVE`, and
`HOMEPATH` derived from the configured host profile when the restricted environment omits
them. This prevents `Path.home()` from failing during worker imports.

The Windows execution lease will use bounded non-blocking acquisition with a clear
`SandboxBackendError` on contention. Cancellation and process exit must always release the
handle. Raw `EDEADLK` errors must not reach tool results.

### Failure presentation

Backend setup/ACL/lease failures are infrastructure failures, not permission denials. They
must:

- produce one concise diagnostic result;
- set `retry_allowed=false` unless the exact failure is classified as a sandbox denial with a
  supported escalation path;
- never create a second path approval for the same read;
- never instruct the model to cycle through `list_dir`, `read_file`, `glob_search`, and shell.

## Verification

Automated coverage must prove:

- a Windows standard profile reads arbitrary paths on multiple drives without approval;
- its workspace is writable and an outside path is not writable;
- explicit denied reads still win;
- drive roots never appear in Windows ACL auto-grants;
- approved outside writes resume the same call exactly once and consume authority;
- denied and expired approvals do not execute;
- filesystem workers always have a usable home;
- lease contention returns a bounded typed failure and recovers on the next call;
- backend infrastructure errors are non-retryable and do not trigger an approval loop.

The final runtime check will use the source gateway in standard mode to read `D:\lrk`, then
perform an approved write to a disposable directory outside the selected workspace and verify
that the operation completes after a single approval.
