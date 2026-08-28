// Renderer-level regressions for the answer/markdown streaming path — the
// content users actually read. Pins (1) heading/link token colors from the
// tree-sitter capture registrations, (2) a live /theme switch repainting the
// already-rendered body, and (3) escape sequences split across stream deltas
// stripping cleanly under the incremental (O(delta)) strip.
//
// Markdown highlighting is asynchronous (a tree-sitter worker), so frames are
// polled briefly before asserting.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, MarkdownRenderable, SyntaxStyle } from "@opentui/core";

import { createAnswerBlock } from "./blocks/answerBlock.mjs";
import { registerThemeStyles } from "./syntaxTheme.mjs";
import { applyTheme, THEME } from "./theme.mjs";

const WIDTH = 70;
const HEIGHT = 18;

function flatText(frame) {
  return frame.lines.map((line) => line.spans.map((s) => s.text).join("")).join("\n");
}

function spanFgIs(span, hex) {
  const n = parseInt(hex.slice(1), 16);
  const want = [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  const fg = span.fg;
  if (!fg) return false;
  const got = [fg.r, fg.g, fg.b];
  return got.every((c, i) => Math.abs(c - want[i]) < 0.004);
}

async function mountAnswer() {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
  const { renderer } = setup;
  const syntaxStyle = SyntaxStyle.create();
  registerThemeStyles(syntaxStyle, THEME);
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
  const block = createAnswerBlock({ renderer, MarkdownRenderable, syntaxStyle, box, idPrefix: "blk" });
  block.begin();
  return { ...setup, block, syntaxStyle };
}

// Poll frames until a span satisfies the predicate (highlighting lands async).
async function waitForSpan(renderOnce, captureSpans, predicate, tries = 80) {
  for (let i = 0; i < tries; i += 1) {
    await renderOnce();
    const frame = captureSpans();
    for (const line of frame.lines) {
      for (const span of line.spans) {
        if (predicate(span)) return frame;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return null;
}

test("a streamed heading + link + fence renders with theme token colors", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    // Stream in fragments (the heading split mid-word) like a provider would.
    block.append("# Big Head");
    block.append("ing\n\nSee [the docs](https://example.com) now.\n\n");
    block.append("```js\nconst x = 1;\n```\n");
    block.end();

    const headingFrame = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("Big Heading") && spanFgIs(s, THEME.brandAccent),
    );
    expect(headingFrame).not.toBeNull(); // heading colored brandAccent, not body text

    const linkFrame = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("the docs") && spanFgIs(s, THEME.routeText),
    );
    expect(linkFrame).not.toBeNull(); // link label colored routeText

    expect(flatText(captureSpans())).toContain("const x = 1");
  } finally {
    renderer.destroy?.();
  }
});

test("a live theme switch repaints the already-rendered answer body", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block, syntaxStyle } = await mountAnswer();
  try {
    block.append("# Switch Me\n\nplain body\n");
    block.end();
    const dark = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("Switch Me") && spanFgIs(s, THEME.brandAccent),
    );
    expect(dark).not.toBeNull();

    // Mirror main.mjs's /theme flow: repopulate THEME, re-register the shared
    // syntaxStyle, then recolor the block.
    applyTheme("opensquilla-light");
    registerThemeStyles(syntaxStyle, THEME);
    block.recolor();
    const light = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("Switch Me") && spanFgIs(s, THEME.brandAccent),
    );
    expect(light).not.toBeNull(); // heading repainted in the LIGHT accent
  } finally {
    renderer.destroy?.();
    applyTheme("opensquilla-dark");
  }
});

test("an escape sequence split across deltas strips cleanly", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    // "\x1b[31m" arrives split mid-sequence; the strip must hold the prefix
    // back rather than leak "1m…" as visible text.
    block.append("plain \x1b[3");
    block.append("1mred\x1b[0m text\n");
    block.end();
    const frame = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("plain red text"),
    );
    expect(frame).not.toBeNull();
    expect(flatText(captureSpans())).not.toContain("[31m");
    expect(block.text).toBe("plain \x1b[31mred\x1b[0m text\n"); // raw kept intact
  } finally {
    renderer.destroy?.();
  }
});

test("an OSC 8 hyperlink whose ST terminator splits between ESC and backslash strips cleanly", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    // The delta boundary lands exactly between the ST's ESC and its "\": the
    // pending buffer ends in a bare ESC while the OSC opener before it is
    // still unterminated. The whole sequence must be held back — flushing up
    // to that last ESC would strip the opener in isolation and leak the URL
    // payload permanently into the rendered answer.
    block.append("click \x1b]8;;https://example.com\x1b");
    block.append("\\here\x1b]8;;\x1b\\ done\n");
    block.end();
    const frame = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("click here done"),
    );
    expect(frame).not.toBeNull();
    expect(flatText(captureSpans())).not.toContain("example.com");
    expect(flatText(captureSpans())).not.toContain("8;;");
  } finally {
    renderer.destroy?.();
  }
});

test("a DCS whose ST terminator splits between ESC and backslash never leaks its payload", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    block.append("alpha \x1bPhidden-payload\x1b");
    block.append("\\omega\n");
    block.end();
    const frame = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("alpha omega"),
    );
    expect(frame).not.toBeNull();
    expect(flatText(captureSpans())).not.toContain("hidden-payload");
  } finally {
    renderer.destroy?.();
  }
});

test("an OSC split mid-payload is held until its terminator arrives", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    block.append("x \x1b]8;;https://e");
    block.append("xample.com\x1b\\y z\n");
    block.end();
    const frame = await waitForSpan(renderOnce, captureSpans, (s) => s.text.includes("x y z"));
    expect(frame).not.toBeNull();
    expect(flatText(captureSpans())).not.toContain("xample");
  } finally {
    renderer.destroy?.();
  }
});

test("a trailing unterminated escape prefix is flushed on end", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    block.append("done\x1b[3"); // never terminated
    block.end();
    const frame = await waitForSpan(renderOnce, captureSpans, (s) => s.text.includes("done"));
    expect(frame).not.toBeNull();
  } finally {
    renderer.destroy?.();
  }
});

test("a terminal snapshot update replaces or clears an ended answer in place", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, block } = await mountAnswer();
  try {
    block.append("stale preview");
    block.end();
    block.update({ text: "canonical answer" });

    const corrected = await waitForSpan(
      renderOnce,
      captureSpans,
      (s) => s.text.includes("canonical answer"),
    );
    expect(corrected).not.toBeNull();
    expect(flatText(captureSpans())).not.toContain("stale preview");
    expect(block.text).toBe("canonical answer");

    block.update({ text: "" });
    await renderOnce();
    expect(flatText(captureSpans())).not.toContain("canonical answer");
    expect(block.text).toBe("");
  } finally {
    renderer.destroy?.();
  }
});
