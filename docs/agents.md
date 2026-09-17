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

## Workspace Files and Default Upgrades

New agent workspaces contain `AGENTS.md`, `SOUL.md`, `IDENTITY.md`,
`USER.md`, `MEMORY.md`, and the `memory/` directory. `AGENTS.md` starts
empty; add workspace operating rules or tool-use conventions when needed.
An empty AGENTS file is valid and adds no prompt content.

`BOOTSTRAP.md`, `HEARTBEAT.md`, and `TOOLS.md` are retired as core workspace
files. They are no longer generated or automatically loaded, and the core
`agents.files` RPCs no longer manage them. Old files and onboarding state stay
on disk. Ordinary file tools can still access them when requested; old custom
instructions may also ask the model to read them.

On a standard Gateway or standalone CLI startup, the held profile writer lease
allows a narrow upgrade of the two defaults replaced by this change:

- An unchanged old AGENTS template becomes empty.
- An unchanged old SOUL template routes tool notes to AGENTS instead of TOOLS.
- Custom text is preserved. Matching tolerates only UTF-8 BOM and LF/CRLF
  differences; it does not guess from headings or keywords.
- Original bytes are retained under
  `.opensquilla/template-backups/md-retirement-v1/` inside that workspace,
  named with the filename and original SHA-256 digest.

File-read RPCs and stateless runs never trigger this upgrade. Embedders without
the profile lease skip it. Unsafe links, read-only files, conflicting backups,
backup failures, or detected concurrent edits leave the source unchanged and
produce a diagnostic. The last check and atomic replacement are not a
cross-process content compare-and-swap against arbitrary external editors.
Repeated startup does not re-upgrade a successfully updated file.

To roll back, stop the service, revert to the previous software version, and
restore the desired file from its backup. Reverting only the file to the exact
old default while keeping the new version makes it eligible again on startup.
No cron tasks are created, moved, or changed by this upgrade.

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
