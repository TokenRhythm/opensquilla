# OpenSquilla MetaSkill User Guide

MetaSkill lets OpenSquilla move from figuring out complex work from scratch on
every turn to reusable, explicitly launchable, auditable, and improvable task
protocols.

A normal conversation solves one request. A MetaSkill preserves a way of doing
high-value work.

## Important Notice

Some MetaSkills in OpenSquilla, and some of the skills they call, are authored,
revised, or composed with AI assistance based on intended functionality,
available capabilities, and usage scenarios.

This means:

- MetaSkills are not merely a collection of fully hand-written scripts. They are
  part of a system where AI can help formalize and evolve reusable task
  protocols.
- AI-authored or AI-assisted MetaSkills should be reviewed through structural
  validation, trigger-surface checks, runtime testing, human review, and
  safety-boundary assessment before they are treated as ready for use.
- MetaSkill outputs are decision-support materials and work-product drafts. They
  are not final professional advice in legal, medical, financial, hiring,
  academic, security, or other high-stakes contexts.
- Actions such as publishing, applying, installing, paying, signing, messaging,
  or modifying production systems require explicit user authorization and remain
  the user's responsibility.
- When a MetaSkill relies on search, document parsing, LLM judgment, or
  third-party tools, the result may be affected by source quality, model
  limitations, tool availability, context completeness, and time-sensitive
  changes.
- Users should review facts, citations, assumptions, risks, and unverifiable
  claims, especially in high-stakes situations.

In short: MetaSkill turns high-value work into reusable, auditable, and
improvable AI collaboration protocols. It does not remove the need for review,
judgment, or accountability.

## What It Is

OpenSquilla is an open-source AI agent runtime. MetaSkill is its task-protocol
layer.

A MetaSkill does not introduce new execution atoms. It defines a way to organize
existing atoms, such as skills, tools, LLM calls, and sub-agents, into a
reusable task protocol.

The analogy is a Makefile and shell commands. A Makefile does not replace
commands; it defines how commands are composed. A MetaSkill does not replace
skills or tools; it tells OpenSquilla how a class of high-value work should be
understood, structured, checked, and delivered.

MetaSkill provides four main advantages:

- protocolized capability captured in a `SKILL.md` file with `kind: meta` and
  `composition.steps`;
- explicit launch through `/meta`, with optional automatic triggering only when
  `meta_skill.auto_trigger = true`;
- auditable and replayable step inputs, outputs, status, and results;
- improvable over time as repeated collaboration patterns become proposals.

## Default Launch Model

MetaSkills are manual-only by default. On supported chat surfaces, use `/meta`
to list available workflows and `/meta <name>` to run one. This keeps workflow
launches deliberate, reviewable, and easier to explain.

Web chat and the CLI gateway TUI support both list and run:

```text
/meta
/meta meta-paper-write
```

Channel surfaces support `/meta` listing only. Standalone CLI chat requires
gateway mode for `/meta`.

To restore the older automatic behavior, set:

```toml
[meta_skill]
auto_trigger = true
```

With `auto_trigger = true`, OpenSquilla may consider MetaSkills during ordinary
natural-language turns. Leave it off when you want workflows to run only after
an explicit `/meta <name>` command.

## User Mental Model

Using a MetaSkill is not just asking a question. It is delegating OpenSquilla to
produce a reviewable result.

A strong MetaSkill request contains four things:

1. Outcome: what you want to receive.
2. Context: materials, entities, time range, and constraints that matter.
3. Standard: what "good" means for this task.
4. Boundaries: what must not happen, what must not be invented, and what requires
   confirmation.

Example:

```text
/meta meta-paper-write

I need a compact research paper, not a generic essay.
Use verifiable sources and distinguish evidence from placeholders.
Include the LaTeX source and compiled PDF.
Do not invent citations or experimental results.
```

The user defines the target and standard; OpenSquilla organizes the execution.

## Current Built-In MetaSkills

The retained built-in MetaSkills cover a focused set of high-value task classes.

| MetaSkill | Positioning |
| --- | --- |
| `meta-paper-write` | Supports academic drafts, manuscript structure, citation planning, experiment placeholders, and LaTeX/PDF paths. |
| `meta-short-drama` | Produces short-drama scripts, visual prompts, video assembly plans, subtitles, and rendered local video artifacts. |
| `AwesomeWebpageMetaSkill` | Builds a packaged local multimedia webpage with researched content, generated media, validation, and usage guidance. |
| `meta-skill-creator` | Turns repeated multi-skill collaboration patterns into new MetaSkill proposals. |

These are designed around quality over quantity. Immature, duplicate, or
single-skill wrapper MetaSkills should not remain in the bundled catalog.

`meta-kid-project-planner` is retired. Its bundled definition remains only as
an upgrade-compatibility tombstone for inspecting, resuming, or replaying
persisted runs. It is not shown by `/meta` and cannot start a new run or
auto-trigger.

## Requirements and Managed Setup

You can start with `/meta <name>` without installing tools manually. Every new
run performs a server-side readiness check before it creates hidden workflow
state or makes a paid provider request. When a supported local dependency is
missing, Web chat follows this flow:

1. Show a setup card naming the missing capability, source, version, license,
   and known download size.
2. Wait for explicit confirmation. Closing or cancelling the card changes
   nothing.
3. Download from a code-owned catalog, verify the recorded size and SHA-256,
   extract into user-local state, and run a real capability smoke test.
4. Activate the verified package and automatically resume the original request.
   Failed or interrupted installs leave the workflow unstarted and offer Retry.

The active install is kept below the OpenSquilla state directory at
`state/toolchains/v1`; it does not modify the user's shell profile or global
`PATH`. A normal `opensquilla uninstall` preserves it with other user state.
`opensquilla uninstall --purge-state` removes OpenSquilla-managed archives,
receipts, and activations.

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Paper (`xelatex`, `bibtex`, CJK/refs smoke test) | Pinned TinyTeX 2026.05 universal archive plus pinned Noto CJK | Pinned TinyTeX 2026.05 plus pinned Noto CJK for glibc arm64/x64 and musl x64 | Pinned ordinary TinyTeX 2026.05 ZIP plus pinned Noto CJK; self-extracting installers are never executed |
| Short-drama rendering (`ffmpeg`, `ffprobe`, filters/codecs, CJK font) | Pinned FFmpeg/FFprobe 8.1.2 ZIPs for Apple Silicon or Intel plus pinned Noto CJK; macOS 12+ | Pinned GPL archive on glibc 2.28+ arm64/x64, kernel 4.18+ | Pinned x64 archive |

All managed executable archives, license files, and fonts are versioned and
checksum-verified. The macOS short-drama download totals about 76 MB on Apple
Silicon or 87 MB on Intel. Its FFmpeg and FFprobe 8.1.2 ZIPs come from a build
whose source is pinned to commit
`bb1d6db29cee948f9685bcd69e6caf17d960662b`. OpenSquilla verifies each original
archive's fixed size and SHA-256 first. It then removes the binaries' invalid
embedded signatures, applies local ad-hoc signatures, and requires strict
`codesign` verification before activation. The result is neither Developer ID
signed nor Apple-notarized. Paper setup is self-contained after
its two downloads; it never updates `tlmgr` or resolves a moving TeX Live
package repository during installation. Fixed paper downloads total about 226
MB on macOS, 165–172 MB on Linux, and 265 MB on Windows; extracted
installations are larger.

`meta-short-drama` and `AwesomeWebpageMetaSkill` also need an OpenRouter
connection for their real media provider calls. Readiness reuses an active
OpenRouter provider, a saved
secondary `llm_profiles.openrouter` profile, the legacy image-provider
connection, or the canonical provider environment without copying a secret into
run metadata. If none is ready, the setup card opens the existing provider
settings editor and preserves the original MetaSkill request. When another
provider is primary, saving OpenRouter creates a secondary profile and does not
switch the primary model or enable routing. Saving or rechecking the connection
does not make a media request. Short-drama's later script-review confirmation
is still the explicit boundary before paid image/video submits; an edit request
only creates a revised preview and requires a new approval. AwesomeWebpage has
its own required, no-default provider-send-and-cost approval choice. Ambiguous
answers or any revision note do not authorize image/audio/video submission.
Both workflows lease the credential, endpoint, and proxy to exact bundled media
children in process memory only; these values never enter the plan, transcript,
or run database. A missing connection blocks the run instead of producing a
fake local substitute. Provider requirements are expressed as ordered
code-owned candidates with a profile preference; OpenRouter is the only current
candidate, so future providers do not require workflow-specific settings UI.

An existing system installation may satisfy readiness when it passes the same
full capability probe. The Skill page remains useful for inventory, but launch
preflight—not a stale UI snapshot—is the final source of truth.

## Two Ways to Use MetaSkill

### Default: Explicit Command

Start the workflow with `/meta <name>` and then describe the outcome:

```text
/meta meta-paper-write

Draft a compact research paper on retrieval-augmented customer support. Include
a citation plan, experiment placeholders, and a compiled PDF.
```

This is the normal 0.4 release-line path. It is best for important, expensive,
or easily confused tasks because the workflow launch is explicit.

### Compatibility: Automatic Triggering

If `meta_skill.auto_trigger = true` is set, OpenSquilla can consider MetaSkills
from natural-language intent:

```text
Use meta-skill `meta-paper-write`.

Draft a compact research paper on retrieval-augmented customer support. Include
a citation plan, experiment placeholders, and a compiled PDF.
```

This mode is for users who intentionally want the older auto-trigger behavior.
It is not the default.

## Low-Cost, High-Quality Request Template

Recommended template:

```text
/meta <name>

Outcome:
Context:
Decision standard:
Expected output:
Constraints:
Do not:
```

Example:

```text
/meta meta-paper-write

Outcome: produce a compact research paper and PDF.
Context: retrieval-augmented generation for customer-support knowledge bases.
Decision standard: source-grounded, concise, and explicit about missing
experimental evidence.
Expected output: manuscript structure, verified citation plan, LaTeX, and PDF.
Constraints: keep unverified results as placeholders.
Do not: invent sources, measurements, or comparisons.
```

Useful constraints:

- Do not invent missing facts.
- Separate facts, assumptions, and recommendations.
- Use only pasted material unless sources are available.
- Do not submit, publish, install, pay, send, or sign automatically.
- Ask me if a decision depends on missing information.

## Built-In MetaSkill Usage Patterns

### `meta-paper-write`

Use for academic papers, research manuscripts, and LaTeX-oriented deliverables.

Good fit:

- compact paper skeleton;
- section structure;
- citation plan;
- experiment and figure/table placeholders;
- LaTeX/PDF path when explicitly requested.

PDF compilation requires the paper capability probe to pass. If it does not,
confirm the managed setup card; on an unsupported platform, install a compatible
TeX distribution yourself and retry so OpenSquilla can probe it.

High-quality request:

```text
/meta meta-paper-write

Draft a compact research paper skeleton on retrieval-augmented generation for
customer-support knowledge bases.

Include:
- title
- abstract
- related work plan
- method outline
- experiment placeholders
- figure/table placeholders
- citation plan

Keep it compact first. Do not write a full manuscript unless I ask.
```

Expected result: a paper-shaped deliverable, not a generic essay. Citations
should not be presented as verified sources unless actually verified.

### `meta-skill-creator`

Use to create a new MetaSkill proposal.

Good fit:

- turning repeated multi-skill collaboration into a reusable capability;
- defining trigger surfaces;
- composing existing skills;
- adding validation and risk checks;
- producing a proposal for review.

Poor fit:

- creating a normal single-purpose skill;
- analyzing existing skill lists without creating anything;
- asking what MetaSkill is;
- pasting old pages for diagnosis.

High-quality request:

```text
/meta meta-skill-creator

Create a new meta-skill for product launch briefs. It should search current
sources, collect product context, draft a launch memo, generate a DOCX handoff,
check evidence gaps, and avoid publishing anything automatically.

Please propose:
- name
- description
- triggers
- steps
- validation gates
- collision checks
```

Expected result: a proposal, not an immediate unreviewed production rollout.

## Avoiding Accidental Activation

If you paste old chat history, Web UI dumps, prompt examples, skill lists, or
test material, mark it as quoted context:

```text
The following is quoted context, not my current request.
Do not run any skill.
Do not create or persist any proposal.
Only analyze this text.
```

This matters because historical material may contain trigger words. Without a
clear boundary, the system may confuse quoted content with current intent.

If you only want to analyze a MetaSkill and do not want proposal creation:

```text
Only analyze. Do not create, assemble, preview, or persist any meta-skill
proposal.
```

## Run Progress Ribbon

While a MetaSkill runs, the WebUI shows a horizontal ribbon at the top
of the agent reply listing every step in the workflow. The currently
running chip is highlighted; succeeded steps show ✓, skipped ↷, failed
✗, and `on_failure` substitutes show ⇄. Click any chip to scroll to
that step's tool card. If a step fails, the ribbon also surfaces
"Retry run", "Switch meta-skill", and "Show error detail" actions
inline.

The ribbon survives disconnects: when the browser reconnects, the gateway
replays the announce → state → completed events so the ribbon rebuilds
to the latest state.

## Reading the Result

A strong MetaSkill result should explain:

- what it produced;
- what facts or sources it used;
- what is inferred or assumed;
- what risks remain;
- what the next action is;
- what could not be verified;
- whether any artifact or proposal was actually created.

Be cautious if the output:

- claims current facts without sources;
- claims a file was created but no artifact exists;
- hides tool failures as success;
- gives generic advice instead of the requested deliverable;
- ignores "do not create", "do not send", "do not publish", or "do not install".

## Correcting a Bad Run

If the wrong MetaSkill triggered:

```text
Stop using the previous MetaSkill. Treat my earlier text as context only. Now
use meta-skill `<correct_name>` for this goal: ...
```

If no MetaSkill triggered:

```text
Please rerun and explicitly use meta-skill `<name>`.
```

If the output is too generic:

```text
Redo this as a decision-ready deliverable with evidence, assumptions, risks, and
next actions.
```

If creator starts creating but you do not want creation:

```text
Do not create, assemble, preview, or persist any meta-skill proposal. Only
analyze.
```

## Building Your Own MetaSkill

A task is a good MetaSkill candidate when:

- you repeatedly perform the same high-value task;
- each run has multiple steps;
- inputs are similar but details vary;
- the output format is relatively stable;
- review, audit, replay, or confirmation matters;
- ordinary prompts require you to restate too many rules every time.

Poor candidates include one-line fact queries, single tool calls, casual
conversation, brainstorming without stable output criteria, and high-risk
automated action without human confirmation.

For the authoring protocol, read [`../authoring/meta-skills.md`](../authoring/meta-skills.md).

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
