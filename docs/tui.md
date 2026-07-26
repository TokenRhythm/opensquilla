# Terminal Chat (TUI)

Terminal chat, also called the TUI, is the command-line chat surface for
OpenSquilla. Use it when you want an interactive conversation in a shell,
especially while working in a local project directory.

## Start Chat

Start terminal chat:

```sh
opensquilla chat
```

Bare `opensquilla chat` uses `auto`: it can start a compatible full-screen host
and otherwise falls back to `plain` before entering the alternate screen.
Current releases do not publish or install the companion, so release-installed
users remain on `plain`. `--ui tui` is strict: a missing or incompatible host
is a clear startup error. `--ui plain` selects the rescue renderer explicitly.

From a verified source checkout, bare chat can print the two development
commands needed to prepare and launch the source host. Installed wheels,
source archives without a Git worktree, Windows, and unsupported layouts stay
quiet rather than advertising unavailable release assets.

For the implicit local configuration, chat checks readiness before taking over
the terminal and starts the lifecycle-managed Gateway when necessary. An
explicit `OPENSQUILLA_GATEWAY_URL` is operator-owned: chat never starts a local
Gateway as a silent replacement.

You can still manage the local Gateway explicitly:

```sh
opensquilla gateway start --json
opensquilla chat
```

Use a specific model for the session:

```sh
opensquilla chat --model gpt-5.4-mini
```

Resume an existing session:

```sh
opensquilla chat --session <session-key>
```

Choose the terminal presentation explicitly when diagnosing startup:

```sh
opensquilla chat --ui tui    # require the full-screen host
opensquilla chat --ui plain  # minimal rescue renderer
```

Terminal chat is interactive and requires a real TTY. For scripts, pipes, CI,
or one-shot automation, use:

```sh
opensquilla agent -m "Inspect this workspace"
```

## Gateway and Standalone Modes

By default, `opensquilla chat` uses the gateway-backed chat path, so it shares
sessions, configuration, approvals, usage, and model/provider state with the Web
UI and other gateway clients.

## Reading the TUI

An empty session starts with a responsive OpenSquilla wordmark, one positioning
line, the resolved Agent/model/workspace/Gateway context, and the shortcuts
needed to begin. At roomy widths the wordmark uses a six-row display face; at
80×24 it becomes a two-row compact face, and pathological narrow/short panes
fall back to the plain product name. This introduction belongs to the empty
transcript: it scrolls away with work, is absent when canonical history is
resumed, and returns after `/new` or `/reset` produces an empty session.

After Gateway bootstrap completes, a fixed identity header keeps the product,
task, canonical Agent identity, shared surface, and Gateway state visible above
the transcript. Agent cards use the same canonical identity instead of a
TUI-local alias. The header is display-width aware: on a small terminal it drops
lower-priority fields instead of wrapping into the conversation.

The rest of the context adapts to terminal width:

- At **132 columns or wider**, a 30–36-column, full-height context rail shows
  Agent, task, workspace, surface, model, permission, Gateway, queue, context,
  and routing state. The transcript and composer are both inset beside it, so
  content never paints underneath the rail. The rail is context for the one
  linear transcript, not a second scrolling conversation.
- Below **132 columns**, the rail collapses into a compact, priority-ordered
  strip in the footer. Agent, permission, the current Router decision, and
  Gateway state take precedence; lower-priority values may be omitted when they
  do not fit. Normal decisions remain visible as `router cN confidence%`, while
  observe/fallback routes use warning styling.

Submitting a prompt immediately creates a live `Thinking` row in the transcript.
Before the provider's first event it says `Waiting for model output…`; if the
provider exposes reasoning, each real reasoning delta replaces that waiting row
and streams in place. The latest line has stronger contrast while a bounded
rolling window keeps earlier context visible. Providers that expose no reasoning
never get invented thought text: a sub-second wait disappears when output starts,
while a longer wait may leave an honest `Worked for Ns` receipt. Real reasoning
finishes as `Thought for Ns`.

Completed reasoning keeps up to eight of its latest visual rows by default.
Short reasoning is therefore fully readable without another action; longer
reasoning preserves the most recent context plus an exact count of earlier
hidden rows. The fixed cap avoids reflowing completed history when the terminal
height changes. Completed thinking narration and tool activity remain compact.
Press **Ctrl+O** to expand or collapse the complete detail delivered to the TUI
across the transcript, including thinking and reasoning deltas plus tool
arguments, process updates, results, and errors. The shortcut does not move
focus out of the composer.

An executed model ensemble appears as one live `Ensemble · n/m complete` row.
**Ctrl+O** expands its member models, providers, status, duration, token/cost
metadata, and errors. The final receipt is restored with session history; raw
candidate answers are not rendered.

Gateway chat exposes one shared model strategy with three states: `direct`,
`router`, and `ensemble`. Run `/strategy` to open the picker, or use
`/strategy direct|router|ensemble|status` for one-step control. The compatibility
commands `/router on|off|status` and `/ensemble on|off|status` operate on the
same state. The Gateway persists the selection and broadcasts it to WebUI and
other TUI clients. A change made while
a turn is streaming is immediate control-plane input: it does not enter the
prompt queue or interrupt the running turn, and applies to the next accepted
turn. The footer always retains the configured strategy; each Turn shows only
the Router decision or Ensemble progress that actually executed for that Turn.

The composer stays usable while a turn streams. **Enter** requests a steer of
the running Gateway turn at its next safe tool boundary; if the turn has already
crossed that boundary, the TUI says so and keeps the input as the next queued
turn. **Tab** explicitly queues the draft (completion-menu Tab still completes
the selected item first). UI-local commands continue to run immediately.

For long sessions, **Ctrl+O** toggles complete thinking/tool detail,
**Ctrl+L** forces a clean full repaint, and **Ctrl+G** or **Ctrl+End** jumps back
to the latest output after reading earlier scrollback. Unified diff output gets
a changed-file/add/remove summary and semantic line colors while retaining its
full raw tool result.

Folding is presentation, not deletion: the TUI retains every delta it receives,
including late deltas after a block is marked complete. Upstream provider or
Gateway compression can still bound what is delivered; see
[`features/tool-compression.md`](features/tool-compression.md) for that separate
contract.

Use standalone mode when you want direct terminal chat without the gateway
daemon:

```sh
opensquilla chat --standalone
```

Standalone mode accepts workspace flags for local file and tool work:

```sh
opensquilla chat --standalone --workspace /path/to/project --workspace-strict
```

In gateway mode, `--workspace` is ignored by terminal chat. Use a gateway-visible
path with `/path`, or use `/file` to upload a local file from the CLI machine.

## Common Commands

Type `/help` in terminal chat to see the commands supported by the current mode.
Typing `/` opens the curated command palette. Continue typing to fuzzy-search
canonical names and aliases; compatibility commands stay out of the default
palette but remain searchable. **Enter** runs a complete highlighted command,
while **Tab** only completes it so you can add arguments. Commands with required
arguments are never submitted by completion before the argument is present.

Slash controls and queries execute on the command plane: they do not become
user Prompt cards and do not enter the Turn queue. Commands such as `/file`,
`/image`, `/path`, and `/meta <name>` intentionally create a Turn because their
purpose is to send model input.

Commands available in both gateway and standalone chat include:

| Command | Purpose |
| --- | --- |
| `/help` | Show command help. |
| `/status` or `/session` | Show the active session and model. |
| `/new [title]` | Start a new session. |
| `/model [auto\|status\|name]` | Inspect or set the session model; Gateway TUI opens a picker when no argument is given. |
| `/cost` | Show usage for the current chat state. |
| `/clear` or `/reset` | Clear the current session context. |
| `/compact` or `/cmp` | Compact long context when possible. |
| `/save [path]` | Save the transcript. |
| `/image <path> [prompt]` | Send an image file with an optional prompt. |
| `/path <path> [prompt]` | Attach a file by path. |
| `/theme [name]` | Open or change terminal theme settings. |
| `/quit` or `/exit` | Leave chat. |

Gateway-only model strategy controls:

| Command | Purpose |
| --- | --- |
| `/strategy [direct\|router\|ensemble\|status]` | Open the shared strategy picker, switch strategy, or inspect canonical state. |
| `/router [on\|off\|status]` | Open the shared strategy picker, enable Router, select direct mode, or inspect canonical state. |
| `/ensemble [on\|off\|status]` | Open the same picker, enable Model Ensemble, select direct mode, or inspect canonical state. |

Standalone chat reports these controls as unavailable rather than maintaining
a second local Router/Ensemble configuration.

Gateway-backed chat also supports session and operations commands:

| Command | Purpose |
| --- | --- |
| `/sessions [limit]` | Open a searchable recent-session picker in TUI (table in plain mode). |
| `/resume [id]` | Open the picker, or resume a specific session. |
| `/delete <id>` | Delete a session. |
| `/usage` | Show aggregate usage. |
| `/meta` | List MetaSkills. |
| `/meta <name>` | Run a MetaSkill in the current session. |
| `/file <path> [prompt]` | Upload a local file and send it with a prompt. |
| `/permissions ...` | Inspect or change interactive permission mode. |
| `/approvals ...` | Inspect or reset approval state. |

`/models` and `/forget` remain executable compatibility commands, but are hidden
from the default palette. Use `/model` for session-model selection and
`/approvals` for the current approval state.

Standalone chat supports the core commands above, but `/models`, `/meta`, and
gateway-wide usage or approval commands require gateway mode.

## Files and Images

Use `/image` for image files:

```text
/image ./screenshot.png Describe the UI issue
```

Use `/path` when the file path is visible to the running chat process:

```text
/path ./docs/quickstart.md Summarize the setup steps
```

In gateway mode with a remote gateway, prefer `/file` so the CLI uploads the
local file before sending the turn:

```text
/file ./report.pdf Extract the action items
```

## TUI Host and Source Development

Current releases do not include the platform-specific TUI host. The companion
package and builders in this repository are development validation machinery,
not published assets. A future distribution rollout must keep core and host on
the same version and land separately.

Maintainers can explicitly use the source host while developing:

```sh
bun install --frozen-lockfile --cwd=src/opensquilla/cli/tui/opentui/package
OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 uv run opensquilla chat --ui tui
```

`OPENSQUILLA_TUI_BACKEND` is an internal runtime handoff, not a public selector;
bare chat ignores any pre-existing value so stale dotenv configuration cannot
disable its plain fallback. `OPENSQUILLA_TUI_DEV_SOURCE_HOST=1` is the explicit
permission to run Bun/source instead of an installed companion. Use the public
`--ui` option to select the presentation.

Read [`features/tui-frontend.md`](features/tui-frontend.md) for OpenTUI backend
status, Router HUD details, and replay benchmarks. Read
[`tui-real-terminal-harness.md`](tui-real-terminal-harness.md) only when you are
running maintainer integration tests for terminal rendering.

## Related Pages

- [`cli.md`](cli.md) for the full CLI reference.
- [`sessions.md`](sessions.md) for listing, resuming, exporting, and deleting
  sessions.
- [`approvals-and-permissions.md`](approvals-and-permissions.md) for permission
  profiles and approval workflows.
- [`features/meta-skill-user-guide.md`](features/meta-skill-user-guide.md) for
  `/meta` workflows.
- [`features/tui-product-contract.md`](features/tui-product-contract.md) for
  ownership, shared-session, fallback, and legacy-freeze rules.

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
