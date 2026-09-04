# Compaction and Cache Continuity

Long agent sessions need context management. OpenSquilla uses compaction,
bounded history, tool-result projection, and cache-aware prompt placement to
keep long-running tasks moving.

Compaction is separate from memory. Memory is durable recall. Compaction is an
active-session continuity tool.

## What Compaction Does

When session history approaches the configured context budget, OpenSquilla can
compact older transcript entries into a durable summary and keep the recent
tail active.

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

When no compaction is needed, OpenSquilla uses this stable message:

```text
Already within context budget; no compact was applied
```

That message is a no-op, not a failure.

## When to Compact Manually

Manual compaction is useful when:

- the session is long and you are about to start a new phase;
- a previous tool-heavy turn produced a lot of context;
- the UI indicates context pressure;
- you want the next answer to focus on the current state rather than the whole
  transcript.

Avoid compact loops when the runtime says the session is already within budget.

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

For memory repair surfaces related to degraded compaction records:

```sh
opensquilla memory repair list
opensquilla memory repair show --summary-id <id>
opensquilla memory raw-fallbacks list
```

## Best Practices

- Keep important final artifacts in files or published artifacts.
- Use memory for durable preferences and reusable project facts.
- Use session export for exact old transcripts.
- Use manual compaction before a new phase in a very long session.
- Do not repeatedly compact a short or already-within-budget session.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
