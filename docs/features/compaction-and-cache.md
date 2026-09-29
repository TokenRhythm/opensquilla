# Compaction and Cache Continuity

Long agent sessions need context management. OpenSquilla uses compaction,
bounded history, tool-result projection, and cache-aware prompt placement to
keep long-running tasks moving.

Compaction is separate from memory. Memory is durable recall. Compaction is an
active-session continuity tool.

## What Compaction Does

When session history approaches the current model's available input capacity, OpenSquilla can
compact older transcript entries into a durable summary and keep the recent
tail active.

Manual and automatic compaction share the same budget resolver, summary
builder, request-fit checks, and durable commit gate.
The budget accounts for the physical provider/model deployment, generation,
system instructions, tools, and the active request and attachments. Idle manual
compaction includes known instructions and tools and reserves space for the
next request; that request is checked again when it arrives.

There is no independent 1,024-token summary-body limit. A complete summary is
accepted when it reduces the relevant pressure and fits the complete current
request, or the reserved request envelope for idle manual compaction. A future
request must still pass its own capacity check. The model's generation limit
and configured resource budgets still apply.
Chunk planning reserves generation space before filling the input; a large
conversation does not force every chunk's output into the remaining few tokens.
An oversized complete summary may be shortened by the same model within the
remaining operation budget. A response reported as truncated, or a draft that
does not cover every selected source chunk, never replaces that source history.
These checks establish protocol and source-range completion; they cannot prove
that the model retained every fact or relationship.
The complete consumer request proof owns capacity admission when available;
local history token and character estimates then remain diagnostics, not a
second rejection gate. Compatibility callers without that proof still use
conservative history estimates. Completeness and actual reduction remain
separate requirements.
Reduction compares the current replay estimate before and after replacement,
including the complete checkpoint, raw tail, tools, reasoning, and media.
Cached historical token counts can conservatively trigger compaction but cannot
make a longer replay qualify as a smaller one.

Initial automatic planning leaves room below the configured pressure trigger.
If a complete draft cannot coexist with the retained tail, compaction can absorb
additional complete, eligible old rounds within the same operation budget.
Each replan advances the source boundary and rechecks attachments, obligations,
and the final consumer request. Intermediate drafts are never committed. An
explicit caller-selected cut is exact and cannot expand during recovery.

Automatic compaction and temporary windows retain recent completed history
where capacity allows. Manual compaction can summarize the entire safe prefix
of completed history. Current user input, active attachments, and unresolved or
approval-pending tool state remain raw.
Completed tool results, including completed errors, can be summarized or omitted
from a temporary request window. Profiles no longer impose an implicit recent
message quota. An explicitly configured `protected_recent_messages` value is
still respected by durable compaction.

Automatic preflight uses `preflight_compact_ratio` (default `0.85`) against the
available history capacity. Manual compaction bypasses this trigger and can
compact useful older history even below it. It still skips when there is no
safe, useful range, and never bypasses request-fit or persistence checks.

`context_budget_tokens` is deprecated and ignored. Existing configurations
still load and produce one migration warning per process when this field is
explicitly present. Remove the field; capacity is derived from the model and
request envelope. The `contextWindowTokens` manual RPC argument remains an
optional history-capacity ceiling. It cannot enlarge model capacity or change
the output reservation.

Model request replay keeps each checkpoint complete, including its preserved
facts. There is no separate fixed 16,000-character cutoff: the shared consumer
budget and final provider request check decide whether the complete checkpoint
fits. Explicitly bounded legacy previews cannot authorize replacing history.

The goal is to preserve:

- user goal;
- current status;
- open steps;
- changed files and artifacts;
- known failures;
- important tool results;
- next action.

Compaction is not a guarantee that every old word remains model-visible. Export
sessions or save files when exact historical text matters.

## User-Visible Lifecycle

Depending on surface and trigger, users may see:

- compaction started;
- compaction skipped;
- compaction completed;
- compaction failed.

Gateway manual compaction, including Web UI requests, appears as a maintenance
operation with one operation ID from start to its `completed`, `skipped`,
`cancelled`, or `failed` terminal event. It does not create an assistant
response. A skipped operation leaves history unchanged;
its reason distinguishes an empty session, a protected range, or an unhelpful
summary. A definitely unapplied summary failure or summary timeout also returns
`skipped` with its reason, allowing a queued conversation to proceed. Cancellation
remains cancellation, and persistence errors remain failures. If cancellation or
timeout races with a completed commit, the actual committed result is reported.
The same result semantics apply to synchronous and asynchronous Gateway
maintenance. Standalone CLI commands report their result in the terminal.

## When to Compact Manually

Manual compaction is useful when:

- the session is long and you are about to start a new phase;
- a previous tool-heavy turn produced a lot of context;
- the UI indicates context pressure;
- you want the next answer to focus on the current state rather than the whole
  transcript.

Avoid repeated attempts when the remaining history has no safe range to compact.

## Passive Compaction

Passive compaction can happen when OpenSquilla detects context pressure before
or during agent work. The exact trigger depends on model context limits,
configured budgets, current history, and tool output size.

If a summary fails but the original request still fits, the conversation
continues with that request. Otherwise, the backend selects a temporary window
of complete conversation and tool groups and checks the full provider request
again. That window is passed directly to this turn's history loader and rechecked
against current source and ownership. It is not stored as a new checkpoint, and
the original history and newly appended messages remain recoverable.

When recovery replaces completed work inside the active turn, the current user
request stays before the checkpoint. The checkpoint includes backend-recorded
tool call IDs, names, arguments, returned-result flags, and actual execution
status, followed by a continuation instruction. These records are independent
of the generated summary; they do not imply that a failed or unknown result
succeeded. Temporary windows retain the same execution records when omitting
returned tool results, and explicitly mark those result contents unavailable.
The complete request check includes these records. Pending work remains raw,
and this request-local layout does not rewrite the stored transcript.
Large inputs of completed calls can use the existing tool-input projection in
these records. Small arguments and path fields remain intact. Projected records
explicitly mark their arguments incomplete and include the original serialized
length and digest; omitted values are unavailable in that request view. This
keeps completed file bodies and other bulk inputs from making a checkpoint
larger than its source. Digests identify omitted data; they cannot recover it
and are not executable tool arguments.

If further completed tool work causes another overflow in the same turn, the
backend can try another local window without requesting another summary. The
complete request must fit and shrink every exceeded capacity measure. An
unchanged rejected request cannot loop through this recovery.

Repeated summary failures can temporarily suppress further summary attempts.
They do not disable ordinary chat or local window recovery. Disabling automatic
compaction likewise suppresses summary calls while retaining request-fit checks.
Changing the actual response deployment or its relevant controls gives the new
configuration its own failure scope.
An attempt that exhausts its entire operation budget starts the existing
five-minute cooldown immediately. Quick candidate failures retain the
three-failure threshold. Dispatched summaries that produce no compression benefit
or progress share that threshold as unproductive candidates; their UI status stays
`skipped` and they are not classified as provider faults. A confirmed provider
overflow can probe a circuit
opened by quick failures once; it cannot immediately repeat an exhausted
operation budget. Explicit manual retry remains available. Appending a new
message alone does not clear the failure state.

This recovery does not override cancellation or the main turn's deadline. Every
summary shares an absolute operation deadline bounded by the parent deadline;
chunks and provider retries cannot extend it. If the required current input or
live tool state alone cannot fit, or the main provider or storage fails, the
operation reports that real failure.

The default `compaction.total_timeout_seconds` is 600 seconds, shared by all
chunks, shortening attempts, replans, and admission work. This is a configurable
operation limit, not a per-call allowance. The normal provider I/O timeout is
unchanged. A separate idle guard inherits the normal request timeout: nonempty
text or reasoning progress refreshes it, while empty heartbeats do not. Neither
kind of progress extends the operation deadline. An explicitly saved legacy
`compaction.timeout_seconds` value sets this idle guard; it no longer cuts off
a progressing stream at that many seconds. Explicit saved total budgets, such
as 120 seconds, are preserved.

Expiry while rebuilding an uninstalled checkpoint can still recover from the
original history using a temporary window, provided the main turn has time left.
The abandoned checkpoint is not committed. Cancellation, an expired main turn,
and errors raised by the rebuild itself retain their own failure meaning.
The same recovery is available when an uninstalled candidate cannot survive a
server retry delay and no physical request has started. The recovered request
still waits until the server permits it; recovery does not reset that delay.

Valid `Retry-After` delays from HTTP 429 and 503 also apply after a failed summary
falls back to ordinary chat. Waiting happens before physical dispatch and usage
accounting, and remains bounded by the caller's deadline and ordinary retry
policy. A summary's timeout or manual retry does not shorten the server's delay.
This state is local to the running backend process. Built-in OpenAI-compatible
adapters expose the endpoint, credential, organization and actual model identity
needed to carry it across newly created adapters; providers without that
identity share it only within the same adapter object.

Local recovery requires proof of the wrapper's complete outgoing request.
Ensemble currently cannot provide that proof, including its fixed-model
continuations: it can prepend its saved base history to the supplied messages.
Proving only the physical model's supplied messages would therefore be unsafe.
Ensemble observes server retry delays, but these local recovery paths can still
end with a capacity or compaction-timeout error while preserving source history.

Local windows first try to retain complete checkpoints created by the runtime,
then select recent complete tool rounds. Later tool execution records stay after
earlier checkpoints. If all checkpoints cannot fit, older ones are released
before newer ones; every candidate still needs complete request admission.
User-written summary labels do not grant this retention priority. These choices
only affect the current request and do not rewrite stored history.

A temporary window can omit information needed for a task. Successful request
admission and preserved source data do not guarantee semantic continuity. Save
important artifacts separately and export the session when exact text matters.

## Summary Requests and Compatibility

All entry points use one suffix layout: selected history followed by the summary
instruction. Summary-purpose system instructions replace the business response
contract. Business stop sequences, JSON schemas, and forced tool execution do
not carry over. Sampling defaults and mandatory reasoning behavior are resolved
by the ordinary provider adapter; compaction does not force temperature zero or
disable reasoning. Historical tools are inert protocol data, never executed by
the summarizer. Attachments use the shared history projection rather than raw
storage JSON or Base64 text.
The summary instruction asks for the entity or field associated with each
retained value, together with any explicitly stated status. Keeping an unlabelled
list of identifiers does not preserve those facts' meaning.

The summarizer uses the current physical response deployment. Router uses its
already selected model; Ensemble uses its aggregator or current fixed takeover
model. Idle manual compaction resolves the session's current stable selection.
Summary generation does not independently route or switch to a fallback model.
Durable checkpoint admission still checks the stable session consumer, even
when a temporary routed model has a larger context window.

The old `OPENSQUILLA_COMPACTION_PROMPT_LAYOUT` switch no longer selects an
alternate engine. Deprecated compaction `provider` and `model` settings remain
load/save compatible but do not affect summary routing. Existing profiles are
not rewritten. Session naming retains its own model selection settings.

## Prompt Cache Continuity

Prompt caching works best when stable prompt parts stay stable. OpenSquilla
tries to keep:

- stable system prompt and tool definitions early;
- current request, volatile runtime context, retrieved history, and tool results
  near the tail;
- model/provider switches visible through diagnostics when they may affect
  cache continuity.

Cache continuity is best-effort. Routing, tools, attachments, provider changes,
or a large new context can reduce cache reuse.

## Related Commands and Surfaces

Manual compaction is primarily surfaced in chat and Web UI flows. For
inspection and recovery:

```sh
opensquilla sessions show <session-key>
opensquilla sessions export <session-key>
opensquilla diagnostics on
```

## Best Practices

- Keep important final artifacts in files or published artifacts.
- Use memory for durable preferences and reusable project facts.
- Use session export for exact old transcripts.
- Use manual compaction before a new phase in a very long session.
- Do not repeatedly compact a short session with no useful older history.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
