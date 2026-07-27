# L3 Approved Write Execution Design

## Problem

Destructive shell and Python operations are classified as high impact and
therefore run under `L3-locked`. L3 correctly starts with a read-only file
system, but the current approval continuation reruns an approved operation
inside the same read-only policy. The approval succeeds while the requested
delete still fails with `Operation not permitted`.

## Decision

Keep the existing operation classification, security levels, run modes, and
platform sandbox backends unchanged.

When an L3 tool operation pauses for human approval and the user approves it,
resume that exact original tool call through the existing one-shot writable
host-execution path. This is the same execution capability already used by
approved sandbox elevation retries.

The override applies only to the resumed call. It does not change the session's
run mode, the global sandbox preference, later tool calls, or any sandbox
backend configuration.

## Authorization Binding

The approval must remain bound to:

- approval ID;
- session key;
- action fingerprint;
- working directory;
- action kind;
- original command or code arguments;
- the critical environment digest carried by the sandbox request.

The legacy L3 approval gate holds the original tool coroutine while the user
decides, so the authority is returned only to that in-flight call and is never
serialized into model-visible arguments. The approval is consumed before the
side effect starts and cannot be reused. Changed calls, changed sessions,
missing approvals, rejected approvals, and already-consumed approvals fail
closed.

## Execution Flow

1. The original L3 call reaches the existing approval gate.
2. The gate records the request and holds the original tool coroutine while the
   existing approval card is shown.
3. Rejection returns an approval-denied result without executing the call.
4. Approval returns a process-local grant containing the approval ID, request
   fingerprint, and L3 level to the waiting call.
5. The gate atomically consumes the approval before returning the grant.
6. The exact original shell command or Python code executes once through the
   existing host-execution path.
7. The result is delivered under the original tool call, without a second
   approval card.

## User Experience

- Standard mode still asks before an L3 destructive operation.
- One approval produces one execution attempt.
- Approving a delete allows the delete to complete in the workspace, `/tmp`,
  or the explicitly requested user path.
- Denying it leaves files unchanged.
- Trusted and Full mode selection remains unchanged.
- The user does not receive a second elevation prompt after approving the L3
  action.

## Tests

Tests must be written before production changes and cover:

- an approved Standard-mode L3 shell delete executes once;
- an approved Standard-mode L3 Python delete executes once;
- rejection produces no side effect;
- a changed request fingerprint or session cannot reuse the approval;
- the approval cannot execute twice;
- the resumed result replaces the pending result under the same tool call;
- only one WebUI approval card is emitted;
- existing Trusted and Full behavior remains unchanged;
- a real macOS Seatbelt test deletes only uniquely named temporary test paths
  after approval.

## Non-goals

- Redesigning risk classification;
- separating risk and isolation levels;
- changing L1, L2, or L3 baseline permissions;
- changing Linux, macOS, or Windows sandbox backend policies;
- adding persistent write grants;
- adding a new WebUI approval type.
