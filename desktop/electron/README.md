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

On first run, the shell opens a setup window for provider, model, base URL, and
API key. The key is encrypted with Electron `safeStorage` when available, and a
desktop-specific gateway config is written under Electron `userData`.

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
