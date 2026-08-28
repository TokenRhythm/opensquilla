import assert from "node:assert/strict";
import test, { afterEach, beforeEach } from "node:test";

import { applyTheme, setColorMode, STATUS, THEME } from "./theme.mjs";
import { createTurnView } from "./turnView.mjs";
import { createBlock } from "./blockRegistry.mjs";
import { createPromptBlock } from "./blocks/promptBlock.mjs";
import { createToolBlock } from "./blocks/toolBlock.mjs";
import { createThinkingBlock } from "./blocks/thinkingBlock.mjs";
import { createReasoningBlock } from "./blocks/reasoningBlock.mjs";
import { createAnswerBlock } from "./blocks/answerBlock.mjs";
import { textWidth } from "./primitives.mjs";

// A live /theme switch mutates THEME/STATUS in place. Renderables captured their
// fg at creation, so a dark→light switch would leave prior transcript unreadable
// unless recolor() re-points every node at the updated tokens. These tests lock
// that contract at the node level with a minimal fake renderable.

class FakeNode {
  constructor(_renderer, options = {}) {
    Object.assign(this, options);
    this.children = [];
  }
  add(node) {
    this.children.push(node);
    return this.children.length;
  }
  remove(node) {
    this.children = this.children.filter((child) => child !== node);
  }
  getChildren() {
    return this.children;
  }
}

const renderer = { requestRender() {}, terminalWidth: 80, terminalHeight: 24 };

function findById(node, id) {
  for (const child of node.getChildren?.() ?? node.children ?? []) {
    if (child.id === id) return child;
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

beforeEach(() => {
  setColorMode("truecolor");
  applyTheme("opensquilla-dark");
});

afterEach(() => {
  setColorMode("truecolor");
  applyTheme("opensquilla-dark");
});

test("prompt block recolors its role, text, rail, and surface to the new theme tokens", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createPromptBlock({
    renderer, BoxRenderable: FakeNode, TextRenderable: FakeNode, box, idPrefix: "p",
  });
  block.begin({ text: "line one\nline two" });

  const darkRail = THEME.promptAccent;
  const darkText = THEME.promptText;
  const darkSurface = THEME.promptSurface;
  const body = findById(box, "p-body");
  const label = findById(box, "p-label");
  const content = findById(box, "p-content");
  assert.ok(body);
  assert.ok(label && content);
  assert.equal(body.borderColor, darkRail);
  assert.equal(body.backgroundColor, darkSurface);
  assert.equal(content.backgroundColor, darkSurface);
  assert.equal(label.content, "you");
  assert.equal(label.fg, darkRail);
  // Compact prompt: one role node + one content column, no card chrome.
  assert.equal(body.children.length, 2);
  assert.equal(findById(box, "p-top"), null);
  assert.equal(findById(box, "p-bot"), null);
  assert.ok(content.children.every((n) => n.fg === darkText));

  applyTheme("opensquilla-light");
  block.recolor();
  assert.notStrictEqual(THEME.promptAccent, darkRail); // the two themes genuinely differ
  assert.notStrictEqual(THEME.promptText, darkText);
  assert.notStrictEqual(THEME.promptSurface, darkSurface);
  assert.ok(content.children.every((n) => n.fg === THEME.promptText));
  assert.equal(label.fg, THEME.promptAccent);
  assert.equal(body.borderColor, THEME.promptAccent);
  assert.equal(body.backgroundColor, THEME.promptSurface);
  assert.equal(content.backgroundColor, THEME.promptSurface);
});

test("tool block recolor re-derives the run-state color for its current state", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createToolBlock({ renderer, TextRenderable: FakeNode, box, idPrefix: "t" });
  block.begin({ name: "grep" });
  const node = box.children[0];
  assert.equal(node.fg, STATUS.running);

  block.update({ status: "ok" });
  assert.equal(node.fg, STATUS.ok);

  applyTheme("opensquilla-light");
  block.recolor();
  // Still the OK color, but from the new palette (not the stale dark one).
  assert.equal(node.fg, STATUS.ok);
});

test("thinking block recolors in place without recreating its nodes", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createThinkingBlock({ renderer, TextRenderable: FakeNode, box, idPrefix: "th" });
  block.append("line one\nline two");
  const before = box.children.map((n) => n.id);
  const dark = THEME.thinkingAccent;
  assert.ok(box.children.length >= 2);
  assert.ok(box.children.every((n) => n.fg === dark));

  applyTheme("opensquilla-light");
  block.recolor();
  // Same node objects, same order — recolor must not remove/re-add (which would
  // re-append the lines after later blocks in a shared card body).
  assert.deepEqual(box.children.map((n) => n.id), before);
  assert.ok(box.children.every((n) => n.fg === THEME.thinkingAccent));
});

test("a streamed thinking delta reuses existing line nodes instead of recreating them", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createThinkingBlock({ renderer, TextRenderable: FakeNode, box, idPrefix: "th" });
  block.append("line one\nline tw");
  const firstNode = box.children[0];
  const lastNode = box.children[1];

  block.append("o grows");
  // The unchanged first line and the growing last line keep their node objects
  // and order — no per-delta remove()+add() churn or transcript reordering.
  assert.strictEqual(box.children[0], firstNode);
  assert.strictEqual(box.children[1], lastNode);
  assert.ok(lastNode.content.includes("line two grows"));
});

test("reasoning marker recolors to the new theme accent while mounted", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createReasoningBlock({ renderer, TextRenderable: FakeNode, box, idPrefix: "r" });
  block.begin({});
  const node = box.children[0];
  const dark = THEME.thinkingAccent;
  assert.equal(node.fg, dark);
  block.append("retained reasoning");
  block.end();
  // Completion collapses the body, but the semantic Thought marker stays
  // purple instead of becoming indistinguishable from a usage receipt.
  assert.equal(node.fg, THEME.thinkingAccent);

  applyTheme("opensquilla-light");
  block.recolor();
  assert.notStrictEqual(THEME.thinkingAccent, dark);
  assert.equal(node.fg, THEME.thinkingAccent);
});

test("answer block recolor explicitly refreshes the baked syntax spans", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  let refreshed = 0;
  class FakeMarkdown extends FakeNode {
    refreshStyles() { refreshed += 1; }
  }
  const block = createAnswerBlock({
    renderer, MarkdownRenderable: FakeMarkdown, syntaxStyle: {}, box, idPrefix: "a",
  });
  block.begin({});
  block.append("body text");

  applyTheme("opensquilla-light");
  block.recolor();
  const md = box.children[0];
  assert.equal(md.fg, THEME.text);
  // Chunk colors are resolved at build time, so recolor must force a span
  // rebuild explicitly — not rely on the base fg happening to differ between
  // the two themes.
  assert.equal(refreshed, 1);
});

test("an unknown block kind degrades to a dim recolorable fallback text block", () => {
  applyTheme("opensquilla-dark");
  const box = new FakeNode();
  const block = createBlock("holo-frame", { renderer, TextRenderable: FakeNode, box, idPrefix: "u" });
  block.begin({ text: "future content" });
  block.append(" plus\x1b[31m more");
  const node = box.children[0];
  assert.ok(node.content.includes("future content plus more"));
  assert.equal(node.fg, THEME.detailText);

  applyTheme("opensquilla-light");
  block.recolor();
  assert.equal(node.fg, THEME.detailText);
});

test("recolor preserves block order in the shared card body", () => {
  applyTheme("opensquilla-dark");
  const conversationBox = new FakeNode();
  const turn = createTurnView(
    {
      renderer,
      BoxRenderable: FakeNode,
      TextRenderable: FakeNode,
      MarkdownRenderable: FakeNode,
      syntaxStyle: {},
      conversationBox,
    },
    "y",
  );
  turn.begin("th", "thinking", {});
  turn.append("th", "reasoning one\nreasoning two");
  turn.begin("tl", "tool", { name: "grep" });

  const cardBody = findById(conversationBox.children[0], "turn-y-cardbody");
  const before = cardBody.children.map((c) => c.id);
  assert.ok(before.indexOf("turn-y-th-l0") < before.indexOf("turn-y-tl-node"));

  applyTheme("opensquilla-light");
  turn.recolor();
  // Thinking lines must still precede the tool row — order unchanged.
  assert.deepEqual(
    cardBody.children.map((c) => c.id),
    before,
  );
});

test("turn card chrome recolors on a theme switch", () => {
  applyTheme("opensquilla-dark");
  const conversationBox = new FakeNode();
  const turn = createTurnView(
    {
      renderer,
      BoxRenderable: FakeNode,
      TextRenderable: FakeNode,
      MarkdownRenderable: FakeNode,
      syntaxStyle: {},
      conversationBox,
    },
    "x",
  );
  turn.begin("b1", "tool", { name: "grep" }); // opens the card (header/body/gutter)

  const box = conversationBox.children[0];
  const darkFrame = THEME.answerFrame;
  const cardTop = findById(box, "turn-x-cardtop");
  const cardBody = findById(box, "turn-x-cardbody");
  assert.ok(cardTop && cardBody);
  assert.equal(cardTop.fg, darkFrame);
  assert.equal(cardBody.borderColor, darkFrame);

  applyTheme("opensquilla-light");
  turn.recolor();
  assert.notStrictEqual(THEME.answerFrame, darkFrame);
  assert.equal(cardTop.fg, THEME.answerFrame);
  assert.equal(cardBody.borderColor, THEME.answerFrame);
});

test("a completed turn settles its connected frame from active accent to muted history", () => {
  const conversationBox = new FakeNode();
  const turn = createTurnView(
    {
      renderer,
      BoxRenderable: FakeNode,
      TextRenderable: FakeNode,
      MarkdownRenderable: FakeNode,
      syntaxStyle: {},
      conversationBox,
    },
    "settled",
  );
  turn.begin("tool", "tool", { name: "grep", args: "needle" });
  const box = conversationBox.children[0];
  const cardTop = findById(box, "turn-settled-cardtop");
  const cardBody = findById(box, "turn-settled-cardbody");
  assert.equal(cardTop.fg, THEME.answerFrame);
  assert.equal(cardBody.borderColor, THEME.answerFrame);

  turn.update("tool", { status: "ok" });
  turn.end("tool");
  turn.finish();
  assert.equal(cardTop.fg, THEME.muted);
  assert.equal(cardBody.borderColor, THEME.muted);
});

test("turn headers follow the canonical agent label and refresh retained cards safely", () => {
  const conversationBox = new FakeNode();
  let label = "  Mira\x1b[31m Prime  ";
  const turn = createTurnView(
    {
      renderer,
      BoxRenderable: FakeNode,
      TextRenderable: FakeNode,
      MarkdownRenderable: FakeNode,
      syntaxStyle: {},
      conversationBox,
      contentWidth: () => 16,
      agentLabel: () => label,
    },
    "identity",
  );
  turn.begin("tool", "tool", { name: "probe" });
  const header = findById(conversationBox.children[0], "turn-identity-cardtop");
  assert.ok(header.content.startsWith("╭ Mira Prime"));
  assert.ok(!header.content.includes("\x1b"));
  assert.ok(textWidth(header.content) <= 16);

  label = "🦐 Very Long Canonical Agent Name";
  turn.refreshContext();
  assert.ok(header.content.startsWith("╭ 🦐"));
  assert.ok(header.content.endsWith("…"));
  assert.ok(textWidth(header.content) <= 16);

  label = "";
  turn.refreshContext();
  assert.equal(header.content, "╭ squilla");
});
