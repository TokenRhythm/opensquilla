// Tool-row rendering regressions for the opencode/codex alignment pass.
//
// A tool renders as one invocation line plus a compact connected detail rail.
// Every result delta and full argument payload remains retained behind the
// deterministic expansion API; completion flips the glyph to ✓/✗ in place.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createToolBlock, unifiedDiffSummary } from "./blocks/toolBlock.mjs";
import { STATUS, STATUS_PULSE_FRAMES } from "./theme.mjs";

const WIDTH = 60;
const HEIGHT = 12;

test("unified diff output exposes changed-file and line totals", () => {
  expect(unifiedDiffSummary(
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n-old\n+new\n+more",
  )).toEqual({ files: 1, added: 2, removed: 1 });
  expect(unifiedDiffSummary("ordinary command output")).toBeNull();
});

function flatText(frame) {
  return frame.lines.map((line) => line.spans.map((s) => s.text).join("")).join("\n");
}

// node.fg is parsed into an RGBA, so compare colors via a probe node that ran
// the same parse path (RGBA#equals is exact channel comparison).
function isColor(renderer, fg, hex) {
  const probe = new TextRenderable(renderer, { id: "probe", content: " ", fg: hex });
  return fg.equals(probe.fg);
}

async function mountTool(meta) {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
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
  const tool = createToolBlock({ renderer, TextRenderable, box, idPrefix: "blk" });
  tool.begin(meta);
  return { ...setup, tool };
}

test("a running tool shows a pulsing glyph + inline args in the run color", async () => {
  const { renderer, renderOnce, captureSpans, tool } = await mountTool({ name: "grep", args: "needle" });
  try {
    await renderOnce();
    let text = flatText(captureSpans());
    // args render INLINE after the name (opencode/codex), not on a separate line
    expect(text).toContain("grep needle");
    expect(text).toContain(STATUS_PULSE_FRAMES.tool[0]); // ◌ initial
    expect(isColor(renderer, tool.node.fg, STATUS.running)).toBe(true); // soft-orange while running

    // the external pulse animates the glyph in place
    tool.setGlyph(STATUS_PULSE_FRAMES.tool[1]);
    await renderOnce();
    expect(flatText(captureSpans())).toContain(`${STATUS_PULSE_FRAMES.tool[1]} grep needle`);
  } finally {
    renderer.destroy?.();
  }
});

test("a tool concatenates every result delta instead of discarding the later stream", async () => {
  const { renderer, renderOnce, captureSpans, tool } = await mountTool({ name: "glob", args: "*.mjs" });
  try {
    tool.append("42 files matched\n");
    tool.append("second result delta");
    await renderOnce();
    const text = flatText(captureSpans());
    expect(text).toContain("42 files matched");
    expect(text).toContain("second result delta");
    expect(tool.rawText).toBe("42 files matched\nsecond result delta");
  } finally {
    renderer.destroy?.();
  }
});

test("completed tool output previews with a hidden count and expands without payload loss", async () => {
  const setup = await createTestRenderer({ width: WIDTH, height: 28 });
  const { renderer, renderOnce, captureSpans } = setup;
  const box = new BoxRenderable(renderer, {
    id: "turn", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(box);
  const tool = createToolBlock({ renderer, TextRenderable, box, idPrefix: "retain" });
  tool.begin({ name: "exec", args: "probe" });
  const initial = "line one\nline two\nline three\nline four\nline five";
  tool.append(initial);
  tool.update({ status: "ok" });
  tool.end();
  try {
    await renderOnce();
    const collapsed = flatText(captureSpans());
    expect(collapsed).toContain("line one");
    expect(collapsed).toContain("line two");
    expect(collapsed).toContain("3 more output lines");
    expect(collapsed).not.toContain("line five");
    expect(tool.rawText).toBe(initial);
    expect(tool.hiddenLineCount).toBe(3);

    expect(tool.toggleExpanded()).toBe(true);
    await renderOnce();
    const expanded = flatText(captureSpans());
    expect(expanded).toContain("output · 5 lines");
    expect(expanded).toContain("line five");
    expect(expanded).toContain("collapse details");

    // A late protocol delta after block.end still belongs to this tool and is
    // immediately visible because the block remains expanded.
    tool.append("\nlate sixth line");
    await renderOnce();
    expect(tool.rawText).toBe(`${initial}\nlate sixth line`);
    expect(flatText(captureSpans())).toContain("late sixth line");
  } finally {
    renderer.destroy?.();
  }
});

test("full structured args are retained and disclosed independently from the inline summary", async () => {
  const setup = await createTestRenderer({ width: WIDTH, height: 24 });
  const { renderer, renderOnce, captureSpans } = setup;
  const box = new BoxRenderable(renderer, {
    id: "turn", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(box);
  const tool = createToolBlock({ renderer, TextRenderable, box, idPrefix: "args" });
  const fullArgs = { command: "printf hello", cwd: "/workspace", timeout: 30 };
  tool.begin({ name: "exec_command", args_summary: "printf hello", args_full: fullArgs });
  tool.update({ status: "ok" });
  tool.end();
  try {
    await renderOnce();
    const collapsed = flatText(captureSpans());
    expect(collapsed).toContain("exec_command printf hello");
    expect(collapsed).toContain("args ·");
    expect(collapsed).toContain("hidden");
    expect(tool.rawArgs).toContain('"timeout": 30');

    tool.toggleExpanded(true);
    await renderOnce();
    const expanded = flatText(captureSpans());
    expect(expanded).toContain('"cwd": "/workspace"');
    expect(expanded).toContain('"timeout": 30');
  } finally {
    renderer.destroy?.();
  }
});

test("a successful tool flips to ✓, recolors to ok, and appends a dim duration", async () => {
  const { renderer, renderOnce, captureSpans, tool } = await mountTool({ name: "read_file", args: "README.md" });
  try {
    tool.update({ status: "ok", duration: "0.2s" });
    await renderOnce();
    const text = flatText(captureSpans());
    expect(text).toContain("✓ read_file README.md · 0.2s");
    expect(isColor(renderer, tool.node.fg, STATUS.ok)).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

test("a failed tool flips to ✗ and recolors to error in place", async () => {
  const { renderer, renderOnce, captureSpans, tool } = await mountTool({ name: "bash", args: "pytest -q" });
  try {
    tool.append("AssertionError: expected 3");
    tool.update({ status: "error", duration: "1.4s" });
    await renderOnce();
    const text = flatText(captureSpans());
    expect(text).toContain("✗ bash pytest -q · 1.4s");
    expect(text).toContain("└ AssertionError: expected 3");
    expect(isColor(renderer, tool.node.fg, STATUS.error)).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

test("a resize re-wraps the retained result from raw text and expansion reveals every row", async () => {
  const setup = await createTestRenderer({ width: 100, height: HEIGHT });
  const { renderer, renderOnce, captureSpans, resize } = setup;
  const doResize = resize || ((w, h) => renderer.resize(w, h));
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
  const tool = createToolBlock({ renderer, TextRenderable, box, idPrefix: "blk" });
  tool.begin({ name: "read_file", args: "big.txt" });
  try {
    const raw = "0123456789".repeat(12);
    tool.append(raw);
    await renderOnce();
    const detailRows = (frame) =>
      frame.lines.map((l) => l.spans.map((s) => s.text).join(""))
        .filter((line) => /[├└]/.test(line));
    const wideCount = detailRows(captureSpans()).length;
    expect(tool.rawText).toBe(raw);
    expect(flatText(captureSpans())).not.toContain("…");

    // Shrink: rebuild rows from rawText. The compact live tail may hide earlier
    // rows, but reports their count rather than clipping bytes away.
    await doResize(40, HEIGHT);
    tool.relayout();
    await renderOnce();
    const narrow = flatText(captureSpans());
    expect(detailRows(captureSpans()).length).toBeGreaterThan(wideCount);
    expect(narrow).toContain("earlier");
    expect(tool.rawText).toBe(raw);

    tool.toggleExpanded(true);
    await renderOnce();
    expect(tool.hiddenLineCount).toBe(0);
    // The expanded rows concatenate back to all 120 payload characters after
    // removing the rail/section labels.
    expect(tool.rawText).toBe(raw);

    // Grow back: expansion stays deterministic and reflows from the same raw.
    await doResize(100, HEIGHT);
    tool.relayout();
    await renderOnce();
    expect(tool.rawText).toBe(raw);
    expect(flatText(captureSpans())).toContain("output · 2 lines");
  } finally {
    renderer.destroy?.();
  }
});
