# Owner Project Default: Full Access

## Goal

Project workspaces default to Full Access with the sandbox disabled for
authenticated owners. This includes:

- the local Control UI owner;
- local CLI and TUI owner contexts;
- a remote channel sender, including Feishu, whose sender ID is configured as a
  channel administrator for that channel.

Remote channel users who are not channel administrators remain non-owners and
must not receive Full Access.

## Design

Use the backend run-mode policy as the single source of truth.

1. A bare configuration continues to resolve to Full Access.
2. `project_default_run_mode` returns that configured Full mode instead of
   replacing an implicit Full value with Standard.
3. Existing project sessions whose saved context contains implicit Full remain
   Full. Explicit user selections continue to take precedence.
4. Principal authorization remains unchanged. Existing routing and RPC policy
   code continues to coerce Full to an allowed non-owner mode.
5. The WebUI continues to consume the backend default instead of forcing a
   project-specific value in request payloads.

The sandbox capability may still be prepared so an owner can manually select a
restricted mode. Preparing that capability does not make it the project default.

## Upgrade Behavior

No database schema, migration, or persisted workspace row changes are required.
Old sessions without run-mode provenance retain the existing Full value instead
of being silently tightened to Standard after update. Workspace paths, project
bindings, transcripts, and user files are not rewritten.

## Verification

Regression coverage must prove:

- a new owner project defaults to Full Access;
- an existing implicit-Full owner project remains Full after update;
- an explicit owner selection remains authoritative;
- a Feishu channel administrator is treated as an owner and receives Full;
- an ordinary Feishu sender remains non-owner and cannot receive Full;
- the real project filesystem path executes through the host path in Full mode;
- frontend type checking and focused project/run-mode tests pass.
