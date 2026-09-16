# Glossary

This glossary defines common OpenSquilla terms in user-facing language. It is
not a runtime design document.

## Agent

A named OpenSquilla identity with defaults such as model, workspace, name, and
description. The built-in `main` agent is always available.

Read: [`agents.md`](agents.md)

## Artifact

A file or media output produced by a run, such as an HTML page, report, image,
spreadsheet, PDF, or slide deck.

Read: [`artifacts-and-media.md`](artifacts-and-media.md)

## Approval

A human decision required before a sensitive tool action can continue. Approval
behavior depends on the surface, permission profile, and tool policy.

Read: [`approvals-and-permissions.md`](approvals-and-permissions.md)

## Channel

A messaging integration such as Telegram, Slack, Feishu/Lark, Discord, DingTalk,
WeCom, Matrix, terminal, or websocket-style clients.

Read: [`channels.md`](channels.md)

## Compaction

The process of reducing old context in a long session so the agent can continue
within the model's context budget.

Read: [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

## Diagnostics

Runtime logging controls used to understand routing, provider behavior,
compaction, tool compression, cache behavior, and delivery failures.

Read: [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

## Gateway

The local server behind the Web UI, channels, sessions, approvals, diagnostics,
usage, and RPC clients.

Read: [`gateway.md`](gateway.md)

## Memory

Durable user or project context that can be searched and recalled later without
stuffing every old transcript into the active prompt.

Read: [`features/memory.md`](features/memory.md)

## MetaSkill

A reusable, auditable workflow protocol that composes multiple skills, tools,
LLM calls, checks, or output steps into one repeatable capability.

Read: [`features/meta-skills.md`](features/meta-skills.md) and
[`features/meta-skill-user-guide.md`](features/meta-skill-user-guide.md)

## Permission Profile

The chosen tool-access posture for a run, such as `restricted`, `on`, `bypass`,
or `full`.

Read: [`approvals-and-permissions.md`](approvals-and-permissions.md)

## Provider

An LLM backend configured for OpenSquilla, such as TokenRhythm, OpenRouter,
OpenAI, Anthropic, Gemini, DeepSeek, DashScope, or Ollama.

Read: [`providers-and-models.md`](providers-and-models.md)

## Replay

A read-only view of a recorded turn from the decision log. Replay does not
re-run tools.

Read: [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

## Scheduler

The `opensquilla cron` feature for recurring and one-time OpenSquilla runs.

Read: [`scheduling.md`](scheduling.md)

## Session

A durable conversation or task history. Sessions can be listed, resumed,
exported, aborted, or deleted.

Read: [`sessions.md`](sessions.md)

## Skill

A reusable package of task-specific guidance, scripts, or workflow instructions
that OpenSquilla can load when needed.

Read: [`features/skills.md`](features/skills.md)

## SquillaRouter

OpenSquilla's local routing layer for choosing an appropriate model tier per
turn.

Read: [`features/squilla-router.md`](features/squilla-router.md)

## Tool Compression

A context-saving feature that keeps large tool results useful while sending a
smaller preview to the model.

Read: [`features/tool-compression.md`](features/tool-compression.md)

## Workspace

The local directory a task is allowed or expected to work in. Workspace flags
help contain file and shell work.

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
