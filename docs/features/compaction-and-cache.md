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

Manual and automatic compaction share the same budget resolver, recent-tail
policy, summary output limit, request-fit checks, and durable commit gate.
The budget accounts for the physical provider/model deployment, generation,
system instructions, tools, and the active request and attachments. Idle manual
compaction includes known instructions and tools and reserves space for the
next request; that request is checked again when it arrives.

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

Manual compaction appears as a maintenance operation, with one operation ID
from start to its `completed`, `skipped`, or `failed` terminal event. It does not
create an assistant response. A skipped operation leaves history unchanged;
its reason distinguishes an empty session, a protected range, or an unhelpful
summary. Cancellation and timeouts are failures with their original reasons.

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

If passive compaction fails, the safest user response is usually:

1. let the current turn finish or fail cleanly;
2. export the session if exact history matters;
3. retry with a narrower request or manually save key artifacts;
4. enable diagnostics if the failure repeats.

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
