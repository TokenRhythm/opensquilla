# Goal Mode

Goal mode gives one session a durable objective that can span multiple ordinary
agent turns. It is useful when the work is too large for a single response but
should stay tied to one outcome and one usage total.

Goal mode is an orchestration layer, not a second agent runtime. Every Goal-owned
turn still runs through the normal AgentTask, TaskRuntime, TurnRunner, sandbox,
approval, provider fallback, transcript, and usage-accounting contracts.

There is no fixed Goal phase sequence, hidden evaluator, or Goal-only execution
loop. The durable objective is added to an ordinary Default-mode turn. If that
turn settles without an explicit terminal Goal decision, the Goal remains
active; once the session becomes eligible through the shared idle gate, the
Gateway starts another ordinary Default-mode turn against the latest transcript,
workspace, external state, and Goal snapshot. The agent chooses the next useful
work from that current evidence rather than following preassigned turn stages.

## Start and manage a Goal

Use Goal mode from a Gateway-backed Web UI or terminal chat:

```text
/goal Prepare the release and verify every required check
/goal set Prepare the release and verify every required check
/goal status
/goal edit Prepare the release, verify every check, and update the changelog
/goal pause
/goal resume
/goal clear
```

`/goal <objective>` and `/goal set <objective>` are equivalent. An objective is
trimmed and must contain 1-4000 Unicode characters. The objective is also stored
as the real first user message for the first Goal turn. Running `/goal` with no
argument shows the current status when a Goal exists and command help otherwise.

Only one unfinished Goal can exist in a session. Starting another one returns a
conflict instead of silently replacing it. After a completed Goal, a new Goal can
start once the old owning task has fully settled and the session is idle.

The management commands control the durable Goal separately from the current
AgentTask:

- `edit` keeps the Goal identity, replaces its objective, and clears the old
  progress view. A running main-agent task adopts the latest objective at its
  next safe model boundary. If that task is already settling or its provider
  topology cannot accept an in-turn update, the next ordinary Goal turn uses
  the latest objective instead. Editing a completed Goal reactivates that same
  Goal: its identity, creation time, and lifetime usage remain, while its
  terminal result, progress view, and current guardrail window are reset.
- `pause` disables future automatic continuation. It does not cancel an already
  accepted task; that task continues and may still submit an explicit complete
  or blocked decision. Use the normal task Stop control when the current task
  itself must be cancelled.
- `resume` returns an unfinished paused, blocked, or usage-limited Goal to
  `active`. If its task still owns the Goal, that same task remains the owner and
  no duplicate task is created. Otherwise the shared idle gate starts work when
  the session is eligible; Plan mode or unrelated session work can defer that
  start without rejecting the state change. A resumed Goal starts a new
  guardrail window while retaining lifetime accounting.
- `clear` removes Goal tracking and revokes its execution lease. It does not
  cancel a current task or remove transcript entries and artifacts. Generation
  and Goal fences prevent the surviving task from recreating or mutating the
  cleared Goal. An objective edit that is still pending is revoked. Once the
  Agent has durably claimed an edit, however, that internal context may already
  be assembled into or sent with the current task's next provider request and
  is not recalled. Clear still prevents a late claim from becoming Goal tool
  authority; an edit applied before Clear remains only as task evidence after
  the Goal row is deleted. Use the normal task Stop control when the current
  task itself, including an edit it has already started processing, must halt.

The composer Stop control is the inverse boundary: it cancels only the current
AgentTask. When that cancellation leaves an unfinished Goal, the Goal pauses
with `user_cancelled` instead of silently starting another continuation.

## Status, progress, and completion

A Goal has one of five states:

| State | Meaning |
| --- | --- |
| `active` | Eligible for user work or automatic continuation. |
| `paused` | Unfinished, but automatic continuation is disabled until an explicit resume. |
| `blocked` | Unfinished; the agent reached a genuine impasse or the runtime recorded a terminal turn failure. Resolve the cause, then resume. |
| `usage_limited` | Unfinished; the provider classified the turn as usage-limited. Resolve the provider limit, then resume. |
| `complete` | Finished. This is the only completed state. |

The Goal ribbon and `/goal status` show the objective, execution state, progress,
turn counts, lifetime and current-window active time, token usage, reasons, and
guardrail state. `executionState` distinguishes idle, queued, and working tasks.
A deferred reason can explain that automatic work is waiting for user ingress,
other session work, or Plan mode.

During complex work, the main agent may replace the Goal progress checklist with
up to 20 structured steps when a concise status view is useful. A step is at
most 200 characters; the optional explanation is at most 1000 characters; at
most one step may be `in_progress`. The checklist is a dynamic projection of the
current work: the agent may merge, remove, reorder, or rewrite steps as the
evidence changes. It is not a phase state machine, does not assign work to
particular turns, and is not required for a Goal to proceed.

Before marking a Goal complete, the agent must audit the whole durable objective
and its referenced requirements against the current authoritative state. Tests,
artifact inspection, command results, and external checks should directly prove
each applicable acceptance condition. Missing, indirect, stale, contradictory,
or uncertain evidence means the Goal is not yet achieved. The agent keeps the
Goal active and continues useful work until every requirement is proven and no
required work remains, then submits the explicit complete decision. Assistant
text, artifact delivery, and a completed-looking checklist never complete a Goal
on their own.

An agent-authored blocked decision is similarly deliberate. The same blocking
condition must prevent meaningful progress for at least three consecutive Goal
turns, counting the turn where it first appears, and the agent must have
exhausted safe in-scope checks and alternatives before declaring a true impasse.
A difficult, slow, uncertain, or merely clarification-sensitive task is not by
itself blocked. This repeated-blocker audit does not replace the runtime's
system-authored handling of real provider, tool, cancellation, timeout, or usage
failures described below.

Literal text such as `[goal:complete]` has no control meaning and remains normal,
copyable assistant text.

## Automatic continuation and user priority

After a Goal-owned turn settles, the Gateway re-evaluates the session through
the shared idle admission gate. A continuation is accepted only when all of the
following are still true:

- the session generation, Goal identity, objective revision, and continuation
  sequence match;
- the Goal is active and has no owning task;
- the session is in Default mode and has no active manual Plan run;
- the execution lease is still valid and its owner is still subscribed and
  authorized;
- there is no explicit user ingress or other queued/running session work; and
- the current guardrail window still permits another turn.

Explicit user input wins every race with automatic work. A normal Default-mode
follow-up can claim the active Goal; if it was queued, the claim is revalidated
when the task actually activates. Plan, Review, subagent, cron, memory,
compaction, and system turns do not claim it.

Automatic continuations are system events. They do not invent a user transcript
row, create a Goal command receipt, or capture a fake user message into memory.
They still render assistant output, tools, approvals, and usage on connected
Web UI and CLI surfaces. If terminal input arrives while an external Goal turn
is active, the CLI first tries the normal steering path and falls back to a new
user turn only if the terminal race rejects the steer.

Goal mode does not retry a failed or timed-out whole turn. A tool may already
have performed an irreversible action, so replaying the turn could duplicate
side effects. Lower-level provider and core retries retain their normal safety
rules.

An automatic continuation is not a separate evaluation pass. It gives the same
main agent the durable objective and current state inside the ordinary Default
loop, where the agent performs the completion or blocker audit and either keeps
working or submits an explicit terminal Goal decision.

## Goal progress is not Plan mode

Goal and Plan are separate concepts:

- Goal is a durable objective carried across ordinary Default-mode turns; its
  optional progress checklist is only a lightweight current-state view.
- Plan mode is an interactive collaboration mode for proposing or revising a
  plan before implementation.

Updating Goal progress does not enter Plan mode, create a PlanRun, prescribe a
phase sequence, or force a turn boundary. If the user switches the session to
Plan mode, the Goal remains durable and active, but Goal-owned execution and
automatic continuation wait with the `plan_mode` deferred reason. User-authored
Plan interaction can still run normally. Returning to Default mode re-enters
the shared idle gate and accepts at most one eligible continuation.

The Goal ribbon remains visible in Plan mode so the persistent objective and its
waiting state are clear.

## Execution lease, disconnects, and restart

Starting or resuming a Goal gives the calling, subscribed Web UI or CLI
connection an in-memory execution lease. A read-only spectator does not acquire,
refresh, or inherit that lease. Authority, credentials, and route envelopes are
not stored in the Goal row; every automatic turn rebuilds and revalidates them
from the live connection.

If the owner disconnects or unsubscribes, the process-local lease detaches but
the durable Goal remains `active`. A running task may finish or report a
blocker, but no new automatic continuation is admitted while the lease is
detached. Status reports `owner_disconnected` as a continuation defer reason;
it does not misrepresent a transient transport loss as a user pause.

The Web UI keeps an opaque, process-only continuity token for the current
browser tab. After a refresh it first restores the authenticated message
subscription, then uses that token to reattach the lease without changing the
Goal revision or resetting its turn/runtime guardrail window. Ordinary
subscribe, status, and hydration calls remain read-only, and a spectator cannot
inherit the lease. If the tab-local token is no longer available, an authorized
operator may explicitly take over a detached lease from the Goal controls or
`/goal resume`; takeover is refused while a live owner is still attached.

Gateway shutdown or restart still pauses unfinished active Goals with
`process_restart`; restart never calls a provider to resume them. Reconnect and
explicitly resume after the Gateway itself has restarted.

Messaging channels can contribute ordinary Default-mode turns to an already
active Goal, but they cannot start or resume Goal execution and do not own a
lease. This keeps unattended channel traffic from authorizing continued token
spend.

Resetting a session rotates its generation, deletes the current Goal, and
revokes its lease. Deleting the session also deletes its Goal and command
receipts. Forked sessions do not inherit a Goal. These rules keep stale work
from the old session generation from controlling a new or copied conversation.

## Artifact delivery

Successful `publish_artifact` delivery remains terminal for ordinary turns. In
a Goal-owned turn, however, delivery is an ordinary tool result: the artifact
is immediately available while the agent continues through the normal tool
loop with the same route, safety policy, approvals, and provider fallback.
The agent may update structured progress and calls `update_goal` only when the
whole objective is complete or truly blocked. Structured progress remains
optional; an artifact or a completed-looking checklist alone never implies Goal
completion.

If a successful turn ends without a terminal Goal decision, the Goal remains
active and the existing idle gate admits at most one automatic continuation.
Previously persisted Goals paused with `goal_checkpoint_required` remain
readable for upgrade compatibility; explicitly resume them to continue under
the normal Goal loop without replaying an idempotent artifact delivery.

## Guardrails and usage

Configure Goal execution in `config.toml`:

```toml
[goal]
execution_enabled = true
max_turns = 50
runtime_budget_seconds = 3600
```

`max_turns` accepts 1-500. `runtime_budget_seconds` accepts 60-86400 seconds and
counts only the actual running duration of Goal-owned tasks. Queue time, pause
time, and Gateway downtime do not count.

Guardrails never kill a task in the middle. A structured complete or blocked
result wins first. If the Goal remains active after settlement, reaching a limit
pauses it with `turn_limit` or `runtime_limit`. A successful resume resets the
window turn and active-time counters; lifetime turn, active-time, and token totals
remain available for status and auditing.

Usage is settled once from the authoritative terminal task and usage ledger.
The Goal snapshot reports input, output, reasoning, cache-read, cache-write, and
total tokens. A provider usage-limit classification moves an otherwise active
Goal to `usage_limited`; an ordinary failure or timeout blocks it rather than
replaying the turn.

## Emergency stop and rollback

Set the kill switch and restart the Gateway to stop new Goal execution without
deleting Goal state:

```toml
[goal]
execution_enabled = false
```

At startup, active Goals transition to `paused` with `feature_disabled`; there
is no Goal retry timer or startup-wide auto-enqueue. Status, edit, pause, and
clear remain available. After re-enabling execution and restarting, reconnect
to the session and explicitly resume the Goal.

This kill-switch procedure is the supported non-destructive operational
rollback. Do not treat a binary downgrade as a database rollback: back up the
session database and follow the target release's migration guidance before
downgrading software.

Older experimental Goal settings such as idle nudges, blocked/failure retries,
retry backoff or polling, unattended continuation, and watcher TTL no longer
control behavior. Remove those keys from local configuration so the file does
not imply protections that are no longer part of the runtime contract.

The Goal schema is applied through the normal session-database migration path.
On upgrade, unfinished Goal execution is never resumed merely because persisted
state exists; an explicit resume and a fresh execution lease are required after
restart.

## RPC contract for clients

Gateway clients use the following session-scoped methods:

```text
goals.capabilities
goals.set
goals.status
goals.edit
goals.pause
goals.resume
goals.reattach
goals.clear
```

Every durable mutation carries a client-generated canonical UUID v4
`clientRequestId`. `goals.reattach` changes only process-local execution
authority, uses exact session-generation and Goal fences, and never writes a
command receipt or resets guardrail counters.
`goals.set` also carries a UUID v4 `clientMessageId`; edit, pause, resume, and
clear carry the last observed `expectedGoalId` and `expectedStateRevision`.
Repeating the same request identity and normalized payload returns the stored
response. Reusing it for a different payload returns `IDEMPOTENCY_CONFLICT`.

Accepted mutations return the request identity, session generation, task and
user-message identities when applicable, the previous Goal identity when
applicable, and the current Goal snapshot. Clients should merge that snapshot
directly instead of issuing a correctness poll.

Goal-specific conflict codes are stable:

```text
INVALID_GOAL_OBJECTIVE
GOAL_ACTIVE
GOAL_NOT_FOUND
GOAL_BUSY
STALE_GOAL
SESSION_GENERATION_CHANGED
PLAN_MODE_ACTIVE
PLAN_RUN_ACTIVE
EXECUTION_LEASE_REQUIRED
GOAL_NOT_RESUMABLE
GOAL_EXECUTION_DISABLED
IDEMPOTENCY_CONFLICT
```

When authorization permits, stale, active, and busy conflicts can include the
current snapshot so the UI can converge without another status request.

## Events, reconnects, and privacy-safe diagnostics

Clients converge from mutation responses, the hydration snapshot, and one
`session.event.goal` event stream. They do not rely on correctness polling or a
watcher heartbeat. Session generation, stream sequence, state revision, and
progress revision fences prevent an old hydrate, replay, task, or callback from
overwriting newer Goal state.

Goal diagnostics use bounded structured fields such as command action/outcome,
defer class, terminal state, guardrail class, turn counts, active duration, and
token counts. Objective text, progress text, blocked reasons, assistant output,
route authority, and credentials must not be included in Goal metric records.

When troubleshooting, capture `/goal status`, the session key, the non-sensitive
reason code, and the relevant Gateway log event name. Do not paste objectives or
transcripts into public issue reports unless they are synthetic or fully
redacted.

---

[Configuration](configuration.md#goal-mode-goal) · [Sessions](sessions.md) ·
[Web UI](web-ui.md) · [Terminal UI](tui.md) ·
[Usage and cost](usage-and-cost.md) · [Troubleshooting](troubleshooting.md)
