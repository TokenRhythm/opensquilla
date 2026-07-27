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
- `/meta` listing and run launch on gateway-backed chat sessions;
- pending message queue behavior while compaction or runtime work is in flight;
- manual `/compact`;
- per-turn usage and savings metadata when available;
- copyable session keys;
- mobile tabs that keep chat, sessions, and operational views reachable on
  narrow screens.

Use the session selector to switch between existing sessions. Copy the session
key when reporting a bug or asking another OpenSquilla surface to inspect the
same session.

Coding mode can be enabled from chat when you want code modifications routed
through `opensquilla code-task`. With Coding mode on, code changes use the
guarded host workflow described in [`cli.md`](cli.md#coding-mode-and-code-task)
instead of ordinary in-session editing.

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

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
