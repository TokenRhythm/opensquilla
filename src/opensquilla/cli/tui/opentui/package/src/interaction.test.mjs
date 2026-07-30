// Behavior tests for the conversation interaction helpers:
//   - isPinnedToBottom decides when streaming/new content should auto-follow the
//     bottom (vs the user having scrolled up to read history);
//   - copySelectionToClipboard mirrors an OpenTUI selection into the system
//     clipboard via OSC 52 (the select-to-copy fix, since a mouse-capturing TUI
//     never receives the terminal's Cmd/Ctrl+C);
//   - createTurnFlow routes protocol events to turn views (queued-prompt
//     isolation and late-block tolerance).
//
// Pure logic, so it runs under `node --test`.
import { test } from "node:test";
import { EventEmitter } from "node:events";
import assert from "node:assert/strict";

import { clampFooterHeight, isPinnedToBottom, copySelectionToClipboard } from "./primitives.mjs";
import { createTurnFlow, isOutOfCardKind } from "./turnView.mjs";
import {
  installTerminalViewportRecovery,
  reconcileTerminalViewport,
  viewportRecoveryWatchdogMs,
  viewportRecoveryWatchdogReassertsSurface,
  TERMINAL_SURFACE_REASSERT_SEQUENCE,
  VIEWPORT_RECOVERY_SETTLE_MS,
} from "./viewportRecovery.mjs";

test("clampFooterHeight keeps the footer within the terminal height", () => {
  assert.equal(clampFooterHeight(6, 24), 6); // normal terminal: full footer
  assert.equal(clampFooterHeight(6, 6), 6); // exact fit
  assert.equal(clampFooterHeight(6, 4), 4); // short pane: clamp to terminal (no overflow)
  assert.equal(clampFooterHeight(6, 2), 2);
  assert.equal(clampFooterHeight(6, 1), 1); // never below one row
  assert.equal(clampFooterHeight(6, 0), 6); // unknown/zero height -> fall back to full footer
  assert.equal(clampFooterHeight(6, undefined), 6);
});

test("isPinnedToBottom only follows when at/near the bottom", () => {
  // viewport 30, content 100 => maxTop 70
  assert.equal(isPinnedToBottom(70, 100, 30), true); // exactly at the bottom
  assert.equal(isPinnedToBottom(69, 100, 30), true); // within default slack (2)
  assert.equal(isPinnedToBottom(50, 100, 30), false); // scrolled up to read history
  assert.equal(isPinnedToBottom(0, 100, 30), false); // at the top
  // content shorter than the viewport is always "at the bottom"
  assert.equal(isPinnedToBottom(0, 10, 30), true);
});

test("copySelectionToClipboard copies selected text via OSC 52 when supported", () => {
  const copied = [];
  const renderer = {
    isOsc52Supported: () => true,
    copyToClipboardOSC52: (text) => {
      copied.push(text);
      return true;
    },
  };
  const result = copySelectionToClipboard(renderer, { getSelectedText: () => "hello world" });
  assert.equal(result, true);
  assert.deepEqual(copied, ["hello world"]);
});

test("copySelectionToClipboard is a no-op for empty selection or unsupported terminal", () => {
  let copyCalls = 0;
  const base = {
    copyToClipboardOSC52: () => {
      copyCalls += 1;
      return true;
    },
  };
  // empty selection -> nothing copied
  assert.equal(
    copySelectionToClipboard({ ...base, isOsc52Supported: () => true }, { getSelectedText: () => "" }),
    false,
  );
  // OSC 52 unsupported terminal -> nothing copied (no stray escape bytes)
  assert.equal(
    copySelectionToClipboard({ ...base, isOsc52Supported: () => false }, { getSelectedText: () => "x" }),
    false,
  );
  assert.equal(copyCalls, 0);
});

test("viewport recovery catches a missed SIGWINCH and forces a clean frame", () => {
  const resizes = [];
  let renders = 0;
  const renderer = {
    terminalWidth: 86,
    terminalHeight: 30,
    forceFullRepaintRequested: false,
    resize(width, height) {
      resizes.push([width, height]);
      this.terminalWidth = width;
      this.terminalHeight = height;
    },
    requestRender() { renders += 1; },
  };

  const result = reconcileTerminalViewport(
    renderer,
    {
      // Embedded terminals can update the PTY before the cached properties;
      // getWindowSize() must win over these stale values.
      columns: 86,
      rows: 30,
      getWindowSize: () => [132, 42],
    },
  );

  assert.equal(result, "resized");
  assert.deepEqual(resizes, [[132, 42]]);
  assert.equal(renderer.forceFullRepaintRequested, true);
  assert.equal(renders, 1);
});

test("viewport recovery repaints a revealed terminal even when its size is unchanged", () => {
  let resizes = 0;
  let renders = 0;
  const renderer = {
    terminalWidth: 100,
    terminalHeight: 30,
    forceFullRepaintRequested: false,
    resize() { resizes += 1; },
    requestRender() { renders += 1; },
  };

  const result = reconcileTerminalViewport(
    renderer,
    { columns: 100, rows: 30 },
    { forceRepaint: true },
  );

  assert.equal(result, "repainted");
  assert.equal(resizes, 0);
  assert.equal(renderer.forceFullRepaintRequested, true);
  assert.equal(renders, 1);
});

test("viewport recovery ignores unavailable or transient zero terminal geometry", () => {
  let calls = 0;
  const renderer = {
    terminalWidth: 100,
    terminalHeight: 30,
    resize() { calls += 1; },
    requestRender() { calls += 1; },
  };
  assert.equal(
    reconcileTerminalViewport(renderer, { columns: 0, rows: 0 }),
    "unavailable",
  );
  assert.equal(calls, 0);
});

test("viewport recovery falls back per dimension when direct PTY geometry is transiently zero", () => {
  const resizes = [];
  const renderer = {
    terminalWidth: 80,
    terminalHeight: 24,
    resize(width, height) {
      resizes.push([width, height]);
      this.terminalWidth = width;
      this.terminalHeight = height;
    },
    requestRender() {},
  };
  const result = reconcileTerminalViewport(renderer, {
    columns: 132,
    rows: 40,
    getWindowSize: () => [0, 36],
  });
  assert.equal(result, "resized");
  assert.deepEqual(resizes, [[132, 36]]);
});

test("viewport recovery defers raw resize and yields to OpenTUI's public resize event", () => {
  const output = new EventEmitter();
  output.columns = 80;
  output.rows = 24;
  output.getWindowSize = () => [132, 42];
  const terminalWrites = [];
  output.write = (value) => { terminalWrites.push(value); };
  const signalSource = new EventEmitter();
  const renderer = new EventEmitter();
  renderer.terminalWidth = 80;
  renderer.terminalHeight = 24;
  renderer.forceFullRepaintRequested = false;
  renderer.resize = (width, height) => {
    renderer.terminalWidth = width;
    renderer.terminalHeight = height;
  };
  let renders = 0;
  renderer.requestRender = () => { renders += 1; };

  const timers = [];
  const recovery = installTerminalViewportRecovery({
    renderer,
    output,
    signalSource,
    watchdogMs: 0,
    setTimer(callback, delay) {
      const timer = { callback, delay, cleared: false, unref() {} };
      timers.push(timer);
      return timer;
    },
    clearTimer(timer) { timer.cleared = true; },
  });

  signalSource.emit("SIGWINCH");
  output.emit("resize");
  assert.deepEqual([renderer.terminalWidth, renderer.terminalHeight], [80, 24]);
  assert.ok(timers[0].delay > 100);
  assert.equal(timers[0].cleared, true);
  assert.equal(renders, 0);

  // OpenTUI's documented resize event owns the application transaction and
  // cancels the raw-event fallback before it can emit a duplicate full frame.
  renderer.resize(132, 42);
  renderer.emit("resize");
  assert.deepEqual([renderer.terminalWidth, renderer.terminalHeight], [132, 42]);
  assert.equal(timers[1].cleared, true);
  assert.equal(renders, 0);

  // A same-size focus/reveal still produces exactly one recovery frame.
  renderer.emit("focus");
  signalSource.emit("SIGWINCH");
  assert.equal(timers.length, 2); // matching geometry does not schedule fallback
  assert.equal(renders, 1);
  // OpenTUI restores modes before its public focus event. Our lifecycle
  // recovery may repaint, but must not toggle DECSET 1049 on a healthy screen.
  assert.deepEqual(terminalWrites, []);

  renderer.emit("destroy");
  assert.equal(output.listenerCount("resize"), 0);
  assert.equal(signalSource.listenerCount("SIGWINCH"), 0);
  assert.equal(renderer.listenerCount("resize"), 0);
  assert.equal(renderer.listenerCount("focus"), 0);
  assert.equal(recovery.recover(), "disposed");
});

test("viewport recovery final pass stays beyond OpenTUI's debounce window", () => {
  assert.ok(VIEWPORT_RECOVERY_SETTLE_MS > 100);
});

test("viewport recovery watchdog is explicit opt-in in every terminal", () => {
  assert.equal(viewportRecoveryWatchdogMs({}), 0);
  assert.equal(viewportRecoveryWatchdogMs({ CODEX_SHELL: "1" }), 0);
  assert.equal(viewportRecoveryWatchdogMs({ TERM_PROGRAM: "vscode" }), 0);
  assert.equal(
    viewportRecoveryWatchdogMs({
      CODEX_SHELL: "1",
      OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS: "0",
    }),
    0,
  );
  assert.equal(
    viewportRecoveryWatchdogMs({ OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS: "1200" }),
    1200,
  );
  assert.equal(viewportRecoveryWatchdogReassertsSurface({ CODEX_SHELL: "1" }), false);
  assert.equal(
    viewportRecoveryWatchdogReassertsSurface({ OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS: "1200" }),
    true,
  );
});

test("raw resize fallback recovers once only when OpenTUI misses its resize event", () => {
  const output = new EventEmitter();
  output.columns = 80;
  output.rows = 24;
  output.getWindowSize = () => [132, 42];
  output.write = () => {};
  const signalSource = new EventEmitter();
  const renderer = new EventEmitter();
  renderer.terminalWidth = 80;
  renderer.terminalHeight = 24;
  renderer.resize = (width, height) => {
    renderer.terminalWidth = width;
    renderer.terminalHeight = height;
  };
  let renders = 0;
  renderer.requestRender = () => { renders += 1; };
  const timers = [];
  const recovery = installTerminalViewportRecovery({
    renderer,
    output,
    signalSource,
    watchdogMs: 0,
    setTimer(callback, delay) {
      const timer = { callback, delay, unref() {} };
      timers.push(timer);
      return timer;
    },
    clearTimer() {},
  });

  signalSource.emit("SIGWINCH");
  assert.equal(renders, 0);
  assert.equal(timers.length, 1);
  timers[0].callback();
  assert.deepEqual([renderer.terminalWidth, renderer.terminalHeight], [132, 42]);
  assert.equal(renders, 1);
  recovery.dispose();
});

test("viewport watchdog restores a same-size framebuffer without any terminal event", () => {
  const output = new EventEmitter();
  output.columns = 120;
  output.rows = 36;
  const terminalWrites = [];
  output.write = (value) => { terminalWrites.push(value); };
  const signalSource = new EventEmitter();
  const renderer = new EventEmitter();
  renderer.terminalWidth = 120;
  renderer.terminalHeight = 36;
  renderer.forceFullRepaintRequested = false;
  let renders = 0;
  renderer.requestRender = () => { renders += 1; };

  const intervals = [];
  const recovery = installTerminalViewportRecovery({
    renderer,
    output,
    signalSource,
    watchdogMs: 750,
    watchdogReassertSurface: false,
    setIntervalFn(callback, delay) {
      const timer = { callback, delay, cleared: false, unref() {} };
      intervals.push(timer);
      return timer;
    },
    clearIntervalFn(timer) { timer.cleared = true; },
  });

  assert.equal(intervals.length, 1);
  assert.equal(intervals[0].delay, 750);
  assert.equal(renders, 0);

  // No resize, SIGWINCH, focus, input, or stream event occurs here. The
  // embedded-host watchdog must independently invalidate OpenTUI's retained
  // back-buffer without periodically toggling the alternate-screen mode.
  intervals[0].callback();
  assert.deepEqual(terminalWrites, []);
  assert.equal(renderer.forceFullRepaintRequested, true);
  assert.equal(renders, 1);

  recovery.dispose();
  assert.equal(intervals[0].cleared, true);
});

test("surface recovery commits application layout before exposing the full frame", () => {
  const order = [];
  const output = new EventEmitter();
  output.columns = 120;
  output.rows = 36;
  output.write = () => { order.push("surface"); };
  const renderer = new EventEmitter();
  renderer.terminalWidth = 120;
  renderer.terminalHeight = 36;
  renderer.requestRender = () => { order.push("repaint"); };

  const recovery = installTerminalViewportRecovery({
    renderer,
    output,
    signalSource: new EventEmitter(),
    watchdogMs: 0,
    onRecovered: () => { order.push("layout"); },
  });

  assert.equal(recovery.recover(), "unchanged");
  assert.deepEqual(order, ["surface", "layout", "repaint"]);
  assert.equal(renderer.forceFullRepaintRequested, true);
  recovery.dispose();
});

test("routine wheels do not touch the terminal surface; first wheel after blur recovers once", () => {
  const output = new EventEmitter();
  output.columns = 120;
  output.rows = 36;
  const terminalWrites = [];
  output.write = (value) => { terminalWrites.push(value); };
  const renderer = new EventEmitter();
  renderer.terminalWidth = 120;
  renderer.terminalHeight = 36;
  let renders = 0;
  renderer.requestRender = () => { renders += 1; };
  const recovery = installTerminalViewportRecovery({
    renderer,
    output,
    signalSource: new EventEmitter(),
    watchdogMs: 0,
  });

  recovery.recoverBeforeWheel();
  recovery.recoverBeforeWheel();
  assert.equal(renders, 0);
  assert.deepEqual(terminalWrites, []);

  renderer.emit("blur");
  recovery.recoverBeforeWheel();
  recovery.recoverBeforeWheel();

  assert.equal(renders, 1);
  assert.deepEqual(terminalWrites, [TERMINAL_SURFACE_REASSERT_SEQUENCE]);
  recovery.dispose();
});

function stubFlow() {
  let seq = 0;
  const flow = createTurnFlow((id) => ({
    id: id ?? `auto-${seq++}`,
    ended: false,
    cancelled: null,
    detailsExpanded: null,
    detailCalls: [],
    refreshCalls: 0,
    finish(c) { this.cancelled = Boolean(c); },
    setDetailsExpanded(value) {
      this.detailsExpanded = Boolean(value);
      this.detailCalls.push(this.detailsExpanded);
      return this.detailsExpanded;
    },
    refreshContext() { this.refreshCalls += 1; },
  }));
  return flow;
}

test("a prompt echoed during a streaming turn gets its own view, adopted at the next turn.begin", () => {
  const flow = stubFlow();
  const streaming = flow.ensure("t1"); // turn 1 begins and streams
  const queued = flow.turnForPrompt("optimistic-2", "client-2"); // user submits while it streams
  assert.notEqual(queued, streaming); // never the live turn: its card must not seal
  assert.equal(flow.active(), streaming); // blocks keep streaming into turn 1
  flow.endTurn();
  assert.equal(streaming.ended, true);
  assert.equal(flow.ensure("t2", "client-2"), queued); // turn 2 adopts the queued view
});

test("queued prompts are adopted by client identity rather than FIFO order", () => {
  const flow = stubFlow();
  flow.ensure("t1");
  const first = flow.turnForPrompt("optimistic-2", "client-2");
  const second = flow.turnForPrompt("optimistic-3", "client-3");
  assert.notEqual(first, second);
  flow.endTurn();
  assert.equal(flow.ensure("t3", "client-3"), second);
  flow.endTurn();
  assert.equal(flow.ensure("t2", "client-2"), first);
});

test("a block after turn.end lands in the ended turn, never an orphan that absorbs the next prompt", () => {
  const flow = stubFlow();
  const done = flow.ensure("t1");
  flow.endTurn();
  assert.equal(flow.turnForBlock(), done); // a late usage straggler stays with its turn
  const next = flow.turnForPrompt("optimistic-2", "client-2"); // the NEXT submission starts a fresh turn
  assert.notEqual(next, done);
  assert.equal(flow.active(), next); // and the following turn.begin reuses it
  assert.equal(flow.ensure("t2", "client-2"), next);
});

test("endTurn passes cancelled through to the view's finish", () => {
  const flow = stubFlow();
  const view = flow.ensure("t1");
  flow.endTurn(true);
  assert.equal(view.cancelled, true);
  assert.equal(view.ended, true);
  const normal = flow.ensure("t2");
  flow.endTurn();
  assert.equal(normal.cancelled, false);
});

test("a cancelled turn.end invalidates queued-prompt views instead of leaving them for adoption", () => {
  // Esc / empty Ctrl+C cancels the streaming turn AND discards the queued
  // submissions server-side. The stale queued views must be flushed — marked
  // cancelled and ended — or the NEXT real submission would be adopted into a
  // discarded prompt's box, rendering its whole turn glued under a dead card.
  const flow = stubFlow();
  flow.ensure("t1"); // turn 1 streams
  const q1 = flow.turnForPrompt("optimistic-2", "client-2"); // two submissions queue behind it
  const q2 = flow.turnForPrompt("optimistic-3", "client-3");
  flow.endTurn(true); // Esc: cancel + queue discard
  assert.equal(q1.ended, true);
  assert.equal(q1.cancelled, true); // visibly unanswered
  assert.equal(q2.ended, true);
  assert.equal(q2.cancelled, true);
  const next = flow.turnForPrompt("optimistic-4", "client-4"); // the next real submission…
  assert.notEqual(next, q1); // …never lands in a discarded prompt's box
  assert.notEqual(next, q2);
  assert.equal(flow.ensure("t4", "client-4"), next); // and its turn.begin adopts the fresh view
});

test("a normal turn.end keeps queued views for identity adoption", () => {
  const flow = stubFlow();
  flow.ensure("t1");
  const queued = flow.turnForPrompt("optimistic-2", "client-2");
  flow.endTurn(); // completed, not cancelled: the queue survives
  assert.equal(queued.ended, false);
  assert.equal(flow.ensure("t2", "client-2"), queued);
});

test("flow-level detail disclosure updates existing turns and is inherited by future turns", () => {
  const flow = stubFlow();
  const first = flow.ensure("t1");
  assert.equal(first.detailsExpanded, false); // initial flow policy applied on create

  assert.equal(flow.setDetailsExpanded(true), true);
  assert.equal(flow.detailsExpanded, true);
  assert.equal(first.detailsExpanded, true);

  flow.endTurn();
  const second = flow.ensure("t2");
  assert.equal(second.detailsExpanded, true); // newly-created view inherits policy

  const toggle = flow.toggleDetails;
  assert.equal(toggle(), false); // safe even when the method is passed as a callback
  assert.equal(first.detailsExpanded, false);
  assert.equal(second.detailsExpanded, false);
});

test("flow-level context refresh repaints every retained turn", () => {
  const flow = stubFlow();
  const first = flow.ensure("t1");
  flow.endTurn();
  const second = flow.ensure("t2");
  flow.refreshContext();
  assert.equal(first.refreshCalls, 1);
  assert.equal(second.refreshCalls, 1);
});

test("flow anchors preserve a real block identity when earlier content changes height", () => {
  let toolTop = 5;
  const flow = createTurnFlow((id) => ({
    turnId: String(id),
    ended: false,
    layoutTop: () => 12,
    measuredRows: () => 40,
    anchorAtRow: (row) => row >= toolTop
      ? { block_id: "tool-call-7", row_within_block: row - toolTop }
      : { block_id: "prompt-7", row_within_block: row },
    rowForAnchor: (anchor) => anchor.block_id === "tool-call-7"
      ? toolTop + anchor.row_within_block
      : anchor.row_within_block,
    setDetailsExpanded() {},
  }));
  flow.ensure("turn-7");

  const anchor = flow.anchorAtRow(20); // turn local row 8, tool local row 3
  assert.deepEqual(anchor, {
    turn_id: "turn-7",
    block_id: "tool-call-7",
    row_within_block: 3,
    _absolute_row: 20,
  });

  // Ctrl+O/streaming inserted six visual rows before the tool. A whole-turn
  // row anchor would still return 20; the semantic block anchor follows it.
  toolTop += 6;
  assert.equal(flow.rowForAnchor(anchor), 26);
});

test("an optimistic-id anchor survives durable turn identity binding", () => {
  const flow = createTurnFlow((id) => ({
    turnId: String(id),
    ended: false,
    layoutTop: () => 4,
    measuredRows: () => 12,
    anchorAtRow: (row) => ({ block_id: "answer", row_within_block: row - 2 }),
    rowForAnchor: (anchor) => 2 + anchor.row_within_block,
    setDetailsExpanded() {},
  }));
  flow.ensure("optimistic-turn", "client-7");
  const anchor = flow.anchorAtRow(9);
  assert.equal(anchor.turn_id, "optimistic-turn");

  flow.bindPrompt("durable-turn", "client-7");
  assert.equal(flow.active().turnId, "durable-turn");
  assert.equal(flow.rowForAnchor(anchor), 9);
});

test("unknown block kinds stay inside the card; only prompt/usage render outside", () => {
  assert.equal(isOutOfCardKind("prompt"), true);
  assert.equal(isOutOfCardKind("usage"), true);
  for (const kind of ["answer", "thinking", "tool", "reasoning", "ensemble", "error", "future-kind"]) {
    assert.equal(isOutOfCardKind(kind), false, `${kind} must render in-card`);
  }
});
