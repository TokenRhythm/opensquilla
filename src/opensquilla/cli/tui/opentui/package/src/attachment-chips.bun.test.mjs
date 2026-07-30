import { expect, test } from "bun:test";
import { BoxRenderable, TextRenderable } from "@opentui/core";
import { createTestRenderer } from "@opentui/core/testing";

import { createComposer } from "./composer.mjs";

async function setup() {
  const sent = [];
  const harness = await createTestRenderer({ width: 72, height: 12 });
  const { renderer } = harness;
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
    footerHeight: 6, sendHostMessage: (message) => sent.push(message),
  });
  try { composer.install(); } catch { composer.rerender(); }
  return { ...harness, renderer, composer, sent };
}

const press = (renderer, name, sequence = name) =>
  renderer.keyInput.emit("keypress", { name, sequence });
const type = (renderer, text) => {
  for (const char of text) press(renderer, char, char);
};

test("pending attachment chip blocks submit, then clears after ready turn", async () => {
  const { renderer, composer, sent, renderOnce, captureSpans } = await setup();
  composer.addAttachmentState({
    id: "a1", kind: "file", label: "brief.pdf", status: "reading",
  });
  type(renderer, "summarize");
  press(renderer, "return");
  expect(sent.some((message) => message.type === "input.submit")).toBe(false);

  composer.updateAttachmentState({ id: "a1", status: "ready" });
  await renderOnce();
  const frame = captureSpans();
  const text = frame.lines.map((line) => line.spans.map((span) => span.text).join("")).join("\n");
  expect(text).toContain("✓ file brief.pdf");

  press(renderer, "return");
  expect(sent.find((message) => message.type === "input.submit")?.text).toBe("summarize");
  composer.clearAttachmentStates("ready");
  await renderOnce();
  const cleared = captureSpans().lines
    .map((line) => line.spans.map((span) => span.text).join(""))
    .join("\n");
  expect(cleared).not.toContain("brief.pdf");
  renderer.destroy?.();
});

test("failed chip remains visible but unrelated input stays recoverable", async () => {
  const { renderer, composer, sent, renderOnce, captureSpans } = await setup();
  composer.addAttachmentState({
    id: "a1", kind: "image", label: "chart.png", status: "failed",
    message: "check the file and retry /image",
  });
  type(renderer, "continue without it");
  press(renderer, "return");
  expect(sent.find((message) => message.type === "input.submit")?.text)
    .toBe("continue without it");

  await renderOnce();
  const text = captureSpans().lines
    .map((line) => line.spans.map((span) => span.text).join(""))
    .join("\n");
  expect(text).toContain("✗ image chart.png");
  renderer.destroy?.();
});
