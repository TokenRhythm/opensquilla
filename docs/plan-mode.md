# Plan collaboration

Plan mode helps clarify a request and prepare a versioned proposal. The same
Agent investigates the workspace, discusses choices and submits a formal plan
for work requests. A substantive work request in Plan mode asks the Agent to
plan that work, even without the word "plan". The Agent investigates first,
uses structured questions for missing material decisions when available, and
submits the proposal when ready. Investigation and other authorized tool work
may happen before submission, but it does not replace the proposal or turn a
proposal into an approval.
Greetings, explanations and requests to keep discussing can finish without
producing a new revision. Revising a proposal
preserves its earlier revisions in history.

Planning uses the ordinary Agent tool surface and its normal permission,
approval and sandbox policies. Commands, tests, builds and other authorized
tool calls may happen while the proposal is being prepared; they do not replace
the required proposal. For a substantive work request, the planning turn must
call `submit_plan` before it ends. A questionnaire is optional and is only
needed for a material user decision that cannot be discovered or reasonably
defaulted. Subagents inherit the planning intent and their parent's permission
ceiling. The main task owns formal proposals and Goal controls.

## Implement and adapt

Implementing approves a particular revision and starts an ordinary Default
Agent task. The task can reorder work, add verification, repair a failed check
and publish again after fixing an artifact. Changes in approach within the
user's authorization do not require another proposal approval. Actions outside
that authorization still require confirmation through the normal controls.
Changing methods or step order does not remove the approved deliverables or
acceptance requirements in the proposal's body and step details.

Before claiming completion, the Agent should inspect the final artifact or
resulting state and compare it with those requirements. For example, counting
document headings and tables checks structure; it does not establish that the
document's content meets every requirement. Unmet requirements need repair and
another check. For documents, reopen the final saved file with a normal reader
(for example, python-docx for DOCX) and read its body and tables. Raw ZIP/XML
extraction alone does not check the complete document package.
Preserve unrelated content and document parts
while repairing omissions, and repeat affected checks after any further edits
on the version actually delivered. If completion or verification is not possible,
the final answer must identify the remaining work and limit its claims to the
available evidence.

The proposal and current progress are separate. `update_plan` replaces an
optional task progress list of up to 20 steps. Steps can be added, removed,
reordered or reopened. For substantive multi-step implementation, the Agent
should report its actual execution list before substantive implementation,
showing the first work in progress, and update at meaningful milestones and
before the final response when needed. Related changes can be batched; reporting
should not all be deferred until the end. Progress does not restrict tools,
schedule future turns, or determine whether a task may finish. A successful implementation turn completes
its associated run without inventing verification for unreported steps. This
runtime status records the turn's normal completion; it is not independent
proof that the deliverable meets its semantic acceptance requirements.

Artifact publication performs the supported format checks and publishes the
artifact. It does not establish semantic acceptance, mark plan steps complete
or prevent subsequent inspection and repair. When a downloadable file is part
of the requested delivery, publish the checked final version and include its
returned artifact reference; a workspace path alone is not that download.
The user's request already authorizes that file delivery in the conversation,
so it does not need another permission question.

## Hide, cancel and continue

- A normally ended Plan run shows the neutral **Execution ended** summary with
  its last reported step statuses and count, then hides after about two seconds.
  Unreported steps stay unreported, including `0/4`; ending the turn does not
  fill the checklist or add an overall success check. Cancelled, failed and
  paused runs retain their distinct meanings.
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
