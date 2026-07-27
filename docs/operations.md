# Operations

This guide covers day-two commands: sessions, cron, cost, diagnostics, replay,
migration, durable agents, MCP, and install inventory. Use it after the first
successful chat or gateway run.

## Sessions

Sessions are durable chat/task histories. Use them to resume, inspect, export,
or clean up prior work.

```sh
opensquilla sessions list
opensquilla sessions show <session-key>
opensquilla sessions resume <session-key>
opensquilla sessions export <session-key>
opensquilla sessions abort <session-key>
opensquilla sessions delete <session-key>
```

Use session export when exact old context matters or when you want to debug a
long-running task outside the chat UI.

For resume, abort, export, and cleanup workflows, see
[`sessions.md`](sessions.md).

## Durable Agents

OpenSquilla supports durable agent entries, including the built-in `main`
agent.

```sh
opensquilla agents list
opensquilla agents add research --name Research --workspace /path/to/research
opensquilla agents delete research
```

Use durable agents when you want separate workspaces, instructions, or tool
profiles for recurring roles. Restart the gateway after agent config changes.

Keep each durable agent's instructions focused on that role instead of turning
every agent into a copy of `main`.

For agent examples and concepts, see [`agents.md`](agents.md).

## Cron and Scheduled Runs

Cron jobs run OpenSquilla tasks on a schedule.

Inspect jobs:

```sh
opensquilla cron list
opensquilla cron status <job-id>
opensquilla cron runs <job-id>
```

Add a simple recurring reminder:

```sh
opensquilla cron add \
  --every 1h \
  --text "Check for urgent project updates and summarize them" \
  --name hourly-project-check
```

Add a daily cron-style task:

```sh
opensquilla cron add \
  --cron "0 9 * * 1-5" \
  --tz "America/Los_Angeles" \
  --text "Prepare my weekday morning briefing" \
  --name weekday-briefing
```

Manage jobs:

```sh
opensquilla cron update <job-id> --enabled
opensquilla cron remove <job-id>
opensquilla cron run <job-id>
```

Good uses:

- morning briefings;
- recurring research digests;
- PR or CI checks;
- channel-delivered reminders;
- scheduled memory consolidation or reporting tasks.

Pair cron with channels when the output should be delivered somewhere other
than the local Web UI.

For scheduling examples, delivery options, and troubleshooting, see
[`scheduling.md`](scheduling.md).

## Cost and Usage

Inspect usage and estimated cost:

```sh
opensquilla cost
opensquilla cost --by-model
opensquilla cost --json
```

Use cost inspection after tool-heavy, routed, or long-context tasks to
understand actual runtime behavior.

For cost investigation workflow, see [`usage-and-cost.md`](usage-and-cost.md).

## Diagnostics

Diagnostics help explain runtime behavior without changing the core task.

```sh
opensquilla diagnostics status
opensquilla diagnostics on
opensquilla diagnostics off
```

Use diagnostics when investigating:

- provider retry behavior;
- router decisions;
- cache breaks;
- compaction events;
- tool-result compression;
- channel delivery failures.

Turn diagnostics off after collecting the needed evidence.

For diagnostics guidance and safe sharing notes, see
[`diagnostics-and-replay.md`](diagnostics-and-replay.md).

## Replay

Replay a recorded turn from the decision log:

```sh
opensquilla replay --session <session-key> --turn <turn-id>
```

Replay is useful for reproducing an agent turn, reviewing decision metadata, or
debugging behavior after the original chat has moved on.

## Migration

Preview first:

```sh
opensquilla migrate openclaw --json
opensquilla migrate hermes --json
```

Apply after reviewing the report:

```sh
opensquilla migrate openclaw --apply
opensquilla migrate hermes --apply
```

See [`../MIGRATION.md`](../MIGRATION.md) for custom paths and conflict
handling.

## MCP Server

OpenSquilla can run an MCP server bridge when installed with the `mcp` extra:

```sh
opensquilla mcp-server run
```

Install with:

```sh
uv tool install --python 3.12 "opensquilla[recommended,mcp] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

Use this when another MCP-capable client should access OpenSquilla-managed tools
or runtime surfaces.

For setup details, see [`mcp-server.md`](mcp-server.md).

## Install Inventory

Emit a reproducible workspace-state inventory:

```sh
opensquilla dist
```

Use this for support, release QA, or environment comparison.

## Models

Inspect available models:

```sh
opensquilla models list
```

In this build, model inspection can be runtime-backed. If it cannot connect,
start the gateway first:

```sh
opensquilla gateway run
```

For provider catalog inspection that does not require a live gateway, use:

```sh
opensquilla providers list
```

Read: [`providers-and-models.md`](providers-and-models.md)

## Health Checklist

For a confusing install or runtime:

```sh
opensquilla doctor
opensquilla gateway status
opensquilla providers list
opensquilla search list
opensquilla channels types
opensquilla sandbox status
```

Then turn on diagnostics only if the basic health surfaces are not enough.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
