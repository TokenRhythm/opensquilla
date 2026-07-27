# Troubleshooting

Start with:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla gateway status
```

The Web UI health view at <http://127.0.0.1:18791/control/> also reports
readiness and recovery steps when the gateway is running.

## `opensquilla` Command Not Found

After `uv tool install`, open a new terminal or run:

```sh
uv tool update-shell
```

Check the executable:

```sh
command -v opensquilla
```

On Windows PowerShell:

```powershell
where.exe opensquilla
```

## Gateway Is Not Running

Start it:

```sh
opensquilla gateway run
```

Or use the managed background process:

```sh
opensquilla gateway start --json
opensquilla gateway status
```

Open:

```text
http://127.0.0.1:18791/control/
```

For a focused gateway guide, see [`gateway.md`](gateway.md).

## Control UI Assets Are Unavailable

This diagnostic means a source checkout is missing the generated Vue artifact,
or the artifact no longer matches the current Web UI sources. Official release
wheels, Desktop installers, and container images already include a verified
artifact and do not require Node.js or npm on the user's machine.

From a repository checkout, rebuild the artifact before starting the gateway or
building a wheel:

```sh
cd opensquilla-webui
npm ci
npm run build
```

Direct VCS URL installs such as `pip install git+https://...` cannot do this
because generated assets are intentionally not tracked. Install an official
release wheel instead, or clone the repository and run the source installer as
described in the [source installation guide](quickstart.md#install-from-source).

## Desktop Gateway Startup Reports a Migration Lock

During first run, the desktop app starts a local gateway and applies pending
SQLite migrations before opening the Control UI. If startup is interrupted, the
gateway may report a yoyo migration lock for `sessions.db`.

Recent versions recover automatically when the lock row points only to dead or
invalid process ids. The gateway keeps the migration failure loud and does not
clear the lock when any recorded pid is still alive.

Check the desktop gateway log for these events:

```text
migrator.lock_timeout
migrator.stale_lock_cleared
migrator.lock_held_by_live_process
migrator.stale_lock_retry_failed
```

If the log says the lock is held by a live process, wait for that gateway to
finish starting or stop the process cleanly. Do not remove `yoyo_lock` rows or
run yoyo break-lock unless you have verified the recorded process is no longer
running.

## Collecting Diagnostics for a Bug Report

One action collects everything a maintainer needs:

- **CLI:** `opensquilla bundle` — works even when the gateway will not start.
- **Web UI:** Logs page → **Diagnostic bundle** button.
- **Desktop app:** application menu → **Download Diagnostics…** (if the app
  cannot reach its gateway, this opens the logs folder instead).

The bundle is a single zip containing gateway logs, recent error records,
router decision and trace slices, an offline health report, and your
configuration with all secrets redacted. Local paths are normalized to `~` and
conversation content is **excluded** unless you explicitly opt in
(`--include-content` or the dialog checkbox). Attach the zip to your GitHub
issue.

When a turn fails, the error message ends with a reference code like
`(ref: a1b2c3d4)`. Quote that code in your report — it joins your description
directly to the recorded error inside the bundle.

### Where logs live

- CLI/gateway installs: `~/.opensquilla/logs/` (`debug.log` is the rotating
  gateway log; `gateway.log` captures daemonized stdout).
- Desktop app (macOS): `~/Library/Application Support/OpenSquilla/logs/`
  (packaged builds) or `~/Library/Application Support/@opensquilla/desktop-electron/logs/`
  (development builds) — `desktop.log` is the app lifecycle log and
  `gateway.log` the embedded gateway's output. The gateway's own state lives
  under `opensquilla/state/` next to them.

## Port Already In Use

Use another port:

```sh
opensquilla gateway run --port 18792
```

Or stop the managed gateway:

```sh
opensquilla gateway stop
```

## Provider Not Configured

Run:

```sh
opensquilla onboard
opensquilla providers list
opensquilla providers configure openrouter
```

Use environment-variable secrets:

```sh
export OPENAI_API_KEY="sk-..."
opensquilla configure provider --provider openai --api-key-env OPENAI_API_KEY
```

## Router Dependency Problems

If SquillaRouter cannot load, OpenSquilla can still run with direct model
routing. To disable the router:

```sh
opensquilla configure router --router disabled
opensquilla gateway restart
```

On Windows, ONNX Runtime may need the Visual C++ Redistributable for Visual
Studio 2015-2022 x64. Install it, then restart the shell and gateway.

On macOS terminal installs, LightGBM may need the system OpenMP runtime. If
startup logs `Library not loaded: @rpath/libomp.dylib` from
`lightgbm/lib/lib_lightgbm.dylib`, install it and restart the gateway:

```sh
brew install libomp
opensquilla gateway restart
```

The desktop app bundles the native runtime it needs; this recovery step
is for terminal or source installs.

## Search Does Not Work

Inspect search providers:

```sh
opensquilla search list
opensquilla search status
```

Use DuckDuckGo for a no-key path:

```sh
opensquilla configure search --search-provider duckduckgo
```

Use Brave with a key:

```sh
export BRAVE_SEARCH_API_KEY="..."
opensquilla configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
```

Use Bocha, IQS, Tavily, or Exa when your workflow needs freshness or richer
source content:

```sh
export BOCHA_SEARCH_API_KEY="..."
opensquilla configure search --search-provider bocha --api-key-env BOCHA_SEARCH_API_KEY

export IQS_SEARCH_API_KEY="..."
opensquilla configure search --search-provider iqs --api-key-env IQS_SEARCH_API_KEY

export TAVILY_API_KEY="..."
opensquilla configure search --search-provider tavily --api-key-env TAVILY_API_KEY

export EXA_API_KEY="..."
opensquilla configure search --search-provider exa --api-key-env EXA_API_KEY
```

For no-key, partial-key, or all-key setups, inspect the effective runtime state:

```sh
opensquilla search status --json
```

## Channel Config Saved but Channel Is Offline

Restart the gateway after editing channel config:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

For webhook channels, confirm the gateway is reachable from the provider and
that callback secrets match.

## A Tool Was Denied

Check sandbox and permission state:

```sh
opensquilla sandbox status
opensquilla doctor
```

For one-shot runs, choose an explicit permission posture:

```sh
opensquilla agent --permissions restricted -m "Read only"
opensquilla agent --permissions full -m "Trusted local automation"
```

## The Agent Seems to Forget Old Context

Long sessions may compact old history. This is expected under context pressure.

Inspect sessions:

```sh
opensquilla sessions show <session-key>
opensquilla sessions export <session-key>
```

If exact old text matters, keep it in a file, memory note, or exported session.

## A Turn Is Too Expensive or Too Slow

Try:

```sh
opensquilla configure router --router recommended
opensquilla diagnostics on
opensquilla cost
```

For automation:

```sh
opensquilla agent --max-iterations 20 --timeout 600 -m "Bounded task"
```

For large tool outputs, see
[`features/tool-compression.md`](features/tool-compression.md).

## Docker: Web UI Is Unreachable from Another Machine

The default compose port publish is loopback-only
(`127.0.0.1:18791:18791`), so other devices cannot reach the gateway.
Publish on all interfaces instead — and configure token auth first:

```yaml
ports:
  - "18791:18791"
```

Keep `OPENSQUILLA_LISTEN` at `0.0.0.0`; exposure is controlled by the
`ports` mapping, not by the bind address. If the host runs a firewall,
allow inbound TCP 18791 from your LAN. Full flow:
[`docker.md`](docker.md).

## Docker: Web UI Connects but Configuration Changes Are Rejected

A containerized gateway binds a wildcard address, so every browser —
including one on the same host — is treated as a remote operator.
Remote operators without a token can chat but cannot administer
configuration or onboarding. Enable token auth:

```yaml
environment:
  OPENSQUILLA_AUTH_MODE: token
  OPENSQUILLA_AUTH_TOKEN: ${OPENSQUILLA_AUTH_TOKEN:?generate one with openssl rand -hex 32}
```

Put the token value in a git-ignored `.env` next to `compose.yaml`, then log
in with the token in the URL:

```text
http://<server-address>:18791/control/?token=<value>
```

Use `token` mode specifically — `password` and `trusted-proxy` modes do
not support the Web UI connection. If the variables have no effect, the
state volume's `config.toml` may already contain an `[auth]` table —
TOML values take precedence over `OPENSQUILLA_AUTH_*` at boot; edit the
token there (or in the Web UI) and restart.

## Docker: Gateway Fails at Boot on a Bind-Mounted State Directory

The container runs as non-root UID 10001. A bind mount owned by another
user is unwritable, and the gateway fails while creating its databases.
Give the directory to the container user and restart:

```sh
sudo chown -R 10001:10001 /srv/opensquilla
docker compose up -d
```

The named-volume default (`opensquilla-state`) does not have this
problem — the image pre-creates the state root with the right owner.

## Docker: Build Fails with "model assets are unavailable"

`docker build` validates the bundled router models and refuses to bake
Git LFS pointer files into the image. Hydrate them before building
(`git-lfs` is a separate package from `git` on Debian):

```sh
sudo apt install -y git git-lfs
git lfs pull --include="src/opensquilla/squilla_router/models/**"
docker build -t opensquilla:local .
```

Prebuilt images avoid this entirely — see [`docker.md`](docker.md).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
