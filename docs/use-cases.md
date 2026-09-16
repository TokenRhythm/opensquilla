# Use Cases and Recipes

Use this page when you know what you want OpenSquilla to do, but you are not
sure which feature guide to read first.

## First Successful Run

Goal: install OpenSquilla, configure one provider, and send a real message.

```sh
opensquilla onboard
opensquilla gateway run
```

Then open:

```text
http://127.0.0.1:18791/control/
```

If you prefer the terminal:

```sh
opensquilla chat
```

Read next:

- [`quickstart.md`](quickstart.md)
- [`web-ui.md`](web-ui.md)
- [`providers-and-models.md`](providers-and-models.md)

## Reduce Model Cost

Goal: keep simple work on cheaper models and reserve stronger models for hard
turns.

```sh
opensquilla configure router --router recommended
opensquilla cost --by-model
```

Use diagnostics when you want to inspect routing and runtime behavior:

```sh
opensquilla diagnostics on
```

Read next:

- [`features/squilla-router.md`](features/squilla-router.md)
- [`features/tool-compression.md`](features/tool-compression.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

## Work With Large Tool Results

Goal: let the agent inspect logs, pages, tables, search results, or diffs
without flooding the model context.

Start with a bounded workspace run:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Inspect the latest logs and summarize the actionable failures"
```

If the turn seems too slow or expensive:

```sh
opensquilla cost
opensquilla diagnostics on
```

Read next:

- [`features/tool-compression.md`](features/tool-compression.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`troubleshooting.md`](troubleshooting.md)

## Build a Repeatable Workflow

Goal: turn recurring work into reusable skills or meta-skills.

Find an existing skill:

```sh
opensquilla skills search report
opensquilla skills view <skill-name>
```

Inspect a meta-skill before running it:

```sh
opensquilla skills inspect <meta-skill-name>
```

Review historical meta-skill runs:

```sh
opensquilla skills meta runs list
opensquilla skills meta runs show <run-id>
```

Read next:

- [`features/skills.md`](features/skills.md)
- [`features/meta-skills.md`](features/meta-skills.md)
- [`artifacts-and-media.md`](artifacts-and-media.md)

## Remember Useful Context

Goal: preserve preferences, project notes, or reusable task context so future
turns can find them.

```sh
opensquilla memory status
opensquilla memory search "project preference"
opensquilla memory list
```

Inspect a stored memory file:

```sh
opensquilla memory show <path>
```

Read next:

- [`features/memory.md`](features/memory.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

## Connect a Messaging Channel

Goal: use OpenSquilla from a supported messaging surface while keeping the
gateway as the local control point.

```sh
opensquilla channels types
opensquilla channels describe telegram
opensquilla channels add telegram --name personal
opensquilla gateway restart
opensquilla channels status personal --json
```

Read next:

- [`channels.md`](channels.md)
- [`configuration.md`](configuration.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Schedule Recurring Work

Goal: ask OpenSquilla to run a recurring task without manually opening a chat.

```sh
opensquilla cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
```

Inspect jobs and runs:

```sh
opensquilla cron list
opensquilla cron status <job-id>
opensquilla cron runs <job-id>
```

Read next:

- [`operations.md`](operations.md)
- [`channels.md`](channels.md)
- [`scheduling.md`](scheduling.md)

## Publish a User-Visible Artifact

Goal: ask the agent to produce a file, report, slide deck, HTML page, image, or
media asset that you can inspect and share.

```sh
opensquilla agent -m "Create a short HTML report from the current notes"
opensquilla sessions export <session-key>
```

Read next:

- [`artifacts-and-media.md`](artifacts-and-media.md)
- [`features/skills.md`](features/skills.md)
- [`features/meta-skills.md`](features/meta-skills.md)

## Recover From a Bad Run

Goal: understand what happened, reduce risk, and continue safely.

```sh
opensquilla doctor
opensquilla gateway status
opensquilla sessions show <session-key>
opensquilla cost
```

If a tool was denied or the agent had too much access:

```sh
opensquilla sandbox status
opensquilla agent --permissions restricted -m "Read only"
```

Read next:

- [`troubleshooting.md`](troubleshooting.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`approvals-and-permissions.md`](approvals-and-permissions.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
