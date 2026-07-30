// Focused regression for the footer resize path that previously produced a
// stranded right border and a hardware cursor below the composer after an
// embedded terminal changed width. Exercise the same composer instance through
// narrow -> wide -> narrow so cached child geometry cannot make the final frame
// pass accidentally.
//
// Run with: bun test src/composer-resize-geometry.bun.test.mjs
import { expect, test } from "bun:test";
import { BoxRenderable, TextRenderable } from "@opentui/core";
import { createTestRenderer } from "@opentui/core/testing";

import { createComposer } from "./composer.mjs";
import { THEME } from "./theme.mjs";

const HEIGHT = 12;
const FOOTER_HEIGHT = 6;
const NARROW = 40;
const WIDE = 100;
const DRAFT = "a".repeat(70);

function rowText(frame, row) {
  return frame.lines[row]?.spans.map((span) => span.text).join("") ?? "";
}

function cornerColumns(line) {
  return {
    open: line.indexOf("╭"),
    close: line.indexOf("╮"),
    opens: Array.from(line.matchAll(/╭/gu)).length,
    closes: Array.from(line.matchAll(/╮/gu)).length,
  };
}

test("composer rebuilds border, wrapped children, and cursor on narrow-wide-narrow resize", async () => {
  const setup = await createTestRenderer({ width: NARROW, height: HEIGHT });
  const { renderer, renderOnce, captureSpans, resize } = setup;
  const cursorPositions = [];
  renderer.setCursorPosition = (x, y, visible) => {
    cursorPositions.push({ x, y, visible });
  };

  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: HEIGHT - FOOTER_HEIGHT,
  });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region",
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: FOOTER_HEIGHT,
    backgroundColor: THEME.footerBg,
  });
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
  renderer.root.add(conversationBox);
  renderer.root.add(inputBox);
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
  composer.install();
  composer.setComposerState({ text: DRAFT });

  async function snapshot(width) {
    if (renderer.terminalWidth !== width) {
      await resize(width, HEIGHT);
      composer.onResize();
    }
    await renderOnce();
    const frame = captureSpans();
    const composerBox = inputBox.getRenderable("composer-box");
    const textRows = composerBox
      .getChildren()
      .filter((child) => child.id.startsWith("composer-text-"))
      .map((child) => child.content?.chunks?.map((chunk) => chunk.text).join("") ?? "");
    return {
      border: cornerColumns(rowText(frame, HEIGHT - FOOTER_HEIGHT + 1)),
      box: composerBox,
      boxX: composerBox.x,
      boxY: composerBox.y,
      boxWidth: composerBox.width,
      boxHeight: composerBox.height,
      textRows,
      cursor: cursorPositions.at(-1),
    };
  }

  try {
    const narrowBefore = await snapshot(NARROW);
    const wide = await snapshot(WIDE);
    const narrowAfter = await snapshot(NARROW);

    for (const [state, width] of [
      [narrowBefore, NARROW],
      [wide, WIDE],
      [narrowAfter, NARROW],
    ]) {
      // left/right:1 means the live box must always be exactly terminalWidth-2.
      expect(state.boxWidth).toBe(width - 2);
      expect(state.border).toEqual({ open: 1, close: width - 2, opens: 1, closes: 1 });
      expect(state.cursor.visible).toBe(true);
      // Hardware coordinates are 1-based. Keep the cursor inside the content
      // rectangle, never on/below the border OpenTUI laid out for this frame.
      expect(state.cursor.x).toBeGreaterThanOrEqual(state.boxX + 3);
      expect(state.cursor.x).toBeLessThanOrEqual(state.boxX + state.boxWidth - 2);
      expect(state.cursor.y).toBeGreaterThanOrEqual(state.boxY + 2);
      expect(state.cursor.y).toBeLessThanOrEqual(state.boxY + state.boxHeight - 1);
    }

    expect(narrowBefore.textRows).toEqual([
      "a".repeat(34),
      "a".repeat(34),
      "aa ",
    ]);
    expect(wide.textRows).toEqual([`${DRAFT} `]);
    expect(narrowAfter.textRows).toEqual(narrowBefore.textRows);
    expect(narrowAfter.cursor).toEqual(narrowBefore.cursor);
    // Each resize must retire the old child tree; otherwise an old-width border
    // can continue participating in the framebuffer alongside the new one.
    expect(narrowBefore.box.isDestroyed).toBe(true);
    expect(wide.box.isDestroyed).toBe(true);
    expect(narrowAfter.box.isDestroyed).toBe(false);
  } finally {
    renderer.destroy?.();
  }
});
