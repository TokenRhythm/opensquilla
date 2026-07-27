# OpenSquilla Documentation

This directory is the user-facing product documentation set. It complements the
root release README with task-oriented guides.

## Read First

1. [`quickstart.md`](quickstart.md) - install, configure, run, and open the Web UI.
2. [`use-cases.md`](use-cases.md) - task-oriented recipes for common goals.
3. [`gateway.md`](gateway.md) - gateway lifecycle, host/port, safety, and status.
4. [`configuration.md`](configuration.md) - provider, router, search, channel,
   memory, and permission configuration.
5. [`cli.md`](cli.md) - command groups and common CLI workflows.
6. [`tui.md`](tui.md) - terminal chat usage, slash commands, files, sessions,
   and the development-only OpenTUI client.
7. [`web-ui.md`](web-ui.md) - local control console and chat UI.
8. [`sessions.md`](sessions.md) - session continuity, export, resume, abort,
   and cleanup.
9. [`glossary.md`](glossary.md) - user-facing terminology.

## Feature Guides

- [`features.md`](features.md) - capability catalog.
- [`features/squilla-router.md`](features/squilla-router.md) - model routing.
- [`features/tui-frontend.md`](features/tui-frontend.md) - terminal backend
  architecture, plugin slots, Router HUD, and OpenTUI validation.
- [`features/tui-product-contract.md`](features/tui-product-contract.md) - TUI,
  Web UI, Gateway, standalone, fallback, and legacy ownership rules.
- [`features/tool-compression.md`](features/tool-compression.md) - compact tool
  results and handles.
- [`features/meta-skills.md`](features/meta-skills.md) - reusable workflow skills.
- [`features/meta-skill-user-guide.md`](features/meta-skill-user-guide.md) -
  user-facing MetaSkill guide.
- [`authoring/meta-skills.md`](authoring/meta-skills.md) - MetaSkill authoring
  guide.
- [`features/memory.md`](features/memory.md) - durable memory and recall.
- [`features/skills.md`](features/skills.md) - skill discovery, install, and
  authoring.
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md) -
  long-session compaction and prompt-cache continuity.

## Surfaces and Operations

- [`releases/0.5.0.md`](releases/0.5.0.md) - OpenSquilla 0.5.0 release notes.
- [`releases/0.5.0rc3.md`](releases/0.5.0rc3.md) - OpenSquilla 0.5.0 Preview 3 release notes.
- [`releases/0.5.0rc2.md`](releases/0.5.0rc2.md) - OpenSquilla 0.5.0 Preview 2 release notes.
- [`releases/0.5.0rc1.md`](releases/0.5.0rc1.md) - OpenSquilla 0.5.0 Preview 1 release notes.
- [`releases/0.4.1.md`](releases/0.4.1.md) - OpenSquilla 0.4.1 release notes.
- [`releases/0.4.0.md`](releases/0.4.0.md) - OpenSquilla 0.4.0 release notes.
- [`releases/0.3.0.md`](releases/0.3.0.md) - OpenSquilla 0.3.0 release notes.
- [`channels.md`](channels.md) - supported messaging channels and setup flow.
- [`providers-and-models.md`](providers-and-models.md) - LLM provider catalog,
  model selection, and runtime-backed model inspection.
- [`search.md`](search.md) - web search providers and query workflow.
- [`artifacts-and-media.md`](artifacts-and-media.md) - artifacts, generated
  files, images, PDF, and TTS.
- [`tools-and-sandbox.md`](tools-and-sandbox.md) - built-in tools, approvals,
  sandbox posture, and write policy.
- [`approvals-and-permissions.md`](approvals-and-permissions.md) - permission
  profiles, approval commands, workspace containment, and sandbox posture.
- [`agents.md`](agents.md) - durable named agents and workspace defaults.
- [`scheduling.md`](scheduling.md) - recurring and one-time scheduled work.
- [`mcp-server.md`](mcp-server.md) - MCP server bridge for MCP-capable clients.
- [`usage-and-cost.md`](usage-and-cost.md) - token usage, estimated cost, and
  cost investigation workflow.
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md) - diagnostics,
  raw capture guidance, read-only turn replay, and developer replay benchmarks.
- [`tui-real-terminal-harness.md`](tui-real-terminal-harness.md) - maintainer
  real-terminal TUI integration harness and evidence capture.
- [`experiments.md`](experiments.md) - opt-in runtime toggle conventions and
  the delivery-verification tooling in `scripts/experiments/`.
- [`docker.md`](docker.md) - Docker/Compose deployment on home servers and
  NAS: prebuilt GHCR images, LAN exposure with token auth, and upgrades.
- [`operations.md`](operations.md) - sessions, cron, usage, diagnostics,
  migration, MCP server, and install inventory commands.
- [`troubleshooting.md`](troubleshooting.md) - common install/runtime issues.
- [`glossary.md`](glossary.md) - short definitions for product terms.

## Improve These Docs

Documentation improvements are welcome. Start with
[`contributing-docs.md`](contributing-docs.md) for docs-specific guidance, then
open a small pull request against `main`.

Fast paths:

- Report a stale command, broken link, or confusing page with the
  [documentation issue template](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml).
- Edit the affected Markdown page on GitHub and open a focused pull request
  against `main`.
- For new feature documentation, keep independent features on independent pages
  under `docs/features/`.

## Design Principle

OpenSquilla documentation should help users run the product first, then
understand its special advantages. Mechanism-heavy runtime detail belongs in
developer design notes or source comments, not in the first-run path.

---

[Product guide](../README.product.md) · [中文](../README.zh-Hans.md) · [日本語](../README.ja.md) · [Français](../README.fr.md) · [Deutsch](../README.de.md) · [Español](../README.es.md) · [Improve these docs](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml) · [Contributing](../CONTRIBUTING.md)
