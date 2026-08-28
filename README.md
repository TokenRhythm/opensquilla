# OpenSquilla mobile

> Android packaging of the OpenSquilla gateway. One APK, no companion app, no
> server required — the FastAPI gateway boots inside the app via Chaquopy and
> serves the Vue web UI in a local WebView on 127.0.0.1:18790.

[![Download APK](https://img.shields.io/badge/download-OpenSquilla--mobile--debug.apk-%233DDC84)](https://github.com/lzhhhhc/OpenSquilla-mobile/releases/download/v0.1.0-mobile/OpenSquilla-mobile-debug.apk)

- **Download APK (recommended, GitHub Release):** [OpenSquilla-mobile-debug.apk](https://github.com/lzhhhhc/OpenSquilla-mobile/releases/download/v0.1.0-mobile/OpenSquilla-mobile-debug.apk)
- **APK also tracked in repo:** [releases/OpenSquilla-mobile-debug.apk](releases/OpenSquilla-mobile-debug.apk)
- Min Android: 8.0 (API 26), ABI: arm64-v8a
- Configuration example: [config.example.toml](config.example.toml)
- License: MIT, see [LICENSE](LICENSE)

## What this is

OpenSquilla mobile is the Android packaging of the
[OpenSquilla](https://github.com/opensquilla/opensquilla) gateway. The full
desktop Python package is bundled into the APK at
`app/src/main/python/opensquilla/`. Chaquopy extracts it to the app's private
files directory and starts a uvicorn instance on the loopback only. The
Vue web UI is served by the same FastAPI process and rendered in a WebView.

## Configuration

The gateway reads a TOML config file from the app's private storage on the
device:

```
/data/user/0/ai.opensquilla.app/files/state/config.toml
```

Use [`config.example.toml`](config.example.toml) as a starting point:

- **Providers** — the app works with the same provider set as the desktop
  build (tokenrhythm, openrouter, DeepSeek, any OpenAI-compatible endpoint).
  Fill in a `[provider.*]` block with your own base URL and API key.
- **Squilla router** — Android builds do not bundle the desktop V4 ML/ONNX
  artifact, so `[squilla_router]` defaults to `enabled = false`,
  `require_router_runtime = false`. Heuristic (default-tier) routing still
  works, and the doctor report stays green.
- **Sandbox** — a real sandbox backend (Bubblewrap) is unavailable on
  Android; the gateway falls back to the `noop` backend by default.
- **Apply changes** — edit the file (via the in-app settings or adb/run-as),
  then force-stop and relaunch the app.

To verify a configuration, open **Overview → Support & diagnostics** in the
app and run the built-in doctor report.

## Project layout

```
app/
|-- build.gradle                 # Chaquopy + Android Gradle configuration
|-- src/main/
|   |-- AndroidManifest.xml
|   |-- java/ai/opensquilla/app/
|   |   `-- MainActivity.kt      # WebView host, status-bar insets, file picker
|   |-- python/
|   |   |-- opensquilla_android.py   # Chaquopy entry: boots the gateway
|   |   `-- opensquilla/             # Full desktop Python package
|   |       `-- gateway/
|   |           |-- config.py
|   |           |-- boot.py
|   |           |-- control_ui.py
|   |           |-- health/           # doctor report + evaluators
|   |           |-- squilla_router/
|   |           |-- skills/
|   |           `-- ...
|   `-- res/
|-- wheels/                      # Pre-built Chaquopy wheels (pydantic-core, etc.)
`-- releases/                    # Debug APK
```

The Android wrapper adds only three things on top of the desktop package:

- `opensquilla_android.py` — Chaquopy entry that redirects HOME,
  OPENSQUILLA_STATE_DIR and TMPDIR into the app's private files directory,
  then runs the gateway start server on 127.0.0.1:18790.
- `MainActivity.kt` — wraps a WebView with proper system-bar / display-cutout
  insets, intercepts file inputs via the system file picker, and routes
  downloads through DownloadManager.
- `gateway/templates/index.html` — adds a small mobile-only CSS block that
  constrains the composer popovers (model routing, run mode, attachment
  picker, "more" menu) to the viewport so they never get clipped on narrow
  screens. This is the only Android-specific front-end change.

## Build from source

Build the web UI first (Vite output lands in the dist directory), then build
the APK:

```bash
cd opensquilla-webui
npm ci
npm run build
cd ..

cd opensquilla-apk
./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk
```

For a release build with your own signing key:

```bash
keytool -genkeypair -v -keystore my-release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias opensquilla
# Wire it into app/build.gradle android.signingConfigs.release
./gradlew :app:assembleRelease
```

See [`local.properties.example`](local.properties.example) for the SDK/NDK
paths you need on your machine (real `local.properties` is git-ignored).

## State, logs, and configuration

All runtime state lives in the app's private files directory:

| Path (inside files/) | What it is |
| --- | --- |
| state/config.toml | Operator-edited configuration (providers, models, router, ...) |
| state/state.db, state/workspace/, state/agents/ | SQLite store + agent workspaces |
| state/logs/debug.log | Structlog JSON / text logs from the gateway |
| py_stderr.log | Python stderr + faulthandler thread dumps |
| chaquopy/AssetFinder/app/opensquilla/... | The extracted opensquilla package |

On API 30+ you also need MANAGE_EXTERNAL_STORAGE granted for the file
browser to see shared storage.

## First-run checklist

1. Install the APK and launch the app.
2. The gateway starts in the background. The web UI loads on 127.0.0.1:18790.
3. Copy `config.example.toml` to `state/config.toml` and fill in a provider.
4. Open Overview and run the built-in doctor report to confirm providers
   and the router are healthy.

## License

MIT, see [LICENSE](LICENSE).