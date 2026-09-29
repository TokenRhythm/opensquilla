# OpenSquilla Electron Desktop Shell

This package is the macOS, Windows, and Linux desktop shell for the existing
OpenSquilla Vue UI. It does not rewrite the frontend. Electron loads a local
Desktop renderer first, then starts the Gateway as a background runtime and
publishes a typed connection descriptor when it is ready. A Gateway failure
disables runtime-backed features but does not take down the application window.

The browser `/control/` entry remains available. Desktop packages keep one
verified Vue artifact at `runtime/gateway/control-ui-dist`; Electron loads it
locally and the bundled Gateway serves that same copy to browser clients. The
artifact is removed from the frozen Python subtree after PyInstaller staging so
the new startup model does not duplicate the UI or inflate the installer.

## Development Flow

From the repository root:

```bash
cd opensquilla-webui
npm ci
npm run build

cd ../desktop/electron
npm ci
npm run dev
```

Use Node.js 22.12 or newer. Vite writes the Vue build under
`opensquilla-webui/dist/`; `npm run build` then verifies and explicitly stages
the same bytes under `src/opensquilla/gateway/static/dist/` for Python
packaging. Both directories are generated and ignored by Git. Local Desktop
packaging consumes the source-owned artifact and verifies it before PyInstaller
runs.

Desktop TypeScript uses Node 24 definitions to match Electron's embedded Node
runtime. The Node.js version used to run build scripts is a separate requirement.
The built-in browser driver uses the pinned `playwright-core` 1.63 dependency
and its public CDP transport API. It attaches to an existing `WebContentsView`
through an isolated debugger session; it does not download or launch a separate
Chromium browser or enable a production remote-debugging port. When upgrading
Electron or Playwright, run both the attachment and native viewport checks.

## Built-in browser MCP

The owned Gateway discovers the Desktop's authenticated local MCP endpoint at
startup. No user-managed MCP configuration is needed. Owner conversations in
the Desktop receive `mcp__desktop-browser__browser_*` tools for listing, opening,
navigating, reloading, inspecting, interacting with, and capturing URL pages.
Existing workspace artifact previews continue to use the native `browser` tool.
Older Desktop shells without the MCP endpoint retain that native tool as a
fallback. CLI, channel, guest and subagent contexts do not receive this capability.

The workspace button is available throughout Desktop chat, including a new task
with no open pages. Its empty panel accepts a web address, and the header's new-tab
button opens another page. Closing the final browser tab leaves the address panel
available. Manually opened pages belong to the current task, including its draft
before the first message, so that task's browser tools can discover and operate
them. Switching tasks hides their pages while retaining their in-memory state.

Mouse movement uses Playwright's browser input, with a visible pointer following
accepted movement and click events. Element clicks retain Playwright's visibility,
stability, enabled-state and hit-target checks. The pointer does not move the
operating-system cursor, intercept page input, or appear in tool screenshots.

Session identity and operation IDs come from trusted Gateway context. Page refs
identify actual conversation-owned views; element refs expire on navigation or
replacement. Mutating calls retain receipts for the server lifetime so replaying
the same call ID cannot repeat a click. A timeout can leave the action outcome
unknown: inspect the page before deciding whether to submit again. Receipt
capacity is bounded to 4,096 mutations per Desktop server lifetime.

The browser advertises supported parameters in its MCP catalog. Existing tool
names and required arguments remain compatible with older clients. In clients
with the extended catalog:

- `browser_act` supports `button` for clicks, bounded `hold` with `durationMs`,
  and `drag` between element refs. Coordinate gestures use `browser_batch` with
  the current screenshot identities; a drag additionally supplies `toX`/`toY`.
  A gesture owns its complete press/move/release sequence and releases held
  input when it is cancelled.
- `browser_inspect` with a readable `ref` returns visible text and form values
  without flattening whitespace. `maxChars` bounds the result and truncation
  is explicit. The ordinary compact snapshot remains available for navigation.
- `browser_open` with `contextTargetRef` creates a related tab in an owned
  page's storage context. Omitting it preserves the independent-page behavior.
  Context inheritance never permits access to another task's pages.
- The `upload` action selects a current task attachment by `fileId`, using an
  input `ref` or the reported `chooserId`. `cancelUpload` dismisses that chooser.
  The Gateway resolves persisted user attachments and supplies their bytes;
  model-provided local paths are not accepted. Available attachments are listed
  in browser results when this capability is negotiated.
- The `download` action clicks a ref with a managed capture already armed.
  Its page-owned `downloadId` can be read through `browser_inspect`; UTF-8 text
  is returned with explicit truncation, while binary artifacts return metadata.
  Uploads and managed downloads are limited to 8 MiB. Download artifacts are
  temporary, bounded, and cleaned up when the owning page closes. Ordinary
  downloads outside this explicit action retain the native save dialog.

File chooser interception is scoped to an automated action, preserving native
pickers for manual clicks while idle. A chooser opened asynchronously after the
action returns may still use the native picker. A managed download blocked by
a JavaScript dialog returns the blocker immediately and disarms its capture;
accepting that dialog can use the native save dialog and does not produce a
managed `downloadId`.

These extensions do not expose arbitrary JavaScript, browser-global commands,
or cookie export. Explicit Gateway network restrictions are rejected in Safe
mode because the attached renderer does not yet use the Gateway's network proxy.
Screenshots use the existing model image-result channel.

```bash
npm run test:browser-mcp
```

These offline fixtures use temporary profiles and synthetic local pages. The
suite includes real Electron tests for existing storage, same-URL page identity,
hidden views and windows, debugger coexistence, cancellation, reconnect and
concurrent calls. It also uses the production dependency collector to build and
run a temporary ASAR, without an installer or signing. Linux without a display
requires `xvfb-run`.

## Desktop startup

On first run, the shell starts the client and Gateway with an explicitly
unconfigured model profile, then offers a non-modal setup window. **Set up later**
and the window close button leave the client running; later launches go straight
to the client. The chat composer offers a shortcut to provider settings while
the Gateway reports that the model is not configured.

Saving setup validates local fields without requiring a network connection or
a successful provider test. **Test connection (optional)** reports connectivity
separately and does not prevent saving or leaving setup. Credentials use Electron
`safeStorage` when available, and the profile lives under Electron `userData`.
Only TokenRhythm and OpenRouter supply a first-run model preset; other providers
require an explicit model choice. The local Gateway restarts after setup is saved
to apply the new configuration.

`npm run test:onboarding-flow` exercises the native invitation, optional probe
failures, dismissal, and restart. `npm run test:onboarding-first-chat` exercises
an empty first run through Settings to a real Gateway chat against a local model
fixture. Both use isolated profiles and synthetic credentials. Set
`OPENSQUILLA_DESKTOP_FIRST_CHAT_OUTPUT_DIR` to retain first-chat screenshots and
its JSON report.

Router support and default tiers come from the backend provider catalog and
preset registry. After changing them, regenerate the checked-in offline catalog
from the repository root:

```bash
uv run python scripts/generate_desktop_router_catalog.py --write
uv run python scripts/generate_desktop_router_catalog.py --check
```

The Desktop build reads `src/generated/desktop-router-catalog.ts` without running
Python. CI checks that it matches the backend and that the compiled Desktop
serializer produces the same Gateway routes. Conflicting routing selections are
rejected; saved routing conflicts expose the boot page's **Reset setup** action.

The shell looks for the checkout root automatically. To point it at a different
checkout:

```bash
OPENSQUILLA_DESKTOP_REPO_ROOT=/path/to/opensquilla npm run dev
```

During development, the shell starts a gateway from the selected checkout by
default. To force a specific local port:

```bash
OPENSQUILLA_DESKTOP_GATEWAY_PORT=18793 npm run dev
```

To attach to an already-running gateway instead of spawning one:

```bash
OPENSQUILLA_DESKTOP_GATEWAY_URL=http://127.0.0.1:18791 npm run dev
```

## Local Release Build

```bash
cd desktop/electron
npm run dist:local
```

This builds the shared Vue browser/Desktop artifact, bundles the gateway with
PyInstaller, removes its staged duplicate UI copy, and emits desktop artifacts
for the current platform under `dist/desktop-electron/`.

`npm run pack` (unpacked directory) and `npm run dist` (installer) both build
the WebUI and Gateway before packaging. The `:local` names are compatibility aliases.
For a faster Electron-only rebuild after a successful Gateway build:

```bash
cd desktop/electron
npm run dist:prepared
```

The internal `pack:prepared` / `dist:prepared` entries reject missing or stale
Gateway build records and changed runtime files before electron-builder runs.
Changes to Python sources, migrations, Router resources, the built WebUI, dependency
locks, or the Gateway build recipe require a new full build. Final package verification
also checks every migration ID/content and the Router manifest's SHA256 values.
Release CI builds the Gateway once, verifies prepared outputs, then signs and packages
them; final signature checks remain separate from resource checks.

## Windows Release Signing

The release workflow signs new Windows builds through DigiCert KeyLocker. It
uses the protected `windows-code-signing` environment for manually
dispatched test artifacts and `v*` release tags. Missing credentials, signing
failures, and signature-policy mismatches fail the Windows build.

Signing runs inside electron-builder before updater metadata, blockmaps, and
`SHA256SUMS` are finalized, so those files describe the signed installer bytes.
The expected public certificate identity and timestamp endpoint are defined in
`.github/signing/windows-signing-policy.json`; credentials remain GitHub
environment secrets. See
[`docs/code-signing-policy.md`](../../docs/code-signing-policy.md) for the
current policy.

## Current Scope

- Reuses `opensquilla-webui` for both the local Desktop entry and browser
  `/control/` entry.
- Loads the local renderer before waiting for Gateway `/readyz`.
- Starts a bundled `runtime/gateway/opensquilla-gateway` in packaged builds.
- Falls back to `uv run opensquilla gateway run --listen 127.0.0.1 --port <port>`
  during development when no bundled runtime exists.
- Uses `contextIsolation: true`, `nodeIntegration: false`, and a minimal preload
  bridge.
- Writes credential, config, state, and gateway logs under the Electron
  `userData` directory.

## Release Work Still Needed

- Enable the runtime updater flow once the published feed is ready.
