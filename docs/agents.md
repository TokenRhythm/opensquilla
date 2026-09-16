# Durable Agents

OpenSquilla agents are named runtime profiles. Use them when different work
streams need different defaults, such as a research workspace, a writing
workspace, or a channel-facing assistant.

The built-in `main` agent is always available. Additional agents are configured
with `opensquilla agents`.

## When to Create an Agent

Create a durable agent when you want a stable identity for:

- a dedicated workspace;
- a default model choice;
- a separate channel or automation target;
- a recurring task profile;
- a specialized assistant name and description.

Do not create a new agent for every conversation. Use sessions for ordinary
conversation continuity.

## List Agents

```sh
opensquilla agents list
opensquilla agents list --json
```

## Add an Agent

```sh
opensquilla agents add research \
  --name Research \
  --description "Research and synthesis workspace" \
  --workspace /path/to/research \
  --model gpt-5.4-mini
```

Agent changes are written to configuration. Restart the gateway before relying
on the updated agent list:

```sh
opensquilla gateway restart
```

## Use an Agent With Sessions

Filter sessions by agent:

```sh
opensquilla sessions list --agent research
```

Create scheduled work for an agent:

```sh
opensquilla cron add \
  --agent research \
  --every 1h \
  --text "Summarize new research notes" \
  --name research-hourly-summary
```

Channel configuration can also route incoming messages to configured agents
depending on the channel setup.

## Delete an Agent

```sh
opensquilla agents delete research
opensquilla agents delete research --force
```

Deleting an agent entry leaves workspace files and state untouched. Clean those
up separately only when you are sure they are no longer needed.

## Agents vs Sessions vs Skills

| Concept | Use for |
| --- | --- |
| Agent | Durable identity and defaults for a work stream. |
| Session | Conversation history and active task continuity. |
| Skill | Reusable workflow instructions or tool routines. |
| Meta-skill | A composed workflow made from multiple skill steps. |

Read next:

- [`sessions.md`](sessions.md)
- [`features/skills.md`](features/skills.md)
- [`features/meta-skills.md`](features/meta-skills.md)
- [`channels.md`](channels.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
