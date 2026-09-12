# Tool Compression

OpenSquilla agents use tools. Tool calls can produce large outputs: command
logs, JSON, web pages, search results, diffs, file contents, tables, and
artifacts. Tool compression keeps those outputs useful without letting them
consume the whole model context.

This is a user-facing context-management feature. It does not change what the
tool returned; it changes how much of that result is shown to the model for the
next step.

## Why It Matters

Tool compression helps when:

- a command prints a large log;
- a web page or search result is too long for a useful prompt;
- a file read returns more text than the next step needs;
- a long session is close to the context budget;
- you want raw results preserved while the model sees a compact preview.

Without compression, one large tool result can crowd out the user's goal,
recent conversation, and next action.

## What Users May See

In long or tool-heavy turns, the model-visible result may include:

- a compact preview;
- a note that a result was shortened;
- a `tool_result_handle` for an out-of-band stored result;
- estimated token-saving diagnostics when diagnostics are enabled.

This is expected. It means OpenSquilla is protecting the active context window.

## Product-Level Model

OpenSquilla separates two views:

| View | Purpose |
| --- | --- |
| Runtime view | The durable result OpenSquilla can preserve, inspect, or export. |
| Provider view | The bounded text sent back to the model for the next reasoning step. |

The agent can continue from the important facts while large raw material stays
available through files, session export, diagnostics, or tool-result handles
when configured.

## Compression Modes

OpenSquilla supports several compression styles depending on configuration and
tool output shape.

| Mode | Best for | Tradeoff |
| --- | --- | --- |
| `truncate` | Fast deterministic previews. | May omit useful middle sections. |
| `summarize` | Slower/background workflows that benefit from semantic summaries. | Adds another model call and should be opt-in. |
| Structured projection | Logs, diffs, JSON, tables, and known tool shapes. | Depends on reducer coverage for that output type. |

Most users should keep the default behavior and use diagnostics only when a
workflow is still too large.

## How to Work With Large Outputs

Ask for focused follow-up reads:

```text
Look at the failing test names and the last 80 lines of the log.
```

Prefer handles, paths, and summaries:

```text
Use the compacted result to identify likely causes, then read the exact file
sections you need.
```

Avoid asking the agent to paste every line of a huge result unless exact text is
the deliverable:

```text
Paste the entire 50,000-line log into chat.
```

## Inspect and Debug

Turn on diagnostics when you need to understand context growth:

```sh
opensquilla diagnostics on
```

Export the session when you need to inspect durable history outside the chat
surface:

```sh
opensquilla sessions export <session-key>
```

Review cost and usage after a large tool-heavy run:

```sh
opensquilla cost
```

## Best Practices

- Keep tool requests specific.
- Ask for the smallest file ranges, log tail, or JSON fields that answer the
  question.
- Use artifacts for large deliverables instead of forcing everything into chat.
- Use session export for audit and debugging.
- Treat tool compression as a continuity feature, not as a substitute for
  storing important files.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
