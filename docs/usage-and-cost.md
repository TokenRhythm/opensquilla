# Usage and Cost

OpenSquilla records token usage and estimated cost from the running gateway.
Use the cost view after routed, tool-heavy, channel, or long-context work to
understand where model spend is going.

## Requirements

Cost inspection uses the gateway:

```sh
opensquilla gateway status
```

If the gateway is not running:

```sh
opensquilla gateway run
```

## Show Cost

```sh
opensquilla cost
```

The default view lists session/model rows with input tokens, output tokens, and
estimated cost.

## Group by Model

```sh
opensquilla cost --by-model
```

Use this when SquillaRouter is enabled and you want to see which models carried
the recent workload.

## Use JSON Output

```sh
opensquilla cost --json
opensquilla cost --by-model --json
```

JSON output is useful for local dashboards, regression checks, and automated
reports.

## What to Check First

| Signal | What it can mean |
| --- | --- |
| Many rows for premium models | Router policy or task shape may be escalating more often than expected. |
| High input tokens | Long history, large tool results, or large prompt/tool schema surfaces may dominate cost. |
| High output tokens | The task may need tighter instructions or a smaller response format. |
| Cost concentrated in one session | Inspect that session before changing global configuration. |

## Lower Cost Safely

Start with router and diagnostics:

```sh
opensquilla configure router --router recommended
opensquilla diagnostics on
opensquilla cost --by-model
```

For large tool results, read:

- [`features/tool-compression.md`](features/tool-compression.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

For simple one-shot automation, bound the run:

```sh
opensquilla agent --max-iterations 20 --timeout 600 -m "Bounded task"
```

## Notes and Limits

- Cost is an estimate based on recorded runtime usage and configured pricing,
  unless the provider itself reports a billed amount. Each row's `costSource`
  (`provider_billed` / `opensquilla_estimate` / `mixed` / `unavailable`) says
  which kind of number you are looking at; see
  [`providers-and-models.md`](providers-and-models.md#pricing-and-cost-estimation)
  for the full pricing and provenance model.
- Provider bills remain the source of truth for actual charges.
- Tool compression and routing can reduce model context cost, but they should
  be checked against task success, not only token totals.
- Diagnostics can explain why a turn routed, compacted, retried, or produced
  unusually large outputs.

Read next:

- [`features/squilla-router.md`](features/squilla-router.md)
- [`features/tool-compression.md`](features/tool-compression.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
