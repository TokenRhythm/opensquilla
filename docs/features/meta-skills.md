# Meta-Skills

Meta-skills package repeatable multi-step work as reusable, inspectable
workflows. Use them when a request needs more than one normal skill, tool,
checkpoint, or final synthesis pass.

For the full user-facing guide, read
[`meta-skill-user-guide.md`](meta-skill-user-guide.md). For authoring rules,
read [`../authoring/meta-skills.md`](../authoring/meta-skills.md).

## Skills vs Meta-Skills

| Capability | Use it for |
| --- | --- |
| Skill | One focused task pattern, instruction set, script, or tool helper. |
| Meta-skill | A reusable workflow made of multiple steps, skills, checks, or outputs. |

For example, "summarize this document" is skill-shaped. "Draft, validate,
compile, and deliver a cited research paper" is meta-skill-shaped.

## Stable Built-In MetaSkills

The retained stable catalog is intentionally small:

| MetaSkill | Positioning |
| --- | --- |
| `meta-paper-write` | Supports academic drafts, manuscript structure, citation planning, experiment placeholders, and LaTeX/PDF paths. |
| `meta-short-drama` | Produces short-drama scripts, visual prompts, subtitles, and local video artifacts. |
| `meta-skill-creator` | Turns repeated multi-skill collaboration patterns into new MetaSkill proposals. |

Experimental meta-skills may exist under development trees, but this page lists
only bundled built-ins that should be presented as retained product
capabilities.

`meta-kid-project-planner` is retired. Its bundled definition is retained only
as a compatibility tombstone so persisted or in-flight runs remain inspectable
and recoverable after upgrade. It is excluded from `/meta`, completion,
automatic triggering, and all new invocations.

## Requirements and Setup Lifecycle

Launch preflight recursively rolls up hard requirements from every referenced
child skill. A blocked run is not stamped or hidden in history. On Web chat, a
supported missing toolchain produces a confirmation card; after consent,
OpenSquilla downloads from its fixed catalog, verifies the artifact, runs the
workflow-specific capability smoke test, activates it under
`state/toolchains/v1`, and resumes the original request. Install failure is
recoverable through Retry and never silently degrades the requested output.

- `meta-paper-write` probes `xelatex`, `bibtex`, bibliography output, hyperlinks,
  tables/math, and CJK rendering. Managed TinyTeX 2026.05 archives cover macOS,
  supported Linux architectures, and Windows x64 together with a pinned Noto
  CJK font. OpenSquilla uses the ordinary Windows ZIP and never executes the
  upstream self-extracting installer. Fixed downloads are about 226 MB on macOS,
  165–172 MB on Linux, and 265 MB on Windows; installed size is larger. The
  install is self-contained and does not update `tlmgr`.
- `meta-short-drama` probes `ffmpeg`, `ffprobe`, required filters/codecs, and a
  CJK font. Linux glibc arm64/x64 and Windows x64 use pinned archives. macOS
  12 or later uses pinned FFmpeg 8.1.2 and FFprobe 8.1.2 ZIPs selected for Apple
  Silicon or Intel, totaling about 76 MB or 87 MB respectively with their
  supporting assets. The build source is pinned to commit
  `bb1d6db29cee948f9685bcd69e6caf17d960662b`. OpenSquilla verifies every
  original archive by fixed byte size and SHA-256 before extraction, then
  removes the binaries' invalid embedded signatures, applies local ad-hoc
  signatures, and requires strict `codesign` verification. This does not provide
  a Developer ID signature or Apple notarization. The Noto CJK font and its
  license are also checksum-verified before installation.
- Real image/audio/video generation for `meta-short-drama` and
  `AwesomeWebpageMetaSkill` currently resolves the OpenRouter capability from
  the existing provider configuration: an active OpenRouter deployment, a saved
  secondary `llm_profiles.openrouter` connection, the legacy image-provider
  connection, or the canonical provider environment. If none is ready, Web chat
  keeps the original request in the current tab and deep-links to the ordinary
  provider settings editor. Adding OpenRouter while another provider is primary
  saves a secondary profile; it does not switch the primary model or enable
  cross-provider routing. Saving a connection does not generate media or incur a
  generation charge. Each workflow has an explicit boundary before paid
  provider submits. Short-drama revisions require another preview and approval;
  AwesomeWebpage accepts only its exact provider-send-and-cost approval choice,
  with no default or natural-language prefill, and treats edits/ambiguous notes
  as not approved. Only secret-free readiness and provenance labels enter run/UI data;
  execution receives one volatile provider/key/endpoint/proxy tuple scoped to
  the exact bundled media child. The setup contract carries
  provider and capability identifiers rather than an OpenRouter-specific UI
  shape, so a future media adapter can add another provider without creating a
  separate workflow-specific settings area. Capability requirements carry an
  ordered list of code-owned provider candidates and a profile preference;
  OpenRouter is the only currently implemented candidate.

Pinned downloads disclose source, license, version, and verified bytes before
installation. A normal uninstall keeps managed toolchains;
`opensquilla uninstall --purge-state` removes OpenSquilla-managed toolchain
state.

## Run MetaSkills

MetaSkills are manual-only by default. They do not auto-trigger from message
keywords or appear in the runtime prompt unless you explicitly opt into the old
automatic behavior.

In Web chat and the CLI gateway TUI:

```text
/meta
/meta meta-paper-write
```

`/meta` lists available MetaSkills. `/meta <name>` starts the selected
workflow. Channel surfaces can list MetaSkills with `/meta`, but they do not run
MetaSkills from chat text. Standalone CLI chat requires gateway mode for
`/meta`.

To restore automatic model-triggered behavior, set:

```toml
[meta_skill]
auto_trigger = true
```

Use this compatibility mode only when you want MetaSkills to be considered by
the model during ordinary chat turns.

## How to Prepare the Request

Ask for the outcome and the standard:

```text
Draft a compact research paper on retrieval-augmented customer support. Include
a citation plan, experiment placeholders, and a compiled PDF.
```

When you start a workflow, include the task after the command:

```text
/meta meta-paper-write

Draft a compact research paper on retrieval-augmented customer support. Include
a citation plan, experiment placeholders, and a compiled PDF.
```

A strong request usually includes:

- outcome;
- context;
- decision standard;
- expected output;
- constraints;
- actions the agent must not take.

## Discover Meta-Skills

Use chat slash commands for the runtime list:

```text
/meta
```

Use the CLI for inventory and inspection:

```sh
opensquilla skills list
opensquilla skills search meta
```

Inspect a meta-skill composition:

```sh
opensquilla skills inspect <meta-skill-name>
```

The inspect command shows the compiled step shape before you rely on a workflow.

## Inspect Run History

List recent runs:

```sh
opensquilla skills meta runs list
```

Inspect one run:

```sh
opensquilla skills meta runs show <run-id>
opensquilla skills meta runs steps <run-id>
opensquilla skills meta runs failures --since 24h
```

Preview replay shape without executing live work:

```sh
opensquilla skills meta runs replay <run-id> --dry-run
```

## Proposals

Meta-skill creation workflows may write proposals before they become managed
skills. Inspect proposals:

```sh
opensquilla skills meta proposals list
opensquilla skills meta proposals show <proposal-id>
```

Accept a proposal only after review:

```sh
opensquilla skills meta proposals accept <proposal-id>
```

## Safety Model

MetaSkill outputs are reviewable work products and decision-support drafts. They
are not final professional advice in legal, medical, financial, hiring,
academic, security, or other high-stakes contexts.

Actions such as publishing, applying, installing, paying, signing, messaging, or
modifying production systems require explicit user authorization.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
