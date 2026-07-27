# Global Run Mode and Active Session Lock

## Goal

Make the run-mode picker behave as one backend-authoritative global default
without changing the authority of work that has already been accepted.

- An idle session shows the latest global default.
- Changing the picker in any idle session persists the new global default on
  the backend and synchronizes other connected browser clients.
- An active session keeps the mode that was accepted at the start of its active
  work epoch. Its picker is visibly disabled.
- Queued turns, approval waits, spawned subagents, background completion, and
  parent synthesis keep that same accepted mode until the session is
  authoritatively idle.
- When the session becomes idle, it immediately unlocks and shows the latest
  global default.

## Definitions

`global default` is the backend-persisted owner preference. Browser
`localStorage` is only a startup cache and may never overwrite a newer backend
value during hydration.

`active work epoch` starts when an idle session accepts a turn. It remains
active while any of the following is true:

- a task is queued or running;
- an approval is pending inside that task;
- an accepted queued message is waiting;
- a background subagent completion group is waiting;
- child work has completed but parent synthesis is pending or running.

The epoch ends only when the existing authoritative task and task-group state
reports no active work.

## Architecture

### Backend global preference

Add a singleton key/value row in session storage for the global run mode. The
initial fallback is the configured run mode. Two owner-facing RPC methods
provide the preference:

- `sandbox.run_mode.preference.get`
- `sandbox.run_mode.preference.set`

The setter validates the requested mode, verifies any required sandbox setup,
persists it, and broadcasts
`sandbox.run_mode.preference.changed`. Reads and event payloads are coerced to
the requesting principal's allowed modes; non-owners cannot set the global
preference.

### Accepted task authority

`AcceptedRunModeOverride` remains the execution authority. Every accepted task
stores a serializable run-mode snapshot in its durable task details. Runtime
queue entries keep the typed override. Background completion already captures
the parent override before task eviction and must continue passing it to the
parent synthesis task.

The request payload is not treated as mutable route authority after
acceptance. It is converted once into the accepted override, then copied
through runtime-owned fields.

### Session lock projection

The backend projects a `run_mode_lock` in
`sessions.messages.subscribe`. If an active task exists, the lock mode comes
from its durable accepted snapshot. If a background completion group is the
remaining active work, the lock comes from the manager's captured parent
override. The persisted session run context is a compatibility fallback for
work accepted before durable task snapshots existed.

The Web UI also captures the selected global mode as soon as a local send
starts, preventing a transient unlock before subscription/task events arrive.
The backend projection replaces that optimistic value during hydration.

### Frontend preference and lock

The run-mode composable exposes:

- cached/backend global mode;
- asynchronous hydration from the backend;
- backend persistence for user changes;
- application of broadcast preference changes.

`ChatView` computes the displayed mode:

1. the active epoch's locked mode when the session is active;
2. otherwise the global preference.

The existing authoritative activity predicate is shared by Stop-button logic
and run-mode locking. While locked, the picker button is greyed, cannot open,
and explains that a running task cannot change mode. Once authoritative idle
is observed, the local lock is cleared and the displayed mode automatically
falls through to the current global preference.

## Data Flow

### Idle selection

1. User chooses a mode in an idle session.
2. UI calls `sandbox.run_mode.preference.set`.
3. Backend validates and persists the mode.
4. Backend broadcasts the changed preference.
5. Every idle client updates immediately; active clients cache the new global
   value without changing their displayed or effective locked value.

### Idle to active

1. UI sends the current global mode with the turn.
2. Backend creates the immutable accepted override and persists it in session
   origin and task details.
3. UI captures that mode as its optimistic lock.
4. Subscription hydration/events confirm the active state and authoritative
   lock.

### Active work and completion

1. Additional accepted queued input uses the locked displayed mode.
2. Runtime follow-ups and subagent completion synthesis receive the original
   accepted override directly.
3. A global preference change elsewhere does not alter this session's lock.
4. After tasks and background groups are terminal, authoritative idle clears
   the lock and the UI displays the newest global preference.

## Error Handling

- Invalid modes produce a validation error and are not persisted.
- Full Host Access remains owner-only.
- Restricted modes require a ready sandbox setup, using the existing setup
  validation and recovery error contract.
- A failed preference write leaves the last confirmed global value selected and
  displays the existing error toast.
- A failed preference hydration keeps the local cache for availability, then
  retries on the next WebSocket reconnect.
- Missing legacy task snapshots fall back to the session's persisted run
  context; they never invent a more privileged mode.

## Upgrade and Compatibility

- Existing session run contexts remain readable.
- Existing active tasks without a task-detail snapshot use the session-origin
  fallback.
- Existing browser `localStorage` values seed first paint only.
- The new preference table is created idempotently and requires no destructive
  migration.
- Existing RPC clients may continue sending `_source.runMode`.

## Verification

Backend tests must prove:

- global preference persistence survives a new storage instance;
- owner set/get and non-owner coercion/denial;
- a change broadcasts once after persistence;
- accepted task details contain the immutable mode snapshot;
- active subscription returns a lock from a task;
- background-only activity returns the captured parent lock;
- parent synthesis and queued runtime work retain the accepted override.

Frontend tests must prove:

- backend hydration supersedes local cache;
- backend writes update cache only after success;
- broadcasts update the global value;
- an active session displays its locked mode despite global changes;
- the picker is disabled and cannot emit changes while locked;
- authoritative idle reveals the latest global default;
- reconnect refreshes the authoritative preference.

