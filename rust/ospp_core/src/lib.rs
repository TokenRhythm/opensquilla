//! ospp_core — optional Rust hot-path kernel for OpenSquilla.
//!
//! This crate is a *drop-in optional accelerator*. It is never required:
//! the Python engine remains fully functional without it, and the
//! `OSPP_RUST_KERNEL` env switch keeps it off by default (zero behaviour
//! change). When enabled it drives the no-tool provider turn through a
//! Rust state machine (IDLE→THINKING→STREAMING→DONE) on top of a
//! dedicated event-loop thread bridge, eliminating per-event asyncio
//! overhead from the hot path.
//!
//! Build (dev):
//!     cd rust/ospp_core && maturin develop --release
//!
//! The Python-side bridge lives in `scripts/ospp_bridge.py` (BridgeLoop).

use pyo3::prelude::*;

mod bridge_v2;
use bridge_v2::bridge_chat_stream_v2;

mod turn_runner;
use turn_runner::run_turn_rust;

/// Rust turn kernel: drive a provider turn and return the full event
/// spec (state changes + text deltas + done), equivalent to the Python
/// engine's no-tool path.
#[pyfunction]
#[pyo3(signature = (provider, message="hello"))]
fn run_turn(provider: Py<PyAny>, message: &str) -> PyResult<Py<PyAny>> {
    run_turn_rust(provider, message)
}

/// Bridge smoke helper: count provider events pumped through the
/// dedicated-loop bridge. Returns (event_count, elapsed_ns).
#[pyfunction]
#[pyo3(signature = (provider, message="hello"))]
fn bridge_events(provider: Py<PyAny>, message: &str) -> PyResult<(usize, u64)> {
    bridge_chat_stream_v2(provider, message)
}

#[pymodule]
fn ospp_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_turn, m)?)?;
    m.add_function(wrap_pyfunction!(bridge_events, m)?)?;
    Ok(())
}
