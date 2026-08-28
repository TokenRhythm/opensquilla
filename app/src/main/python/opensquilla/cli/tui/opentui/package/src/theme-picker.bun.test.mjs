// Theme picker overlay tests:
//   - themePickerKeyAction maps keys to navigate(preview)/confirm/cancel and is
//     modal (swallows every other key while open);
//   - openThemePicker renders a titled panel listing every theme with the active
//     one marked — a real overlay panel, not stray scrollback text.
//
// Run with: bun test src/theme-picker.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer, themePickerKeyAction } from "./composer.mjs";
import { THEME_NAMES, applyTheme } from "./theme.mjs";

test("themePickerKeyAction navigates, confirms, cancels, and is modal", () => {
  const picker = { active: true, names: ["a", "b", "c"], selected: 1 };
  expect(themePickerKeyAction(picker, "up")).toMatchObject({ action: "preview", selected: 0 });
  expect(themePickerKeyAction(picker, "down")).toMatchObject({ action: "preview", selected: 2 });
  // clamps at the ends
  expect(themePickerKeyAction({ ...picker, selected: 2 }, "down")).toMatchObject({ selected: 2 });
  expect(themePickerKeyAction({ ...picker, selected: 0 }, "up")).toMatchObject({ selected: 0 });
  expect(themePickerKeyAction(picker, "return")).toMatchObject({ action: "confirm" });
  expect(themePickerKeyAction(picker, "escape")).toMatchObject({ action: "cancel" });
  // every other key is swallowed (modal) so it never leaks into the input
  expect(themePickerKeyAction(picker, "x")).toMatchObject({ handled: true, action: "none" });
  // inactive picker passes keys through
  expect(themePickerKeyAction({ active: false }, "up")).toMatchObject({ handled: false });
});

test("openThemePicker renders a titled panel listing every theme, active one marked", async () => {
  applyTheme("opensquilla-dark", { explicit: true });
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width: 50, height: 20 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 14,
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
    footerHeight: 6, sendHostMessage: () => {},
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }

  composer.openThemePicker();
  await renderOnce();
  const text = captureSpans()
    .lines.map((line) => line.spans.map((span) => span.text).join(""))
    .join("\n");

  for (const name of THEME_NAMES) expect(text).toContain(name); // every theme listed
  // The grayscale degradation palette is a first-class pick in ANY color mode,
  // not only the forced NO_COLOR fallback — pin it by name so a registry
  // reshuffle can never drop it from the panel.
  expect(text).toContain("monochrome");
  expect(text).toContain("theme"); // panel title
  expect(text).toContain("› opensquilla-dark"); // active theme marked
  expect(text.toLowerCase()).toContain("preview"); // the key hint
  renderer.destroy?.();
});

test("picker survives footer/theme re-renders without a duplicate remount", async () => {
  // Regression: a footer re-render (router update or keystroke) ran
  // renderCompletionMenu -> clearOverlay, wiping the
  // picker while it stayed modally active — picker flashed once then the TUI
  // looked frozen (keys swallowed by an invisible modal). It must stay mounted.
  applyTheme("opensquilla-dark", { explicit: true });
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width: 50, height: 20 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 14,
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
    footerHeight: 6, sendHostMessage: () => {},
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }

  composer.openThemePicker();
  let pickerRemovals = 0;
  const remove = overlayLayer.remove.bind(overlayLayer);
  overlayLayer.remove = (node) => {
    if (node?.id === "theme-picker") pickerRemovals += 1;
    return remove(node);
  };
  // Theme application uses the same footer path: one overlay remount is
  // sufficient to recolor the picker and reassert the caret.
  composer.applyHostTheme("midnight");
  expect(pickerRemovals).toBe(1);
  // An ordinary later footer update still preserves the active picker.
  composer.rerender();
  await renderOnce();
  const text = captureSpans()
    .lines.map((line) => line.spans.map((span) => span.text).join(""))
    .join("\n");
  expect(text).toContain("theme"); // panel title still present
  expect(text).toContain("midnight"); // theme rows still present after re-renders
  renderer.destroy?.();
});

test("paste while the picker is open never reaches the composer draft", async () => {
  // The picker is modal for keypresses; bracketed paste must be modal too, or
  // pasted text lands invisibly in the draft while the user previews themes.
  applyTheme("opensquilla-dark", { explicit: true });
  const sent = [];
  const { renderer } = await createTestRenderer({ width: 50, height: 20 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 14,
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

  composer.openThemePicker();
  renderer.keyInput.emit("paste", { bytes: new TextEncoder().encode("sneaky") });
  renderer.keyInput.emit("keypress", { name: "return" }); // keep theme, close picker
  // Enter on an empty draft is a no-op (no submit frame), so prove the draft
  // stayed untouched with a sentinel: the submission must be ONLY the sentinel.
  renderer.keyInput.emit("keypress", { name: "x", sequence: "X" });
  renderer.keyInput.emit("keypress", { name: "return" });
  expect(sent.find((m) => m.type === "input.submit")?.text).toBe("X");
  renderer.destroy?.();
});

test("confirming a picker choice reports theme.selected for CLI-side persistence", async () => {
  applyTheme("opensquilla-dark", { explicit: true });
  const sent = [];
  const { renderer } = await createTestRenderer({ width: 50, height: 20 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 14,
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

  composer.openThemePicker();
  renderer.keyInput.emit("keypress", { name: "down" }); // preview the next theme
  renderer.keyInput.emit("keypress", { name: "return" }); // keep it

  const reported = sent.filter((m) => m.type === "theme.selected");
  expect(reported.length).toBe(1);
  // The picker opened on the active theme (index 0) and moved down once, so
  // the exact next palette must be what gets reported for persistence.
  expect(reported[0].name).toBe(THEME_NAMES[1]);

  // Escape reverts the live preview — a cancelled picker must persist nothing.
  composer.openThemePicker();
  renderer.keyInput.emit("keypress", { name: "down" });
  renderer.keyInput.emit("keypress", { name: "escape" });
  expect(sent.filter((m) => m.type === "theme.selected").length).toBe(1);
  renderer.destroy?.();
});
