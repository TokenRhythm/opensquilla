// Notice rendering: command notices captured from Python render INSIDE the host
// frame as a severity glyph + theme-colored line (never raw ANSI on the
// terminal). Renders through the SAME noticeContent recipe main.mjs's notice
// handler consumes — so these tests exercise the shipped composition, including
// cross-theme color correctness and the live-recolor path for already-rendered
// notice nodes.
//
// Run with: bun test src/notice-render.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { noticeContent, recolorNoticeNodes } from "./ansiNotice.mjs";
import { applyTheme, THEME } from "./theme.mjs";

const ESC = "\u001b";

// The thin renderer-binding around noticeContent (mirrors only node creation;
// the recipe itself — blank drop, glyph, structured branch, token — is shipped).
function renderNotice(renderer, conversationBox, rawText, idx, registry = []) {
  const spec = noticeContent(rawText);
  if (!spec) return null;
  const node = new TextRenderable(renderer, {
    id: `notice-${idx}`,
    content: spec.content,
    fg: THEME[spec.token] ?? THEME.detailText,
  });
  registry.push({ node, token: spec.token });
  conversationBox.add(node);
  return node;
}

const rgb = (c) => [Math.round(c.r * 255), Math.round(c.g * 255), Math.round(c.b * 255)];

async function makeConversation(width) {
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width, height: 12 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 12,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  return { renderer, renderOnce, captureSpans, conversationBox };
}

async function renderNotices(width, lines) {
  const { renderer, renderOnce, captureSpans, conversationBox } = await makeConversation(width);
  lines.forEach((raw, i) => renderNotice(renderer, conversationBox, raw, i));
  await renderOnce();
  const frame = captureSpans();
  return { renderer, frame };
}

const lineText = (line) => line.spans.map((s) => s.text).join("");
const findLine = (frame, needle) => frame.lines.find((l) => lineText(l).includes(needle));

test("notices render with a severity glyph, clean text, and theme color", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, frame } = await renderNotices(70, [
    `${ESC}[31mUnknown command. Use /help.${ESC}[0m`,
    `${ESC}[38;2;245;102;0mcompacting context...${ESC}[0m`,
    `${ESC}[32mStarted new session${ESC}[0m`,
  ]);
  const all = frame.lines.map(lineText).join("\n");
  expect(all).toContain("✗ Unknown command. Use /help."); // error glyph + clean text
  expect(all).toContain("› compacting context..."); // accent (brand) glyph
  expect(all).toContain("✓ Started new session"); // success glyph
  expect(all).not.toContain("[31m"); // no raw ANSI survives -> no compact:->act: clipping

  // The error line is painted THEME.error (#FF6B6B = 255,107,107 on dark).
  const errLine = findLine(frame, "Unknown command");
  const span = errLine.spans.find((s) => s.text.trim().length > 0);
  expect(rgb(span.fg)).toEqual([255, 107, 107]);
  renderer.destroy?.();
});

test("the same notice recolors for a light theme (legible on light bg)", async () => {
  applyTheme("opensquilla-light");
  const { renderer, frame } = await renderNotices(70, [
    `${ESC}[31mUnknown command. Use /help.${ESC}[0m`,
  ]);
  const errLine = findLine(frame, "Unknown command");
  const span = errLine.spans.find((s) => s.text.trim().length > 0);
  // light THEME.error = #C2382E = 194,56,46 (a dark red, legible on the light bg)
  expect(rgb(span.fg)).toEqual([194, 56, 46]);
  renderer.destroy?.();
  applyTheme("opensquilla-dark");
});

test("table/panel border lines render without a glyph", async () => {
  applyTheme("opensquilla-dark");
  const { renderer, frame } = await renderNotices(70, ["│ model   deepseek-v4 │"]);
  const line = findLine(frame, "model");
  const text = lineText(line);
  expect(text).toContain("│ model   deepseek-v4 │");
  expect(text).not.toContain("·"); // structured lines skip the glyph
  renderer.destroy?.();
});

test("a live theme switch re-points already-rendered notice nodes", async () => {
  // Notice nodes capture their fg VALUE at creation, so main.mjs registers each
  // as { node, token } and re-points them via recolorNoticeNodes on every
  // /theme switch — otherwise a dark→light flip leaves dark-theme colors on
  // the new light background.
  applyTheme("opensquilla-dark");
  const { renderer, renderOnce, captureSpans, conversationBox } = await makeConversation(70);
  const registry = [];
  renderNotice(renderer, conversationBox, `${ESC}[31mUnknown command. Use /help.${ESC}[0m`, 0, registry);
  await renderOnce();

  applyTheme("opensquilla-light");
  recolorNoticeNodes(registry, THEME);
  await renderOnce();
  const frame = captureSpans();
  const errLine = findLine(frame, "Unknown command");
  const span = errLine.spans.find((s) => s.text.trim().length > 0);
  // The pre-rendered node now paints light THEME.error, not the stale dark red.
  expect(rgb(span.fg)).toEqual([194, 56, 46]);
  renderer.destroy?.();
  applyTheme("opensquilla-dark");
});
