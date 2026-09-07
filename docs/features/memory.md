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

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
