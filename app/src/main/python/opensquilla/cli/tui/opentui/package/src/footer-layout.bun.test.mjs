// Renderer-level regression tests for the footer (composer + router HUD).
//
// Two guarantees:
//   1. The footer container paints an OPAQUE background. A transparent footer
//      leaves cells it vacates on resize/reflow uncleared by the terminal diff,
//      so stale glyphs from a prior layout linger (the router model text bleeding
//      into the composer placeholder, stray edge characters, etc.). The tmux/PTY
//      text-snapshot harness cannot see cell background alpha, so this class of
//      bug slipped past it; here we inspect captured span backgrounds (RGBA).
//   2. The composer box and router HUD box never overlap, at several widths and
//      after a resize — locking the layout contract.
//
// Must run under bun: @opentui/core/testing needs bun FFI. Run with:
//   bun test src/footer-layout.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import { DOUBLE_CTRL_C_MS, createCtrlCExitHatch } from "./main.mjs";
import { SURFACE_Z_INDEX } from "./screenMode.mjs";
import { THEME } from "./theme.mjs";

const FOOTER_HEIGHT = 6;
const HEIGHT = 12;
const LONG_MODEL = "deepseek/deepseek-v4-pro-20260423";

async function renderFooter({
  width,
  withFooterBg = true,
  resizeTo = null,
  overlappingTranscript = false,
}) {
  const setup = await createTestRenderer({ width, height: HEIGHT });
  const { renderer, renderOnce, captureSpans, resize } = setup;

  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: HEIGHT - FOOTER_HEIGHT,
    zIndex: SURFACE_Z_INDEX.transcript,
  });
  renderer.root.add(conversationBox);

  const inputOptions = {
    id: "input-region",
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: FOOTER_HEIGHT,
    zIndex: SURFACE_Z_INDEX.footer,
  };
  if (withFooterBg) inputOptions.backgroundColor = THEME.footerBg;
  const inputBox = new BoxRenderable(renderer, inputOptions);
  renderer.root.add(inputBox);

  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 1000,
    shouldFill: false,
    visible: false,
  });
  renderer.root.add(overlayLayer);

  const composer = createComposer({
    renderer,
    BoxRenderable,
    TextRenderable,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight: FOOTER_HEIGHT,
    sendHostMessage: () => {},
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  composer.setComposerState({ placeholder: "send a message" });
  composer.setRouterState({ model: LONG_MODEL, route: "route c1", saving: "-", context: "-" });

  if (overlappingTranscript) {
    // Mount this *after* the footer to prove paint ownership comes from the
    // explicit OpenTUI z-index contract rather than incidental insertion order.
    const staleTranscript = new BoxRenderable(renderer, {
      id: "stale-overlapping-transcript",
      position: "absolute",
      left: 0,
      right: 0,
      bottom: 0,
      height: FOOTER_HEIGHT,
      zIndex: SURFACE_Z_INDEX.transcript,
      backgroundColor: THEME.appBg,
    });
    staleTranscript.add(new TextRenderable(renderer, {
      id: "stale-overlapping-text",
      content: "TRANSCRIPT MUST NOT OWN FOOTER",
      fg: THEME.text,
    }));
    renderer.root.add(staleTranscript);
  }

  if (resizeTo) {
    const doResize = resize || ((w, h) => renderer.resize(w, h));
    await doResize(resizeTo, HEIGHT);
    composer.onResize();
  }

  await renderOnce();
  const frame = captureSpans();
  renderer.destroy?.();
  return frame;
}

function rowText(frame, rowIndex) {
  const line = frame.lines[rowIndex];
  return line ? line.spans.map((span) => span.text).join("") : "";
}

// Background of the inputBox left margin (column 0). The composer box starts at
// left:1, so column 0 in a footer row is the container's own fill.
function footerMarginBg(frame) {
  const line = frame.lines[HEIGHT - 3];
  if (!line) return null;
  let col = 0;
  for (const span of line.spans) {
    const width = Math.max(1, span.width || 1);
    for (let i = 0; i < width; i += 1) {
      if (col === 0) return span.bg;
      col += 1;
    }
  }
  return null;
}

test("footer paints an opaque background so it self-clears on reflow", async () => {
  const bg = footerMarginBg(await renderFooter({ width: 80, withFooterBg: true }));
  expect(bg).not.toBeNull();
  // Opaque: every footer cell is rewritten each frame, so stale glyphs cannot
  // survive a composer/router box moving on resize.
  expect(bg.a).toBeGreaterThan(0);
});

test("a footer without backgroundColor is transparent (contrast case)", async () => {
  // Proves the assertion above discriminates: drop the fix and the margin cell is
  // transparent (alpha 0) — the bug that lets ghosts linger.
  const bg = footerMarginBg(await renderFooter({ width: 80, withFooterBg: false }));
  expect(bg).not.toBeNull();
  expect(bg.a).toBe(0);
});

test("composer is one full-width box with the strategy strip on its own row above", async () => {
  // Model strategy is a single-line status strip on the TOP footer row; the composer
  // is one full-width box below it. Nothing shares the composer's (caret's) rows
  // — that adjacency is what corrupted the router under a macOS IME overlay, and
  // removing it is the fix (mirrors opencode on the same @opentui/core engine).
  for (const width of [60, 80, 120, 160]) {
    const frame = await renderFooter({ width });
    const stripRow = rowText(frame, HEIGHT - FOOTER_HEIGHT);
    const boxTopRow = rowText(frame, HEIGHT - FOOTER_HEIGHT + 1);
    // The canonical direct/router/ensemble strategy and model live on the
    // strip; the default fixture is direct. The strip is NOT a box.
    expect(stripRow).toContain("direct");
    expect(stripRow).toContain("model");
    expect(stripRow.includes("╭")).toBe(false);
    // Exactly ONE box on the composer's rows: a single ╭…╮ pair, no second box.
    const open = boxTopRow.indexOf("╭");
    const close = boxTopRow.indexOf("╮");
    expect(open).toBeGreaterThanOrEqual(0);
    expect(boxTopRow.lastIndexOf("╭")).toBe(open); // only one opening corner
    expect(close).toBeGreaterThan(open);
    expect(boxTopRow.lastIndexOf("╮")).toBe(close); // only one closing corner
  }
});

test("the router model renders on the strip, never on the composer/caret rows", async () => {
  const frame = await renderFooter({ width: 100, resizeTo: 60 });
  // The model value lives on the router strip (top footer row)...
  const stripRow = rowText(frame, HEIGHT - FOOTER_HEIGHT);
  expect(stripRow).toContain("deepseek-v4-pro");
  // ...and never on the composer box's border or input/caret rows, where a macOS
  // IME would composite its marked text and candidate popover.
  for (const r of [HEIGHT - FOOTER_HEIGHT + 1, HEIGHT - FOOTER_HEIGHT + 2, HEIGHT - 1]) {
    expect(rowText(frame, r).includes("deepseek-v4-pro")).toBe(false);
  }
});

test("footer z-index wins a real paint conflict with a late transcript surface", async () => {
  const frame = await renderFooter({ width: 100, overlappingTranscript: true });
  const footer = frame.lines
    .slice(HEIGHT - FOOTER_HEIGHT)
    .map((_, index) => rowText(frame, HEIGHT - FOOTER_HEIGHT + index))
    .join("\n");

  expect(footer).not.toContain("TRANSCRIPT MUST NOT OWN FOOTER");
  expect(footer).toContain("direct");
  expect(footer).toContain("send a message");
  expect(footerMarginBg(frame).a).toBeGreaterThan(0);
});

test("double Ctrl+C exits only on two consecutive interrupt-path presses", async () => {
  // The host-local escape hatch must never hard-kill a healthy session on the
  // routine clear-then-cancel double press: a Ctrl+C the composer consumed to
  // clear a draft (or that a modal overlay is up for) disarms the chord; only
  // presses that actually reached the interrupt path (input.cancel sent, no
  // overlay) count, and only within the chord window.
  const setup = await createTestRenderer({ width: 60, height: HEIGHT });
  const { renderer } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0,
    height: HEIGHT - FOOTER_HEIGHT,
  });
  renderer.root.add(conversationBox);
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0,
    height: FOOTER_HEIGHT, backgroundColor: THEME.footerBg,
  });
  renderer.root.add(inputBox);
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(overlayLayer);

  const sent = [];
  const exits = [];
  let clock = 10_000;
  // Mirror main.mjs's wiring order exactly: hatch first (its per-press reset
  // listener must run ahead of the composer's handler), composer.install(),
  // then hatch.install() so the chord check sees what the composer did.
  const hatch = createCtrlCExitHatch({
    keyInput: renderer.keyInput,
    isOverlayOpen: () => Boolean(overlayLayer.visible),
    onExit: () => exits.push(clock),
    now: () => clock,
  });
  const composer = createComposer({
    renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, overlayLayer,
    footerHeight: FOOTER_HEIGHT,
    sendHostMessage: (m) => { hatch.noteHostMessage(m); sent.push(m); },
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  hatch.install();

  const cancels = () => sent.filter((m) => m.type === "input.cancel").length;
  const ctrlC = () => renderer.keyInput.emit("keypress", { name: "c", ctrl: true });
  const type = (c) => renderer.keyInput.emit("keypress", { name: c, sequence: c });

  // Clear-then-cancel: the first press clears the draft (composer-consumed),
  // the second — now empty — interrupts the turn. No exit, ever.
  type("h");
  type("i");
  ctrlC();
  expect(cancels()).toBe(0); // the clear press never reached the interrupt path
  clock += 200;
  ctrlC();
  expect(cancels()).toBe(1); // the cancel went out…
  expect(exits.length).toBe(0); // …and only ARMED the chord

  // The second consecutive interrupt press within the window is the hatch.
  clock += 200;
  ctrlC();
  expect(exits.length).toBe(1);

  // Interrupt presses spaced beyond the window never chord.
  clock += DOUBLE_CTRL_C_MS + 1;
  ctrlC();
  clock += DOUBLE_CTRL_C_MS + 1;
  ctrlC();
  expect(exits.length).toBe(1);

  // A modal overlay up: Ctrl+C still interrupts (the approval overlay passes
  // it through by design) but the chord stays disarmed.
  composer.openApprovalOverlay({ id: "appr-1", tool: "shell", summary: "touch demo.txt" });
  clock += 200;
  ctrlC();
  clock += 200;
  ctrlC();
  expect(cancels()).toBeGreaterThan(1);
  expect(exits.length).toBe(1); // unchanged
  renderer.destroy?.();
});
