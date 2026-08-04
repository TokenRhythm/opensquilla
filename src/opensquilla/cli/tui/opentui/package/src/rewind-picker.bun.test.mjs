// Rewind-picker behavior, driven through real keypresses:
//   - A rewind.pick frame opens the picker listing user-message rewind points.
//   - Up/Down move the selection; Enter submits /rewind <message-id> so the
//     Python command path owns the fork (mirrors the session picker contract).
//   - Esc closes the picker without submitting.
//   - Typing filters by ordinal/preview.
//
// Run with: bun test src/rewind-picker.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import { applyTheme } from "./theme.mjs";

async function setupComposer() {
  applyTheme("opensquilla-dark");
  const sent = [];
  const { renderer, flush, captureCharFrame } = await createTestRenderer({ width: 80, height: 24 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 6,
  });
  renderer.root.add(conversationBox);
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0, height: 6,
  });
  renderer.root.add(inputBox);
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(overlayLayer);
  const composer = createComposer({
    renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, overlayLayer,
    footerHeight: 6, sendHostMessage: (m) => sent.push(m),
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  return { renderer, composer, sent, flush, captureCharFrame };
}

const press = (renderer, name, sequence = name) =>
  renderer.keyInput.emit("keypress", { name, sequence });
const type = (renderer, text) => {
  for (const ch of text) press(renderer, ch, ch);
};

function openRewindPicker(composer) {
  composer.openRewindPicker({
    current_key: "agent:main:test:0",
    points: [
      { id: "m1", ordinal: 1, preview: "first question" },
      { id: "m3", ordinal: 2, preview: "second question" },
      { id: "m5", ordinal: 3, preview: "third question" },
    ],
  });
}

test("rewind.pick opens a picker and Enter submits /rewind with the message id", async () => {
  const { renderer, composer, sent } = await setupComposer();
  openRewindPicker(composer);

  // The picker is open; Enter should confirm the first (default) point.
  press(renderer, "return");

  const submits = sent.filter((m) => m.type === "input.submit");
  expect(submits.length).toBe(1);
  expect(submits[0].text).toBe("/rewind m1");
  renderer.destroy?.();
});

test("arrow keys move the selection before Enter", async () => {
  const { renderer, composer, sent } = await setupComposer();
  openRewindPicker(composer);
  press(renderer, "down");
  press(renderer, "down");
  press(renderer, "return");

  const submits = sent.filter((m) => m.type === "input.submit");
  expect(submits.length).toBe(1);
  expect(submits[0].text).toBe("/rewind m5");
  renderer.destroy?.();
});

test("Esc closes the picker without submitting", async () => {
  const { renderer, composer, sent } = await setupComposer();
  openRewindPicker(composer);
  press(renderer, "escape");

  const submits = sent.filter((m) => m.type === "input.submit");
  expect(submits.length).toBe(0);
  renderer.destroy?.();
});

test("typing filters rewind points by ordinal or preview", async () => {
  const { renderer, composer, sent } = await setupComposer();
  openRewindPicker(composer);
  type(renderer, "third");
  press(renderer, "return");

  const submits = sent.filter((m) => m.type === "input.submit");
  expect(submits.length).toBe(1);
  expect(submits[0].text).toBe("/rewind m5");
  renderer.destroy?.();
});

test("picker rows render the message text only — no id or timestamp", async () => {
  const { composer, flush, captureCharFrame } = await setupComposer();
  openRewindPicker(composer);
  await flush();
  const frame = captureCharFrame();
  // Every point row shows its ordinal and the preview text, never a
  // timestamp or a raw message id.
  const flat = frame.replace(/\s+/g, " ");
  for (const fragment of ["#1 first question", "#2 second question", "#3 third question"]) {
    expect(flat).toContain(fragment);
  }
  expect(frame).not.toContain("2026-08-04");
  expect(frame).not.toContain("m1");
  composer.rerender?.();
});
