# CLI Reference

The `opensquilla` CLI is the fastest way to configure, run, inspect, and
automate OpenSquilla.

This document describes the separate Python/uv CLI installation. The Windows
Desktop installer does not install a global `opensquilla` command; Desktop
users should launch OpenSquilla from the Start menu or taskbar.

To install the command-line interface separately from OpenSquilla Desktop:

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/TokenRhythm/opensquilla/releases/download/v0.5.5/opensquilla-0.5.5-py3-none-any.whl"
```

Run:

```sh
opensquilla --help
opensquilla <command> --help
```

## Main Commands

| Command | Purpose |
| --- | --- |
| `opensquilla init` | Initialize a workspace. |
| `opensquilla doctor` | Diagnose readiness and print recovery steps. |
| `opensquilla uninstall` | Remove OpenSquilla; keeps your data by default (`--purge-*` to delete). |
| `opensquilla onboard` | Run or inspect first-run setup. |
| `opensquilla configure` | Reconfigure provider, router, channels, search, image generation, or memory embedding. |
| `opensquilla gateway` | Run and manage the gateway server. |
| `opensquilla chat` | Start interactive terminal chat. |
| `opensquilla agent` | Run a single automation-friendly agent turn. |
| `opensquilla sessions` | List, inspect, resume, abort, delete, or export sessions. |
| `opensquilla skills` | List, search, view, install, update, publish, and inspect skills. |
| `opensquilla memory` | Inspect and maintain memory. |
| `opensquilla channels` | Configure and inspect messaging channels. |
| `opensquilla providers` | Configure and inspect LLM providers. |
| `opensquilla search` | Configure and use web search. |
| `opensquilla sandbox` | Inspect or change default sandbox posture. |
| `opensquilla cron` | Manage scheduled OpenSquilla runs. |
| `opensquilla cost` | Inspect usage and estimated cost. |
| `opensquilla diagnostics` | Enable or disable runtime diagnostics logging. |
| `opensquilla replay` | Replay a recorded turn from the decision log. |
| `opensquilla migrate` | Import state from external agent runtimes. |
| `opensquilla models` | Inspect available models. |
| `opensquilla agents` | Manage durable agents. |
| `opensquilla mcp-server` | Run the OpenSquilla MCP server bridge. |
| `opensquilla dist` | Emit a reproducible workspace-state inventory. |
| `opensquilla reset` | Archive the transcript and summaries, then reset the session. |

## Run Surfaces

Web UI and gateway:

```sh
opensquilla gateway run
opensquilla gateway start --json
opensquilla gateway status
opensquilla gateway restart
opensquilla gateway stop
```

Terminal chat:

```sh
opensquilla chat
opensquilla chat --model gpt-5.4-mini
opensquilla chat --session <session-key>
opensquilla chat --standalone --workspace /path/to/project
```

Release installs use the stable Python-native terminal backend. The full-screen
OpenTUI host is development-only and is not published as a release asset. It
can be evaluated from a source checkout with pinned Bun dependencies installed:

```sh
bun install --frozen-lockfile --cwd=src/opensquilla/cli/tui/opentui/package
OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 uv run opensquilla chat --ui tui
```

Use `--ui plain` to select the rescue renderer explicitly. Read [`tui.md`](tui.md) for
terminal chat usage and [`features/tui-frontend.md`](features/tui-frontend.md)
for backend architecture, plugin slots, Router HUD, and replay benchmark
workflow.

One-shot automation:

```sh
opensquilla agent -m "Review the current directory"
opensquilla agent --json -m "Return a short machine-readable summary"
opensquilla agent --workspace /path/to/project --workspace-strict -m "Inspect this repo"
opensquilla agent --timeout 600 --max-iterations 30 -m "Run a bounded investigation"
```

Useful automation flags:

| Flag | Purpose |
| --- | --- |
| `--workspace` | Set the workspace root. |
| `--workspace-strict` | Restrict read-side file tools to the workspace. |
| `--workspace-lockdown` | Contain writes to workspace or scratch directory. |
| `--scratch-dir` | Place temporary scripts/logs/candidate patches in a known directory. |
| `--timeout` | Set total agent wall-clock timeout. |
| `--max-iterations` | Bound the model/tool loop. |
| `--max-provider-retries` | Bound transient provider retries. |
| `--length-capped-continuations` | Bound automatic continuations after length-limited provider output. |
| `--thinking` | Override reasoning level. |
| `--permissions` | Select restricted, bypass, or full permission posture. |
| `--transcript-path` | Write a JSONL transcript for automation. |
| `--usage-path` | Write usage JSON. |
| `--event-stream-stderr` | Stream stable v1 progress-event JSONL on stderr. |
| `--session-db-path` | Persist session replay across invocations. |

### Agent Progress Event Stream

`--event-stream-stderr` is opt-in. It does not change the final stdout payload
or exit status. Each supported event is flushed to stderr as one compact JSON
object with this envelope:

```json
{"_event":true,"schema_version":1,"kind":"thinking"}
```

stderr can also contain ordinary diagnostics. Subprocess consumers must drain
it continuously, parse it line by line, and accept only objects whose `_event`
value is `true`. A closed or unwritable stderr disables further progress events
without failing an otherwise successful agent run.

The v1 event fields are intentionally smaller and more stable than the engine's
internal event dataclasses:

| `kind` | Additional fields |
| --- | --- |
| `router_decision` | `tier`, `model`, `source` |
| `thinking` | None |
| `text_delta` | `presentation` |
| `run_heartbeat` | `phase`, `elapsed_ms`, `idle_ms` |
| `tool_use_start` | `tool_use_id`, `tool_name`, `started_at` |
| `tool_result` | `tool_use_id`, `tool_name`, `is_error` |
| `warning`, `error` | `code`, redacted and bounded `message` |
| `artifact` | `id`, `name`, `mime`, `size` |
| `done` | None; read the final result from stdout |

The stream does not expose reasoning text, answer text, tool arguments, tool
results, internal routing probabilities, session paths, or fields added to
future engine events. Unsupported internal events are skipped. Consumers may
use the stable top-level fields above and must ignore additional fields that a
future compatible v1 producer may add.

### Concurrent Agent Subprocesses

Each write-capable agent holds a profile-wide writer lease. Calls that share an
`OPENSQUILLA_STATE_DIR` therefore conflict instead of writing the same profile
concurrently. An orchestrator that needs parallel agents must give every child
both a distinct profile home and a distinct gateway state root:

```sh
OPENSQUILLA_STATE_DIR=/tmp/agent-a \
OPENSQUILLA_GATEWAY_STATE_DIR=/tmp/agent-a/state \
  opensquilla agent -m "task A" --json &
```

`OPENSQUILLA_STATE_DIR` alone does not override a `state_dir` from a
current-directory `opensquilla.toml`, an explicit gateway config, or a copied
profile. When copying `config.toml` or `.env`, remove or rewrite `state_dir` and
`OPENSQUILLA_GATEWAY_STATE_DIR`. Also choose distinct `--session-db-path`,
workspace, scratch, transcript, and usage paths when those outputs must be
isolated. On Windows, pass both environment variables in each child process
rather than relying on POSIX inline assignment syntax.

## Configuration Commands

Provider and router:

```sh
opensquilla onboard
opensquilla onboard status
opensquilla configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
opensquilla configure router --router recommended
opensquilla providers list
opensquilla providers configure openrouter
opensquilla providers status
```

Search:

```sh
opensquilla search list
opensquilla search configure duckduckgo
opensquilla search query "latest OpenSquilla release"
opensquilla configure search --search-provider duckduckgo
```

Channels:

```sh
opensquilla channels types
opensquilla channels describe telegram
opensquilla channels add telegram --name personal
opensquilla channels list
opensquilla channels status
opensquilla channels enable personal
opensquilla channels disable personal
opensquilla channels restart personal
opensquilla channels remove personal
```

Raw config:

```sh
opensquilla config get llm.provider
opensquilla config set port 18791
```

More detail:

- [`configuration.md`](configuration.md)
- [`providers-and-models.md`](providers-and-models.md)
- [`search.md`](search.md)
- [`channels.md`](channels.md)

## Skills

```sh
opensquilla skills list
opensquilla skills search pdf
opensquilla skills search pdf --json --include-diagnostics
opensquilla skills view pdf-toolkit
opensquilla skills install <install-reference> --source <clawhub|skillhub|github>
opensquilla skills install <install-reference> --source <clawhub|skillhub|github> \
  --force --risk-confirmation <token>
opensquilla skills update --install-id <install-id>
opensquilla skills update --all
opensquilla skills uninstall <skill-name>
opensquilla skills uninstall --install-id <install-id>
opensquilla skills doctor [<skill-name-or-install-id>] --json
```

`skills search --json` keeps the legacy top-level array for existing clients.
Add `--include-diagnostics` when a stable results-and-source-diagnostics envelope is needed.

Read:

- [`features/skills.md`](features/skills.md)

## Sessions and History

```sh
opensquilla sessions list
opensquilla sessions show <session-key>
opensquilla sessions resume <session-key>
opensquilla sessions abort <session-key>
opensquilla sessions export <session-key>
opensquilla sessions delete <session-key>
```

Read: [`sessions.md`](sessions.md)

## Memory

```sh
opensquilla memory status
opensquilla memory index
opensquilla memory list
opensquilla memory search "preference"
opensquilla memory show <path>
opensquilla memory dream
```

Read: [`features/memory.md`](features/memory.md)

## Durable Agents and Scheduling

```sh
opensquilla agents list
opensquilla agents add research --name Research --workspace /path/to/research
opensquilla agents delete research
opensquilla cron list
opensquilla cron add --every 1h --text "Summarize important updates" --name hourly-summary
opensquilla cron status <job-id>
opensquilla cron runs <job-id>
```

Read:

- [`agents.md`](agents.md)
- [`scheduling.md`](scheduling.md)

## Cost, Diagnostics, and Replay

```sh
opensquilla cost
opensquilla diagnostics status
opensquilla diagnostics on
opensquilla diagnostics off
opensquilla replay --session <session-key> --turn <turn-id>
```

Use diagnostics and replay when you need to understand why a turn behaved a
certain way.

Read:

- [`usage-and-cost.md`](usage-and-cost.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

## MCP Server Bridge

MCP support is included in the current source's base installation. See the
[MCP installation requirements](mcp-server.md#requirements) for SDK 2.x source
and release installation options. Start the gateway before launching the stdio
bridge from an MCP-capable client:

```sh
opensquilla mcp-server run
opensquilla mcp-server run --gateway ws://localhost:18792/ws
```

Read: [`mcp-server.md`](mcp-server.md)

## Uninstall

```sh
opensquilla uninstall --dry-run        # preview what is removed and kept
opensquilla uninstall                  # remove the program, keep your data
opensquilla uninstall --purge-state    # also delete runtime state (sessions, logs, cache)
opensquilla uninstall --purge-config   # also delete config and secrets
opensquilla uninstall --purge-all      # delete ALL OpenSquilla data (needs a typed phrase)
opensquilla uninstall --json           # machine-readable plan/result
```

Your data is kept by default; `--purge-*` opts into deletion, and `--purge-all`
requires typing a confirmation phrase (or `--confirm-purge-all "delete
everything"` on non-interactive surfaces). The running gateway is drained and
stopped before anything is removed, and deletion is contained to the OpenSquilla
home — a relocated or shared root is refused. Docker and desktop installs print
guided removal steps instead of deleting an image layer or app bundle; source
installs never delete your checkout.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
