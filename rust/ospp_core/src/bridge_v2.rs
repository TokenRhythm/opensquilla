//! Dedicated event-loop thread bridge.
//!
//! A direct bridge from Rust to a Python async generator deadlocks:
//! advancing the generator depends on the event loop, while a synchronous
//! Rust call holds the main thread's loop. This module instead drives
//! `provider.chat` on a **dedicated thread running its own asyncio loop**
//! (`scripts/ospp_bridge.py`'s BridgeLoop), pumping events through a
//! thread-safe `queue.Queue`. Rust consumes with `q.get()` — queue.get()
//! blocks with the GIL released, so the dedicated loop thread keeps
//! advancing the generator.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyList};
use std::path::Path;
use std::time::Instant;

/// Import `ospp_bridge` module, adding `scripts/` to sys.path if needed.
pub(crate) fn import_ospp_bridge(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    match py.import("ospp_bridge") {
        Ok(m) => Ok(m.into_any()),
        Err(_) => {
            // Fall back to repo-relative scripts dir (dev layout).
            let scripts = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../scripts")
                .canonicalize()
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "ospp_bridge: cannot resolve scripts dir: {e}"
                    ))
                })?;
            let sys = py.import("sys")?;
            let path = sys.getattr("path")?;
            let _ = path.call_method1("insert", (0, scripts.to_string_lossy().as_ref()));
            py.import("ospp_bridge").map(Bound::into_any)
        }
    }
}

/// Bridge v2: consume `provider.chat(messages)` through the dedicated
/// event-loop thread. Returns (event_count, elapsed_ns).
#[pyfunction]
#[pyo3(signature = (provider, message="hello"))]
pub fn bridge_chat_stream_v2(provider: Py<PyAny>, message: &str) -> PyResult<(usize, u64)> {
    Python::with_gil(|py| {
        let bridge_mod = import_ospp_bridge(py)?;
        let loop_obj = bridge_mod.call_method0("get_loop")?;
        let messages = PyList::new(
            py,
            vec![format!("{{\"role\":\"user\",\"content\":\"{message}\"}}")],
        )?;
        // Start the pump on the dedicated loop thread; returns a queue.
        let q = loop_obj.call_method1("start_chat", (provider, messages))?;
        // Consume events until the None sentinel. q.get() releases the GIL
        // while waiting, so the dedicated loop thread can keep advancing
        // the async generator.
        let t0 = Instant::now();
        let mut count = 0usize;
        loop {
            let item = q.call_method0("get")?;
            if item.is_none() {
                break;
            }
            count += 1;
        }
        Ok((count, t0.elapsed().as_nanos() as u64))
    })
}
