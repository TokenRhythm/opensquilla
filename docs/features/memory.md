# Memory

OpenSquilla memory helps the agent recall durable context without replaying
every old conversation. Use it for stable preferences, reusable project facts,
previous decisions, and notes that should survive across sessions.

Memory is separate from skills. Skills teach the agent how to do a task; memory
stores useful facts and context the agent may need later.

## What to Store

Good memory entries are stable and reusable:

- user preferences;
- project conventions;
- recurring output formats;
- names of important repositories, directories, or services;
- decisions the user wants reused;
- brief notes from completed tasks.

Avoid memory for:

- API keys or secrets;
- raw private data that does not need long-term recall;
- one-off instructions for the current turn;
- noisy dumps that would pollute future retrieval;
- exact transcripts that should instead be exported as session records.

## Common Commands

Inspect memory health:

```sh
opensquilla memory status
opensquilla memory status --deep
```

Index and list memory sources:

```sh
opensquilla memory index
opensquilla memory list
```

Search and inspect memory:

```sh
opensquilla memory search "release note format"
opensquilla memory show <path>
```

Search previous sessions as well as memory:

```sh
opensquilla memory search "deployment decision" --source all
```

## Natural Chat Usage

Ask naturally when something should be remembered:

```text
Remember that I prefer concise release notes with a risk section.
```

Later, refer to the preference:

```text
Use my usual release-note format for this changelog.
```

When memory seems stale, ask the agent to search explicitly:

```text
Search memory for my release-note preferences before drafting this.
```

## Session-Derived Memory

For long or important sessions, flush session state into memory before
archiving, compacting, or switching tasks:

```sh
opensquilla memory flush-session <session-key>
```

Use session export when exact old wording matters:

```sh
opensquilla sessions export <session-key>
```

Memory is for useful recall. Session export is for exact records.

## Automatic Consolidation (Dream)

Dream promotes recurring facts out of dated memory notes and into the curated
`MEMORY.md`, so long-lived preferences survive without being restated. It reads
the dated notes, ranks them against accumulated evidence, and asks the model for
a constrained patch — only `upsert`, `merge`, or `skip`, one entry at a time.
Promoted entries land under headings such as `User Preferences` and
`Project Practices`. `MEMORY.md` itself is never a candidate, so consolidation
cannot feed on its own output.

**Nothing runs on its own until you opt in**, and nothing is promoted into
`MEMORY.md` until you turn preview off. The settings gate different things, and
only `preview_mode` stands between a run and a write:

| Setting | Default | What the default does |
| --- | --- | --- |
| `memory.dream.preview_mode` | `true` | a run reports what it would promote and leaves `MEMORY.md` alone |
| `memory.dream.enabled` | `false` | no scheduled run is registered — invoking the command still runs |
| `memory.dream.auto_schedule` | `false` | no scheduled run, same as above; both must be true to schedule one |
| `memory.flush_enabled` | `false` | sessions write no dated notes of their own |

`enabled` and `auto_schedule` govern only the scheduled path;
`opensquilla memory dream` runs whatever the two are set to. And
`flush_enabled` is not the only writer of dated notes — the `memory_save` tool
writes the same `memory/<date>.md` files, so an agent you asked to remember
something can leave Dream candidates behind with flush off.

Check what is waiting without running anything:

```sh
opensquilla memory dream --status
```

Preview a run. A preview leaves `MEMORY.md`, the backups, the evidence store and
the cursor untouched, so it is safe to repeat while you decide — it does record
a receipt under `memory/.dream_receipts/`:

```sh
opensquilla memory dream
```

To let Dream write, set `preview_mode = false` under `[memory.dream]`. To let it
run on its own, set both `enabled = true` and `auto_schedule = true`, then pick a
cadence (`interval_h`, default 24, or a `cron` expression). Turning on
`memory.flush_enabled` gives it a steady supply of notes to read.

Each run backs up `MEMORY.md` under `memory/.dream_backups/` before writing and
records a receipt, so a promotion you dislike can be traced and reverted. A
cursor tracks which notes have been consumed; re-process everything with:

```sh
opensquilla memory dream --force
```

Consolidation makes provider calls and turns conversation into durable
on-disk notes. Both are reasons the defaults are conservative — review a few
previews before enabling writes, and keep the guidance in
[What to Store](#what-to-store) in mind, since promoted entries persist.

## Maintenance and Repair

Refresh the index after editing memory files or changing memory configuration:

```sh
opensquilla memory index --force
```

Inspect fallback and repair surfaces:

```sh
opensquilla memory raw-fallbacks list
opensquilla memory repair list
```

Show or repair a degraded compaction memory record when instructed by
diagnostics:

```sh
opensquilla memory repair show --summary-id <id>
opensquilla memory repair run --summary-id <id>
```

## Best Practices

- Keep entries short and sourceable.
- Prefer "Remember X for project Y" over vague "remember this."
- Search memory before assuming the agent forgot.
- Remove or revise stale preferences instead of adding contradictory ones.
- Keep secrets out of memory.
- Use artifacts or files for large reference material.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
