# Quickstart

This guide gets OpenSquilla installed, configured, and running locally. It
assumes you want the standard product experience: terminal commands, local Web
UI, SquillaRouter, memory/search support, and safe local defaults.

## Requirements

- Python 3.12 or newer for terminal installs.
- `uv` for the recommended terminal install.
- Git, Git LFS, Node.js 22.12+, and npm only when installing or developing
  from source.
- A provider API key unless you use a local provider such as Ollama.

## Recommended Install

Install the current release wheel with the recommended extras:

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

The `recommended` extra includes SquillaRouter dependencies and memory/search
support used by the default product experience.

If `opensquilla` is not found after install, open a new shell or run:

```sh
uv tool update-shell
```

## First-Run Setup

Interactive setup:

```sh
opensquilla onboard
```

Script-friendly setup:

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Useful variants:

```sh
opensquilla onboard --if-needed
opensquilla onboard --minimal
opensquilla onboard --provider openai --api-key-env OPENAI_API_KEY
opensquilla onboard --provider ollama --model llama3.1
```

`--if-needed` is safe for install scripts because it avoids rewriting an
already-ready setup. `--minimal` configures the provider path and skips optional
channels/search/image-generation sections.

Check onboarding state:

```sh
opensquilla onboard status
```

## Run the Gateway

Foreground gateway:

```sh
opensquilla gateway run
```

Background gateway with readiness wait:

```sh
opensquilla gateway start --json
opensquilla gateway status
```

Default address:

```text
http://127.0.0.1:18791/control/
```

The gateway defaults to loopback for safety. To bind elsewhere, opt in:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Only expose a non-loopback gateway behind appropriate auth and network controls.

## First Useful Run

Open the Web UI:

```text
http://127.0.0.1:18791/control/
```

Start terminal chat:

```sh
opensquilla chat
```

Run one automation turn:

```sh
opensquilla agent -m "Inspect this workspace and suggest a test plan"
```

Run a one-shot task in a specific workspace:

```sh
opensquilla agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Review the current diff and list the highest-risk changes"
```

Use the Web UI for browser-based chat, approvals, setup, channels, usage, and
logs. Use `opensquilla chat` when you want a terminal conversation. Use
`opensquilla agent` for one-shot automation.

## Resume Work

Resume a terminal chat session:

```sh
opensquilla chat --session <session-key>
```

Inspect sessions:

```sh
opensquilla sessions list
opensquilla sessions show <session-key>
opensquilla sessions export <session-key>
```

Export a session when exact history matters for debugging or handoff.

## Check Readiness

Run these after setup:

```sh
opensquilla doctor
opensquilla providers list
opensquilla search list
opensquilla channels types --json
```

If the gateway is running, inspect runtime status:

```sh
opensquilla gateway status
opensquilla providers status
opensquilla channels status
opensquilla memory status
```

For provider/model selection details, see
[`providers-and-models.md`](providers-and-models.md). For search setup, see
[`search.md`](search.md).

For gateway lifecycle, host/port, and exposure guidance, see
[`gateway.md`](gateway.md).

## Stop or Restart

Foreground gateway:

```text
Ctrl+C
```

Managed background gateway:

```sh
opensquilla gateway stop
opensquilla gateway restart
```

## Next Steps

After the first run:

1. Configure search if you want web research:
   [`search.md`](search.md).
2. Enable channels if you want Slack, Telegram, Feishu/Lark, or another
   messaging surface: [`channels.md`](channels.md).
3. Review memory behavior if you want durable recall:
   [`features/memory.md`](features/memory.md).
4. Review tool permissions before unattended automation:
   [`tools-and-sandbox.md`](tools-and-sandbox.md).
5. Learn SquillaRouter if you want cost-aware model routing:
   [`features/squilla-router.md`](features/squilla-router.md).
6. Use the glossary if product terms are unfamiliar:
   [`glossary.md`](glossary.md).

## Install From Source

Use source install when you want a checkout-backed install:

```sh
git lfs install
git clone https://github.com/opensquilla/opensquilla.git
cd opensquilla
git lfs pull --include="src/opensquilla/squilla_router/models/**"
bash scripts/install_source.sh
```

The source installer runs `npm ci` and builds the Vue control console before
installing Python. Official release wheels and Desktop installers already
contain that console and do not require Node.js or npm.

For development, use the repository virtual environment:

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
uv run opensquilla gateway run
```

When developing from source, prefix commands with `uv run` so they use the
checkout you are editing. Rebuild after changing Web UI sources. Standard
wheel and sdist builds reject a missing or stale console; backend-only editable
`uv sync` remains available without it.

Direct `pip install .`, `uv tool install .`, and VCS URL installs are low-level
source-build paths. A local checkout must have a verified Web UI artifact first,
while a VCS URL checkout has none; use the source installer or an official
release wheel instead.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
