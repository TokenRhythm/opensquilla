import { expect, test } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import { destroyChildren, destroyRenderable } from "./renderableLifecycle.mjs";

test("remove-only lifecycle adapters receive the child object required by OpenTUI 0.4", () => {
  const child = { id: "fallback-child" };
  let removed = null;
  const parent = {
    getChildren: () => [child],
    remove(node) { removed = node; },
  };

  expect(destroyRenderable(parent, "fallback-child")).toBe(true);
  expect(removed).toBe(child);
});

test("destroyChildren recursively releases a real OpenTUI subtree", async () => {
  const { renderer } = await createTestRenderer({ width: 80, height: 24 });
  const parent = new BoxRenderable(renderer, { id: "lifecycle-parent" });
  const child = new BoxRenderable(renderer, { id: "lifecycle-child" });
  const leaf = new TextRenderable(renderer, { id: "lifecycle-leaf", content: "leaf" });
  renderer.root.add(parent);
  parent.add(child);
  child.add(leaf);

  expect(destroyChildren(parent)).toBe(1);
  expect(parent.getChildrenCount()).toBe(0);
  expect(child.isDestroyed).toBe(true);
  expect(leaf.isDestroyed).toBe(true);
  renderer.destroy?.();
});

test("repeated composer edits keep two live footer children and destroy replacements", async () => {
  const { renderer } = await createTestRenderer({ width: 160, height: 30 });
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 36, height: 24,
  });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 36, bottom: 0, height: 6,
  });
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    shouldFill: false, visible: false,
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
    footerHeight: 6,
    sendHostMessage: () => {},
  });
  composer.install();

  for (let index = 0; index < 100; index += 1) {
    const previousComposer = inputBox.getRenderable("composer-box");
    const previousRouter = inputBox.getRenderable("router-strip");
    renderer.keyInput.emit("keypress", { name: "a", sequence: "a" });
    expect(previousComposer.isDestroyed).toBe(true);
    expect(previousRouter.isDestroyed).toBe(true);
    expect(inputBox.getChildrenCount()).toBe(2);
  }

  expect(inputBox.getRenderable("composer-box")?.isDestroyed).toBe(false);
  expect(inputBox.getRenderable("router-strip")?.isDestroyed).toBe(false);
  renderer.destroy?.();
});
