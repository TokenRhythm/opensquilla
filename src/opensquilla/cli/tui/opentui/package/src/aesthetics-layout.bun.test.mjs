// Layout/spacing regression tests for the modern-TUI refinements:
//   - card chrome is width-INDEPENDENT (a short "╭ squilla" label and a bare
//     "╰ …" footer) so a scrollbar stealing a viewport column can never wrap
//     a full-width rule into stray dash rows;
//   - turns carry one blank line of vertical rhythm so they read as distinct
//     groups (proximity) and the conversation breathes;
//   - card open/close discipline (empty shells removed, prompts never seal a
//     streaming card, cancelled turns are marked) and the ScrollBox
//     manual-scroll contract the bottom-follow logic relies on.
//
// Run with: bun test src/aesthetics-layout.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createTurnView } from "./turnView.mjs";
import { applyTheme, THEME } from "./theme.mjs";

const frameText = (frame) => frame.lines.map((l) => l.spans.map((s) => s.text).join("")).join("\n");
const rgb = (c) => [Math.round(c.r * 255), Math.round(c.g * 255), Math.round(c.b * 255)];
const hexRgb = (hex) => {
  const value = Number.parseInt(String(hex).replace("#", ""), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
};

async function makeTurnHarness({ width = 60, height = 14 } = {}) {
  const setup = await createTestRenderer({ width, height });
  const { renderer } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  const turn = createTurnView(
    { renderer, BoxRenderable, TextRenderable, MarkdownRenderable: null, syntaxStyle: null, conversationBox },
    "t",
  );
  return { ...setup, conversationBox, turn };
}

test("turns are separated by a blank line of vertical rhythm", async () => {
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width: 50, height: 14 });
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
  const deps = {
    renderer,
    BoxRenderable,
    TextRenderable,
    MarkdownRenderable: null,
    syntaxStyle: null,
    conversationBox,
  };
  for (const id of ["A", "B"]) {
    createTurnView(deps, id).begin(`b${id}`, "tool", { name: `tool_${id}`, args: "" });
  }
  await renderOnce();
  const frame = captureSpans();
  const row = (r) => (frame.lines[r] ? frame.lines[r].spans.map((s) => s.text).join("") : "");

  // Find the two tool labels and assert a blank line sits between the turns.
  const aRow = [...Array(10).keys()].find((r) => row(r).includes("tool_A"));
  const bRow = [...Array(10).keys()].find((r) => row(r).includes("tool_B"));
  expect(aRow).toBeGreaterThanOrEqual(0);
  expect(bRow).toBeGreaterThan(aRow);
  // At least one fully-blank row separates the end of turn A from turn B.
  const between = [...Array(bRow - aRow).keys()].map((i) => row(aRow + 1 + i));
  expect(between.some((line) => line.trim() === "")).toBe(true);
  renderer.destroy?.();
});

test("a prompt is an explicit user-role surface, not a dim transcript line", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await makeTurnHarness({ width: 60, height: 12 });
  turn.begin("p", "prompt", { text: "first user line\nsecond user line" });
  await renderOnce();

  const frame = captureSpans();
  const rows = frame.lines.map((line) => ({
    text: line.spans.map((span) => span.text).join(""),
    spans: line.spans,
  }));
  const first = rows.find((row) => row.text.includes("first user line"));
  const second = rows.find((row) => row.text.includes("second user line"));
  expect(first).toBeTruthy();
  expect(second).toBeTruthy();
  expect(first.text).toContain("you");
  expect(second.text).not.toContain("you");

  const role = first.spans.find((span) => span.text.includes("you"));
  const firstText = first.spans.find((span) => span.text.includes("first user line"));
  const secondText = second.spans.find((span) => span.text.includes("second user line"));
  expect(rgb(role.fg)).toEqual(hexRgb(THEME.promptAccent));
  expect(rgb(firstText.fg)).toEqual(hexRgb(THEME.promptText));
  expect(rgb(secondText.fg)).toEqual(hexRgb(THEME.promptText));
  expect(rgb(firstText.bg)).toEqual(hexRgb(THEME.promptSurface));
  expect(rgb(secondText.bg)).toEqual(hexRgb(THEME.promptSurface));
  renderer.destroy?.();
});

test("card chrome is width-independent: a resize strands no dash rules", async () => {
  // The old full-width header rule was baked at begin() time and wrapped a
  // stray "─" run onto its own row whenever the viewport narrowed (e.g. the
  // scrollbar stealing a column). The chrome is now a fixed short label, so a
  // resize must leave it byte-identical with no dash-only lines anywhere.
  const { renderer, renderOnce, captureSpans, resize } = await createTestRenderer({
    width: 100,
    height: 16,
  });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: 16,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  const turn = createTurnView(
    { renderer, BoxRenderable, TextRenderable, MarkdownRenderable: null, syntaxStyle: null, conversationBox },
    "rx",
  );
  turn.begin("p", "prompt", { text: "hi there" });
  turn.begin("tl", "tool", { name: "grep", args: "x" }); // opens the squilla card
  turn.update("tl", { status: "ok" });
  turn.end("tl");
  turn.finish();
  await renderOnce();

  const lines = (f) => f.lines.map((l) => l.spans.map((s) => s.text).join("").trim());
  const strandedDash = (ls) => ls.some((line) => /^─+$/.test(line));

  // At width 100 the header is the short label, not a rule filled to 100 cells.
  expect(lines(captureSpans())).toContain("╭ squilla");
  expect(strandedDash(lines(captureSpans()))).toBe(false);

  // Shrink to 50 and reflow.
  const doResize = resize || ((w, h) => renderer.resize(w, h));
  await doResize(50, 16);
  conversationBox.height = 16;
  turn.relayout();
  await renderOnce();

  // Same label, still no stranded dash run wrapped onto its own line.
  const after = lines(captureSpans());
  expect(after).toContain("╭ squilla");
  expect(strandedDash(after)).toBe(false);
  renderer.destroy?.();
});

test("relayout skips entirely when the terminal width is unchanged", async () => {
  const { renderer, renderOnce, resize, turn } = await makeTurnHarness({ width: 80, height: 16 });
  turn.begin("tl", "tool", { name: "grep", args: "x" }); // opens the squilla card
  turn.append("tl", "a result preview that gets width-clipped"); // the └ corner
  await renderOnce();

  let renders = 0;
  const original = renderer.requestRender?.bind(renderer);
  renderer.requestRender = () => { renders += 1; original?.(); };

  // Same width (a height-only resize path): no text-buffer work at all.
  turn.relayout();
  expect(renders).toBe(0);

  // A real width change still re-clips block content (the result corner).
  const doResize = resize || ((w, h) => renderer.resize(w, h));
  await doResize(50, 16);
  const before = renders;
  turn.relayout();
  expect(renders).toBeGreaterThan(before);
  renderer.requestRender = original;
  await renderOnce();
  renderer.destroy?.();
});

test("a height-only resize recomputes the active reasoning peek", async () => {
  const { renderer, renderOnce, captureSpans, resize, turn } = await makeTurnHarness({
    width: 80,
    height: 40,
  });
  turn.begin("r", "reasoning", {});
  turn.append("r", Array.from({ length: 12 }, (_, index) => `line-${index}`).join("\n"));
  await renderOnce();
  const before = frameText(captureSpans());
  expect(before).toContain("line-11");
  expect(before).toContain("line-4"); // 40 rows => the maximum eight-line live peek

  const doResize = resize || ((w, h) => renderer.resize(w, h));
  await doResize(80, 15);
  turn.relayout();
  await renderOnce();

  const after = frameText(captureSpans());
  expect(after).toContain("line-11");
  expect(after).toContain("… 9 earlier lines"); // 15 rows => the minimum three-line peek
  expect(after).not.toContain("line-4");
  renderer.destroy?.();
});

test("a turn cancelled during reasoning keeps the Thought record in its card", async () => {
  // Cancel during extended thinking: the reasoning block settles to its
  // "Thought for Ns" record and bounded preview, so the card keeps a real body
  // and closes into a footer carrying both cancel marker and usage receipt.
  const { renderer, renderOnce, captureSpans, turn } = await makeTurnHarness();
  turn.begin("r1", "reasoning", {});
  turn.append("r1", "weighing the options");
  turn.end("r1");
  turn.begin("u1", "usage", { text: "in 10 / out 0" });
  turn.end("u1");
  turn.finish(true);
  await renderOnce();
  const text = frameText(captureSpans());
  expect(text).toContain("╭ squilla");
  expect(text).toContain("Thought for");
  expect(text).toContain("weighing the options");
  const footer = text.split("\n").find((line) => line.includes("╰"));
  expect(footer).toContain("cancelled");
  expect(footer).toContain("in 10 / out 0");
  renderer.destroy?.();
});

test("an empty card shell (no surviving body rows) still drops its chrome", async () => {
  // An unknown block kind that renders nothing must not leave a framed void:
  // the fallback block only mounts nodes when content arrives, so a card whose
  // body kept no children closes by dropping the chrome — and the usage
  // receipt still renders as a plain standalone row.
  const { renderer, renderOnce, captureSpans, turn } = await makeTurnHarness();
  turn.begin("x1", "future-kind", {}); // fallback block, no content appended
  turn.end("x1");
  turn.begin("u1", "usage", { text: "in 10 / out 0" });
  turn.end("u1");
  turn.finish(false);
  await renderOnce();
  const text = frameText(captureSpans());
  expect(text).not.toContain("╭"); // no stranded header
  expect(text).not.toContain("╰"); // no footer wrapping an empty body
  expect(text).toContain("in 10 / out 0"); // the receipt still renders
  renderer.destroy?.();
});

test("turn.end with cancelled=true appends a warning cancel marker; a normal finish does not", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, turn } = await makeTurnHarness();
  turn.begin("tl", "tool", { name: "grep", args: "x" });
  turn.update("tl", { status: "ok" });
  turn.end("tl");
  turn.finish();
  await renderOnce();
  expect(frameText(captureSpans())).not.toContain("cancelled"); // normal turns unchanged
  turn.finish(true); // late cancel signal is still honored once
  await renderOnce();
  const frame = captureSpans();
  const line = frame.lines.find((l) => l.spans.map((s) => s.text).join("").includes("cancelled"));
  expect(line).toBeTruthy();
  const span = line.spans.find((s) => s.text.includes("cancelled"));
  const warningProbe = new TextRenderable(renderer, { id: "warning-probe", content: " ", fg: THEME.warning });
  // Assert the semantic warning token in the active color mode. In NO_COLOR /
  // TERM=dumb the same token intentionally quantizes to monochrome.
  expect(rgb(span.fg)).toEqual(rgb(warningProbe.fg));
  renderer.destroy?.();
});

test("a prompt block never seals an open card; only usage closes it", async () => {
  // A queued submission's echo can land while the assistant card is still
  // streaming; the prompt kind must not draw the card footer under it.
  const { renderer, renderOnce, captureSpans, turn } = await makeTurnHarness();
  const footers = () =>
    frameText(captureSpans()).split("\n").filter((l) => l.trimStart().startsWith("╰"));
  turn.begin("tl", "tool", { name: "grep", args: "x" }); // opens the squilla card
  turn.begin("p1", "prompt", { text: "queued question" });
  await renderOnce();
  // The prompt block is chrome-free and the assistant card is still open, so
  // no ╰ footer exists anywhere yet.
  expect(footers()).toHaveLength(0);
  turn.begin("u1", "usage", { text: "in 5 / out 2" });
  await renderOnce();
  // The trailing usage summary closed the card into exactly one footer, and
  // the receipt rides on that footer line instead of a row below it.
  const closed = footers();
  expect(closed).toHaveLength(1);
  expect(closed[0]).toContain("in 5 / out 2");
  renderer.destroy?.();
});
