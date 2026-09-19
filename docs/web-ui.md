# Web UI

The OpenSquilla Web UI is the local control console for setup, chat sessions,
approvals, channels, logs, agents, usage, and operational status. It is the
best surface when you want browser-based chat, visible tool activity, durable
approvals, and a quick view of runtime health.

The Control UI is the Vue product UI served by the gateway. The historical
`control_ui.frontend = "legacy"` setting is accepted temporarily so existing
profiles still start, but it is deprecated, normalized to `"vue"`, and no
longer activates the vanilla-JS client.

## Start the Web UI

Run the gateway in the foreground:

```sh
opensquilla gateway run
```

Open:

```text
http://127.0.0.1:18791/control/
```

Or start a managed background gateway:

```sh
opensquilla gateway start --json
opensquilla gateway status
```

The default gateway binds to `127.0.0.1` for safety.

For gateway lifecycle, host/port, and exposure details, see
[`gateway.md`](gateway.md).

## Packaged and Source Installs

Official Python wheels, Desktop installers, and container images already
include the built Vue console. Those install paths do not require Node.js or
npm on the user's machine.

A Git checkout contains the Web UI sources instead of a committed build tree.
The source installers run the following automatically and then package the
result with OpenSquilla:

```sh
cd opensquilla-webui
npm ci
npm run build
```

Source installers and contributors therefore need Node.js 22.12+ and npm.
Every source reinstall runs `npm ci` and rebuilds the console; the first run
normally downloads the most, while a warm npm cache reduces later network use
but not all build time or disk writes. Contributors should rerun the build after
Web UI changes. A standard wheel build rejects a missing or stale console rather
than silently producing a wheel with an empty `/control/` page.

That fail-closed rule also applies to direct `pip install .`,
`uv tool install .`, and VCS URL installs. Build the Web UI first when working
from a local checkout; VCS URL users should clone the repository and run the
source installer, or install the official release wheel.

Standard source archives (`sdist`) also reject ignored personal BGM files so a
shareable archive cannot accidentally leak private or copyrighted audio. A
direct locally built wheel or local Docker image can still contain an explicitly
customized library; official release artifacts always require the tracked
playlist to remain empty.

## Main Areas

| Area | Use it for |
| --- | --- |
| Chat | Run and resume chat sessions, inspect tool activity, launch `/meta` workflows, publish artifacts, and use manual compact controls. |
| Conversations | Switch active sessions from the sidebar and keep long-running work visible. |
| Overview / Health | See readiness, provider state, memory state, sandbox posture, and recovery hints. |
| Settings | Configure providers, router, search, channels, permissions, and other setup sections from a modal flow. |
| Channels | Inspect configured channel adapter status and jump to guided setup for configuration changes. |
| Skills | Browse skill readiness and MetaSkill availability. |
| Sessions | Inspect the durable sessions ledger and operational state. |
| Agents | Manage durable agent entries. |
| Usage | Inspect token and estimated-cost rollups. |
| Cron | View and manage scheduled runs. |
| Logs | Inspect runtime logs and diagnostics. |
| Approvals | Respond to sensitive tool-call approval requests. |

## Chat Sessions

The chat UI supports:

- streaming assistant output;
- tool-call cards;
- turn activity and RunTrace views for provider, router, tool, and usage events;
- inline approval requests for sensitive actions;
- artifact cards with thumbnails when previews are available;
- a deliverables drawer for generated outputs;
- share and export actions for handoff;
- a conversation sidebar for switching sessions;
- durable `/goal` objectives with structured progress, usage, pause/resume,
  edit, clear, guardrail, and Plan-mode waiting states;
- `/meta` listing and run launch on gateway-backed chat sessions;
- pending message queue behavior while compaction or runtime work is in flight;
- manual `/compact`;
- per-turn usage and savings metadata when available;
- copyable session IDs;
- mobile tabs that keep chat, sessions, and operational views reachable on
  narrow screens.

Use the session selector to switch between existing sessions. Copy the session
key when reporting a bug or asking another OpenSquilla surface to inspect the
same session.

Slash command suggestions complete before they run. `Tab` always completes the
active candidate, while `Enter` completes a partial match and runs only an
exact command. Unknown commands remain in the composer with a recovery hint.

Use `/goal <objective>` to start a multi-turn Goal. Its ribbon remains visible
while working or waiting, and mutation results, hydration, and the Goal event
stream keep it synchronized after reconnects. See [`goal-mode.md`](goal-mode.md)
for the lifecycle, execution-lease, guardrail, and Plan-mode contracts.

Coding mode can be enabled from chat when you want code modifications routed
through `opensquilla code-task`. With Coding mode on, code changes use the
guarded host workflow described in [`cli.md`](cli.md#coding-mode-and-code-task)
instead of ordinary in-session editing. Enter `/coding` to toggle the mode.
While it is enabled, the composer shows a `Coding ON` status control that can
also turn the mode off. The explicit `/coding on`, `/coding off`, and
`/coding status` forms remain available for compatibility.

## Workspace Changes

A project workspace can be reviewed without leaving the console. Open the
project's action menu in the sidebar and choose **View changes**; the review
opens as a Workbench panel titled with the project name.

The panel shows:

- the current branch, and how far it has diverged from its upstream;
- every changed path, each stamped with its change type (modified, added,
  deleted, renamed, untracked, conflict) and whether the change is staged,
  unstaged, or both;
- the unified diff for the selected file, with the staged or unstaged half
  chosen to match the entry that was selected.

A file that is not tracked yet has no index entry to diff against, so the panel
compares it against an empty file and shows the ordinary "new file" diff.

A workspace that is not a Git repository reports that explicitly instead of
showing an empty change list, and a workspace whose directory is missing or
untrusted reports why it cannot be read. Large change sets and large diffs are
bounded and say so when they are.

Git is read with the same read-only hardening as the agent's own Git tools, so a
repository cannot make this panel run a helper it controls (an external diff
driver, a diff textconv, or a filesystem monitor hook).

## Staging And Committing

Each row carries its own actions: a path can be staged or unstaged, a tracked
change can be discarded (which asks first, and is refused for a path Git does
not track yet, because restoring one of those would delete it), and a section
offers the same action for every row in it. The commit row at the top writes the
message and commits the index; push and undo-commit sit on the branch row, next
to the divergence they depend on.

Undo is a soft reset: the commit goes and its content stays staged. It refuses a
tip the upstream already has, and the root commit, which has no parent to reset
to. Push refuses a branch with no upstream rather than guessing one.

A commit message can be drafted from the staged changes with the ✨ action in
the commit row. The draft is text the operator edits before committing: nothing
is committed by drafting, and the model never writes to the repository, the
index, or the working tree. The rule the draft follows is an application
setting — **Session behavior → Commit message rule** — where the switch that
offers drafting at all sits beside it. Drafting a message sends the staged patch
to the configured model; with the switch off, it never leaves the machine.

The actions skip the read-only hardening on purpose: staging, committing and
undoing are the operations that legitimately take the index lock, and a push is
the one call allowed to wait on a credential helper.

## Manual Compaction

Long sessions can be compacted from chat. If no compaction is needed, the UI
reports:

```text
Already within context budget; no compact was applied
```

If compaction is running, wait for its terminal state before assuming the next
message has the compacted context. See
[`features/compaction-and-cache.md`](features/compaction-and-cache.md).

## Artifacts

When the agent publishes a file, the Web UI shows an artifact card. Use artifact
cards for:

- generated HTML prototypes;
- reports and briefings;
- exported data files;
- PDFs, slide decks, images, and other generated outputs.

Artifact cards may include thumbnails or preview metadata, and the deliverables
drawer keeps published outputs discoverable after the originating turn has
scrolled away.

HTML artifact previews always show whether they are using full network access
or offline mode. A local Web UI may request either mode; a remotely reached Web
UI is forced offline and runs bundle scripts in an opaque sandbox while
explicitly reporting that workers, persistent storage, and root-absolute paths
are not guaranteed. Ordinary web links continue to open in a separate browser
tab with `noopener,noreferrer`. The Desktop app additionally offers an explicit
action to open an HTTP(S) link in its isolated side browser.

The full Desktop preview is intentionally browser-like, not a privileged
Electron view. Each open item has a separate temporary cookie/storage/cache
partition and no Node, preload, IPC, host filesystem, OpenSquilla identity, or
system-browser session. Closing an item clears its temporary state. Device
permissions, user-initiated downloads, popups, and external protocols remain
host-mediated; client certificates from the operating-system store are never
offered to preview pages. The active OpenSquilla Gateway is also unreachable
inside side previews, preventing network proximity from becoming an ambient
OpenSquilla identity; users can still open an explicit link in the system
browser.

For channel delivery limits and artifact recovery, see
[`artifacts-and-media.md`](artifacts-and-media.md).

## Approvals

Some tools require confirmation. The approvals area gives operators a durable
place to approve or deny sensitive actions instead of burying the decision in
chat text.

Use the approvals area when:

- the agent wants to write files;
- a command requires elevated permissions;
- a channel or external action needs human confirmation;
- unattended automation should pause before a risky operation.

## Logs and Diagnostics

For local diagnosis:

```sh
opensquilla diagnostics on
opensquilla gateway status
opensquilla doctor
```

Use the Web UI logs and health views to correlate provider readiness, channel
state, session state, and user-visible errors.

## Safety

The Web UI is local by default. If you bind the gateway to a public interface,
configure token auth and network controls first:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Do not expose an unauthenticated gateway to the public internet.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
