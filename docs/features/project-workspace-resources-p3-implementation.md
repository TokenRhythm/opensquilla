# P3 implementation and prompt changes

P3 preserves existing source locations. It does not migrate files, introduce a global
USER.md, or add a storage or transaction framework.

## Sources and compatibility

`agents.scope.resolve_agent_source_paths` keeps bootstrap, MEMORY.md, and notes as
independent paths. The manager supplies its actual paths to tools, runtime snapshots,
inspection RPCs, and Gateway Dream. Standalone CLI Dream uses the same resolver.

| Configuration | MEMORY.md root | Notes |
| --- | --- | --- |
| `source=workspace` | Configured Agent workspace | Its `memory/` directory |
| `source=state`, ordinary case | Configured Agent state directory | Its `memory/` directory |
| `source=state`, main Agent with `OPENSQUILLA_MEMORY_DIR` | Existing default Agent state root, preserving the previous manager branch | The environment override |

Logical note paths remain `memory/<relative note>`, including separate note trees.
Sync, stale-index filtering, retention, memory tools, inspection, and Dream use that
mapping. Dream cursor, evidence, backups and receipts remain beside the selected notes;
curated writes target the selected MEMORY.md root. Existing Dream enable/schedule and
memory-maintenance ownership checks remain in force.

The old `agents.files` RPC continues to address bootstrap files, including its legacy
MEMORY.md/memory.md entries. It does not silently redirect these files to runtime memory.
There is currently no WebUI caller or editor for these RPCs. Settings Memory is the
existing profile-import workflow, not a bootstrap-file editor.

## Profile tool and revisions

`agent_file(read|replace)` accepts only USER.md, IDENTITY.md and SOUL.md. The bound
registry selects the current Agent and source; arguments cannot select another Agent
or a host path. Replacement requires the revision returned by read. Missing and empty
files are distinct (`absent` versus a SHA-256 revision).

The tool defaults to deny. Existing owner facts permit its runtime surfacing for Web,
CLI, Agent, and verified private-channel conversations. Guests, unverified channels,
groups, subagents and cron remain excluded. Explicit tool allow/deny still applies.
`memory_save` retains its existing default-deny behavior and explicit authorization;
this phase does not grant every owner conversation memory-write access.

Registry and RPC use the same bounded file reader/writer. The application serializes
same-target writes, checks expected bytes, preserves existing link/file checks, and
verifies writeback. This is not a cross-process transaction or durable receipt.
Bootstrap snapshots are invalidated after actual writes, including an unconfirmed
writeback. RPC revision parameters remain optional for existing clients; conflicts use
`AGENT_FILE_CONFLICT`. Legacy MEMORY setters also refresh the Agent memory snapshot,
without changing their target or source precedence.

## Exact prompt changes

`identity/templates/system_prompt.j2` replaces **Memory Write Guidance** with the
**Profile and Memory Updates** block recorded verbatim in plan section Q3. It directs
profile/persona updates to `agent_file`, authorized memory updates to `memory_save`,
and prohibits creating an ineffective task-local substitute. It preserves ordinary
file tasks at explicitly requested paths and distinguishes confirmed saves from
rollback or unresolved outcomes. The block renders in full mode even when saving
tools are absent. Its save/read branches use the final authorized persistence-tool
set; provider schemas retain existing discovery behavior.

`tools/builtin/agent_files.py` adds this tool description:

> Read or replace USER.md, IDENTITY.md, or SOUL.md for the current Agent. The runtime selects the source. Read first; replacement requires the returned revision. Preserve unrelated content. Existing caller authorization and tool policy apply.

`tools/builtin/memory_tools.py` replaces the claim that profile files use ordinary
filesystem tools with:

> Write durable non-profile memory to the configured Agent sources, which may differ from the task workspace. Profile and persona files use agent_file when available, not memory_save.

It retains the existing replace/append and complete-read guidance. `memory_get.path`
now says **Logical path in the configured Agent memory sources: MEMORY.md or
memory/**/*.md.** `memory_delete.path` now says **Supported logical file path in the
configured Agent memory sources to delete.** No project-init or delivery prompt text
is changed by P3.

## Validation

Focused tests cover missing/empty files, source and Agent isolation, Web/CLI/channel
authority, explicit tool denial, revision conflicts, concurrent writers, failed
writeback, snapshot callbacks, source precedence, independent notes, sync/recall/delete,
inspection, retention keys, and Dream's split-source artifacts. Existing memory,
profile, tool-policy and Dream regressions are included. Source-level tests do not
claim live-provider or packaged-product acceptance.
