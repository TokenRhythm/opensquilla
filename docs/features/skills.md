# Skills

Skills are task-specific instruction packages and scripts. They let OpenSquilla
load relevant guidance only when a task needs it, instead of putting every
possible instruction into every prompt.

Skills are separate from memory. Memory stores facts; skills describe repeatable
ways to work.

## What Skills Are For

Use skills for repeatable work patterns such as:

- deep research;
- summarization;
- GitHub and PR workflows;
- document generation;
- spreadsheet, slide, PDF, and DOCX work;
- web search;
- weather lookup;
- terminal or tmux monitoring;
- subagent delegation;
- skill creation and review.

If the workflow combines multiple skills or a reusable multi-step plan, use a
meta-skill instead.

## Discover Installed Skills

List skills available in the current install:

```sh
opensquilla skills list
```

View one skill:

```sh
opensquilla skills view <skill-name>
```

Search community sources:

```sh
opensquilla skills search pdf
```

Some skills may be ineligible when optional dependencies are missing or when the
skill is intentionally demo-only. Use `skills doctor` when you need to distinguish
installation, loader acceptance, precedence, and dependency readiness:

```sh
opensquilla skills doctor
opensquilla skills doctor <skill-name-or-install-id> --json
```

## Install, Update, and Remove Skills

Install a managed skill:

```sh
opensquilla skills install <clawhub-install-reference> --source clawhub
opensquilla skills install <owner/repo[@ref][:subpath]> --source github
```

Update one skill or all managed skills:

```sh
opensquilla skills update <skill-name>
opensquilla skills update --install-id <install-id>
opensquilla skills update --all
```

When the heuristic scanner requests confirmation, review the returned findings
and `confirmationToken`, then retry that exact artifact with both
`--force --risk-confirmation <confirmation-token>`. The token is bound to the
resolved source revision and fetched content; a changed artifact requires a new
review. Confirmation does not bypass archive, path, digest, transaction, or
postflight validation.

Remove a managed skill:

```sh
opensquilla skills uninstall <skill-name>
opensquilla skills uninstall --install-id <install-id>
```

OpenSquilla currently supports single-root, instruction-first Community Skills
from ClawHub and GitHub. A flat package or one wrapper directory is accepted. A
GitHub branch or tag is resolved to an immutable commit before files are fetched.
An install commits content and provenance; it does not install declared runtime
dependencies.

An installed Skill is not necessarily usable. It may be shadowed by a
higher-precedence Skill, disabled by configuration, or require setup. Online
install results become observable to agent turns from the next turn because the
current turn keeps a pinned catalog. Offline CLI installs are only validated for
the next Gateway start; activation and readiness are evaluated at that start.

Community package identity, the safe managed-directory key, and the runtime
frontmatter `name` are tracked separately. A package can therefore retain an
ecosystem-native runtime name without using that value as a filesystem path.
When multiple packages expose the same runtime name, exact mutations use the
install identity; ambiguous name-only mutations fail without changing either
installation.

This is not full Claude Skills or OpenClaw Skills execution compatibility.
Direct `/skill` commands, argument substitution, scoped tool permissions, hooks,
context forks, plugin/MCP activation, and executable sandbox materialization are
not activated by Community installation. Community manifests are projected into
an instruction-only runtime profile: instruction text and safe invocation
metadata remain usable, while unsupported control-flow or executable fields are
kept inert and reported as compatibility diagnostics. A Skill that declares
`allowed-tools` does not receive that preapproval; OpenSquilla keeps the normal
tool approval policy and reports `TOOL_PREAPPROVAL_IGNORED` through Doctor.
Claude-style ``!`command` `` dynamic context is retained as instruction text
rather than executed while loading; Doctor reports
`DYNAMIC_CONTEXT_UNSUPPORTED` and marks the installation as degraded.

The Web UI installs GitHub references serially, with at most 10 unique references
per batch. Ordinary item failures do not roll back successful items or stop later
items. A GitHub rate-limit response pauses the remaining references as not
attempted and keeps them in the input for a later batch.

## Manage Skill Sources

Custom source repositories are called taps:

```sh
opensquilla skills tap list
opensquilla skills tap add <owner/repo>
opensquilla skills tap remove <owner/repo>
```

Use taps when your team maintains its own skill catalog.

## Publish and Inspect

Publish a skill directory:

```sh
opensquilla skills publish <path-to-skill>
```

Inspect the compiled composition for a meta-skill:

```sh
opensquilla skills inspect <meta-skill-name>
```

For ordinary skill content, use:

```sh
opensquilla skills view <skill-name>
```

## How to Ask for a Skill

Ask for the outcome:

```text
Create a PowerPoint deck summarizing this report.
```

Better than:

```text
Load the pptx skill and run its script.
```

OpenSquilla can choose eligible skills from the current catalog when the task
matches their description and triggers.

## Bundled Skill Families

| Family | Examples |
| --- | --- |
| Research | deep research, multi-source search, summarization |
| Documents | DOCX, PPTX, XLSX, PDF, HTML-to-PDF |
| Operations | cron, GitHub, terminal monitoring, subagents |
| Memory | memory-oriented helpers and history exploration |
| Creation | skill creator, skill review, proposal helpers |

## Troubleshooting

If a skill is not selected:

1. Confirm it appears in the installed catalog:

   ```sh
   opensquilla skills list
   ```

2. Inspect its description and eligibility:

   ```sh
   opensquilla skills view <skill-name>
   opensquilla skills doctor <skill-name>
   ```

3. Ask for the outcome in normal language. Skill names can help, but user
   intent should still be clear.

4. If Doctor reports `needs_setup`, install the declared dependency separately,
   then rerun Doctor. Doctor itself is read-only: it does not use the network,
   run third-party scripts, or call an LLM.

If a newly upgraded CLI reports `GATEWAY_UPGRADE_REQUIRED`, restart the running
Gateway from the same upgraded installation before retrying Doctor. The CLI
does not silently switch to an offline scan while an older Gateway still owns
the profile.

For composed workflows, read [`meta-skills.md`](meta-skills.md). For the full
MetaSkill user guide, read [`meta-skill-user-guide.md`](meta-skill-user-guide.md).
For authoring rules, read [`../authoring/meta-skills.md`](../authoring/meta-skills.md).

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
