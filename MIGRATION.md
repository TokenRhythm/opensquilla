# Migration Guide

## Retired workflow modes

MetaSkill, Coding Mode, and the `code-task` command have been removed. Ordinary
Agent conversations, file editing, Shell tools, and ordinary skills continue to
use their existing permissions.

Loading an old profile discards `[meta_skill]` and `skills.coding_mode`; normal
config loading rewrites those fields out, while read-only loading leaves the
file untouched. Old MetaSkill definitions and the retired `code-task` skill are
ignored. Retired coding prompt and tool-profile values resolve to the ordinary
prompt defaults and coding tool profile. No execution or RPC compatibility layer
is provided.

Upgrading cancels unfinished MetaSkill control tasks, removes their unaccepted
drafts and authorizations, and disables old automatic-proposal jobs. Conversation
history is retained. Execution audit rows remain in the SQLite tables
`retired_meta_skill_runs` and `retired_meta_skill_run_steps` for offline export;
they cannot be resumed or replayed. Deleting a session also removes its retained
audit rows. Existing migration files remain to preserve the database upgrade
chain. Use the pre-upgrade database backup if reverting to an older release.

## Importing another agent's data

OpenSquilla can import state from OpenClaw and Hermes Agent into OpenSquilla
native files. The migration commands are designed to be previewed first, then
applied explicitly.

Supported migration paths:

- Auto-detect everything found under your home: `opensquilla migrate`
- OpenClaw -> OpenSquilla: `opensquilla migrate openclaw`
- Hermes Agent -> OpenSquilla: `opensquilla migrate hermes`

`opensquilla migrate` (with no subcommand) scans `~/.openclaw` and
`~/.hermes` and decides what to do based on what it finds:

- **Nothing detected**: prints the default paths it checked and exits 0.
- **Exactly one detected**: runs that migrator. No prompt, no flag needed.
- **Both detected, interactive (TTY) shell**: opens a multi-select prompt
  so you can pick one, both, or neither.
- **Both detected, non-interactive context (CI, piped, `--json`)**: prints
  the detected sources and exits 0 without migrating. Re-run with
  `--source openclaw,hermes` (or a subset) to opt in explicitly.

When both sources are selected, OpenSquilla runs OpenClaw first and Hermes
second. The second migrator sees whatever the first one wrote, so its
existing per-file dedupe / persona-conflict rules kick in normally. Use
`--source openclaw` or `--source hermes` (comma-separated) to narrow the
selection.

If you are running from a source checkout instead of an installed
`opensquilla` command, prefix the examples with `uv run`:

```sh
uv run opensquilla migrate openclaw --json
uv run opensquilla migrate hermes --json
```

## Before You Start

1. Stop any running OpenSquilla gateway if it is using the target home.
2. Make a manual backup of your OpenSquilla home if you need whole-home
   rollback. The migrators can back up overwritten items, but they do not yet
   create a complete pre-migration snapshot of `~/.opensquilla`.
3. Run a dry run first and inspect the report.
4. Do not pass `--migrate-secrets` until you have reviewed what will be copied.

Default locations:

- OpenSquilla home: `~/.opensquilla`
- OpenClaw source home: `~/.openclaw`
- Hermes Agent source home: `~/.hermes`

On Windows, these are under your user profile, for example
`C:\Users\<you>\.opensquilla`.

## Common Options

Both migration commands support the same main controls:

| Option | Meaning |
| --- | --- |
| `--source PATH` | Source OpenClaw or Hermes Agent home. |
| `--config PATH` | OpenSquilla config path to preview or write. |
| `--apply` | Apply the migration. Without this, the command is a dry run. |
| `--migrate-secrets` | Copy recognized secrets such as API keys and channel tokens. Defaults to false. |
| `--overwrite` | Allow replacing existing targets. Existing overwritten items are backed up where supported. |
| `--preset user-data` | Migrate only user-facing data such as persona, memory, and skills. |
| `--preset full` | Migrate user data plus supported config/runtime artifacts. This is the default. |
| `--include IDS` | Include only selected migration option ids. Comma-separated. |
| `--exclude IDS` | Exclude selected migration option ids. Comma-separated. |
| `--skill-conflict MODE` | Handle imported skill name conflicts: `skip`, `overwrite`, or `rename`. |
| `--json` | Print a machine-readable report. Recommended for dry runs. |

## OpenClaw -> OpenSquilla

Use this path if your existing agent state is in an OpenClaw home.

Preview first:

```sh
opensquilla migrate openclaw --json
```

Preview a custom OpenClaw home:

```sh
opensquilla migrate openclaw --source /path/to/.openclaw --json
```

Apply without secrets:

```sh
opensquilla migrate openclaw --apply
```

Apply and copy recognized secrets:

```sh
opensquilla migrate openclaw --apply --migrate-secrets
```

Apply and rename imported skill conflicts instead of skipping them:

```sh
opensquilla migrate openclaw --apply --skill-conflict rename
```

### What Is Migrated From OpenClaw

OpenSquilla currently maps OpenClaw data into OpenSquilla-native locations:

- Workspace persona files such as `SOUL.md`, `AGENTS.md`, and `USER.md`.
- Long-term memory and daily memory where supported.
- User skills and shared skills, imported under `~/.opensquilla/skills/openclaw-imports/`.
- TTS assets, while unsupported TTS configuration is archived for review.
- Command allowlists.
- Model config, including string, object, and alias/catalog forms.
- Provider keys from `.env` or provider config when `--migrate-secrets` is set.
- MCP server definitions where OpenSquilla has native fields.
- Telegram, Discord, and Slack channel config where OpenSquilla has native channel support.
- Selected agent and tool settings with OpenSquilla-native equivalents.
- Unsupported or unsafe OpenClaw artifacts are archived for manual review.

The OpenClaw migrator also rewrites OpenClaw branding in migrated user-facing
workspace text to OpenSquilla branding and archives the original changed text
for review.

**Mixed-subject prose is kept verbatim.** When a workspace file (or a
single MEMORY.md block) already mentions `OpenSquilla` (any case), the
migrator skips the mechanical rebrand for that file/block and writes it
verbatim. Mechanical replacement of `OpenClaw` -> `OpenSquilla` in prose
that already names both runtimes as distinct entities produces factual
errors and self-referential nonsense. The report records
`details.rebrand_skipped: "mentions-opensquilla"` for the affected file
and, for MEMORY.md, `details.rebrand_skipped_block_count` so you can
reword the relevant lines by hand.

### SOUL.md / USER.md / AGENTS.md Conflict Handling

These persona files are identity definitions (not additive like memory), so
when the destination already holds real user-curated content the migrator
asks you which version to keep instead of either silently dropping the
imported content or clobbering the existing file.

Use ``--persona-conflict`` to control the behavior:

| Mode | Behavior |
| --- | --- |
| ``prompt`` (default) | When stdin is a TTY: prompt for each conflicting file with a side-by-side preview and the four choices below. When stdin is not a TTY (CI, pipe, ``--json``): fall back to ``use-opensquilla`` and record a note so the choice is visible in the report. |
| ``use-opensquilla`` | Keep the destination file untouched. The OpenClaw original is copied to ``<output_dir>/archive/files/openclaw-orphaned/<filename>`` for review so nothing is silently lost. ``status: skipped``, ``details.persona_conflict_resolution: "use-opensquilla"``. |
| ``use-openclaw`` | Back up the destination to ``<name>.backup.<timestamp>`` and replace it with the OpenClaw content. ``status: migrated``, ``details.persona_conflict_resolution: "use-openclaw"``. |
| ``merge`` | Back up the destination and append the OpenClaw content below it under a ``## Imported from OpenClaw`` separator. Useful when the two versions are complementary rather than conflicting. ``status: migrated``, ``details.persona_conflict_resolution: "merge"``. |
| ``skip`` | Leave both files alone. The OpenClaw original is *not* archived — use ``use-opensquilla`` instead if you want a recoverable copy. ``status: skipped``, ``details.persona_conflict_resolution: "skip"``. |

``--overwrite`` short-circuits all of this and replaces the destination
wholesale (still with an item-level backup).

The pristine bootstrap-template case described below is handled before
``--persona-conflict`` and never asks for input: a freshly initialised
OpenSquilla workspace where the template is still untouched is treated as
overwrite-safe.

### MEMORY.md Merge Semantics

OpenClaw memory is additive by nature: every imported daily-memory file is
its own ``## Imported daily memory: <name>`` section. The OpenClaw migrator
therefore handles ``MEMORY.md`` differently from other workspace files: it
will never silently overwrite existing user-curated memory and it will
never silently drop the imported memory either.

Behaviour matrix (without ``--overwrite``):

| Destination state | What happens |
| --- | --- |
| ``MEMORY.md`` does not exist | Imported memory is written fresh. |
| Pristine OpenSquilla bootstrap template | Template is backed up, imported memory replaces it. ``details.replaced_bootstrap_template: true``. |
| Real user-curated content | Imported blocks that are not already present (after a whitespace-normalised, header-stripped comparison) are appended below the existing content. The pre-existing file is backed up first. ``details.appended_to_existing: true``, ``new_blocks_appended: N``, ``deduplicated_blocks_vs_existing: M``. |
| All imported blocks already present | The file is left untouched. ``status: skipped, reason: "all openclaw memory blocks already present in destination"``, ``details.deduplicated_against_existing: true``. No backup created. |

``--overwrite`` is the explicit "replace, do not merge" escape hatch — the
destination is backed up and replaced wholesale regardless of its current
contents.

### OpenSquilla Bootstrap-Template Handling

`ensure_agent_workspace` seeds placeholder ``SOUL.md`` / ``USER.md`` /
``AGENTS.md`` / ``MEMORY.md`` files when an OpenSquilla home is first
initialised. Without special handling those placeholders would block every
workspace-file migration with a silent ``conflict: target exists`` —
including the imported daily memory the user is migrating for in the first
place.

The OpenClaw migrator detects a destination that still holds the pristine
bootstrap template (byte-identical to the shipped placeholder after a
trailing-whitespace normalisation) and treats it as overwrite-safe:

- The pristine template is backed up to
  ``<name>.backup.<timestamp>`` next to the destination so the placeholder
  guidance can be recovered on demand.
- The imported content replaces the template.
- The migration report marks the item with
  ``details.replaced_bootstrap_template: true`` so the special case is
  visible rather than silent.

A destination file that the user has truly edited (i.e. no longer matches
the canonical template byte-for-byte) still gets the normal
``status: conflict`` treatment — only the pristine placeholder is treated
as overwrite-safe. To accept user edits being overwritten as well, pass
``--overwrite``.

### OpenClaw Limits

Some OpenClaw runtime behavior is not fully mapped yet:

- WhatsApp and Signal settings are detected, but OpenSquilla does not yet create
  native migrated channel entries for them.
- Some advanced MCP fields such as headers/auth/cwd/include/exclude are not
  native mapped.
- Some gateway, session, browser, approval, logging, plugin, cron, hook, memory
  backend, skills registry, and UI settings are archived rather than applied.
- OpenSquilla does not widen channel privileges: ordinary OpenClaw allowlists
  are not treated as OpenSquilla admin senders.

Review `MIGRATION_NOTES.md` after an applied migration for partial mappings and
manual follow-up.

## Hermes Agent -> OpenSquilla

Use this path if your existing agent state is in a Hermes Agent home.

Preview first:

```sh
opensquilla migrate hermes --json
```

Preview a custom Hermes Agent home:

```sh
opensquilla migrate hermes --source /path/to/.hermes --json
```

Preview a Hermes profile:

```sh
opensquilla migrate hermes --profile work --json
```

Apply without secrets:

```sh
opensquilla migrate hermes --apply
```

Apply and copy recognized secrets:

```sh
opensquilla migrate hermes --apply --migrate-secrets
```

Apply and rename imported skill conflicts instead of skipping them:

```sh
opensquilla migrate hermes --apply --skill-conflict rename
```

### What Is Migrated From Hermes Agent

OpenSquilla currently maps the common Hermes Agent home surface:

- Persona and user data files such as `SOUL.md`, `MEMORY.md`, and `USER.md`.
- Hermes skills, imported under `~/.opensquilla/skills/hermes-imports/`.
- Hermes model/provider config where there is an OpenSquilla-native equivalent.
- Hermes custom providers with `base_url`, mapped to OpenAI-compatible provider config.
- Environment values and recognized provider keys when `--migrate-secrets` is set.
- Search config where supported.
- MCP server definitions where supported.
- Telegram, Discord, and Slack channel tokens when `--migrate-secrets` is set.
- Selected plugin, cron, and unsupported runtime artifacts are archived for review.

### Hermes Agent Limits

The Hermes Agent migrator is newer than the OpenClaw migrator and has a smaller
coverage surface. Review the dry-run report carefully before applying.

Current limits:

- Live runtime state, active sessions, process state, and gateway state are not imported.
- Some Hermes runtime config option ids are accepted but currently deferred:
  `workspace-files`, `tools-config`, `browser-config`, `session-config`,
  `gateway-config`, `approvals-config`, `logging-config`, and `memory-backend`.
  Each appears in the migration report as `status: deferred` with reason
  `handler not implemented yet`. Selecting them via `--include` is not an
  error; the migrator just records the gap so it is visible.
- Browser, tool, session, gateway, approval, and logging settings may require manual review.
- A full pre-apply snapshot of `~/.opensquilla` is not created automatically.

### Hermes Agent Migration Behavior

The Hermes migrator now mirrors the OpenClaw migrator on a few correctness
behaviors that were previously documented but not implemented:

- **Item-level backups.** When `--overwrite` replaces an existing
  workspace file (`SOUL.md`, `MEMORY.md`, `USER.md`) or skill directory, the
  prior contents are written to `<name>.backup.<timestamp>` next to the
  original before the new content is applied.
- **Semantic deduplication on merge.** Existing destination content is split
  into paragraph blocks and compared after whitespace normalization. A new
  source body is appended unless an equivalent block already exists. The
  previous naive substring check could silently drop short source bodies.
- **Memory overflow archival.** If the merged `MEMORY.md` would exceed
  OpenSquilla's per-file size limit, the overflow is split at a paragraph
  boundary and archived to
  `~/.opensquilla/migration/hermes/<timestamp>/archive/memory-overflow/MEMORY.overflow.md`.
  A short pointer is left at the end of `MEMORY.md`.
- **Branding rewrite.** Hermes branding in imported workspace prose
  (`SOUL.md`, `MEMORY.md`, `USER.md`) is rewritten to OpenSquilla. Bare
  `Hermes` is only rewritten when it is followed by a workspace-context word
  (e.g. `home`, `workspace`, `memory`, `config`). Source-reference tokens
  such as `HERMES_HOME`, `NousResearch`, and `hermes-agent` are preserved so
  the migration archive still points back at the original source. The
  unrebranded original is copied to
  `<output_dir>/archive/files/workspace-original/<name>.md` for review.
- **Path-token replacement is word-boundary aware.** Previously a plain
  string replace turned `~/.hermesrc` into `~/.opensquillarc` and
  `.hermes_backup` into `.opensquilla_backup` — meaningless paths. The
  rebrand now only rewrites `.hermes` when it ends a path token (i.e.
  followed by `/`, whitespace, quote, or end-of-string). Same rule
  applied to `.openclaw` on the OpenClaw side, and bare-word
  `OpenClaw` / `openclaw` are now matched with `\b` so substrings like
  `OpenClawFlavored` or `openclaw_pid` are left alone.
- **Non-UTF-8 source files no longer crash.** Hand-edited source files
  with stray bad bytes (CP1252 fragments, leftover binary paste, etc.)
  used to abort the entire Hermes migration with
  `UnicodeDecodeError`. The source read now uses `errors="replace"`
  (matching OpenClaw); offending bytes become U+FFFD so users can spot
  them.
- **`mcp.enabled = false` is no longer silently flipped.** When the
  destination home already has MCP servers AND `mcp.enabled = false`
  (an explicit "I don't want MCP right now" choice), importing more
  MCP servers leaves the flag at `false` and surfaces
  `details.mcp_enabled_left_disabled` plus a `manual_steps` hint. MCP
  is still flipped on automatically when the destination had no
  servers (framework default — flipping is what the user wants).
- **Mixed-subject prose is kept verbatim.** Workspace notes often
  describe Hermes AND OpenSquilla as distinct entities ("Hermes Agent
  v0.13.0 installed at ~/.local/bin/hermes; OpenSquilla also installed
  at ~/.local/bin/opensquilla. Has `migrate hermes` subcommand."). A
  mechanical rebrand collapses the two subjects into one and produces
  factual errors (path mismatches), tautologies ("OpenSquilla skills
  loadable by OpenSquilla"), and self-referential commands ("migrate
  OpenSquilla skills to OpenSquilla"). When the source already
  mentions `OpenSquilla` (any case), the migrator now skips rebrand
  for that file and writes it verbatim, recording
  `details.rebrand_skipped: "mentions-opensquilla"` so you can decide
  which mentions to reword by hand. The same rule applies per-block
  during MEMORY.md merging; the count of skipped blocks is reported
  via `details.rebrand_skipped_block_count`.
- **Skill compatibility reporting.** Each imported skill's
  report record now includes `details.compatibility` (`loadable` /
  `needs_review` / `not_loadable`) and `details.compatibility_issues` listing
  missing frontmatter, oversize bodies, or invalid YAML. Skills are still
  copied; the field is informational so you can find ones that may need
  attention before activating.
- **Unknown providers are no longer written to `llm.provider`.** Hermes
  uses values like `auto` (runtime auto-detect) and may ship experimental
  providers (`bedrock`, `ollama`, ...) that have no OpenSquilla equivalent.
  Writing them verbatim used to crash `persist_config` because OpenSquilla
  validates `llm.provider` against a known set AND requires
  `squilla_router.tier_profile` to agree with it. The migrator now leaves
  `llm.provider` untouched in that case; the model id and base URL are
  still migrated, and the model-config item carries
  `details.unrecognized_provider`, `details.llm_provider_left_unchanged`,
  and a `manual_steps` hint explaining how to set the provider explicitly.
- **Known providers that clash with an existing
  `squilla_router.tier_profile` are also not written.** Even when the
  Hermes provider is recognized (e.g. `anthropic`), persisting it would
  fail if the destination home already pins `squilla_router.tier_profile`
  to a different provider (e.g. `openrouter`) — OpenSquilla requires the
  two to match. The migrator now detects the clash, leaves `llm.provider`
  unchanged, and records `details.tier_profile_conflict`,
  `details.llm_provider_left_unchanged`, and a `manual_steps` hint so you
  can switch providers explicitly via `opensquilla config set` or by
  clearing `squilla_router.tier_profile` first.
- **MCP server entries upsert instead of replacing.** Both migrators
  used to assign `cfg.mcp.servers = imported`, silently destroying any
  pre-existing OpenSquilla MCP servers the user already had configured.
  Now the imported servers are upserted by name: same-name entries are
  replaced (the imported version wins), unrelated entries are preserved.
  The `mcp-servers` report record carries `details.added`,
  `details.replaced`, and `details.preserved_existing`.
- **Resilient SKILL.md compatibility check.** A SKILL.md with empty or
  non-dict YAML frontmatter (e.g. `---\n\n---`) used to crash the whole
  migration with `AttributeError`. The check now records the skill as
  `compatibility: "not_loadable"` and continues. The reported
  `compatibility` string is also kept consistent with the
  `opensquilla_loadable` boolean.

## Reports

Use `--json` for dry-run automation:

```sh
opensquilla migrate openclaw --json
opensquilla migrate hermes --json
```

Applied migrations write report files under:

```text
~/.opensquilla/migration/openclaw/<timestamp>/
~/.opensquilla/migration/hermes/<timestamp>/
```

Typical files:

- `report.json`: structured item-level report.
- `summary.md`: human-readable count summary.
- `MIGRATION_NOTES.md`: OpenClaw migration notes when semantic conversions or
  partial mappings are present.
- `archive/`: unsupported or review-only artifacts.

Hermes dry runs also write report files. OpenClaw dry runs are best inspected
with `--json`; apply mode writes the report files.

## Validate After Migration

After applying a migration, start the gateway and run a small chat check:

```sh
opensquilla gateway start --json
opensquilla chat
```

Or use a one-shot prompt:

```sh
opensquilla agent -m "Briefly summarize your active persona and available memory."
```

Also check:

- `~/.opensquilla/workspace/` for migrated persona and memory files.
- `~/.opensquilla/skills/openclaw-imports/` or `~/.opensquilla/skills/hermes-imports/`.
- `~/.opensquilla/migration/<source>/<timestamp>/summary.md`.
- `~/.opensquilla/migration/<source>/<timestamp>/MIGRATION_NOTES.md` when present.

If behavior does not look right, stop the gateway, review the migration report,
and re-run with a narrower `--preset`, `--include`, or `--exclude` selection.

## Examples

Migrate only user data from OpenClaw:

```sh
opensquilla migrate openclaw --preset user-data --apply
```

Migrate only Hermes skills and persona files:

```sh
opensquilla migrate hermes --include soul,skills --apply
```

Preview OpenClaw migration while excluding channel settings:

```sh
opensquilla migrate openclaw --exclude telegram-settings,discord-settings,slack-settings --json
```

Apply Hermes migration to a custom config file:

```sh
opensquilla migrate hermes --config /path/to/opensquilla.toml --apply
```
