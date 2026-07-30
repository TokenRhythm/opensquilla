// Renderer-level pins for the trailing usage summary and the in-card error
// block. The Python renderer emits the usage block BEFORE turn.end and relies
// on begin(usage) sealing the assistant card, so the "in X / out Y" summary
// must ride ON the "╰ …" footer line itself (the card closes into its
// receipt) — that folding is a cross-language visual contract with no other
// renderer-level coverage.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable, MarkdownRenderable } from "@opentui/core";

import { createTurnView } from "./turnView.mjs";
import { applyTheme, THEME } from "./theme.mjs";

const WIDTH = 60;
const HEIGHT = 14;

async function createTurnHarness() {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
  const { renderer } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  const turn = createTurnView(
    {
      renderer,
      BoxRenderable,
      TextRenderable,
      MarkdownRenderable,
      syntaxStyle: undefined,
      conversationBox,
    },
    "probe",
  );
  return { ...setup, turn };
}

function rows(frame) {
  return frame.lines.map((line) => line.spans.map((s) => s.text).join(""));
}

async function waitForText(renderOnce, captureSpans, needle, tries = 80) {
  for (let i = 0; i < tries; i += 1) {
    await renderOnce();
    const frame = captureSpans();
    if (rows(frame).join("\n").includes(needle)) return frame;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return captureSpans();
}

function spanFgIs(span, hex) {
  const n = parseInt(hex.slice(1), 16);
  const want = [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  const fg = span.fg;
  if (!fg) return false;
  return [fg.r, fg.g, fg.b].every((c, i) => Math.abs(c - want[i]) < 0.004);
}

test("the usage summary folds into the card footer line", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness();
  try {
    turn.begin("a", "answer", {});
    turn.append("a", "the answer body");
    turn.end("a");
    // renderer.py emits usage BEFORE turn.end; begin(usage) must seal the card.
    turn.begin("u", "usage", { text: "in 1 / out 2" });
    turn.finish();
    await renderOnce();

    const frame = captureSpans();
    const lines = rows(frame);
    const footerRow = lines.findIndex((l) => l.trimStart().startsWith("╰"));
    expect(footerRow).toBeGreaterThanOrEqual(0);
    // The footer IS the receipt: "╰ <usage>", nothing more on that line.
    expect(lines[footerRow].trim()).toBe("╰ in 1 / out 2");
    // No separate usage row anywhere else — the text lives only on the footer.
    expect(lines.filter((l) => l.includes("in 1 / out 2")).length).toBe(1);
    // Exactly one footer: begin(usage) closed the card, finish() was a no-op.
    expect(lines.filter((l) => l.trimStart().startsWith("╰")).length).toBe(1);
    // A footer carrying a usage receipt renders muted, not frame-colored.
    const footerSpan = frame.lines[footerRow].spans.find((s) => s.text.includes("in 1"));
    expect(footerSpan).toBeDefined();
    expect(spanFgIs(footerSpan, THEME.muted)).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

test("an error block renders ✗ text inside the card in the error color", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness();
  try {
    turn.begin("e", "error", { text: "boom" });
    turn.finish();
    await renderOnce();

    const frame = captureSpans();
    const lines = rows(frame);
    const errorRow = lines.findIndex((l) => l.includes("✗ boom"));
    const footerRow = lines.findIndex((l) => l.trimStart().startsWith("╰"));
    expect(errorRow).toBeGreaterThanOrEqual(0);
    // In-card: the error row sits between the card header and its footer.
    expect(lines.findIndex((l) => l.includes("╭"))).toBeLessThan(errorRow);
    expect(footerRow).toBeGreaterThan(errorRow);
    // And it is painted with the theme's error token.
    const errorSpan = frame.lines[errorRow].spans.find((s) => s.text.includes("boom"));
    expect(errorSpan).toBeDefined();
    expect(spanFgIs(errorSpan, THEME.error)).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

test("a corrected snapshot clears stale prose and keeps a visible notice plus tool rows", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness();
  try {
    turn.begin("n", "thinking", {});
    turn.append("n", "stale narration");
    turn.end("n");
    turn.begin("tool", "tool", { name: "lookup", args: "x" });
    turn.update("tool", { status: "ok" });
    turn.end("tool");

    turn.update("n", { text: "" });
    turn.begin("notice", "status", {
      text: "Final answer corrected; an earlier streamed preview was superseded.",
    });
    turn.end("notice");
    turn.finish();
    const frame = await waitForText(renderOnce, captureSpans, "superseded");

    const text = rows(frame).join("\n");
    expect(text).not.toContain("stale narration");
    expect(text).toContain("lookup");
    expect(text).toContain("superseded");
  } finally {
    renderer.destroy?.();
  }
});

test("an explicit empty terminal snapshot withdraws stale prose visibly", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness();
  try {
    turn.begin("a", "thinking", {});
    turn.append("a", "preview to withdraw");
    turn.end("a");
    turn.update("a", { text: "" });
    turn.begin("notice", "status", {
      text: "Streamed preview withdrawn; the final answer is empty.",
    });
    turn.end("notice");
    turn.finish();
    await renderOnce();

    const text = rows(captureSpans()).join("\n");
    expect(text).not.toContain("preview to withdraw");
    expect(text).toContain("Streamed preview withdrawn");
    expect(text).toContain("final answer is empty");
  } finally {
    renderer.destroy?.();
  }
});
