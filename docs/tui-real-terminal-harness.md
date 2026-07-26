# Real Terminal TUI Harness

The real-terminal harness launches the OpenTUI chat surface in a child process,
drives it through tmux when available, falls back to PTY when needed, and stores
evidence under `.artifacts/tui-real-terminal/runs`.

## Platform requirements

The harness runs on Unix-like terminal environments. Linux and macOS can run it
directly. Windows users need WSL2 because the harness depends on Unix terminal
primitives. It prefers tmux when available and falls back to a Unix
pseudo-terminal (Python's `pty` module) when tmux is missing.

- Linux and macOS can run the deterministic suite with either tmux or the PTY
  fallback.
- Native Windows shells such as PowerShell and `cmd.exe` are not supported:
  Python's `pty` module is Unix-only, and tmux has no native Windows build.
- WSL2 is mentioned only as the Windows compatibility path; inside WSL2 this is
  just the Linux path.

Install tmux when you want the tmux driver:

```bash
# Debian/Ubuntu Linux, including WSL2:
sudo apt-get update && sudo apt-get install -y tmux

# macOS:
brew install tmux
```

Windows-only setup:

```bash
# In an elevated PowerShell, once:
wsl --install            # installs WSL2 + a default Ubuntu distro

# Then inside the WSL2 shell:
sudo apt-get update && sudo apt-get install -y tmux
uv run pytest tests/integration/cli/tui_real_terminal -q
```

When neither tmux nor PTY is available (e.g. native-Windows CI),
`probe_terminal_capabilities()` reports `preferred_driver="none"` and every
scenario test is skipped with a `pytest.skip` reason that names the missing
capability — the run does not fail. The pure-logic driver, capability-probe, and
scenario-model unit tests run on every platform.

## Commands

Fast smoke:

```bash
uv run pytest tests/integration/cli/tui_real_terminal/test_launch_input_loop.py -q
```

Full deterministic suite:

```bash
uv run pytest tests/integration/cli/tui_real_terminal -q
```

### Development packaged-host gate

Before any future distribution rollout, maintainers can locally rerun the
deterministic OpenTUI scenarios on native macOS and Linux runners from a clean environment containing
only the built core and companion wheels plus test dependencies. This gate is
not currently wired into the formal `v*` release workflow and does not publish
assets. It clears `PYTHONPATH`, source-host overrides, and Bun from
`PATH`, forces the tmux driver, and passes `--tui-require-capabilities`; a
missing terminal capability is therefore a failure rather than a skip. It
covers launch/input, CJK, long streaming, complex tool UI, architecture replay,
resize and multiline paste, completion overlays, and primary-screen/shell
restoration.

The same matrix also runs
`test_packaged_gateway_e2e.py`. That scenario does not use
`fake_opentui_app.py`: it starts an auth-free loopback Gateway with a
deterministic provider, creates a session through `GatewayClient`, uploads a
small attachment, and resumes the session through the installed
`opensquilla chat --ui tui --session` command and installed companion host. A
second Gateway client acts as the Web surface. The scenario proves canonical
history and attachment hydration, Web-to-TUI turn projection, first-valid
approval convergence, queued-turn cancellation, alternate-screen recovery,
and a usable echoed shell after exit.

Scenario frames, scrollback, terminal logs, result files, pytest output, and
JUnit output are retained as development evidence, including on failures. The real
Gateway scenario additionally retains Gateway logs, provider lifecycle JSONL,
RPC/event snapshots, installed core/companion/host version provenance, and an
explicit `fake_opentui_app: false` assertion. Normal
developer runs keep the existing capability-aware skip behavior unless they
explicitly pass `--tui-require-capabilities`.

Manual lab:

```bash
uv run python scripts/tui_real_terminal_lab.py --scenario long_streaming --backend opentui
```

Run the architecture scenario through the development full-screen renderer:

```bash
uv run --extra dev python scripts/tui_real_terminal_lab.py \
  --scenario architecture_prompt --backend opentui --driver tmux
```

OpenTUI backend path:

```bash
uv run pytest tests/integration/cli/tui_real_terminal -q --tui-backend opentui
```

Deterministic styled-framebuffer visual matrix (direct cold start at every
size, with cursor and fixed-layer ownership checks):

```bash
uv run pytest \
  tests/integration/cli/tui_real_terminal/test_visual_layout_matrix.py -q \
  --tui-backend opentui --tui-driver tmux --tui-require-capabilities
```

This gate inspects terminal cells and their resolved RGB styles at 80×24,
120×30, and 160×40. It asserts one header/logo/footer/composer, a cursor inside
the composer, no transcript paint in footer rows, and coherent narrow/wide rail
geometry. It is intentionally font-independent; screenshots remain evidence,
while the styled framebuffer is the blocking visual oracle.

The `opentui` backend runs deterministic fake-provider apps through the real
terminal harness. A guarded `live-opentui` backend exists for manual real CLI
smoke checks:

```bash
OPENSQUILLA_TUI_LIVE_REAL=1 uv run pytest \
  tests/integration/cli/tui_real_terminal/test_live_opentui_real_cli.py -q \
  --tui-backend live-opentui --tui-driver tmux

OPENSQUILLA_TUI_LIVE_REAL=1 uv run python scripts/tui_real_terminal_lab.py \
  --scenario live_opentui_architecture_prompt --backend live-opentui
```

The live smoke launches bare `opensquilla chat --standalone`, drives the
default `auto` policy through tmux, sends a real prompt,
and captures text evidence. Use it deliberately because it may hit the
configured live provider.

## Evidence

Each run writes:

- `scenario.json`
- `terminal.log`
- `app.log`
- `transcript.txt`
- `frames/*.txt`
- `screenshots/`
- `result.json`
- `visual-verdict.json`

The packaged Gateway scenario also writes `gateway.log`,
`provider-events.jsonl`, and `gateway-rpc-events.json`.

Capability misses are explicit skips in normal developer runs and hard failures
when `--tui-require-capabilities` is set by the development packaged-host gate. Deterministic
assertion failures block.
Visual verdicts with `inspect` preserve evidence without blocking unrelated
backend changes.
