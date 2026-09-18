# Plan collaboration

Plan mode helps clarify a request and prepare a versioned proposal. The same
Agent investigates the workspace, discusses choices and submits a formal plan
when one is useful. A normal discussion can finish without producing a new
revision. Revising a proposal preserves its earlier revisions in history.

Planning can include necessary tests, builds, commands and delegated
investigation. These operations use the normal tool permissions, approval and
sandbox policies. Plan mode communicates the intent to investigate and defer
implementation; it is not a guarantee that no files will be written. Subagents
inherit that intent and their parent's permission ceiling. The main task owns
formal proposals and Goal controls.

## Implement and adapt

Implementing approves a particular revision and starts an ordinary Default
Agent task. The task can reorder work, add verification, repair a failed check
and publish again after fixing an artifact. Changes in approach within the
user's authorization do not require another proposal approval. Actions outside
that authorization still require confirmation through the normal controls.

The proposal and current progress are separate. `update_plan` replaces an
optional task progress list of up to 20 steps. Steps can be added, removed,
reordered or reopened. Progress does not restrict tools, schedule future turns,
or determine whether a task may finish. A successful implementation completes
its associated run without inventing verification for unreported steps.

Artifact publication validates and publishes the artifact. It does not mark
plan steps complete or prevent subsequent inspection and repair.

## Hide, cancel and continue

- **Hide plan** hides the selected revision's default card. Its body and history
  remain available, and the preference survives refresh. Restore it from history
  to show the card again. A new revision has its own presentation preference.
- Hiding a proposal does not change collaboration mode or cancel a task. Active
  progress and Stop remain available separately.
- **Cancel execution** and composer **Stop** use the same ordinary task cancel
  path. Queued work can be cancelled before model execution; running work stops
  at the normal cancellation boundaries. The UI confirms cancellation after
  task termination is acknowledged.
- Reconnection first recovers the existing task. Retrying an implementation
  whose acceptance response was lost retains its request identity and destination
  rather than silently starting another task.
- Continuing terminated work preserves the conversation, proposal and artifacts.
  A new ordinary task checks the actual state before acting. An unfinished
  checkbox is not evidence that an external action must be replayed. Commands
  are not guaranteed to execute exactly once across process crashes.

A questionnaire keeps its task and tool-call identity while waiting. Other
sessions can use the released compute capacity; another task in the same
session still waits for its execution lane. Answering reacquires capacity and
continues that tool call. A Gateway restart ends the old execution; it does not
restore a partially executing coroutine.

[Goal mode](goal-mode.md) · [Web UI](web-ui.md) · [Sandbox](sandbox-security.md)
