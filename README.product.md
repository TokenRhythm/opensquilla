# OpenSquilla Product Guide

OpenSquilla is a token-efficient personal agent runtime for the terminal, a
local Web UI, and messaging channels. It is designed for users who want one
agent surface that can chat, use tools, remember useful context, run scheduled
work, publish artifacts, and route work across multiple LLM providers without
rewriting their workflow for each provider.

This guide is the product and usage entry point. The existing
[`README.md`](README.md) remains the package/release README.

## Start Here

1. Install OpenSquilla:

   ```sh
   uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
   ```

2. Configure your provider:

   ```sh
   opensquilla onboard
   ```

3. Start the gateway:

   ```sh
   opensquilla gateway run
   ```

4. Open the control UI:

   <http://127.0.0.1:18791/control/>

For platform-specific install paths and recovery steps, see
[`docs/quickstart.md`](docs/quickstart.md).

## Why OpenSquilla

OpenSquilla focuses on cost-per-success and long-running usefulness rather than
single-turn chat alone.

| Product capability | What users get |
| --- | --- |
| SquillaRouter | Local, on-device routing that chooses an appropriate model tier per turn so simple tasks avoid premium-model cost. |
| Tool compression | Large tool outputs stay useful without flooding the model context; raw results can be preserved while compact previews are sent to the model. |
| Meta-skills | Repeatable workflows can be packaged as composable skills, so users can turn recurring multi-step work into reusable agent routines. |
| Unified surfaces | CLI, Web UI, gateway RPC, and channels share the same runtime path, tools, memory, approvals, and usage accounting. |
| Durable sessions | Conversations, transcripts, compaction summaries, artifacts, cost, and replay data are persisted for later inspection. |
| Personal memory | User facts, notes, and task traces can be saved and recalled through local keyword and semantic search. |
| Multi-provider runtime | OpenRouter, OpenAI, Anthropic, Gemini, DeepSeek, DashScope, Ollama, and other provider-compatible backends can be configured through one schema. |
| Safe tool use | File, shell, web, memory, git, artifact, media, channel, and agent tools run behind policy layers and approval surfaces. |

## Documentation Map

| Need | Read |
| --- | --- |
| Install and run OpenSquilla | [`docs/quickstart.md`](docs/quickstart.md) |
| Choose the right workflow for a goal | [`docs/use-cases.md`](docs/use-cases.md) |
| Start, stop, expose, or troubleshoot the gateway | [`docs/gateway.md`](docs/gateway.md) |
| Configure providers, router, search, channels, memory, and permissions | [`docs/configuration.md`](docs/configuration.md) |
| Learn the CLI command groups | [`docs/cli.md`](docs/cli.md) |
| Use the local control console | [`docs/web-ui.md`](docs/web-ui.md) |
| Resume, export, abort, or delete sessions | [`docs/sessions.md`](docs/sessions.md) |
| Choose and inspect LLM providers/models | [`docs/providers-and-models.md`](docs/providers-and-models.md) |
| Configure web search | [`docs/search.md`](docs/search.md) |
| Understand the main product capabilities | [`docs/features.md`](docs/features.md) |
| Use SquillaRouter | [`docs/features/squilla-router.md`](docs/features/squilla-router.md) |
| Understand tool compression and tool-result handles | [`docs/features/tool-compression.md`](docs/features/tool-compression.md) |
| Use MetaSkills | [`docs/features/meta-skills.md`](docs/features/meta-skills.md) |
| Work with memory | [`docs/features/memory.md`](docs/features/memory.md) |
| Work with skills | [`docs/features/skills.md`](docs/features/skills.md) |
| Understand compaction, cache, and long-session continuity | [`docs/features/compaction-and-cache.md`](docs/features/compaction-and-cache.md) |
| Publish artifacts and use media features | [`docs/artifacts-and-media.md`](docs/artifacts-and-media.md) |
| Connect chat channels | [`docs/channels.md`](docs/channels.md) |
| Create durable named agents | [`docs/agents.md`](docs/agents.md) |
| Schedule recurring or one-time work | [`docs/scheduling.md`](docs/scheduling.md) |
| Understand tools, sandboxing, and approvals | [`docs/tools-and-sandbox.md`](docs/tools-and-sandbox.md) |
| Choose permissions and approval posture | [`docs/approvals-and-permissions.md`](docs/approvals-and-permissions.md) |
| Inspect usage and model cost | [`docs/usage-and-cost.md`](docs/usage-and-cost.md) |
| Diagnose and replay a turn | [`docs/diagnostics-and-replay.md`](docs/diagnostics-and-replay.md) |
| Connect MCP-capable clients | [`docs/mcp-server.md`](docs/mcp-server.md) |
| Run sessions, cron, diagnostics, migration, and MCP operations | [`docs/operations.md`](docs/operations.md) |
| Fix common install/runtime problems | [`docs/troubleshooting.md`](docs/troubleshooting.md) |
| Understand OpenSquilla terminology | [`docs/glossary.md`](docs/glossary.md) |

## The Fastest Useful Workflow

After the gateway is running, use the surface that fits the job:

```sh
opensquilla chat
```

Use this for interactive terminal work.

```sh
opensquilla agent -m "Summarize this repo and tell me what to test"
```

Use this for one-shot automation.

```sh
opensquilla gateway start --json
```

Use this for background Web UI, channels, and RPC clients.

```sh
opensquilla sessions list
opensquilla cost
opensquilla doctor
```

Use these to inspect history, cost, and readiness.

## Configuration Essentials

OpenSquilla loads configuration in this order:

1. `OPENSQUILLA_GATEWAY_CONFIG_PATH`
2. `./opensquilla.toml`
3. `~/.opensquilla/config.toml`
4. built-in defaults

Use the CLI for routine changes:

```sh
opensquilla onboard --if-needed
opensquilla configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
opensquilla configure router --router recommended
opensquilla configure search --search-provider duckduckgo
opensquilla configure search --search-provider tavily --api-key-env TAVILY_API_KEY
opensquilla configure channels
opensquilla config get llm.provider
opensquilla config set gateway.port 18791
```

See [`docs/configuration.md`](docs/configuration.md) for details.

## Feature Highlights

### SquillaRouter

SquillaRouter is OpenSquilla's local routing layer. It keeps lightweight tasks
on cheaper models and reserves stronger tiers for harder turns. Router
decisions stay local; the user's prompt is not sent to a separate external
classifier just to decide the model.

Read: [`docs/features/squilla-router.md`](docs/features/squilla-router.md)

### Tool Compression

Agent work often creates huge tool results: logs, web pages, search results,
spreadsheets, diffs, and JSON. OpenSquilla can keep the raw result available
while projecting a compact model-visible preview, reducing context pressure
without throwing away the user's working state.

Read: [`docs/features/tool-compression.md`](docs/features/tool-compression.md)

### Meta-Skills

Meta-skills let OpenSquilla present higher-level workflows instead of making the
user re-describe the same multi-step process. They are useful for repeatable
research reports, document-to-decision work, daily operating briefs, account
watching, job-search preparation, kid project planning, academic paper drafting,
and MetaSkill proposal creation.

By default, launch them deliberately with `/meta` and `/meta <name>`.

Read: [`docs/features/meta-skills.md`](docs/features/meta-skills.md),
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md),
and [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md)

## What OpenSquilla Can Do

- Run chat from Web UI, CLI, gateway RPC, terminal channels, and supported
  messaging platforms.
- Use tools for files, shell commands, code execution, git, web search/fetch,
  memory, sessions, artifacts, media, Feishu, scheduled jobs, and subagents.
- Install, inspect, publish, and compose skills.
- Schedule recurring runs with `opensquilla cron`.
- Save durable memory and search previous sessions.
- Track usage and estimated cost with `opensquilla cost`.
- Diagnose readiness with `opensquilla doctor` and `/control/` health views.
- Export reproducible install state with `opensquilla dist`.
- Remove OpenSquilla cleanly with `opensquilla uninstall` — keeps your data
  unless you pass `--purge-state` / `--purge-config` / `--purge-all`.
- Bridge OpenSquilla into MCP-capable clients with `opensquilla mcp-server run`
  when the `mcp` extra is installed.
- Create and deliver artifacts such as HTML files, PDF reports, slides,
  spreadsheets, generated images, and channel-delivered files.

## Safety Defaults

The gateway binds to `127.0.0.1` by default. Binding to public interfaces is
opt-in:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Do not expose a public gateway without token auth and a network boundary you
trust. For tool behavior, approval flow, and workspace containment, see
[`docs/tools-and-sandbox.md`](docs/tools-and-sandbox.md).

## Existing Reference Docs

- [`README.md`](README.md) - release/package README
- [`MIGRATION.md`](MIGRATION.md) - migration from OpenClaw and Hermes Agent
- [`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md) - MetaSkill user guide
- [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md) - MetaSkill authoring guide
- [`CONTRIBUTING.md`](CONTRIBUTING.md) - contributor workflow
- [`CHANGELOG.md`](CHANGELOG.md) - release history

## Improve the Documentation

OpenSquilla documentation is part of the product. If a setup step is confusing,
a command is stale, or a feature guide needs a clearer example, open a small
pull request against `main`. Existing `dev` pull requests may continue during
the branch transition.

Read [`docs/contributing-docs.md`](docs/contributing-docs.md) for docs-specific
guidance.

---

[Docs index](docs/README.md) · [Improve these docs](docs/contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml) · [Contributing](CONTRIBUTING.md)
