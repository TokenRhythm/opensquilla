# Tools, Approvals, and Sandbox

OpenSquilla tools give the agent useful capabilities. Policy layers, approval
surfaces, workspace constraints, and sandbox posture control how those tools are
allowed to act.

Use this page before running unattended automation, file edits, shell commands,
or channel-connected agents.

For a focused permissions guide, see
[`approvals-and-permissions.md`](approvals-and-permissions.md).
For the current Safe mode and Full access behavior, including guest isolation
and upgrade compatibility, see [`sandbox-security.md`](sandbox-security.md).

## Built-In Tool Areas

| Area | Examples |
| --- | --- |
| Filesystem | `read_file`, `write_file`, `edit_file`, `list_dir`, `glob_search`, `grep_search`, spreadsheet reads. |
| Shell and code | `exec_command`, `background_process`, `process`, `execute_code`. |
| Git | `git_status`, `git_diff`, `git_log`, `git_commit`, `apply_patch`. |
| Web | `web_search`, `web_discover`, `web_fetch`, `http_request`. |
| Memory | `memory_search`, `memory_save`, `memory_get`, `memory_delete`. |
| Sessions | `sessions_send`, `sessions_spawn`, `sessions_list`, `sessions_history`, `session_status`. |
| Artifacts | `publish_artifact`. |
| Media | image generation, PDF, TTS, and media helpers. |
| Skills | `skill_list`, `skill_view`, `skill_create`, `skill_edit`, `install_skill_deps`, `meta_invoke`. |
| Admin | cron and gateway administration. |
| Channels/platforms | messaging and Feishu/Lark docs, chat, drive, wiki, media, and permission helpers. |

## Permission Modes

Use stricter modes when running unattended:

```sh
opensquilla agent --permissions restricted -m "Inspect this repo"
```

Use broader modes only when you trust the task and workspace:

```sh
opensquilla agent --permissions full --workspace /path/to/project -m "Run tests and fix failures"
```

For interactive work, the Web UI approvals surface can pause sensitive tool
calls for review. For automation, choose a permission mode and workspace policy
before the run starts.

Read: [`approvals-and-permissions.md`](approvals-and-permissions.md)

## Approval Flow

Sensitive actions may pause for human approval depending on permission mode,
tool policy, channel surface, and runtime configuration.

Approvals are most important for:

- filesystem writes;
- shell commands;
- external channel or webhook delivery;
- generated artifacts that will be published;
- actions that affect another service.

Use the Web UI approvals page when you want durable review outside the chat
scrollback.

## Workspace Controls

Read-side restriction:

```sh
opensquilla agent --workspace /path/to/project --workspace-strict -m "Summarize this repo"
```

Write containment:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and prepare a minimal patch"
```

`--workspace-lockdown` is intended for automation where writes must stay inside
the workspace or scratch directory.

## Sandbox Commands

```sh
opensquilla sandbox status
opensquilla sandbox on
opensquilla sandbox full
opensquilla sandbox bypass
opensquilla sandbox reset
```

Sandbox behavior is platform-dependent. Treat `sandbox status` and `doctor` as
the source of truth for the current machine.

## Recommended Patterns

| Task | Recommended posture |
| --- | --- |
| Read-only repo summary | `--workspace` plus `--workspace-strict` |
| Local patch with tests | `--workspace`, `--workspace-lockdown`, and a scratch dir |
| Chat with possible writes | Web UI with approvals visible |
| Channel-connected agent | Conservative permissions and explicit channel config |
| Provider/debug investigation | Diagnostics on, minimal tool permissions |

## Web Safety

OpenSquilla web tools use provider configuration and guardrails. Use provider
diagnostics when web search behaves unexpectedly:

```sh
opensquilla search status
opensquilla search query "test query"
opensquilla diagnostics on
```

Search results and fetched pages are external data. They should inform the
answer, not override tool policy or user instructions.

For source-backed answers, `web_search` is the default high-level web tool.
`web_discover` is lightweight link discovery, `web_fetch` reads a specific
page, and `http_request` is reserved for raw HTTP/API requests.

## Tool Compression

Large tool results may be compacted before they are shown to the model. This is
normal and protects the active context window. See
[`features/tool-compression.md`](features/tool-compression.md).

## Artifacts and Media

Tool calls can publish artifacts and generate media. See
[`artifacts-and-media.md`](artifacts-and-media.md) for user-facing artifact,
document, image, PDF, and TTS workflows.

## Troubleshooting

If a tool does not run:

1. Check permission posture:

   ```sh
   opensquilla sandbox status
   opensquilla doctor
   ```

2. Check whether the gateway or channel surface requires approval.
3. Confirm the workspace path is correct.
4. Use diagnostics for repeated failures:

   ```sh
   opensquilla diagnostics on
   ```

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
