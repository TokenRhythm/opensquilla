//! Rust turn state machine (optional OSPP_RUST_KERNEL path).
//!
//! Drives the provider stream through IDLE→THINKING→STREAMING→DONE,
//! consuming events via the dedicated-loop bridge queue and emitting
//! an event-spec list equivalent to the Python engine's run_turn.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList};
use std::time::Instant;

use crate::bridge_v2::import_ospp_bridge;

/// Canonical agent states (mirrors engine.types.AgentState).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum TurnState {
    Idle,
    Thinking,
    Streaming,
    Done,
    Error,
}

impl TurnState {
    fn as_str(&self) -> &'static str {
        match self {
            TurnState::Idle => "idle",
            TurnState::Thinking => "thinking",
            TurnState::Streaming => "streaming",
            TurnState::Done => "done",
            TurnState::Error => "error",
        }
    }
}

/// Accumulates turn output while driving the state machine.
struct TurnMachine {
    state: TurnState,
    text: String,
    input_tokens: i64,
    output_tokens: i64,
    model: String,
    stop_reason: String,
    iterations: i64,
}

impl TurnMachine {
    fn new() -> Self {
        TurnMachine {
            state: TurnState::Idle,
            text: String::new(),
            input_tokens: 0,
            output_tokens: 0,
            model: String::new(),
            stop_reason: String::new(),
            iterations: 1,
        }
    }

    fn transition(&mut self, to: TurnState) {
        self.state = to;
    }

    fn on_text_delta(&mut self, text: &str) {
        if self.state == TurnState::Thinking {
            self.transition(TurnState::Streaming);
        }
        self.text.push_str(text);
    }

    fn on_provider_done(&mut self, stop_reason: &str, input: i64, output: i64, model: &str) {
        self.stop_reason = stop_reason.to_string();
        self.input_tokens = input;
        self.output_tokens = output;
        self.model = model.to_string();
        self.transition(TurnState::Done);
    }
}

fn make_state_change(py: Python<'_>, from_s: &str, to_s: &str) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("kind", "state_change")?;
    d.set_item("from_state", from_s)?;
    d.set_item("to_state", to_s)?;
    Ok(d.into_any().unbind())
}

fn make_text_delta(py: Python<'_>, text: &str) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("kind", "text_delta")?;
    d.set_item("text", text)?;
    d.set_item("presentation", "answer")?;
    Ok(d.into_any().unbind())
}

fn make_done(
    py: Python<'_>,
    text: &str,
    iterations: i64,
    input_tokens: i64,
    output_tokens: i64,
    model: &str,
) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("kind", "done")?;
    d.set_item("text", text)?;
    d.set_item("iterations", iterations)?;
    d.set_item("input_tokens", input_tokens)?;
    d.set_item("output_tokens", output_tokens)?;
    d.set_item("model", model)?;
    Ok(d.into_any().unbind())
}

/// Rust turn kernel: consume provider.chat via the 5a bridge and drive
/// the state machine, returning an event-spec list equivalent to the
/// Python engine's run_turn (no-tools path).
#[pyfunction]
#[pyo3(signature = (provider, message="hello"))]
pub fn run_turn_rust(provider: Py<PyAny>, message: &str) -> PyResult<Py<PyAny>> {
    Python::with_gil(|py| {
        let bridge_mod = import_ospp_bridge(py)?;
        let loop_obj = bridge_mod.call_method0("get_loop")?;
        let messages = PyList::new(
            py,
            vec![format!("{{\"role\":\"user\",\"content\":\"{message}\"}}")],
        )?;
        let q = loop_obj.call_method1("start_chat", (provider, messages))?;

        let mut m = TurnMachine::new();
        let mut events: Vec<Py<PyAny>> = Vec::new();
        let t0 = Instant::now();

        // IDLE -> THINKING
        events.push(make_state_change(
            py,
            TurnState::Idle.as_str(),
            TurnState::Thinking.as_str(),
        )?);
        m.transition(TurnState::Thinking);

        loop {
            let item = q.call_method0("get")?;
            if item.is_none() {
                break;
            }
            let kind: String = item.getattr("kind")?.extract()?;
            match kind.as_str() {
                "text_delta" => {
                    let text: String = item.getattr("text")?.extract()?;
                    let was_thinking = m.state == TurnState::Thinking;
                    m.on_text_delta(&text);
                    if was_thinking {
                        events.push(make_state_change(
                            py,
                            TurnState::Thinking.as_str(),
                            TurnState::Streaming.as_str(),
                        )?);
                    }
                    events.push(make_text_delta(py, &text)?);
                }
                "done" => {
                    let stop: String = item.getattr("stop_reason")?.extract()?;
                    let inp: i64 = item.getattr("input_tokens")?.extract()?;
                    let out: i64 = item.getattr("output_tokens")?.extract()?;
                    let model: String = item.getattr("model")?.extract()?;
                    m.on_provider_done(&stop, inp, out, &model);
                    events.push(make_state_change(
                        py,
                        TurnState::Streaming.as_str(),
                        TurnState::Done.as_str(),
                    )?);
                    events.push(make_done(
                        py,
                        &m.text,
                        m.iterations,
                        m.input_tokens,
                        m.output_tokens,
                        &m.model,
                    )?);
                    break;
                }
                "error" => {
                    m.transition(TurnState::Error);
                    let d = PyDict::new(py);
                    d.set_item("kind", "error")?;
                    let msg: String = item.getattr("message")?.extract().unwrap_or_default();
                    d.set_item("message", msg)?;
                    events.push(d.into_any().unbind());
                    break;
                }
                other => {
                    // Unknown provider event: forward best-effort as-is.
                    let d = PyDict::new(py);
                    d.set_item("kind", format!("provider_{other}"))?;
                    events.push(d.into_any().unbind());
                }
            }
        }

        let elapsed = t0.elapsed().as_nanos() as u64;
        let out = PyList::new(py, events)?;
        let res = PyDict::new(py);
        res.set_item("events", out)?;
        res.set_item("elapsed_ns", elapsed)?;
        res.set_item("final_state", m.state.as_str())?;
        Ok(res.into_any().unbind())
    })
}
