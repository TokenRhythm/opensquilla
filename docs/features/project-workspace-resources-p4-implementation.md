# P4 implementation and exact prompt changes

This phase uses ordinary turns, existing tool authorization, and the source-edit contract. It adds no separate executor, rule watcher, durable transaction, whole-tree scan, or permission system.

## Behavior and impact

- Web `/init` enters ordinary TURN / NEXT_TURN processing through explicit `turnCommand` provenance. InputStage does not infer commands from message text, history, quotations, or attachments. The CLI setup wizard remains unchanged; CLI/channel surfaces without this integration do not advertise project init.
- InputStage captures the bound root, selected file, and Q1 template. AGENTS.md takes precedence; an existing agent.md is retained when AGENTS.md is absent. Ordinary tools enforce exclusive creation, revision conflicts, permissions, and cancellation. A suggested target does not authorize a path.
- The three source tools are surfaced only for this turn, then filtered through existing allow/deny/profile policy. Q1 selects writable or read-only guidance from final authorized_tool_names without mutating the caller's context.
- Project instructions from the root through the default working directory enter dynamic context. The current tool context uses the bound root as its default CWD; the loader supports bounded hierarchy without following temporary shell cd operations. It selects the standard or legacy candidate at each level and deduplicates physical files, excluding ancestors and link targets outside the bound root.
- Reads use bootstrap per-file and total character budgets, retaining source, scope, unreadable/truncated/failure status, injection scanning, and untrusted wrappers. Matching bootstrap AGENTS content is not injected twice. Missing rules require no extra model round trip.
- A snapshot belongs to one turn. Router replay retains it; provider retries reuse the assembled request; compaction refreshes only the stable system base without rereading rules. The next turn and each side chat read their own current files. Stateless and heartbeat_light retain their existing minimal behavior; stateless_keep_project_rules permits rule loading.
- Reading an empty source file returns a revision, range=[0,0], total_lines=0, and lines=[], without an invalid zero-line resource reference. Only an empty file accepts a single [0,0] insertion edit. Positive-line ranges, expected_revision, and read freshness remain enforced.

## Frontend command persistence and compatibility

The WebUI admits `/init` only when the current command catalog advertises
`project.init` with TURN / NEXT_TURN / TURN metadata. Gateway/profile/connection
changes invalidate that capability evidence. `//init`, quoted text, and reference
chips do not create command intent. An older unsupported Gateway keeps the draft
and displays the existing unavailable-state message.

The explicit name/arguments survive the existing send envelope, pending queue,
request fingerprint, receipt lookup, and IndexedDB WAL. A busy conversation queues
the command as a follow-up rather than steering the active turn. Unknown acceptance
uses the original receipt request without reclassifying or resending it. If support
is revoked before a staging RPC starts, the record stays locally owned; an earlier
ambiguous attempt retains its existing server-copy provenance.

The WAL database version advances from 3 to 4 without deleting either object store
or existing drafts. Older clients opening that database receive `VersionError`
instead of replaying a command after dropping its intent. There is no automatic
database wipe or reverse migration for downgrade; use the compatible client to
review retained queued input. Focused fake-IndexedDB coverage verifies both record
preservation and the older-version failure.

## Q1: new init template

File: `src/opensquilla/identity/templates/project_init.md.j2`.
Loaded by `engine/turn_runner/input_stage.py::InputStage.run` and rendered by `engine/runtime.py::TurnRunner._run_turn` after ProviderAndToolsStage finishes authorization. The rendered template is appended to this turn's runtime_message. The persisted user message remains the original `/init` text.

Compared with the plan draft, save instructions are conditional on `can_save`. The template adds empty-file ranges, candidate-change conflicts, and precise handling of uncertain results. Missing tools permit read-only analysis. Complete template:

```jinja2
The user invoked /init to create or improve project instructions.
Bound workspace: {{ workspace_root }}
Selected instruction file: {{ selected_path }}

Inspect the existing instructions, manifests, README/docs, directory layout,
and build/test/lint configuration before editing. Include only project-specific
facts supported by those sources: useful directories, exact commands, observed
conventions, architectural boundaries, and concrete pitfalls.
Use read-only exploration. Do not install dependencies, initialize Git, run
project setup/build/test commands, or create profile/persona/memory files.

If instructions exist, read them completely. Preserve the user's wording,
structure, and rules, making small edits for missing or demonstrably stale facts.
If absent, prepare a concise guide. Aim for fewer than 100 lines for new content;
do not delete existing user content to meet that target.
If evidence is insufficient, explain what is missing instead of writing an
empty file or generic template.

{% if can_save %}
Use read_source before editing; create_source for a missing file; edit_source
with the read revision for existing content. For an empty file, read_source
returns range [0, 0]; use a single edit with start_line=0 and end_line=0 to fill it.
Recheck the selected instruction candidates before creating. Do not create a
second instructions file or change the target after a shell cd. If the preferred
candidate has changed, explain the conflict instead of writing either candidate.
On conflict or an uncertain result, reread before further changes. Matching bytes
confirm current content, not that an earlier uncertain operation succeeded.
Report only the file path and changes supported by tool results or readback.
Updated instructions will load automatically at the next chat turn.
{% else %}
The required source tools are unavailable under the current tool permissions.
Use available read-only tools to analyze the project and explain the limitation.
Do not write instructions or use shell, Python, or other write tools as a workaround.
{% endif %}

Additional instructions from this /init request:
{{ user_notes }}

```

## Q2: new turn-scoped project rules template

File: `src/opensquilla/identity/templates/project_rules.md.j2`.
Injected into the dynamic suffix by `engine/runtime.py::TurnRunner._assemble_prompt`, through the existing PromptAssemblerStage to the provider. Each rule.content uses the existing untrusted XML wrapper; scanning follows safety.injection_scan_mode.

```jinja2
Project instructions for this chat turn:
Workspace: {{ workspace_root }}
Apply each file within its listed scope. More specific conventions apply within
that directory. They do not override explicit current user instructions, system
requirements, or tool permissions.
This is a turn-start snapshot. Automatic loading covers the workspace root through
the default working directory. Read relevant deeper-directory instructions when
working there. File changes will be loaded automatically at the next chat turn.

{% for rule in rules %}
Source: {{ rule.path }}
Scope: {{ rule.scope }}
Read status: {{ rule.read_status }}
{{ rule.content }}

{% endfor %}

```

## Tool-description additions

File: `src/opensquilla/tools/builtin/filesystem.py`.

Append to the `read_source` description:

```text
An empty file returns range [0, 0], total_lines 0, and a valid revision.
```

Insert after the first sentence of the `edit_source.edits` parameter description:

```text
For an empty file, use one edit with start_line=0 and end_line=0.
```

## Reference behavior and tradeoffs

- Codex init uses an ordinary prompt turn. This implementation retains that entry path while allowing improvements to an existing AGENTS file.
- Q1 adopts ZCode's read-before-incremental-edit behavior and Hermes's concrete project commands and preservation of existing wording. It does not create empty templates or run installation/build commands to initialize a guide.
- Hierarchical rules use a bounded root-to-default-CWD snapshot. Hermes's later shell-CWD additions and DeepSeek's provider/tool-step refresh are outside this scope.
- Reference paths and frozen SHAs are recorded in the main implementation plan. This phase required no external network request or new dependency.

## Validation evidence

- 321 passed / 3 platform-specific skips on Windows: ordinary-turn source surfacing and deny fallback, existing source edits, bootstrap snapshots, and provider/prompt/bootstrap stages.
- New coverage includes rule hierarchy, candidate precedence, budgets, failures, physical deduplication, explicit InputStage provenance, and empty-file revision/read freshness.
- Router replay, compaction refresh, injection scanning, and existing compaction/stream stages: 97 passed / 3 platform-specific skips.
- Focused deletion/legacy-switch cases ensure cached bootstrap content does not reappear: 243 passed / 4 platform-specific skips.
- Final cross-phase integration of architecture, project-init turns, and project instructions: 34 passed / 1 platform-specific skip. Earlier cross-phase dependency issues are resolved.
- A real ToolRegistry with a simulated provider verifies final-authorized persistence guidance: an authorized discoverable memory_save is described, explicit deny removes that guidance, and initial provider schemas are not expanded. The tool retains its existing default-deny behavior; this phase does not grant default memory-write access.
- Ruff passed for edited source files. Mypy passed for runtime, harness, InputStage, PromptAssemblerStage, the rules helper, the source contract, and filesystem: 7 files.
- Simulated-provider verification does not establish live-model or packaged-product acceptance.
