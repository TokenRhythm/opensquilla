// Composer input behavior, driven through real keypresses:
//   - a single keystroke must only insert real text, never control bytes
//     (Tab with no menu, ESC sequences from unhandled special keys);
//   - paste is sanitized but preserves line structure (including CR-encoded
//     newlines from real terminals and tmux);
//   - readline-style editing, grapheme-aware stepping, history recall, the
//     kill ring, and the wrapped-caret layout model (composerLayout).
//
// Run with: bun test src/composer-input.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { composerLayout, createComposer } from "./composer.mjs";
import { cellWidth, textWidth } from "./primitives.mjs";
import { applyTheme } from "./theme.mjs";

const ESC = "\x1b";

async function setupComposer(callbacks = {}) {
  applyTheme("opensquilla-dark");
  const sent = [];
  const { renderer } = await createTestRenderer({ width: 50, height: 10 });
  const mk = (id, height) => {
    const box = new BoxRenderable(renderer, {
      id, position: "absolute", left: 0, right: 0, bottom: 0, height,
    });
    renderer.root.add(box);
    return box;
  };
  const conversationBox = mk("conversation", 4);
  const inputBox = mk("input-region", 6);
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(overlayLayer);
  const composer = createComposer({
    renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, overlayLayer,
    footerHeight: 6, sendHostMessage: (m) => sent.push(m),
    ...callbacks,
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  return { renderer, composer, sent };
}

test("Ctrl+L repaints and Ctrl+G jumps to the latest transcript row", async () => {
  let redraws = 0;
  let jumps = 0;
  const { renderer } = await setupComposer({
    onFullRedraw: () => { redraws += 1; },
    onJumpToLatest: () => { jumps += 1; },
  });
  renderer.keyInput.emit("keypress", { name: "l", ctrl: true, sequence: "\x0c" });
  renderer.keyInput.emit("keypress", { name: "g", ctrl: true, sequence: "\x07" });
  expect(redraws).toBe(1);
  expect(jumps).toBe(1);
  renderer.destroy?.();
});

async function submittedAfter(keys) {
  const { renderer, sent } = await setupComposer();
  for (const key of keys) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", { name: "return" });
  const text = sent.find((m) => m.type === "input.submit")?.text;
  renderer.destroy?.();
  return text;
}

test("Tab with no completion menu does not insert a literal tab", async () => {
  const text = await submittedAfter([
    { name: "h", sequence: "h" },
    { name: "i", sequence: "i" },
    { name: "tab", sequence: "\t" },
  ]);
  expect(text).toBe("hi"); // no trailing control byte got submitted
});

test("unhandled special keys never leak control/escape bytes into the input", async () => {
  const text = await submittedAfter([
    { name: "a", sequence: "a" },
    { name: "insert", sequence: `${ESC}[2~` }, // ESC sequence from an unhandled key
    { name: "f5", sequence: `${ESC}[15~` },
    { name: "b", sequence: "b" },
  ]);
  expect(text).toBe("ab");
});

test("normal printable typing (incl. space) is unaffected", async () => {
  const text = await submittedAfter(
    [..."hello world"].map((c) => ({ name: c === " " ? "space" : c, sequence: c })),
  );
  expect(text).toBe("hello world");
});

async function pastedThenSubmitted(text) {
  const { renderer, sent } = await setupComposer();
  renderer.keyInput.emit("paste", { bytes: new TextEncoder().encode(text) });
  renderer.keyInput.emit("keypress", { name: "return" });
  const submitted = sent.find((m) => m.type === "input.submit")?.text;
  renderer.destroy?.();
  return submitted;
}

test("pasted ANSI/terminal output is stripped of escape and control bytes", async () => {
  const submitted = await pastedThenSubmitted(`a${ESC}[31mred${ESC}[0m b`);
  expect(submitted).toBe("ared b"); // colors removed
  expect(submitted.includes(ESC)).toBe(false); // no raw control bytes submitted
});

test("paste preserves newlines and tabs (multi-line / indented paste)", async () => {
  const submitted = await pastedThenSubmitted("line1\nline2\tindented");
  expect(submitted).toBe("line1\nline2\tindented");
});

test("CR-encoded pasted newlines (real terminals, tmux) survive as line breaks", async () => {
  // Terminal emulators and tmux paste-buffer transmit pasted LF as CR; the
  // control-byte strip must not swallow them and concatenate the lines.
  expect(await pastedThenSubmitted("line1\rline2")).toBe("line1\nline2");
  expect(await pastedThenSubmitted("a\r\nb")).toBe("a\nb");
});

const ctrl = (name) => ({ name, ctrl: true });
const typed = (s) => [...s].map((c) => ({ name: c === " " ? "space" : c, sequence: c }));

test("Ctrl+A/Ctrl+E jump to the start/end of the line", async () => {
  // Ctrl+A moves to line start (the inserted X lands first)...
  expect(await submittedAfter([...typed("hello"), ctrl("a"), { name: "X", sequence: "X" }])).toBe("Xhello");
  // ...and Ctrl+E moves back to the end.
  expect(
    await submittedAfter([...typed("hello"), ctrl("a"), ctrl("e"), { name: "!", sequence: "!" }]),
  ).toBe("hello!");
});

test("Home and End move to the start/end of the line", async () => {
  expect(
    await submittedAfter([
      ...typed("hello"),
      { name: "home", sequence: `${ESC}[H` },
      { name: "X", sequence: "X" },
    ]),
  ).toBe("Xhello");
  expect(
    await submittedAfter([
      ...typed("hello"),
      { name: "home", sequence: `${ESC}[H` },
      { name: "end", sequence: `${ESC}[F` },
      { name: "!", sequence: "!" },
    ]),
  ).toBe("hello!");
});

test("Ctrl+W and Alt+Backspace delete the previous word", async () => {
  expect(await submittedAfter([...typed("hello world"), ctrl("w")])).toBe("hello ");
  expect(await submittedAfter([...typed("foo bar"), { name: "backspace", meta: true }])).toBe("foo ");
});

test("Ctrl+U cuts to line start and Ctrl+K cuts to line end", async () => {
  // A fully-cut draft cannot be observed via submit (blank Enter is a no-op),
  // so type a sentinel after the cut: only the sentinel must remain.
  const X = { name: "X", sequence: "X" };
  expect(await submittedAfter([...typed("hello world"), ctrl("u"), X])).toBe("X");
  expect(await submittedAfter([...typed("hello world"), ctrl("a"), ctrl("k"), X])).toBe("X");
});

test("Ctrl+Y yanks the last kill back at the caret", async () => {
  // Ctrl+W kills "world"; two yanks re-insert it twice.
  expect(
    await submittedAfter([...typed("hello world"), ctrl("w"), ctrl("y"), ctrl("y")]),
  ).toBe("hello worldworld");
  // Ctrl+U kill is recoverable the same way.
  expect(await submittedAfter([...typed("draft"), ctrl("u"), ctrl("y")])).toBe("draft");
  // Ctrl+Y with nothing killed is a no-op.
  expect(await submittedAfter([...typed("ab"), ctrl("y")])).toBe("ab");
});

test("line editing acts on the current line in a multi-line draft", async () => {
  // Alt+Enter inserts a newline; Ctrl+A then goes to the start of the SECOND line.
  const keys = [
    ...typed("ab"),
    { name: "return", meta: true },
    ...typed("cd"),
    ctrl("a"),
    { name: "X", sequence: "X" },
  ];
  expect(await submittedAfter(keys)).toBe("ab\nXcd");
});

const alt = (name) => ({ name, meta: true });

test("Ctrl+Left / Ctrl+Right move the caret by word", async () => {
  const X = { name: "X", sequence: "X" };
  // From the end, Ctrl+Left lands at the start of "bar".
  expect(await submittedAfter([...typed("foo bar"), ctrl("left"), X])).toBe("foo Xbar");
  // Twice lands at the start of "foo".
  expect(await submittedAfter([...typed("foo bar"), ctrl("left"), ctrl("left"), X])).toBe("Xfoo bar");
  // From the start (Ctrl+A), Ctrl+Right lands at the end of "foo".
  expect(await submittedAfter([...typed("foo bar"), ctrl("a"), ctrl("right"), X])).toBe("fooX bar");
});

test("Alt+B / Alt+F also move the caret by word", async () => {
  const X = { name: "X", sequence: "X" };
  expect(await submittedAfter([...typed("foo bar"), alt("b"), X])).toBe("foo Xbar");
  expect(await submittedAfter([...typed("foo bar"), ctrl("a"), alt("f"), X])).toBe("fooX bar");
});

test("Alt+Left / Alt+Right (modified CSI arrows) move the caret by word", async () => {
  const X = { name: "X", sequence: "X" };
  expect(await submittedAfter([...typed("foo bar"), alt("left"), X])).toBe("foo Xbar");
  expect(await submittedAfter([...typed("foo bar"), ctrl("a"), alt("right"), X])).toBe("fooX bar");
});

test("plain Left still moves a single character (word movement needs a modifier)", async () => {
  expect(await submittedAfter([...typed("abc"), { name: "left" }, { name: "X", sequence: "X" }])).toBe("abXc");
});

test("the Delete key forward-deletes the character at the caret", async () => {
  // Ctrl+A to the start, then Delete removes the first character.
  expect(await submittedAfter([...typed("abc"), ctrl("a"), { name: "delete" }])).toBe("bc");
  // Delete at the end of the input is a no-op.
  expect(await submittedAfter([...typed("abc"), { name: "delete" }])).toBe("abc");
});

test("Alt+D and Ctrl+Delete delete the next word", async () => {
  expect(await submittedAfter([...typed("foo bar"), ctrl("a"), alt("d")])).toBe(" bar");
  expect(await submittedAfter([...typed("foo bar"), ctrl("a"), { name: "delete", ctrl: true }])).toBe(" bar");
});

// ---- kitty keypad aliases ---------------------------------------------------

test("numpad Enter (kitty kpenter) submits like the main Enter key", async () => {
  const { renderer, sent } = await setupComposer();
  for (const key of typed("hi")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", { name: "kpenter", sequence: `${ESC}[57414u` });
  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("hi");
  renderer.destroy?.();
});

test("kitty keypad navigation keys alias onto their base names", async () => {
  // kphome behaves as Home; kpdelete as forward Delete.
  expect(
    await submittedAfter([
      ...typed("abc"),
      { name: "kphome", sequence: `${ESC}[57423u` },
      { name: "kpdelete", sequence: `${ESC}[57426u` },
    ]),
  ).toBe("bc");
  // kpleft moves one character left.
  expect(
    await submittedAfter([
      ...typed("abc"),
      { name: "kpleft", sequence: `${ESC}[57417u` },
      { name: "X", sequence: "X" },
    ]),
  ).toBe("abXc");
});

// ---- modified space / modified Enter ---------------------------------------

test("Ctrl+Space and Alt+Space do not insert a stray space", async () => {
  expect(
    await submittedAfter([...typed("ab"), { name: "space", ctrl: true, sequence: " " }]),
  ).toBe("ab");
  expect(await submittedAfter([...typed("ab"), { name: "space", meta: true, sequence: " " }])).toBe("ab");
});

test("Shift+Enter inserts a newline in terminals that can report it", async () => {
  expect(
    await submittedAfter([...typed("ab"), { name: "return", shift: true }, ...typed("cd")]),
  ).toBe("ab\ncd");
});

// ---- grapheme-cluster editing ----------------------------------------------

test("backspace removes a whole emoji family / flag, not one code point", async () => {
  // ZWJ family: one backspace deletes the entire cluster (only the sentinel
  // typed afterwards remains — an emptied draft cannot be observed via submit).
  const X = { name: "X", sequence: "X" };
  expect(
    await submittedAfter([{ name: "👨‍👩‍👧", sequence: "👨‍👩‍👧" }, { name: "backspace" }, X]),
  ).toBe("X");
  // Regional-indicator flag: no lone half survives.
  expect(
    await submittedAfter([{ name: "🇺🇸", sequence: "🇺🇸" }, { name: "backspace" }, X]),
  ).toBe("X");
});

test("arrows step over grapheme clusters instead of into them", async () => {
  // Left steps over the family cluster in one keystroke, landing after "a".
  expect(
    await submittedAfter([
      ...typed("a"),
      { name: "👨‍👩‍👧", sequence: "👨‍👩‍👧" },
      { name: "left" },
      { name: "X", sequence: "X" },
    ]),
  ).toBe("aX👨‍👩‍👧");
  // Delete at the cluster removes it whole.
  expect(
    await submittedAfter([
      ...typed("a"),
      { name: "👨‍👩‍👧", sequence: "👨‍👩‍👧" },
      { name: "left" },
      { name: "delete" },
    ]),
  ).toBe("a");
});

// ---- control-key host messages ----------------------------------------------

test("Ctrl+C with text clears the draft without cancelling the turn", async () => {
  const { renderer, sent } = await setupComposer();
  for (const key of typed("abc")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", { name: "c", ctrl: true });
  expect(sent.some((m) => m.type === "input.cancel")).toBe(false);
  // The cleared draft cannot be observed via submit (blank Enter is a no-op):
  // type a sentinel and submit — only the sentinel proves "abc" is gone.
  renderer.keyInput.emit("keypress", { name: "X", sequence: "X" });
  renderer.keyInput.emit("keypress", { name: "return" });
  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("X");
  renderer.destroy?.();
});

test("Ctrl+C on an empty input sends exactly one input.cancel", async () => {
  const { renderer, sent } = await setupComposer();
  renderer.keyInput.emit("keypress", { name: "c", ctrl: true });
  expect(sent.filter((m) => m.type === "input.cancel").length).toBe(1);
  renderer.destroy?.();
});

test("Ctrl+D sends input.eof and Escape sends input.cancel", async () => {
  const { renderer, sent } = await setupComposer();
  renderer.keyInput.emit("keypress", { name: "d", ctrl: true });
  expect(sent.some((m) => m.type === "input.eof")).toBe(true);
  renderer.keyInput.emit("keypress", { name: "escape" });
  expect(sent.some((m) => m.type === "input.cancel")).toBe(true);
  renderer.destroy?.();
});

// ---- input history ------------------------------------------------------------

const up = { name: "up" };
const down = { name: "down" };
const enter = { name: "return" };

async function historyComposer(entries) {
  const setup = await setupComposer();
  for (const entry of entries) {
    for (const key of typed(entry)) setup.renderer.keyInput.emit("keypress", key);
    setup.renderer.keyInput.emit("keypress", enter);
  }
  return setup;
}

function lastSubmitted(sent) {
  return sent.filter((m) => m.type === "input.submit").at(-1)?.text;
}

test("Up recalls previous submissions, oldest last", async () => {
  const { renderer, sent } = await historyComposer(["one", "two"]);
  renderer.keyInput.emit("keypress", up);
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("two");
  renderer.keyInput.emit("keypress", up);
  renderer.keyInput.emit("keypress", up);
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("one");
  renderer.destroy?.();
});

test("Down returns to the in-progress draft after browsing history", async () => {
  const { renderer, sent } = await historyComposer(["sent"]);
  for (const key of typed("wip")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", up); // recall "sent" (draft saved)
  renderer.keyInput.emit("keypress", down); // back to the draft
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("wip");
  renderer.destroy?.();
});

test("consecutive identical submissions are deduped in history", async () => {
  const { renderer, sent } = await historyComposer(["same", "same"]);
  renderer.keyInput.emit("keypress", up);
  renderer.keyInput.emit("keypress", up); // only one entry exists; stays on it
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("same");
  renderer.destroy?.();
});

test("backspacing a recalled entry detaches from browsing (Down keeps the edit)", async () => {
  const { renderer, sent } = await historyComposer(["alpha"]);
  renderer.keyInput.emit("keypress", up); // recall "alpha"
  renderer.keyInput.emit("keypress", { name: "backspace" }); // edit -> detach
  renderer.keyInput.emit("keypress", down); // must NOT clobber the edit
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("alph");
  renderer.destroy?.();
});

test("re-entering history browse after editing a recall preserves the saved draft", async () => {
  const { renderer, sent } = await historyComposer(["alpha"]);
  for (const key of typed("keep me")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", up); // recall "alpha", draft "keep me" saved
  for (const key of typed("X")) renderer.keyInput.emit("keypress", key); // edit recall
  renderer.keyInput.emit("keypress", up); // browse again — draft must survive
  renderer.keyInput.emit("keypress", down); // through the stashed edit ("alphaX")…
  renderer.keyInput.emit("keypress", down); // …back to the draft slot
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("keep me");
  renderer.destroy?.();
});

test("an edited recall re-enters history as the newest entry instead of being lost", async () => {
  // Shell-like: editing a recalled entry then browsing again keeps the edit —
  // it is appended to history as the newest entry, so Down passes back through
  // it on the way to the draft. Neither the edit nor the draft is discarded.
  const { renderer, sent } = await historyComposer(["alpha"]);
  for (const key of typed("keep me")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", up); // recall "alpha" (draft saved)
  for (const key of typed("X")) renderer.keyInput.emit("keypress", key); // edit -> "alphaX"
  renderer.keyInput.emit("keypress", up); // re-enter browse: the edit is stashed, "alpha" shows
  renderer.keyInput.emit("keypress", down); // Down recalls the stashed edit
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("alphaX");
  renderer.destroy?.();
});

// ---- composer.set caret preservation ----------------------------------------

test("composer.set without a text field keeps the caret where the user put it", async () => {
  const { renderer, composer, sent } = await setupComposer();
  for (const key of typed("hello")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", { name: "left" });
  renderer.keyInput.emit("keypress", { name: "left" });
  // Turn-boundary frames carry only `disabled` — the caret must not move.
  composer.setComposerState({ type: "composer.set", disabled: true });
  composer.setComposerState({ type: "composer.set", disabled: false });
  for (const key of typed("X")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("helXlo");
  renderer.destroy?.();
});

test("composer.set WITH text still places the caret at the end of the new text", async () => {
  const { renderer, composer, sent } = await setupComposer();
  composer.setComposerState({ type: "composer.set", text: "reset" });
  for (const key of typed("!")) renderer.keyInput.emit("keypress", key);
  renderer.keyInput.emit("keypress", enter);
  expect(lastSubmitted(sent)).toBe("reset!");
  renderer.destroy?.();
});

// Vertical (Up/Down) caret movement tracks the DISPLAY-CELL column, so the caret
// keeps its visual column across lines that mix narrow and wide (CJK) glyphs, and
// preserves a goal column through short intervening lines. nl inserts
// a newline without submitting (Alt/Option+Return).
const nl = { name: "return", meta: true };

test("Up keeps the visual column on an all-ASCII draft (regression)", async () => {
  // "abcdef" / "ghijkl": from after "ghij" (col 4) Up lands after "abcd".
  expect(
    await submittedAfter([
      ...typed("abcdef"), nl, ...typed("ghijkl"),
      { name: "left" }, { name: "left" }, { name: "up" }, ...typed("X"),
    ]),
  ).toBe("abcdXef\nghijkl");
});

test("Up into a wide (CJK) line lands at the visual column, not the char index", async () => {
  // "你好world" / "abcdefgh": from visual col 5 on the ASCII line, Up must land
  // after "你好w" (你=2 + 好=2 + w=1 = col 5) -> "你好wXorld", NOT the char-index-5
  // result "你好worXld".
  expect(
    await submittedAfter([
      ...typed("你好world"), nl, ...typed("abcdefgh"),
      { name: "left" }, { name: "left" }, { name: "left" }, // col 8 -> col 5
      { name: "up" }, ...typed("X"),
    ]),
  ).toBe("你好wXorld\nabcdefgh");
});

test("Down into a wide (CJK) line keeps the visual column", async () => {
  // "abcdefgh" / "你好world": from col 5 on the ASCII line, Down lands after "你好w".
  expect(
    await submittedAfter([
      ...typed("abcdefgh"), nl, ...typed("你好world"),
      { name: "up" }, // -> line 0 (goal 9 clamps to end, col 8)
      { name: "left" }, { name: "left" }, { name: "left" }, // -> col 5, resets goal
      { name: "down" }, ...typed("X"),
    ]),
  ).toBe("abcdefgh\n你好wXorld");
});

test("the goal column is preserved across a short intervening line", async () => {
  // "abcdefgh" / "你好" / "ABCDEFGH": from col 7 on line 0, Down through the short
  // wide line "你好" (width 4) returns to col 7 on line 2 -> "ABCDEFGXH".
  expect(
    await submittedAfter([
      ...typed("abcdefgh"), nl, ...typed("你好"), nl, ...typed("ABCDEFGH"),
      { name: "up" }, { name: "up" }, // -> line 0 end (col 8), goal seeded 8
      { name: "left" }, // -> col 7, resets goal
      { name: "down" }, { name: "down" }, // through "你好" (clamps) back to col 7 on line 2
      ...typed("X"),
    ]),
  ).toBe("abcdefgh\n你好\nABCDEFGXH");
});

// ---- unicode width (primitives) ----------------------------------------------

test("cellWidth counts emoji as 2 cells and zero-width marks as 0", () => {
  expect(cellWidth("🙂")).toBe(2);
  expect(cellWidth("你")).toBe(2);
  expect(cellWidth("a")).toBe(1);
  expect(cellWidth("́")).toBe(0); // combining acute
  expect(cellWidth("‍")).toBe(0); // ZWJ
  expect(cellWidth("️")).toBe(0); // variation selector 16
  expect(textWidth("é")).toBe(1); // decomposed é occupies one cell
  expect(textWidth("🙂🙂 hi")).toBe(7);
});

test("text-presentation pictographs stay 1 cell; VS16 promotes them to 2", () => {
  // © ® ™ ↔ ♥ ⚠ ‼ ✔ default to TEXT presentation: terminals (and the
  // renderer's own width measurement) draw them in one cell without VS16.
  for (const ch of ["©", "®", "™", "↔", "♥", "⚠", "‼", "✔"]) {
    expect(cellWidth(ch)).toBe(1);
  }
  // Emoji-presentation defaults stay wide with no selector.
  expect(cellWidth("⌚")).toBe(2);
  expect(cellWidth("⏰")).toBe(2);
  // An explicit VS16 forces emoji presentation: the pair renders 2 cells.
  expect(textWidth("⚠️")).toBe(2);
  expect(textWidth("©️")).toBe(2);
  // Bare text-presentation glyphs count 1 inside a longer string.
  expect(textWidth("warning ⚠ here")).toBe(14);
});

// ---- composerLayout: the wrapped, caret-windowed layout model -----------------

test("composerLayout: short ASCII line does not wrap; caret cell is appended", () => {
  const layout = composerLayout("hello", 5, 20, 3);
  expect(layout.visibleLines).toEqual(["hello "]);
  expect(layout.caretRow).toBe(0);
  expect(layout.caretCol).toBe(5);
  expect(layout.scrollRowOffset).toBe(0);
});

test("composerLayout: mid-text caret splices its blank cell at the caret", () => {
  const layout = composerLayout("hello", 2, 20, 3);
  expect(layout.visibleLines).toEqual(["he llo"]);
  expect(layout.caretCol).toBe(2);
});

test("composerLayout: word-wraps at spaces like the renderer", () => {
  const layout = composerLayout("aaaa bbbb", 9, 5, 3);
  expect(layout.visibleLines).toEqual(["aaaa ", "bbbb "]);
  expect(layout.caretRow).toBe(1);
  expect(layout.caretCol).toBe(4);
});

test("composerLayout: hard-breaks an unbroken word wider than the row", () => {
  const layout = composerLayout("abcdefghij", 10, 4, 3);
  expect(layout.visibleLines).toEqual(["abcd", "efgh", "ij "]);
  expect(layout.caretRow).toBe(2);
  expect(layout.caretCol).toBe(2);
});

test("composerLayout: CJK wraps by display cells, not code points", () => {
  const layout = composerLayout("你好世界", 4, 4, 4);
  // 你好 (4 cells) / 世界 (4 cells) / caret cell on its own row
  expect(layout.visibleLines).toEqual(["你好", "世界", " "]);
  expect(layout.caretRow).toBe(2);
  expect(layout.caretCol).toBe(0);
});

test("composerLayout: emoji count 2 cells for the caret column", () => {
  const layout = composerLayout("🙂🙂", 2, 10, 3);
  expect(layout.caretCol).toBe(4);
});

test("composerLayout: combining marks add no width to the caret column", () => {
  const layout = composerLayout("éx", 3, 10, 3);
  expect(layout.caretCol).toBe(2); // é (1 cell) + x (1 cell)
});

test("composerLayout: windows a >3-line draft around the caret", () => {
  const text = "l1\nl2\nl3\nl4\nl5";
  const layout = composerLayout(text, Array.from(text).length, 20, 3);
  expect(layout.totalRows).toBe(5);
  expect(layout.caretRow).toBe(4);
  expect(layout.scrollRowOffset).toBe(2);
  expect(layout.visibleLines).toEqual(["l3", "l4", "l5 "]);
});

test("composerLayout: caret at the top of a long draft scrolls the window up", () => {
  const text = "l1\nl2\nl3\nl4\nl5";
  const layout = composerLayout(text, 0, 20, 3);
  expect(layout.caretRow).toBe(0);
  expect(layout.scrollRowOffset).toBe(0);
  expect(layout.visibleLines).toEqual([" l1", "l2", "l3"]);
});

test("composerLayout: soft-wrapped rows count toward the caret row", () => {
  // One logical line that wraps into 3 rows of width 4; the caret at the end
  // sits on the LAST wrapped row, not row 0.
  const layout = composerLayout("aaaabbbbcc", 10, 4, 3);
  expect(layout.caretRow).toBe(2);
  expect(layout.caretCol).toBe(2);
});

test("Enter on a blank composer submits nothing (no phantom queued message)", async () => {
  const { renderer, sent } = await setupComposer();
  renderer.keyInput.emit("keypress", { name: "return" });
  renderer.keyInput.emit("keypress", { name: "space", sequence: " " });
  renderer.keyInput.emit("keypress", { name: "return" });
  expect(sent.filter((m) => m.type === "input.submit")).toEqual([]);

  // Real text still submits, and the whitespace-only attempt left no history.
  renderer.keyInput.emit("keypress", { name: "h", sequence: "h" });
  renderer.keyInput.emit("keypress", { name: "i", sequence: "i" });
  renderer.keyInput.emit("keypress", { name: "return" });
  expect(sent.filter((m) => m.type === "input.submit")).toEqual([
    { type: "input.submit", text: " hi" },
  ]);
  renderer.destroy?.();
});
