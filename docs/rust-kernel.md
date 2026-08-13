# Optional Rust Turn Kernel (`OSPP_RUST_KERNEL`)

OpenSquilla ships a fully Python engine. For users who want to eliminate
per-event asyncio overhead from the no-tool turn hot path, the repository
also contains an **optional** Rust state machine (`rust/ospp_core/`) that
can drive provider turns directly. It is:

- **off by default** — no behaviour change unless you opt in;
- **fallback-safe** — any import/build/runtime error degrades to the
  Python kernel transparently;
- **scoped** — it only takes over turns that run **without tools** and
  **without meta resume/replay** metadata.

## What it does

For a no-tool turn, the Python engine's `_turn_generator` state machine is
the hottest asyncio code path: every provider event crosses the event loop,
and each `yield` in the async generator adds scheduling overhead. The Rust
kernel replaces that loop with a compiled state machine
(`IDLE → THINKING → STREAMING → DONE`) driven through a dedicated
event-loop thread bridge (`scripts/ospp_bridge.py`), so the generator is
advanced by a worker thread while Rust consumes events with no per-event
GIL churn.

The Rust kernel emits the same event stream as the Python engine
(state changes, text deltas, done payloads), so downstream consumers see
identical output.

## Build

Requires a Rust toolchain (1.85+), `maturin`, and a Python 3.12 venv:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
cd rust/ospp_core
source ~/.cargo/env            # if cargo is not on PATH
maturin develop --release      # installs ospp_core into the active venv
```

The wheel is abi3 (`cp312-abi3`), so it works on any Python ≥ 3.12.

## Enable

```bash
OSPP_RUST_KERNEL=1 opensquilla
```

or export the variable before starting the gateway/agent process.

## Scope and guards

The hook activates only when **all** of these hold:

- `OSPP_RUST_KERNEL=1`;
- the agent has **no** tool definitions (`self.tool_definitions` empty);
- the turn metadata has **no** `meta_resume` / `meta_replay` /
  `meta_replay_error` keys (those follow the Python-specific meta paths).

If `ospp_core` is not installed/importable, or any call fails, the engine
logs `rust_kernel.fallback` and continues on the Python path.

## Tests

```bash
.venv/bin/python -m pytest tests/test_engine/test_rust_kernel_hook.py -q
```

- Without the extension: fallback / guard / default-off cases run;
- With the extension built: the `rust_kernel`-marked test runs and asserts
  the Rust event stream is equivalent to the Python engine's.

## Benchmarks

Engine-level numbers from the original research fork (OS++, Python 3.12,
200-turn FakeProvider benchmark, no-tool turns):

| Metric | Python kernel | Rust kernel | Delta |
|---|---|---|---|
| TTFT (p50) | 3.6 ms | 0.6 ms | **-83%** |
| Wall time (p50) | 7.7 ms | 0.8 ms | **-90%** |
| Throughput | 129 tok/s | 1256 tok/s | **+9.75x** |

Real provider latencies dominate in production, so absolute gains shrink;
the win is concentrated in long chat sessions and high-event-rate turns
where engine scheduling overhead is a meaningful fraction of TTFT.
