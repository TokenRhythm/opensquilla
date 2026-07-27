import { expect, test } from "bun:test";
import { BoxRenderable, TextRenderable } from "@opentui/core";
import { createTestRenderer } from "@opentui/core/testing";

import {
  createHeldOutputIndicator,
  HELD_OUTPUT_MESSAGE,
} from "./heldOutputIndicator.mjs";
import { THEME } from "./theme.mjs";

function rowText(frame, row) {
  return frame.lines[row]?.spans.map((span) => span.text).join("") ?? "";
}

function cells(frame, row) {
  const result = [];
  for (const span of frame.lines[row]?.spans ?? []) {
    const width = Math.max(1, span.width || 1);
    for (let index = 0; index < width; index += 1) {
      result.push({ text: span.text, bg: span.bg });
    }
  }
  return result;
}

test("held-output notice atomically masks a changing transcript row", async () => {
  const width = 72;
  const height = 12;
  const bottom = 4;
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width, height });
  const backdrop = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
  });
  renderer.root.add(backdrop);
  const stream = new TextRenderable(renderer, {
    id: "stream",
    position: "absolute",
    left: 0,
    bottom,
    content: "stream-token-000 ".repeat(5),
    fg: THEME.text,
  });
  backdrop.add(stream);

  const indicator = createHeldOutputIndicator({
    renderer,
    BoxRenderable,
    TextRenderable,
    bottom,
    theme: THEME,
  });
  renderer.root.add(indicator.node);
  indicator.setVisible(true);

  try {
    await renderOnce();
    stream.content = "stream-token-079 ".repeat(5);
    await renderOnce();
    const frame = captureSpans();
    const row = height - bottom - 1;
    const text = rowText(frame, row);
    expect(text).toContain(HELD_OUTPUT_MESSAGE);
    expect(text.indexOf(HELD_OUTPUT_MESSAGE)).toBe(2);

    // Every cell in the notice rectangle is opaque. This is the property that
    // prevents a later streaming repaint from interleaving backdrop glyphs.
    const noticeCells = cells(frame, row).slice(1, 1 + indicator.node.width);
    expect(noticeCells).toHaveLength(indicator.node.width);
    expect(noticeCells.every((cell) => cell.bg?.a > 0)).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});
