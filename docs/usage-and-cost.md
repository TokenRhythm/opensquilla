# Usage and Cost

OpenSquilla records provider calls, measured usage, and cost from the running gateway.
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

### Task ownership and totals

The ledger attributes recorded charges to their original task. Each instrumented
provider call has one durable ledger identity. Its owner is a session ID and
epoch, rather than a reusable session key or the chat that happens to poll its
result.

| Usage branch | Owner and parent total |
| --- | --- |
| Main session | The root session's own calls. |
| Inline subagent | Its own execution under the same session generation. |
| Child session | Its own session generation, linked to the original parent task. |
| Side chat | Its own session generation, linked to the parent task at creation. All descendants stay in this side-chat branch. |
| Unattributed | Calls whose original lineage cannot be proven. They remain visible with partial coverage. |

The branches are disjoint: a side chat's child is not also added to the child
session branch. Task, capability, model, and session projections describe the same
charges; adding their subtotals together would double count spend.
For example, $0.10 main + $0.10 inline + $0.20 child + $0.30 side-chat subtree
produces one $0.70 parent total.

The selected time range applies to these charges; a task total is not necessarily
its lifetime cost. The all-time total may also contain a legacy residual
whose individual calls cannot be recovered. That residual is reported separately
and is not assigned to a task by guesswork. The WebUI's task count and average
task cost use parent task generations; model cards count the parent tasks that
used each model. The API retains a separate session-generation count for audit.
The API's `totals.costSourceCounts` describe calls; settlement-source counts describe
charge items, so an event containing multiple items can make those counts differ.

Resetting a session starts a new task generation. Closing or deleting a chat
does not delete its charges or move them to another chat that reuses its key.
Cloned side-chat history is context, not a new bill. Historical records with
missing parent IDs are not assigned by guessing from session-key text.

Automatic and manual context compaction use the same ledger and parent-task
ownership as other model calls. Each physical summary attempt has its own call
identity; retrying an HTTP request can incur another charge, while replaying its
recorded completion cannot. A rejected summary still retains any provider usage
receipt. Missing prices or absent receipts do not establish that a call was free.
Compaction costs are included in the existing totals and model rows.

The WebUI retains its existing summary, chart, model cards, and task table. Each
parent task occupies one row: its own calls, inline agents, child sessions, side
chats, and their descendants are included in that row's total. Child and side-chat
sessions do not appear as additional cost rows. Expand the parent row's existing
model details to inspect the combined models and capability calls, their recorded
units, cost, and source. Single image, audio, search, and video models are also
expandable; a single text-model task keeps its existing compact display. No
additional task or capability panels are needed.

The backend publishes this parent view as `taskGroups`, including its time range
and model breakdown. `sessions` remains an independent projection of the same
calls for audit, not an input to add onto parent totals. An older gateway without
the task-group field keeps the legacy session view; an empty task-group array
does not fall back to child rows. Current parent titles are resolved only by
matching both session ID and epoch; historical generations retain their recorded
title.

### Images, search, audio, and video

Quantities and monetary receipts are independent. Image counts, audio/video
seconds, characters, requests, and provider credits retain their own units and
dimensions. Credits are not dollars, requested duration is not measured duration,
and missing token counters do not invalidate an otherwise valid monetary receipt.

| Capability | Recorded evidence |
| --- | --- |
| Image generation | Generated image count, available modality token details, provider charge, or a captured supported catalog estimate. |
| Search | One item per physical search request, Tavily credits when returned, Exa cost estimate when returned. Bundled model search totals are not added a second time. |
| Audio | Measured input/output duration where available, requested characters, ElevenLabs character credits, and monetary receipts when returned. |
| Video | Accepted job identity, requested and observed duration separately, original owner, and subsequent settlement revisions. |

TokenRhythm image `cost_cny` receipts and video
`billing.total_cost_cny` receipts are recorded in CNY. Video component amounts
explain the inclusive total and are not summed on top of it. A pending receipt
remains pending until a final observation arrives. When a supported catalog
estimate is available, it can be displayed while the receipt is pending; the
unconfirmed provider amount is retained separately and is not added to that
estimate. The normalized USD amount retains the fixed OpenSquilla conversion
policy used at settlement; this is not a claim about the provider's card exchange
rate. The CNY display uses a complete native CNY receipt when possible, and
otherwise converts the normalized USD total with the display rate.

Video polling, download retries, and process restarts reuse the original paid
submission. The receipt is committed before downloading the media, so a failed
download does not erase an incurred charge. Repeated identical observations are
idempotent; a weaker observation cannot overwrite a stronger recorded valuation.
A completed TokenRhythm video's bill can arrive after media delivery. Calling
`video_status` again refreshes an unconfirmed bill with one status GET while
returning the existing media; a confirmed bill needs no further request. This
does not create a background polling loop or submit another generation.
A new generation submission or other billable provider attempt gets a separate
identity, even when it retries the same user operation. Status polls and media
downloads do not create another generation charge.

Voice input captures the session ID and epoch when recording starts. The gateway
rejects a stale owner before transcription dispatch, and the client discards
responses from a cancelled recording after a reset. A call already dispatched
keeps its captured owner. Audio started without a durable chat owner is retained
as partial draft usage; it is not automatically transferred to a later chat.

### Amount provenance and precision

The ledger distinguishes `provider_confirmed`, `provider_estimate`,
`local_estimate`, and `unknown`. A supported local estimate captures its price
rules, source, currency, formula version, and content identity in the ledger.
Unsupported formulas or missing prices remain unknown, including dynamic routes
without a bounded price and unrecognized video SKUs. An explicit zero-dollar
receipt is valid; an absent receipt is not a zero-dollar receipt.

The new ledger's native amounts, normalized amounts, and quantities retain decimal
strings. Compatibility nano-unit and numeric fields remain available, while exact
fields are summed before presentation rounding. This does not recover precision
already lost in historical records. Settlement corrections append a revision
with an expected predecessor and replace the current valuation of the same item.
They never add a second physical call. Concurrent report reads check a monotonic
accounting revision to avoid mixing different ledger states.

Unknown monetary amounts are excluded from the numeric sum and accompanied by
coverage counts. Such a total is incomplete. External tools that bypass the
instrumented provider boundaries cannot automatically contribute their bills.
These additions do not reconstruct missing historical receipts or infer an
account's subscription discounts from a public list price.

### CSV exports

The CSV contains one `summary` row and the visible parent `task` rows. Descendant
session projections are not exported as additional monetary rows. An older
gateway without task groups retains `session` rows. Do not sum the summary and
details together. Task groups, capabilities, meters, and settlement summaries
are JSON audit columns on the summary row, not additional monetary rows.
`cost_usd_exact` preserves the exact summary amount; existing
numeric columns retain their compatibility rounding. `cost_cny` is a display-rate
conversion, not a native provider receipt. Use `native_billed_by_currency` for
the original currency amounts and recorded normalization evidence.

### Implementation references

The design uses [Models.dev](https://models.dev/) as model/catalog reference data,
not proof of account entitlement or actual endpoint support. The explicit price
rules and source snapshots follow the separation between usage and valuation
illustrated by [genai-prices](https://github.com/pydantic/genai-prices) and
[LiteLLM's cost accounting](https://docs.litellm.ai/docs/completion/token_usage).
Stable event identities follow the duplicate-detection principle in
[CloudEvents](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md).
Keeping monetary currency separate from consumed/billed quantity and unit is
consistent with the distinctions in [FOCUS](https://focus.finops.org/docs/specification/v1-4/).
These are design references, not claims of FOCUS export or CloudEvents wire-format
compliance.

### Existing cost surfaces

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
