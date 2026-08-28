// Renderer-level regressions for retained reasoning and intermediate rails.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, MarkdownRenderable, TextRenderable } from "@opentui/core";

import { createTurnView } from "./turnView.mjs";
import { createThinkingBlock } from "./blocks/thinkingBlock.mjs";
import { STATUS_PULSE_FRAMES } from "./theme.mjs";

function flatText(frame) {
  return frame.lines
    .map((line) => line.spans.map((span) => span.text).join(""))
    .join("\n");
}

async function createTurnHarness({ width = 52, height = 14 } = {}) {
  const setup = await createTestRenderer({ width, height });
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

function visibleLines(text) {
  return text.split("\n").filter((line) => line.trim());
}

test("the active reasoning marker settles into a completed Thought record", async () => {
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness();
  try {
    turn.begin("r1", "reasoning", {});
    turn.append("r1", "private reasoning text");
    await renderOnce();
    expect(flatText(captureSpans())).toContain("Thinking");

    turn.refreshPulse(1);
    await renderOnce();
    expect(flatText(captureSpans())).toContain(`${STATUS_PULSE_FRAMES.thinking[1]} Thinking`);

    turn.end("r1");
    turn.begin("tool1", "tool", { name: "web_search", args: "" });
    await renderOnce();

    const text = flatText(captureSpans());
    expect(text).not.toContain("Thinking");
    expect(text).toContain("Thought for");
    expect(text).toContain("web_search");
  } finally {
    renderer.destroy?.();
  }
});

test("intermediate narration keeps the timeline rail on every rendered line", async () => {
  const { renderer, renderOnce, captureSpans, turn } = await createTurnHarness({
    width: 42,
    height: 14,
  });
  try {
    turn.begin("thinking1", "thinking", {});
    turn.append(
      "thinking1",
      "first intermediate line that is intentionally long\nsecond line should keep rail\nthird line should keep rail",
    );
    await renderOnce();

    const lines = visibleLines(flatText(captureSpans()));
    const narrationLines = lines.filter(
      (line) =>
        line.includes("first intermediate") ||
        line.includes("second line") ||
        line.includes("third line"),
    );

    expect(narrationLines.length).toBe(3);
    for (const line of narrationLines) {
      expect(line.trimStart()).toMatch(/^[│✻]/);
    }
    expect(narrationLines[1].trimStart().startsWith("│")).toBe(true);
    expect(narrationLines[2].trimStart().startsWith("│")).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

async function mountThinking({ width, height = 14, contentWidth } = {}) {
  const setup = await createTestRenderer({ width, height });
  const { renderer } = setup;
  const box = new BoxRenderable(renderer, {
    id: "turn",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(box);
  const block = createThinkingBlock({ renderer, TextRenderable, box, idPrefix: "blk", contentWidth });
  return { ...setup, block };
}

test("long narration soft-wraps into continuation rows with no content loss", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountThinking({ width: 36 });
  try {
    block.append("alpha bravo charlie delta echo foxtrot golf hotel india juliett");
    await renderOnce();
    const text = flatText(captureSpans());
    // Every word survives (no one-row "…" clip) …
    for (const word of ["alpha", "charlie", "foxtrot", "india", "juliett"]) {
      expect(text).toContain(word);
    }
    expect(text).not.toContain("…");
    // … across multiple rows: the first carries the ✻ marker, continuations
    // indent under it.
    const rows = visibleLines(text);
    expect(rows.length).toBeGreaterThan(1);
    expect(rows[0].trimStart().startsWith("✻")).toBe(true);
    for (const row of rows.slice(1)) {
      expect(row.startsWith("   ")).toBe(true);
    }
  } finally {
    renderer.destroy?.();
  }
});

test("narration wraps to the transcript content width instead of rendering beneath a side rail", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountThinking({
    width: 100,
    contentWidth: () => 38,
  });
  try {
    block.append("alpha bravo charlie delta echo foxtrot golf hotel india juliett");
    await renderOnce();
    // This fits on one 100-column terminal row, but not in the 38-column
    // transcript remaining beside the wide context rail.
    expect(visibleLines(flatText(captureSpans())).length).toBeGreaterThan(1);
  } finally {
    renderer.destroy?.();
  }
});

test("completed long narration keeps a preview and deterministically expands every late delta", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountThinking({
    width: 70,
    height: 24,
  });
  const initial = Array.from({ length: 10 }, (_, i) => `narration line ${i + 1}`).join("\n");
  try {
    block.append(initial);
    block.end();
    await renderOnce();
    const collapsed = flatText(captureSpans());
    expect(collapsed).toContain("narration line 1");
    expect(collapsed).toContain("4 more lines");
    expect(collapsed).not.toContain("narration line 10");
    expect(block.rawText).toBe(initial);
    expect(block.hiddenLineCount).toBe(4);

    expect(block.toggleExpanded()).toBe(true);
    await renderOnce();
    expect(flatText(captureSpans())).toContain("narration line 10");

    block.append("\nlate narration line 11");
    await renderOnce();
    expect(block.rawText).toBe(`${initial}\nlate narration line 11`);
    expect(flatText(captureSpans())).toContain("late narration line 11");

    block.toggleExpanded(false);
    await renderOnce();
    expect(flatText(captureSpans())).toContain("5 more lines");
  } finally {
    renderer.destroy?.();
  }
});

test("a terminal snapshot replaces completed narration and can withdraw it without stale rows", async () => {
  const { renderer, renderOnce, captureSpans, block } = await mountThinking({
    width: 70,
    height: 24,
  });
  const stale = Array.from({ length: 10 }, (_, i) => `stale narration ${i + 1}`).join("\n");
  try {
    block.append(stale);
    block.end();
    await renderOnce();
    expect(flatText(captureSpans())).toContain("4 more lines");

    block.update({
      text: "canonical narration line 1\ncanonical narration line 2",
      expanded: true,
    });
    await renderOnce();
    const replaced = flatText(captureSpans());
    expect(replaced).toContain("canonical narration line 1");
    expect(replaced).toContain("canonical narration line 2");
    expect(replaced).not.toContain("stale narration");
    expect(replaced).not.toContain("more lines");
    expect(block.rawText).toBe("canonical narration line 1\ncanonical narration line 2");
    expect(block.isExpanded).toBe(true);

    block.update({ text: "" });
    await renderOnce();
    const cleared = flatText(captureSpans());
    expect(cleared).not.toContain("canonical narration");
    expect(cleared).not.toContain("stale narration");
    expect(cleared).not.toContain("more lines");
    expect(block.rawText).toBe("");
    expect(block.hiddenLineCount).toBe(0);

    block.relayout();
    block.toggleExpanded(false);
    await renderOnce();
    expect(visibleLines(flatText(captureSpans()))).toEqual([]);
  } finally {
    renderer.destroy?.();
  }
});

test("a resize re-wraps narration from the raw text at the new width", async () => {
  const { renderer, renderOnce, captureSpans, resize, block } = await mountThinking({
    width: 90,
  });
  const doResize = resize || ((w, h) => renderer.resize(w, h));
  try {
    block.append("one two three four five six seven eight nine ten eleven twelve");
    await renderOnce();
    const wide = visibleLines(flatText(captureSpans()));
    expect(wide.length).toBe(1);

    // Shrink: the single baked row must re-wrap into indented rows, keeping
    // every word visible (not stay clipped/wrapped to the stale 90-cell width).
    await doResize(40, 14);
    block.relayout();
    await renderOnce();
    const narrowText = flatText(captureSpans());
    const narrow = visibleLines(narrowText);
    expect(narrow.length).toBeGreaterThan(1);
    for (const word of ["one", "six", "twelve"]) {
      expect(narrowText).toContain(word);
    }
    for (const row of narrow.slice(1)) {
      expect(row.startsWith("   ")).toBe(true);
    }

    // Grow back: the continuation rows collapse into the original single row
    // (stale extra nodes are dropped, nothing stays clipped to 40 cells).
    await doResize(90, 14);
    block.relayout();
    await renderOnce();
    const wideAgain = visibleLines(flatText(captureSpans()));
    expect(wideAgain).toEqual(wide);
  } finally {
    renderer.destroy?.();
  }
});

test("a shrink relayout keeps every narration row above a later tool row", async () => {
  // The thinking block shares the turn's card body with every later in-card
  // block. A shrink re-wrap grows the narration's row count AFTER a tool row
  // was mounted below it — the new continuation rows must be inserted after
  // their predecessor row, not appended to the end of the card body, or the
  // narration renders split around the tool row.
  const { renderer, renderOnce, captureSpans, resize, turn } = await createTurnHarness({
    width: 90,
    height: 16,
  });
  const doResize = resize || ((w, h) => renderer.resize(w, h));
  try {
    turn.begin("think1", "thinking", {});
    turn.append("think1", "one two three four five six seven eight nine ten eleven twelve");
    turn.end("think1");
    turn.begin("tool1", "tool", { name: "web_search", args: "" });
    turn.update("tool1", { status: "ok" });
    turn.end("tool1");
    await renderOnce();

    await doResize(40, 16);
    turn.relayout();
    await renderOnce();

    const lines = flatText(captureSpans()).split("\n");
    const toolRow = lines.findIndex((line) => line.includes("web_search"));
    expect(toolRow).toBeGreaterThan(0);
    const narrationRows = [...lines.keys()].filter((r) =>
      ["one", "six", "eleven", "twelve"].some((w) => lines[r].includes(w)),
    );
    expect(narrationRows.length).toBeGreaterThan(1); // the shrink did re-wrap
    for (const r of narrationRows) {
      expect(r).toBeLessThan(toolRow); // never split around the tool row
    }
  } finally {
    renderer.destroy?.();
  }
});
