import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ALTERNATE_SCREEN,
  SURFACE_Z_INDEX,
  assertRendererScreenMode,
  createRendererViewportState,
  rendererLayoutHeight,
  rendererOptions,
  rendererViewportSnapshot,
} from "./screenMode.mjs";

test("the host has one fixed alternate-screen renderer contract", () => {
  assert.deepEqual(rendererOptions(), {
    screenMode: ALTERNATE_SCREEN,
    useMouse: true,
  });
  assertRendererScreenMode({ screenMode: ALTERNATE_SCREEN });
  assert.throws(
    () => assertRendererScreenMode({ screenMode: "main-screen" }),
    /screen mode mismatch/,
  );
});

test("renderer layout height follows the owned alternate-screen viewport", () => {
  assert.equal(rendererLayoutHeight({ height: 30, terminalHeight: 30 }), 30);
  assert.equal(rendererLayoutHeight({ height: 30, terminalHeight: 42 }), 30);
  assert.equal(rendererLayoutHeight({ terminalHeight: 24 }), 24);
  assert.equal(rendererLayoutHeight({}), 1);
});

test("one viewport snapshot owns layout and cursor geometry for an epoch", () => {
  const renderer = { width: 80, height: 24, terminalWidth: 120, terminalHeight: 36 };
  assert.deepEqual(rendererViewportSnapshot(renderer), { width: 80, height: 24 });

  const state = createRendererViewportState(renderer);
  assert.deepEqual(state.current(), { width: 80, height: 24, epoch: 0, reason: "startup" });
  renderer.width = 120;
  renderer.height = 36;
  renderer.terminalWidth = 160;
  renderer.terminalHeight = 42;
  assert.deepEqual(state.refresh("resize", { width: 160, height: 42 }), {
    width: 160,
    height: 42,
    epoch: 1,
    reason: "resize",
  });
});

test("fixed footer chrome has an explicit paint layer above transcript", () => {
  assert.ok(SURFACE_Z_INDEX.footer > SURFACE_Z_INDEX.transcript);
  assert.ok(SURFACE_Z_INDEX.footerIndicator > SURFACE_Z_INDEX.footer);
  assert.ok(SURFACE_Z_INDEX.contextRail > SURFACE_Z_INDEX.footer);
  assert.ok(SURFACE_Z_INDEX.overlay > SURFACE_Z_INDEX.contextRail);
});
