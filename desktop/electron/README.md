# OpenSquilla Electron Desktop Shell

This package is the macOS desktop shell for the existing OpenSquilla Control UI.
It does not rewrite the Vue frontend. The Electron main process configures a
desktop credential, starts a local gateway, and loads the backend-served
`/control/` app.

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

Use Node.js 22.12 or newer. The Vue build under
`src/opensquilla/gateway/static/dist/` is generated and ignored by Git; local
Desktop packaging verifies it against the current frontend source before
PyInstaller runs.

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

This builds the Vue Control UI, bundles the gateway with PyInstaller, and emits
desktop artifacts for the current platform under `dist/desktop-electron/`.

For a faster rebuild after the runtime already exists:

```bash
cd desktop/electron
npm run build:web
npm run dist
```

## Windows Release Signing

Windows release builds are currently unsigned. The release workflow builds the
NSIS installer with electron-builder and uploads the unsigned `.exe`,
`.blockmap`, and `latest.yml` artifacts together so updater metadata matches
the exact installer bytes.

Do not sign the `.exe` after `latest.yml` is emitted; that changes the
installer bytes and invalidates the updater hash. If Windows code signing is
enabled later, it must run inside the release build before updater metadata,
blockmaps, and `SHA256SUMS` are finalized. See
[`docs/code-signing-policy.md`](../../docs/code-signing-policy.md) for the
current policy.

## Current Scope

- Reuses `opensquilla-webui` and the Python gateway exactly as they run in the
  browser.
- Starts a bundled `runtime/gateway/opensquilla-gateway` in packaged builds.
- Falls back to `uv run opensquilla gateway run --bind 127.0.0.1 --port <port>`
  during development when no bundled runtime exists.
- Uses `contextIsolation: true`, `nodeIntegration: false`, and a minimal preload
  bridge.
- Writes credential, config, state, and gateway logs under the Electron
  `userData` directory.

## Release Work Still Needed

- Enable the runtime updater flow once the published feed is ready.
