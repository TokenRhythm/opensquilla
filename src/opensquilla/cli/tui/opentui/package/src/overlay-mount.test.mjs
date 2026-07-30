import assert from "node:assert/strict";
import test from "node:test";

import { createComposer } from "./composer.mjs";
import { THEME } from "./theme.mjs";
import { textWidth } from "./primitives.mjs";

// Minimal fake renderable that records its children by id, mirroring the
// add/remove/getChildren contract the composer relies on. Nodes carry the
// option bag so tests can assert positioning/zIndex.
// Mirrors the real `new BoxRenderable(renderer, options)` two-arg constructor;
// the composer always passes the renderer first, options second.
class FakeNode {
  constructor(_renderer, options = {}) {
    this.options = options;
    this.id = options.id;
    this.zIndex = options.zIndex ?? 0;
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

function makeHarness({ terminalWidth = 100, terminalHeight = 24 } = {}) {
  const keypressHandlers = [];
  const pasteHandlers = [];
  const cursorPositions = [];
  const scrolls = [];
  const sent = [];
  const renderer = {
    terminalWidth,
    terminalHeight,
    keyInput: {
      on(event, handler) {
        if (event === "keypress") keypressHandlers.push(handler);
        if (event === "paste") pasteHandlers.push(handler);
      },
    },
    setCursorPosition(x, y, visible) {
      cursorPositions.push({ x, y, visible });
    },
    requestRender() {},
  };
  const rootNode = new FakeNode(renderer, { id: "root" });
  const conversationBox = new FakeNode(renderer, { id: "conversation", zIndex: 0 });
  conversationBox.scrollBy = (delta) => scrolls.push(delta);
  const inputBox = new FakeNode(renderer, { id: "input-region", zIndex: 0 });
  const overlayLayer = new FakeNode(renderer, { id: "overlay-layer", zIndex: 100 });
  rootNode.add(conversationBox);
  rootNode.add(inputBox);
  rootNode.add(overlayLayer);

  const composer = createComposer({
    renderer,
    BoxRenderable: FakeNode,
    TextRenderable: FakeNode,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight: 6,
    sendHostMessage: (m) => sent.push(m),
  });
  composer.install();
  const press = (key) => keypressHandlers.forEach((handler) => handler(key));
  const paste = (text) =>
    pasteHandlers.forEach((handler) => handler({ bytes: new TextEncoder().encode(text) }));
  const type = (text) => {
    for (const ch of text) press({ name: ch === " " ? "space" : ch, sequence: ch });
  };
  const lastCursor = () => cursorPositions.at(-1);
  return {
    renderer, composer, press, paste, type, inputBox, overlayLayer, conversationBox,
    lastCursor, cursorPositions, scrolls, sent,
  };
}

function findDeep(node, id) {
  if (node.id === id) return node;
  for (const child of node.getChildren?.() ?? []) {
    const hit = findDeep(child, id);
    if (hit) return hit;
  }
  return null;
}

test("completion menu mounts on the overlay layer, never inside the footer box", () => {
  const { composer, press, inputBox, overlayLayer } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/compact", description: "compact", insert_text: "/compact " }],
  });

  press({ name: "/", sequence: "/" });

  assert.ok(
    findDeep(overlayLayer, "completion-menu"),
    "menu should be mounted on the overlay layer",
  );
  assert.equal(
    findDeep(inputBox, "completion-menu"),
    null,
    "menu must not be mounted inside the footer/input box",
  );
});

test("closing the menu removes it from the overlay layer", () => {
  const { composer, press, overlayLayer } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/compact", description: "compact", insert_text: "/compact " }],
  });

  press({ name: "/", sequence: "/" });
  assert.ok(findDeep(overlayLayer, "completion-menu"), "menu present after trigger");

  press({ name: "escape" });
  assert.equal(
    findDeep(overlayLayer, "completion-menu"),
    null,
    "menu node must be cleared from the overlay on close",
  );
});

test("re-rendering the active menu does not stack duplicate nodes", () => {
  const { composer, press, overlayLayer } = makeHarness();
  composer.setCompletionContext({
    catalog: [
      { label: "/compact", description: "compact", insert_text: "/compact " },
      { label: "/compress", description: "compress", insert_text: "/compress " },
    ],
  });

  press({ name: "/", sequence: "/" });
  press({ name: "c", sequence: "c" });
  press({ name: "down" });
  press({ name: "up" });

  const menus = overlayLayer
    .getChildren()
    .filter((child) => child.id === "completion-menu");
  assert.equal(menus.length, 1, "exactly one menu node should exist after several re-renders");
});

test("completion menu clips long rows to the menu body width", () => {
  const { composer, press, overlayLayer } = makeHarness({ terminalWidth: 72 });
  composer.setCompletionContext({
    catalog: [
      {
        label: "/cost",
        description: "Show current REPL session usage.",
        insert_text: "/cost ",
      },
      {
        label: "/compact",
        description: "Compact older context in the current session.",
        insert_text: "/compact ",
      },
      {
        label: "Cost",
        description: "Show current session usage and cost.",
        insert_text: "/cost",
      },
      {
        label: "/html-coder",
        description: "Expert HTML development skill for building web pages and forms.",
        insert_text: "use the html-coder skill: ",
      },
    ],
  });

  press({ name: "/", sequence: "/" });
  press({ name: "c", sequence: "c" });
  press({ name: "o", sequence: "o" });

  const menu = findDeep(overlayLayer, "completion-menu");
  const rowContents = menu.getChildren().map((child) => child.options.content);

  assert.ok(rowContents.some((content) => content.includes("/compact")));
  assert.ok(
    rowContents.every((content) => textWidth(content) <= 33),
    "row content must fit the 72-column menu body",
  );
});

test("on narrow terminals the menu narrows itself instead of clipping wider than its box", () => {
  // Below 55 columns the old fixed right-inset made the box inner width smaller
  // than the 16-cell clip floor, so rows wrapped inside the fixed-height box.
  const { composer, press, overlayLayer } = makeHarness({ terminalWidth: 50 });
  composer.setCompletionContext({
    catalog: [
      { label: "/compact-with-a-long-name", description: "desc", insert_text: "/compact " },
    ],
  });

  press({ name: "/", sequence: "/" });

  const menu = findDeep(overlayLayer, "completion-menu");
  assert.ok(menu, "menu renders on a 50-column terminal");
  // inner width = 50 - left(1) - right - border(2) - padding(2); right shrinks
  // so the inner width equals the 16-cell row clip.
  assert.equal(menu.options.right, 29);
  for (const child of menu.getChildren()) {
    assert.ok(textWidth(child.options.content) <= 16, "row must fit the shrunken box");
  }
});

test("rail inset constrains completion, composer wrapping, and hardware caret together", () => {
  const { composer, press, type, inputBox, overlayLayer, lastCursor } = makeHarness({ terminalWidth: 160 });
  // Mirrors the wide context controller: the final 36 cells belong to the rail.
  inputBox.right = 36;
  composer.rerender();
  composer.setCompletionContext({
    catalog: [{ label: "/compact", description: "compact session", insert_text: "/compact " }],
  });
  press({ name: "/", sequence: "/" });
  const menu = findDeep(overlayLayer, "completion-menu");
  assert.equal(menu.options.right, 70); // 36 rail + the normal 34-cell local inset

  press({ name: "escape" });
  press({ name: "c", ctrl: true }); // clear the slash draft before wrap testing
  type("a".repeat(130));
  const composerBox = findDeep(inputBox, "composer-box");
  const lines = composerBox.getChildren().map((child) => child.options.content);
  // surface 124 - composer chrome/margins 6 = 118 content cells
  assert.equal(lines[0].length, 118);
  assert.equal(lines[1].length, 13); // 12 chars plus the caret cell
  // Cursor coordinates are 1-based; the rail begins at 0-based x=124.
  assert.ok(lastCursor().x <= 121);
});

test("menu height is clamped to the rows above the footer on short terminals", () => {
  const { composer, press, overlayLayer } = makeHarness({ terminalHeight: 10 });
  composer.setCompletionContext({
    catalog: Array.from({ length: 6 }, (_, i) => ({
      label: `/cmd${i}`,
      description: "",
      insert_text: `/cmd${i} `,
    })),
  });

  press({ name: "/", sequence: "/" });

  const menu = findDeep(overlayLayer, "completion-menu");
  assert.ok(menu, "menu still renders when a few rows fit");
  // 10 rows - 6 footer rows = 4 rows available; 2 borders + 2 candidate rows.
  assert.equal(menu.options.height, 4);
  assert.equal(menu.getChildren().length, 2);
});

test("with no room above the footer the menu deactivates instead of going invisible-modal", () => {
  const { composer, press, overlayLayer, sent } = makeHarness({ terminalHeight: 8 });
  composer.setCompletionContext({
    catalog: [{ label: "/compact", description: "", insert_text: "/compact " }],
  });

  press({ name: "/", sequence: "/" });
  assert.equal(findDeep(overlayLayer, "completion-menu"), null, "no invisible menu");

  // Keys must behave as plain typing: Enter submits the literal text instead of
  // accept-submitting an invisible highlighted command.
  press({ name: "return" });
  const submit = sent.find((m) => m.type === "input.submit");
  assert.equal(submit?.text, "/");
});

test("caret motion re-derives the menu so accepts never splice a stale range", () => {
  const { composer, press, type, overlayLayer, sent } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/theme", description: "", insert_text: "/theme " }],
  });

  type("/th");
  assert.ok(findDeep(overlayLayer, "completion-menu"), "menu open on the token");

  press({ name: "a", ctrl: true }); // caret to line start: token no longer under caret
  assert.equal(
    findDeep(overlayLayer, "completion-menu"),
    null,
    "menu must close when the caret leaves the trigger token",
  );

  // Enter now submits the typed text — it must NOT accept a stale completion.
  press({ name: "return" });
  assert.equal(sent.find((m) => m.type === "input.submit")?.text, "/th");
});

test("Alt+Enter with an open menu inserts a newline instead of accepting", () => {
  const { composer, press, type, sent } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/theme", description: "", insert_text: "/theme " }],
  });

  type("/theme");
  press({ name: "return", meta: true }); // newline chord, menu open
  assert.equal(sent.some((m) => m.type === "input.submit"), false, "must not submit");

  type("x");
  press({ name: "return" });
  assert.equal(sent.find((m) => m.type === "input.submit")?.text, "/theme\nx");
});

test("Escape dismissal is sticky for the same token", () => {
  const { composer, press, type, overlayLayer } = makeHarness();
  composer.setCompletionContext({ files: ["src/a.ts"] });

  type("@a");
  assert.ok(findDeep(overlayLayer, "completion-menu"), "file menu open");

  press({ name: "escape" });
  assert.equal(findDeep(overlayLayer, "completion-menu"), null, "menu dismissed");

  type("b"); // same token: the menu must stay closed
  assert.equal(
    findDeep(overlayLayer, "completion-menu"),
    null,
    "menu must not reopen for the dismissed token",
  );

  type(" @c"); // a NEW token starts: the dismissal latch clears
  assert.ok(
    findDeep(overlayLayer, "completion-menu"),
    "menu reopens for a different token",
  );
});

test("submitting clears the Escape dismissal for the next slash command", () => {
  const { composer, press, type, overlayLayer, sent } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/theme", description: "", insert_text: "/theme " }],
  });

  type("/th");
  press({ name: "escape" }); // dismiss for this token
  press({ name: "return" }); // submit the literal "/th" (the menu is closed)
  assert.equal(sent.find((m) => m.type === "input.submit")?.text, "/th");

  // The next command starts at the same tokenStart (0); the latch must not
  // outlive the submitted input and suppress its menu.
  type("/th");
  assert.ok(
    findDeep(overlayLayer, "completion-menu"),
    "menu must reopen for the next command after submit",
  );
});

test("Ctrl+C clearing the input ends the Escape dismissal scope", () => {
  const { composer, press, type, overlayLayer } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/theme", description: "", insert_text: "/theme " }],
  });

  type("/th");
  press({ name: "escape" });
  press({ name: "c", ctrl: true }); // clear the dismissed draft

  type("/th");
  assert.ok(
    findDeep(overlayLayer, "completion-menu"),
    "menu must reopen after Ctrl+C reset the input",
  );
});

test("Tab-closing a no-matches menu does not latch the dismissal", () => {
  const { composer, press, type, overlayLayer } = makeHarness();
  composer.setCompletionContext({
    catalog: [{ label: "/exit", description: "", insert_text: "/exit " }],
  });

  type("/xy"); // zero matches: the menu shows its "no matches" shell
  press({ name: "tab" }); // closes it with no insert
  assert.equal(findDeep(overlayLayer, "completion-menu"), null, "menu closed");

  press({ name: "backspace" }); // "/x" matches /exit again
  assert.ok(
    findDeep(overlayLayer, "completion-menu"),
    "menu reopens once the token has matches again",
  );
});

test("an async completion.response follows the highlighted item, not its index", () => {
  const { composer, press, type, sent } = makeHarness();
  composer.setCompletionContext({ files: ["aaa", "bbb", "ccc"] });

  type("@");
  press({ name: "down" });
  press({ name: "down" }); // highlight "ccc" (index 2)

  // The response lands re-ranked: "ccc" is now first. The highlight must follow
  // the ITEM so the accept inserts what the user selected.
  composer.applyCompletionResponse({
    kind: "file",
    request_id: 1,
    items: [
      { label: "ccc", insert_text: "@ccc ", category: "file" },
      { label: "aaa", insert_text: "@aaa ", category: "file" },
      { label: "bbb", insert_text: "@bbb ", category: "file" },
    ],
  });

  press({ name: "tab" }); // accept
  press({ name: "return" });
  assert.equal(sent.find((m) => m.type === "input.submit")?.text, "@ccc ");
});

test("file completion rows do not render the path twice", () => {
  const { composer, type, overlayLayer } = makeHarness();
  composer.setCompletionContext({ files: ["src/app.mjs"] });

  type("@");
  const menu = findDeep(overlayLayer, "completion-menu");
  const row = menu.getChildren()[0].options.content;
  assert.equal(row.split("src/app.mjs").length - 1, 1, "path appears exactly once");
});

test("router strip values honor the semantic style from router.update", () => {
  const { composer, inputBox } = makeHarness();
  composer.setRouterState({
    model: "vendor/model-x", route: "fallback", saving: "-", context: "-", style: "warning",
  });
  let value = findDeep(inputBox, "router-route-value");
  assert.equal(value.options.fg, THEME.warning);

  composer.setRouterState({ style: "normal" });
  value = findDeep(inputBox, "router-route-value");
  assert.equal(value.options.fg, THEME.routeText);

  composer.setRouterState({ style: "dim" });
  value = findDeep(inputBox, "router-model-value");
  assert.equal(value.options.fg, THEME.detailText);
});

test("paste is ignored while the theme picker is modal", () => {
  const { composer, press, paste, type, sent } = makeHarness();
  composer.openThemePicker();
  paste("sneaky");
  press({ name: "return" }); // keep the theme, close the picker
  // Enter on an empty draft is a no-op (no submit frame), so prove the paste
  // never landed with a sentinel: the submission must be ONLY the sentinel.
  type("X");
  press({ name: "return" });
  assert.equal(sent.find((m) => m.type === "input.submit")?.text, "X");
});

test("PageUp/PageDown scroll the conversation, not the composer", () => {
  const { press, scrolls } = makeHarness();
  press({ name: "pageup" });
  press({ name: "pagedown" });
  assert.deepEqual(scrolls, [{ x: 0, y: -10 }, { x: 0, y: 10 }]);
});

test("composer clamps its box to a short terminal so the footer never overflows", () => {
  // main.mjs clamps inputBox.height with clampFooterHeight on short panes; the
  // composer must lay out against the SAME clamped height, not the fixed 6, or a
  // 3–5 row terminal overflows. composer height = clampFooterHeight(6, H) - 1.
  const tall = makeHarness({ terminalHeight: 24 });
  assert.equal(findDeep(tall.inputBox, "composer-box").options.height, 5);

  const short = makeHarness({ terminalHeight: 4 });
  const shortHeight = findDeep(short.inputBox, "composer-box").options.height;
  assert.equal(shortHeight, 3); // clamp(6,4)=4 → 4-1, not the unclamped 5
  assert.ok(shortHeight < 5);
});

test("composer shows the terminal cursor at the visual caret for IME popovers", () => {
  // visible MUST be true: macOS IME anchors the candidate popover to the VISIBLE
  // hardware cursor. With visible:false OpenTUI keeps the cursor hidden at home
  // and the popover drifts to a corner. x advances by display cells (CJK = 2).
  // Coordinates are 1-based (0-based cell + 1) to match OpenTUI's native cursor
  // convention (TextEditor.renderCursor passes screenX/Y + visual + 1).
  const { press, lastCursor } = makeHarness();

  assert.deepEqual(lastCursor(), { x: 4, y: 21, visible: true });

  press({ name: "h", sequence: "h" });
  press({ name: "i", sequence: "i" });
  assert.deepEqual(lastCursor(), { x: 6, y: 21, visible: true });

  press({ name: "backspace" });
  press({ name: "backspace" });
  press({ name: "你", sequence: "你" });
  press({ name: "好", sequence: "好" });
  assert.deepEqual(lastCursor(), { x: 8, y: 21, visible: true });

  press({ name: "return", option: true });
  press({ name: "a", sequence: "a" });
  assert.deepEqual(lastCursor(), { x: 5, y: 22, visible: true });
});

test("the cursor follows the caret onto soft-wrapped rows", () => {
  // 26-column terminal -> composer content width 20. A 25-char word wraps onto
  // a second row; the hardware cursor must sit on THAT row, not pinned to the
  // right edge of row 0 (which is where the pre-wrap math left it).
  const { type, lastCursor, inputBox } = makeHarness({ terminalWidth: 26 });
  type("a".repeat(25));

  const box = findDeep(inputBox, "composer-box");
  const lines = box.getChildren().map((child) => child.options.content);
  assert.deepEqual(lines, ["a".repeat(20), "aaaaa "]);
  // caret: row 1 col 5 -> x = 3 + 5 + 1, y = footerTop(18) + 2 + 1 + 1.
  assert.deepEqual(lastCursor(), { x: 9, y: 22, visible: true });
});

test("drafts taller than the composer window scroll to keep the caret visible", () => {
  const { type, press, lastCursor, inputBox } = makeHarness();
  const nl = { name: "return", meta: true };
  type("l1"); press(nl); type("l2"); press(nl); type("l3"); press(nl); type("l4");

  // 4 logical rows in a 3-row content window: the first row scrolls out and the
  // caret stays on the last visible row instead of being clamped onto other text.
  const box = findDeep(inputBox, "composer-box");
  const lines = box.getChildren().map((child) => child.options.content);
  assert.deepEqual(lines, ["l2", "l3", "l4 "]);
  assert.deepEqual(lastCursor(), { x: 6, y: 23, visible: true });
});

test("model strategy picker is modal and submits one control command", () => {
  const { composer, press, paste, overlayLayer, sent } = makeHarness();
  composer.openModelRoutingPicker({
    current: "direct",
    options: ["direct", "router", "ensemble"],
  });

  assert.ok(findDeep(overlayLayer, "model-routing-picker"));
  paste("must-not-enter-the-composer");
  press({ name: "down" });
  press({ name: "return" });

  assert.equal(findDeep(overlayLayer, "model-routing-picker"), null);
  assert.deepEqual(sent.at(-1), {
    type: "input.submit",
    text: "/router on",
    intent: "control",
  });
});

test("model picker starts with Auto and submits the selected session pin", () => {
  const { composer, press, paste, overlayLayer, sent } = makeHarness();
  composer.openModelPicker({
    current: null,
    options: [
      { id: "deepseek/deepseek-v4-pro", provider: "deepseek", context_window: 128000 },
      { id: "z-ai/glm-5.2", provider: "z-ai", context_window: 200000 },
    ],
  });

  const picker = findDeep(overlayLayer, "model-picker");
  assert.ok(picker);
  assert.match(findDeep(picker, "model-picker-row-0").options.content, /● Auto/);
  paste("must-not-enter-the-composer");
  press({ name: "down" });
  press({ name: "return" });

  assert.equal(findDeep(overlayLayer, "model-picker"), null);
  assert.deepEqual(sent.at(-1), {
    type: "input.submit",
    text: "/model deepseek/deepseek-v4-pro",
    intent: "control",
  });
});

test("model picker filters without mutating the composer and Escape cancels", () => {
  const { composer, type, press, overlayLayer, sent } = makeHarness();
  composer.openModelPicker({
    current: "deepseek/deepseek-v4-pro",
    options: [
      { id: "deepseek/deepseek-v4-pro", provider: "deepseek" },
      { id: "z-ai/glm-5.2", provider: "z-ai" },
    ],
  });

  type("glm");
  const picker = findDeep(overlayLayer, "model-picker");
  assert.ok(findDeep(picker, "model-picker-row-0").options.content.includes("z-ai/glm-5.2"));
  press({ name: "escape" });

  assert.equal(findDeep(overlayLayer, "model-picker"), null);
  assert.equal(sent.length, 0);
});

test("strategy changes during a turn render as next until the turn ends", () => {
  const { composer, inputBox } = makeHarness({ terminalWidth: 160 });
  const strategyText = () => findDeep(inputBox, "router-strategy")?.options?.content;

  composer.setModelRoutingState({ mode: "direct" });
  assert.equal(strategyText(), "direct");
  composer.setTurnActive({ active: true });
  composer.setModelRoutingState({ mode: "ensemble" });
  assert.equal(strategyText(), "next ensemble");
  composer.setTurnActive({ active: false });
  assert.equal(strategyText(), "ensemble");
});
