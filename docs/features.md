# Feature Catalog

OpenSquilla combines a personal-agent runtime with model routing, tools, memory,
channels, scheduling, and reusable skills.

## Product Surfaces

| Surface | What it is for |
| --- | --- |
| Web UI | Local control console, setup, chat sessions, approvals, logs, channels, and usage surfaces. |
| CLI chat | Interactive terminal agent work. |
| CLI agent | Single-turn automation, CI-like runs, and benchmark-style invocations. |
| Gateway RPC | Local server surface for Web UI, CLI clients, channels, and external clients. |
| Channels | Telegram, Slack, Feishu/Lark, Discord, DingTalk, WeCom, Matrix, QQ, terminal, and websocket-style integrations. |

## Distinctive Features

### SquillaRouter

Local routing for model tier selection. It is designed to keep easy turns cheap
and reserve expensive models for work that needs them.

Read: [`features/squilla-router.md`](features/squilla-router.md)

### TUI Frontend

Terminal chat uses a streaming plane for token deltas and a structured UI plane
for plugin snapshots such as the Router HUD.

Read: [`tui.md`](tui.md) for user-facing terminal chat usage and
[`features/tui-frontend.md`](features/tui-frontend.md) for backend details.

### Tool Compression

Large tool outputs are projected into compact provider-visible previews while
the runtime can keep richer raw results out-of-band.

Read: [`features/tool-compression.md`](features/tool-compression.md)

### Memory

Durable memory lets OpenSquilla recall useful user preferences, project notes,
and previous task traces without forcing every old transcript into the active
prompt.

Read: [`features/memory.md`](features/memory.md)

### Skills

Skills package task-specific guidance and scripts so the agent can load the
right operating instructions only when a task needs them.

Read: [`features/skills.md`](features/skills.md)

### Compaction and Cache Continuity

Long sessions can compact old context, preserve recent task state, and report
compaction lifecycle events.

Read: [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

### Sessions and Durable Agents

Sessions preserve conversation continuity, exports, and running-task control.
Durable agents provide named identities and defaults for recurring workstreams.

Read: [`sessions.md`](sessions.md) and [`agents.md`](agents.md)

### Goal Mode

Goal mode keeps one durable objective and a structured progress checklist on a
session. It can continue through ordinary agent turns while a subscribed Web UI
or CLI owner remains connected, pauses safely on disconnect or restart, and
defers automatic work while Plan mode is active.

Read: [`goal-mode.md`](goal-mode.md)

### Usage, Diagnostics, and Permissions

Usage reports explain recent model spend. Diagnostics and replay help inspect a
turn after it runs. Permission and approval controls keep tool access matched to
the task.

Read: [`usage-and-cost.md`](usage-and-cost.md),
[`diagnostics-and-replay.md`](diagnostics-and-replay.md), and
[`approvals-and-permissions.md`](approvals-and-permissions.md)

## Core Runtime Capabilities

- Unified `TurnRunner` path across Web UI, CLI, and channels.
- Provider abstraction for OpenAI-compatible APIs, Anthropic, Ollama, and other
  configured backends.
- Streaming responses, tool calls, retries, approvals, artifacts, and final
  usage accounting.
- Durable session storage with transcript, summaries, context states, and
  replay support.
- Per-agent workspaces and durable agent entries.
- Subagent support for bounded delegation.

## Tools

OpenSquilla includes tools for:

- Filesystem read/write/edit/list/glob/grep.
- Shell commands, background processes, and code execution.
- Git status, diff, log, and commit.
- Web search and web fetch.
- Memory search/save/get/delete.
- Session search, session spawn/send/history/status.
- Artifact publication.
- Image generation, PDF, TTS, and media workflows.
- Spreadsheet, PPTX, DOCX, CSV, and PDF authoring through bundled skills.
- Feishu/Lark docs, chat, drive, wiki, permissions, and media upload.
- Cron and gateway administration.
- Skill listing, viewing, creating, editing, and installing dependencies.

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Skills

Bundled user-facing skills include:

- `deep-research`
- `docx`
- `github`
- `html-coder`
- `pdf-toolkit`
- `pptx`
- `skill-creator`
- `xlsx`

Read: [`features/skills.md`](features/skills.md)

## Scheduling

The `cron` command group manages scheduled OpenSquilla runs:

```sh
opensquilla cron list
opensquilla cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
opensquilla cron status <job-id>
opensquilla cron run <job-id>
opensquilla cron runs <job-id>
```

Scheduled jobs can deliver work through configured surfaces such as channels or
webhooks depending on the configured job.

Read: [`scheduling.md`](scheduling.md)

## Migration

OpenSquilla can import compatible state from OpenClaw and Hermes Agent. It can
also copy a complete supported CLI/Desktop profile, or historical Windows
Portable data, into a separately owned OpenSquilla profile:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply
opensquilla migrate hermes --json
opensquilla migrate hermes --apply
opensquilla migrate opensquilla --source PATH --json
opensquilla migrate opensquilla --source PATH --apply
```

Same-product imports always require explicit source selection. A populated
target is never merged: it can only be kept or fully backed up and replaced.

Read: [`../MIGRATION.md`](../MIGRATION.md).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
