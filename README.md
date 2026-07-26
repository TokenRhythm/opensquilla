# OpenSquilla — Token-Efficient AI Agent

<p align="center">
  <img src="assets/opensquilla-long-logo.png" alt="OpenSquilla logo" width="500">
</p>

<p align="center">
  <b>Same budget, more capability, better results.</b><br>
  A microkernel AI agent for your CLI, Web UI, and chat channels.
</p>

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/opensquilla/opensquilla/ci.yml?style=for-the-badge" alt="CI"></a>
  <a href="https://opensquilla.ai/"><img src="https://img.shields.io/badge/website-opensquilla.ai-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://github.com/opensquilla/opensquilla/releases"><img src="https://img.shields.io/github/v/release/opensquilla/opensquilla?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge" alt="Apache 2.0 License"></a>
</p>

<p align="center">
  <b>English</b> · <a href="README.zh-Hans.md">中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.fr.md">Français</a> · <a href="README.de.md">Deutsch</a> · <a href="README.es.md">Español</a>
</p>

---

## News

- 📢 **2026-07-14** — Our technical report **[Agentic Routing: The Harness-Native Data Flywheel](https://arxiv.org/abs/2607.11399)** is now on arXiv. It shows how the harness-native router turns everyday agent traffic into a self-improving data flywheel, and how **multi-model ensemble routing surpasses Fable 5**.

---

## Overview

OpenSquilla is a token-efficient, microkernel AI agent. A local model
router sends each turn to the cheapest model that can handle it, while
persistent memory, a layered sandbox, built-in web search, and
on-device embeddings round out a single shared turn loop.

Every entry point — Web UI, CLI, and chat channels — runs through that
same loop, so tool dispatch, retries, and decision logging behave
identically everywhere. A pluggable provider layer speaks to
TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama, DeepSeek, Gemini,
Qwen/DashScope, and 20+ other LLM providers with no change to your code or config
schema.

OpenSquilla 0.5.0 is the current stable release.

For task-oriented product documentation, start with the
[OpenSquilla Product Guide](README.product.md) or the
[documentation index](docs/README.md).

---

## Installation

OpenSquilla runs on Windows, macOS, and Linux. Pick the path that
matches your use case.

Desktop installers and Quick terminal install give you a prebuilt **release** —
no Git required. The other two — Install from source and
Develop from source — build **from a Git checkout** (`git clone` + Git LFS),
including the Vue control console. Release wheels and Desktop installers already
contain that console, so their users do **not** need Node.js or npm.

Release install commands use published GitHub release assets. Python wheel installs use versioned wheel filenames because installers validate the version
embedded in the wheel filename.

For 0.5.0 desktop use, prefer the packaged desktop installers from
the GitHub Release: `OpenSquilla-0.5.0-mac-arm64.dmg` on macOS and
`OpenSquilla-0.5.0-win-x64.exe` on Windows.

| Path | Audience | When to use |
| --- | --- | --- |
| [Desktop installers](#desktop-installers) **(recommended desktop)** | macOS and Windows users | Packaged desktop app |
| [Quick terminal install](#quick-terminal-install) **(recommended)** | End users on any OS | Release wheel from a terminal |
| [Install from source](#install-from-source) | Users tracking `main` | Run from a checkout, not edit it |
| [Develop from source](#develop-from-source) | Contributors | Edit, test, or debug the source |

### Prerequisites

| Requirement | Quick terminal install | Install from source | Develop from source |
| --- | :---: | :---: | :---: |
| Python 3.12+ | via `uv` | via `uv` or system | via `uv` |
| Git + Git LFS | — | required | required |
| Node.js 22.12+ + npm | — | required to build the Web UI | required for Web UI and wheels |
| `uv` | installed if missing | recommended | required |

The default `recommended` profile installs **SquillaRouter** —
OpenSquilla's on-device model router — and its model assets;
`OPENSQUILLA_INSTALL_PROFILE=core` omits those dependencies. The
separate `--router disabled` onboarding flag keeps the dependencies
installed but turns the router off at runtime.

On Windows, SquillaRouter's bundled ONNX runtime also needs the Visual
C++ runtime. The from-source PowerShell installer installs it automatically via
`winget`; the **Quick terminal install** (`uv tool install`) path does not — if
startup logs a `DLL load failed` error, install it manually (see
[Troubleshooting](#troubleshooting)). OpenSquilla keeps running with direct
single-model routing until it is installed.

On macOS terminal installs, SquillaRouter's LightGBM runtime may also
need the system OpenMP library. The desktop app bundles the
runtime it needs, but **Quick terminal install** does not install
Homebrew/system libraries. If startup logs `Library not loaded:
@rpath/libomp.dylib`, run `brew install libomp`, then restart the
gateway. OpenSquilla keeps running with direct single-model routing
until it is installed.

Install links: [Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[Node.js](https://nodejs.org/en/download) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/).

### Desktop installers

The 0.5.0 desktop installers package the Vue control console and
gateway runtime in an Electron shell.

- macOS Apple Silicon: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg>
- Windows x64: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe>

For faster Mainland China downloads, use the OSS direct-download aliases:
- macOS Apple Silicon: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg>
- Windows x64: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe>

These fixed links advance only after a newer eligible release passes mirror
verification. Use the versioned GitHub Release links above when you need a
specific release.

Quit any running OpenSquilla desktop app before upgrading. On macOS, drag the
app from the DMG into Applications for installation or updates, eject the DMG,
then open the Applications copy. The existing Desktop profile in the platform
application-data directory is reused. A terminal installation's
`~/.opensquilla` is a separate profile; transfer it explicitly from Settings
only if needed.

When upgrading the Windows Desktop from RC3 to RC4 or later, run the new
installer directly over the existing installation. Do **not** uninstall RC3
first: its uninstaller may remove Desktop user data. Back up
`%APPDATA%\OpenSquilla` before upgrading. RC4 and later installers preserve
profile data during a normal uninstall.

Code signing policy: [`docs/code-signing-policy.md`](docs/code-signing-policy.md).

> [!NOTE]
> Windows builds are currently unsigned. If SmartScreen appears, choose
> **More info** → **Run anyway**. If Smart App Control or enterprise policy
> blocks the unsigned app, use [Quick terminal install](#quick-terminal-install)
> instead.

### Quick terminal install

The recommended path on Windows, macOS, and Linux. `uv` installs
OpenSquilla into its own isolated environment and manages its own
Python — no system Python required. This path installs published
releases only; for `main`, development branches, or local checkouts
use [Install from source](#install-from-source).

**1. Install `uv`** — skip if `uv --version` already works.

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. Install OpenSquilla** — the same command on every platform.

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

This installs the OpenSquilla wheel from the release URL, then lets
`uv` download the dependencies declared by the selected extras. The
default `recommended` extra includes SquillaRouter runtime dependencies
such as ONNX Runtime, LightGBM, NumPy, and tokenizers, so a first install
needs network access unless those wheels are already cached. `uv` does
not install system native runtimes such as macOS `libomp` or the Windows
Visual C++ Redistributable; see [Troubleshooting](#troubleshooting) if
the router runtime reports a native-library load error.

**3. Configure and run.**

```sh
opensquilla onboard
opensquilla gateway run
```

> [!NOTE]
> If `opensquilla` is not found right after a fresh `uv` install, open
> a new terminal, or re-run the PATH line from step 1.

For a fully pinned install, use the versioned wheel URL:
`https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`.

### Install from source

Use this path to run OpenSquilla from a checkout without editing it.
The clone is only the package source for the installer; after install,
use the `opensquilla` command — do not run `uv run`. Choose
[Develop from source](#develop-from-source) instead if you intend to
modify the code.

1. **Clone with LFS assets**

   ```sh
   git lfs install
   git clone https://github.com/opensquilla/opensquilla.git
   cd opensquilla
   git lfs pull --include="src/opensquilla/squilla_router/models/**"
   ```

2. **Run the installer**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   The script installs `.[recommended]` (SquillaRouter + memory + local
   models) into a dedicated user environment via `uv tool install`. Before the
   Python install, it runs `npm ci` and `npm run build` in
   `opensquilla-webui`. Every source reinstall recreates the locked
   `node_modules` tree and rebuilds the console; the first run normally has the
   largest dependency download, while a warm npm cache reduces later network
   use but not all build time or disk writes. It then installs the built console
   with the Python package,
   falling back to `python -m pip install --user` when `uv` is
   unavailable. If `opensquilla` is not on `PATH` after install (common
   on a fresh host where `~/.local/bin` is not yet on `PATH`), run
   `uv tool update-shell` and open a new terminal; see
   [Troubleshooting](#troubleshooting) for details.

   Direct `pip install .`, `uv tool install .`, and VCS URL installs are
   low-level source-build paths, not substitutes for this installer. A local
   checkout works only after its Web UI has been built; a VCS URL checkout has
   no generated artifact and is intentionally rejected. Use this source
   installer or an official release wheel instead.

3. **(optional) Install advanced extras.** Most channels — Feishu,
   Telegram, DingTalk, QQ, WeCom, Slack, and Discord — work from the
   base install. The opt-in extras are:

   - `matrix` — Matrix channel (pulls in `matrix-nio`)
   - `matrix-e2e` — Matrix channel with end-to-end encryption (requires
     libolm)
   - `document-extras` — PDF generation via WeasyPrint

   ```sh
   OPENSQUILLA_INSTALL_EXTRAS=matrix bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras matrix   # Windows
   ```

4. **Configure and run** — see [Configuration](#configuration).

<details>
<summary>Install from source — terminal prerequisites and installer options</summary>

**Install prerequisites (Git, Git LFS, Node.js 22.12+ with npm, uv) from a terminal**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS (Homebrew):

```sh
brew install git git-lfs node uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

On Fedora use `sudo dnf install -y git git-lfs`; on Arch use
`sudo pacman -S --needed git git-lfs`; install Node.js 22.12+ and npm from
your distribution or nodejs.org, then install `uv` with the `curl` command
above. PATH changes from these installers apply to new
terminal sessions.

**Installer environment variables and PATH checks**

```sh
OPENSQUILLA_INSTALL_PROFILE=core   bash scripts/install_source.sh   # minimal runtime, no SquillaRouter
OPENSQUILLA_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # print the plan only
```

Verify which `opensquilla` your shell runs with `command -v
opensquilla` (macOS/Linux) or `where.exe opensquilla` (Windows). If it
is not on `PATH`, run `uv tool update-shell`. After reinstalling from a
local checkout, restart the gateway so it loads the updated package.

</details>

### Develop from source

Use this path when you are working on OpenSquilla's source code:
making changes, running tests, or debugging behavior against this
checkout. It is not the normal install path. Unlike
[Install from source](#install-from-source), this path requires `uv`:
`uv sync` creates a repository-local `.venv`, and `uv run` executes
commands against the files in this checkout.

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
```

Run `npm run build` again after changing Web UI sources. Standard wheel builds
fail closed when the generated console is missing or stale; editable `uv sync`
installs remain available for backend-only work.

The `recommended` extra includes SquillaRouter for development too;
the `dev` extra installs the test, lint, and typecheck tools. Install
additional extras into the same environment you run:

```sh
uv sync --extra recommended --extra dev --extra matrix
uv run opensquilla channels status matrix --json
```

In this mode, prefix every `opensquilla` command in
[Configuration](#configuration) with `uv run`. Do not debug a
development checkout through a user-local `opensquilla` command — that
command runs in a different Python environment.

### Uninstall

Remove OpenSquilla with `opensquilla uninstall`. It keeps your data by default
and removes only the program:

```sh
opensquilla uninstall --dry-run   # preview what would be removed and kept
opensquilla uninstall             # remove the program, keep your data
```

To delete data too, opt in explicitly:

```sh
opensquilla uninstall --purge-state    # sessions, logs, cache, scheduler, memory
opensquilla uninstall --purge-config   # config.toml and secrets (.env)
opensquilla uninstall --purge-all      # everything (asks you to type a confirmation)
```

The running gateway is drained and stopped first, deletion stays inside the
OpenSquilla home, and Docker/desktop installs get guided removal steps instead.
Desktop or OS app removal remains platform-specific; the CLI guidance does not
remove a desktop app bundle. See [`docs/cli.md`](docs/cli.md#uninstall) for the
full reference.

---

## Installation Privacy

OpenSquilla uses anonymous installation telemetry to estimate install counts,
version adoption, and runtime compatibility. Data is sent on first gateway
startup and once per OpenSquilla version. It also records content-free daily
aggregates of completed top-level conversations and token usage by UTC date,
and attempts to upload pending cumulative UTC-day snapshots to the telemetry
service at startup and once per hour. OpenSquilla may also make
passive update checks, including automatic desktop update checks at startup
and, while the app remains open, at most once per day. Uploads use a short
timeout and never block startup.

See [`PRIVACY.md`](PRIVACY.md) for the full privacy policy covering local data,
provider requests, network observability, logs, release downloads, and deletion.

What is sent:

- schema version
- locally generated stable `install_id` digest
- OpenSquilla version
- event type (`install`, `version_seen`, or `daily_usage`)
- install method (`pip`, `source`, `docker`, `desktop`, or `unknown`)
- operating system, OS version, CPU architecture, and Python major/minor
  version
- first-seen and sent timestamps
- CI/test-environment marker (`ci_environment`)
- completed UTC day, conversation count, and aggregate input/output/cache/cache-write
  token counts for daily-usage events

The `install_id` is a local one-way SHA-256 digest derived from usable MAC
addresses, then local IP addresses when no MAC is available, with a random
persisted fallback. Raw MAC/IP values are not uploaded.

What is not sent: usernames, hostnames, paths, API keys, provider config,
chat/session/memory/agent content, file names, or file contents. Source IP may
be visible to HTTP servers at the transport layer, but is not part of the
payload.

To disable non-user-initiated network observability before startup:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

or set:

```toml
[privacy]
disable_network_observability = true
```

That unified switch covers automatic install telemetry, daily aggregate usage
telemetry, passive update checks,
and automatic desktop update checks at startup and during long-running app
sessions. Explicit update-availability checks remain disabled while the unified
or legacy opt-out is active. Other user-initiated actions may still contact
network services after user intent, including release downloads and configured
providers, search, or channels.

Legacy opt-out environment variables remain honored:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

Advanced deployments can use their own installation telemetry endpoint:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
```

---

## Configuration

### First-run setup

`opensquilla onboard` is the interactive first-run wizard. It writes
the active config file and keeps provider secrets in environment
variables when you pass `--api-key-env`. The router defaults to
`recommended` (SquillaRouter on supported providers); pass
`--router disabled` for direct single-model routing.

```sh
opensquilla onboard                # full interactive wizard
opensquilla onboard --if-needed    # idempotent: safe for scripts and re-installs
opensquilla onboard --minimal      # provider only; skip channels and search
opensquilla onboard status         # inspect every setup section without writing
```

In SSH, CI, or any environment without a TTY, use the non-interactive
form — keep the secret in the environment and pass its **name**, not
its value:

**Linux / macOS**

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

**Windows PowerShell**

```powershell
$env:OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

OpenRouter is only an example — substitute any supported provider and
its API-key variable.

Re-configure one section later without redoing the whole wizard (these
examples assume the relevant API key is already in the environment):

```sh
opensquilla configure provider --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY
opensquilla configure router --router recommended
opensquilla configure search   --search-provider duckduckgo
opensquilla configure search   --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure channels
```

Sections: `provider`, `router`, `channels`, `search`,
`image-generation`, `memory-embedding`. The Web UI exposes the same
catalog and status model at `/control/setup`: Provider and Router are
the fast path, while Channels, Search, Image generation, and Memory
embedding sit in the Capability Center and can be configured later.
Empty channels are treated as an opt-out, not a failed setup.

**Config load order:** `OPENSQUILLA_GATEWAY_CONFIG_PATH` →
`./opensquilla.toml` → `~/.opensquilla/config.toml` → built-in
defaults. Environment values for individual secrets always win over
file values.

### Migrate from OpenClaw or Hermes Agent

If you already have state under `~/.openclaw` or `~/.hermes`, run a
dry run first to inspect the migration report, then apply it explicitly:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply

opensquilla migrate hermes --json
opensquilla migrate hermes --apply
```

Use `opensquilla migrate --source openclaw,hermes --apply` to import
both default homes. Add `--migrate-secrets` only after reviewing the dry-run
report. See [`MIGRATION.md`](MIGRATION.md) for custom paths and conflict
handling.

### Run

```sh
opensquilla gateway run                # foreground, 127.0.0.1:18791
opensquilla gateway start --json       # background + health wait
opensquilla chat                       # interactive REPL
opensquilla agent -m "your prompt"     # one-shot, automation-friendly
```

> **Development-only OpenTUI terminal UI.** Release installs continue to use
> the Python-native chat. The richer full-screen frontend currently runs only
> from a [Develop from source](#develop-from-source) checkout; no companion host
> is published in release assets or installed by the release installer. From
> the checkout, install the pinned Bun dependencies once, then launch against
> that same source tree:
>
> ```sh
> bun install --frozen-lockfile --cwd=src/opensquilla/cli/tui/opentui/package
> OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 uv run opensquilla chat --ui tui
> ```
>
> Use `opensquilla chat --ui plain` to require the stable renderer. See
> [docs/tui.md](docs/tui.md) for terminal chat usage and
> [docs/features/tui-frontend.md](docs/features/tui-frontend.md) for backend
> details.

Open the Web UI at <http://127.0.0.1:18791/control/>. The **Health**
view shows whether OpenSquilla is ready, what is not ready, and the
next recovery steps. From the CLI, run:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla doctor --config ./opensquilla.toml --json
```

`/health` and `/healthz` are lightweight liveness endpoints for process
checks. `opensquilla doctor` and the Web UI Health view are the readiness
surfaces for provider config, memory, logs, search, channels, sandbox
posture, router, image generation, and recovery guidance. Press
`Ctrl+C` to stop a foreground gateway.

Other command groups include `sessions`, `skills`, `memory`, `migrate`,
`cron`, `channels`, `providers`, `models`, and `cost`. Run
`opensquilla --help` or `opensquilla <group> --help` for details.

<details>
<summary>Advanced configuration — verify a channel, public network binding, Docker</summary>

**Connect and verify a messaging channel**

Channel saves are config changes, not runtime-connectivity proof.
Restart the gateway after channel edits, then verify the live channel:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

Treat a channel as connected only when the status payload reports
`enabled=true`, `configured=true`, and `connected=true`. Feishu
defaults to websocket mode, Telegram to polling, and Slack can use
Socket Mode — none of those modes needs a public URL. Feishu webhook
mode, Telegram webhook mode, Slack webhook mode, and WeCom require a
public, provider-reachable URL.

**Public network binding**

To reach the Web UI from another machine, bind the gateway to all
interfaces and use the host's public IP:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Public access also requires the host firewall or cloud security group
to allow inbound TCP on that port. Do not expose the gateway with
`[auth] mode = "none"` — configure token auth before binding to
`0.0.0.0`.

**Docker**

Prebuilt multi-arch images (`amd64`/`arm64`) are published to
`ghcr.io/opensquilla/opensquilla` on release tags. 0.5.0 is published as
both `v0.5.0` and the moving `latest` tag —
[`docs/docker.md`](docs/docker.md) is the full container guide
(home servers and NAS, LAN exposure with token auth, upgrades):

```sh
OPENSQUILLA_GATEWAY_IMAGE=ghcr.io/opensquilla/opensquilla:latest docker compose up -d
```

Without `OPENSQUILLA_GATEWAY_IMAGE`, the compose path runs an
`opensquilla:local` image you build yourself. Build it from a source
checkout with the Git LFS router assets pulled
(see [Install from source](#install-from-source) for the clone and
`git lfs pull`):

```sh
docker build -t opensquilla:local .
```

`./start.sh` (or `start.ps1` on Windows) then runs `docker compose
up -d` and tails the gateway logs. Docker avoids a host Python
toolchain — not the local image build.

</details>

Provider tiers, sandbox tuning, image generation, and concurrency
settings live in `opensquilla.toml.example`.

---

## What's New in 0.5.0

OpenSquilla 0.5.0 is the first stable release of the 0.5 line, collecting
Previews 1-4 and the fixes since:

- **Model Ensemble and multi-provider routing** - one turn can run across
  several models with preset or custom lineups, provider management keeps
  verified provider state across restarts, and on-device routing keeps
  classification local.
- **Safe upgrades and profile protection** - guarded migration previews,
  profile recovery, and Windows profile-data preservation on uninstall.
- **Desktop maturity** - signed and notarized macOS builds with in-app
  updates, gateway boot recovery, and fail-closed restarts.
- **Usage and cost reporting** - daily usage summaries on a durable ledger
  with exact billing arithmetic.
- **Download options** - versioned GitHub assets, multi-architecture GHCR
  images, and an Alibaba Cloud OSS mirror with stable download aliases.
  Windows Portable archives remain retired.

Full notes: [`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/releases/0.5.0.md`](docs/releases/0.5.0.md).

## What's New in 0.2.1

OpenSquilla 0.2.1 is a maintenance release focused on release-package
startup and long-running agent reliability:

- **Windows portable startup** — the portable launcher better detects and
  bootstraps the Visual C++ runtime needed by the bundled ONNX router.
- **Long-running agent turns** — tool-heavy WebUI sessions recover more
  cleanly from oversized tool results, malformed tool calls, artifact
  delivery handoffs, and degraded final responses.
- **Cleaner WebUI output** — generated artifact markers are kept out of
  normal chat replay while delivered files remain visible.
- **Memory recall scoring** — local and OpenAI-compatible embedding vectors
  are normalized before semantic search, and strong keyword matches remain
  usable when vector scores are low.

Full notes: [`CHANGELOG.md`](CHANGELOG.md) ·
[release notes](https://opensquilla.ai/news/).

## What's New in 0.2.0

This release expands OpenSquilla across migration, CLI chat, channels,
scheduling, and long-running tool work:

- **Migration path from existing agent homes** — `opensquilla migrate` previews
  and applies imports from existing OpenClaw/Hermes homes, including memory,
  persona files, skills, MCP/channel config, conflict handling, and migration
  reports.
- **Usable chat CLI** — `opensquilla chat` has a stable terminal UI, streaming
  output, queued input, slash-mode discovery, tool/status strips, and more
  deterministic live prompt behavior.
- **Cross-surface cron automation** — cron jobs now cover structured schedules,
  timezone-aware exact/every/cron runs, channel or webhook delivery, failure
  destinations, manual runs, and WebUI/CLI/RPC parity.
- **Better Feishu and Discord channels** — channel adapters expose clearer
  capability metadata, safer DM/group handling, native file and artifact paths,
  and improved attachment/thread behavior while privileged actions stay scoped.
- **Sturdier long-running turns** — failed turns are kept out of provider
  replay, malformed tool calls are handled more safely, and approval-gated
  retries wait for operator decisions.
- **Smarter context and tool budgeting** — provider-budget compaction, prompt
  cache preservation, bounded tool results, and side-effect-aware concurrency
  make large tool-heavy sessions more predictable.
- **Web UI and release polish** — recency ordering, table layout, mobile
  controls, duplicate notifications, setup forms, release URLs, and install
  paths are tightened for 0.2.0.

Full notes: [`CHANGELOG.md`](CHANGELOG.md) ·
[release notes](https://opensquilla.ai/news/).

---

## Key Features

| Capability | What it does |
| --- | --- |
| **Token-efficient routing** | `SquillaRouter` — a local LightGBM + ONNX classifier in the `recommended` extra — scores each turn on length, language, code, keywords, and semantic embeddings, then routes it across four tiers (C0–C3; legacy T0–T3 names are aliases) to the cheapest capable model. Classification runs on-device; your prompt never leaves the machine to make that decision. |
| **Adaptive reasoning and prompts** | OpenSquilla requests extended reasoning only for turns the router scores as complex, and the system prompt scales with task complexity — lightweight for trivial turns, full instructions for complex ones. |
| **20+ LLM providers** | The provider registry targets 20+ LLM backends — TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama, DeepSeek, Gemini, DashScope/Qwen, Moonshot, Mistral, Groq, Zhipu, SiliconFlow, vLLM, LM Studio, and more, with primary-plus-fallback selection; first-run onboarding exposes the verified subset. |
| **On-demand skills and MCP** | 15 bundled skills (coding, GitHub, cron, pptx/docx/xlsx/pdf, summarization, tmux, weather, and more) load only when the task needs them. OpenSquilla is an MCP client, and can also run as an MCP server — `opensquilla mcp-server run` needs the `mcp` extra (install `opensquilla[recommended,mcp]`). Skills can be authored, installed, and published from the CLI. |
| **Persistent local memory** | A curated `MEMORY.md` plus dated Markdown notes, searched with SQLite full-text keyword search and `sqlite-vec` semantic recall. Embeddings run on-device via bundled ONNX, or swap to OpenAI/Ollama. Optional exponential decay and opt-in "dream" consolidation are available. |
| **Layered security sandbox** | Three policy tiers (Standard / Strict / Locked) on a permission matrix. Bubblewrap isolates code execution on Linux; macOS runs commands through Seatbelt (`sandbox-exec`) with generated SBPL profiles; Windows uses the native `windows_default` backend after setup readiness checks. A denial ledger auto-pauses autonomous runs after repeated denials, rejected outputs are purged, and skill metadata and tool results are XML-escaped against prompt injection. |
| **Built-in tools** | File read/write/edit, shell and background processes, git, web search (DuckDuckGo, Bocha, Brave, IQS, Tavily, or Exa) and fetch behind an SSRF guard, spreadsheet/PPTX/PDF authoring, image generation, and text-to-speech. |
| **Unified gateway** | A Starlette ASGI server on `127.0.0.1:18791` with WebSocket RPC and an embedded control console (`/control/`). Web UI, CLI, and channels for Terminal, WebSocket, Slack, Telegram, Discord, Feishu, DingTalk, WeCom, Matrix, and QQ all share one `TurnRunner`. |
| **Durable sessions, subagents, and scheduling** | SQLite-backed session, transcript, and replay storage with per-agent workspaces. Agents spawn depth-bounded subagents, and a `SchedulerEngine` with an in-tree cron parser runs recurring jobs via `opensquilla cron`. |
| **Operator controls** | Human-in-the-loop approvals can pause sensitive tool calls for a decision; per-turn and per-session token and cost rollups (`opensquilla cost`) and diagnostics are available from the CLI and Web UI. |

MetaSkill docs: [`docs/features/meta-skills.md`](docs/features/meta-skills.md),
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md),
and [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md).

---

## Benchmark Results

PinchBench 1.2.1 average results across 25 tasks:

| Agent | Base Model | Avg. score | Total input tokens | Total output tokens | Total cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenSquilla | Model router (Opus4.7, GLM5.1, DS4 Flash) | 0.9251 | 1,721,328 | 61,475 | $0.688 |
| OpenClaw | Claude Opus 4.7 | 0.9255 | 3,066,243 | 50,890 | $6.233 |

Score is the mean across the 25 tasks; token counts and cost are
totals for the full run.

---

## Troubleshooting

<details>
<summary>macOS desktop app keeps bouncing or reports AppTranslocation</summary>

If macOS starts OpenSquilla from a temporary AppTranslocation path, quit
OpenSquilla, drag the app into Applications if you are installing it, eject the
DMG, then open OpenSquilla again. If an old OpenSquilla icon is still bouncing,
force quit the old process first and reopen OpenSquilla.

</details>

<details>
<summary>macOS: <code>Library not loaded: @rpath/libomp.dylib</code></summary>

If startup logs `Library not loaded: @rpath/libomp.dylib` from
`lightgbm/lib/lib_lightgbm.dylib`, OpenSquilla keeps running with
direct single-model routing, but the bundled `SquillaRouter` runtime
stays inactive until the macOS OpenMP runtime is installed.

The desktop app bundles the native runtime it needs. If you used
Quick terminal install or source install from a shell, install `libomp`
with Homebrew and restart the gateway:

```sh
brew install libomp
opensquilla gateway restart
```

</details>

<details>
<summary>Windows: <code>DLL load failed</code> / Visual C++ runtime</summary>

If startup logs `DLL load failed while importing
onnxruntime_pybind11_state`, OpenSquilla keeps running with direct
single-model routing, but the bundled `SquillaRouter` runtime stays
inactive until the Visual C++ Redistributable for Visual Studio
2015–2022 (x64) is installed.

The from-source PowerShell installer attempts to install the redistributable via
`winget`. If you used Quick terminal install, or `winget` is unavailable,
install it manually and restart PowerShell:
<https://aka.ms/vs/17/release/vc_redist.x64.exe>. Then restore the recommended
router:

```powershell
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY --router recommended
opensquilla gateway restart
```

</details>

---

## Credits

OpenSquilla is inspired by
[OpenClaw](https://github.com/openclaw/openclaw). Bundled third-party
content is attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Community contributors are acknowledged in
[`CONTRIBUTORS.md`](CONTRIBUTORS.md), including release-specific attribution
notes for squash-merged or replayed work.

---

## Contributors

Thanks to all the people who contribute to OpenSquilla.

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=opensquilla/opensquilla&max=100&columns=10" alt="OpenSquilla contributors" />
  </a>
</p>

---

## Contributing

Contributions of every kind are welcome — bug reports, feature ideas,
documentation, new provider or channel adapters, skills, and core
runtime work. See [`CONTRIBUTING.md`](CONTRIBUTING.md), then open an
issue or pull request on
[GitHub](https://github.com/opensquilla/opensquilla).

[Code of Conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md) ·
[Privacy](PRIVACY.md) · [Code signing policy](docs/code-signing-policy.md) ·
[Third-party notices](THIRD_PARTY_NOTICES.md) · [Support](SUPPORT.md) ·
[License](LICENSE) (Apache-2.0)
