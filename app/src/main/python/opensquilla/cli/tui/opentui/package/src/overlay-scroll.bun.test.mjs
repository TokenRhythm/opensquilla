// Renderer-level regression for the full-screen overlay mouse hit-test.
//
// The completion overlay is a transparent, full-screen, high-zIndex root
// sibling of the conversation ScrollBox. `shouldFill:false` only prevents
// painting; a visible overlay still participates in hit-testing and receives
// wheel events before the conversation can. The production fix keeps the layer
// hidden while no menu is mounted, then reveals it while the menu is open.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import {
  BoxRenderable,
  Renderable,
  ScrollBoxRenderable,
  TextRenderable,
} from "@opentui/core";

const WIDTH = 60;
const HEIGHT = 24;
const FOOTER_HEIGHT = 6;
const CONVERSATION_HEIGHT = HEIGHT - FOOTER_HEIGHT;
const PROBE_X = 3;
const PROBE_Y = 3;

function renderableAt(renderer, x, y) {
  const num = renderer.hitTest(x, y);
  return Renderable.renderablesByNumber.get(num) ?? null;
}

function isDescendantOf(renderable, ancestor) {
  let current = renderable;
  while (current) {
    if (current === ancestor) return true;
    current = current.parent;
  }
  return false;
}

async function createMainLikeLayout({ overlayVisible }) {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
  const { renderer, renderOnce, mockMouse } = setup;

  const conversation = new ScrollBoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: CONVERSATION_HEIGHT,
    stickyScroll: true,
    stickyStart: "bottom",
    scrollY: true,
    viewportCulling: true,
  });
  renderer.root.add(conversation);

  for (let row = 0; row < 80; row += 1) {
    conversation.add(new TextRenderable(renderer, {
      id: `line-${row}`,
      content: `${String(row).padStart(2, "0")} conversation scrollback line`,
    }));
  }

  const inputBox = new BoxRenderable(renderer, {
    id: "input",
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: FOOTER_HEIGHT,
  });
  renderer.root.add(inputBox);

  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 1000,
    shouldFill: false,
    visible: overlayVisible,
  });
  renderer.root.add(overlayLayer);

  await renderOnce();
  conversation.scrollTop = 0;
  await renderOnce();

  return {
    renderer,
    renderOnce,
    mockMouse,
    conversation,
    overlayLayer,
  };
}

test("hidden overlay lets wheel events reach the conversation ScrollBox", async () => {
  const layout = await createMainLikeLayout({ overlayVisible: false });
  const { renderer, renderOnce, mockMouse, conversation, overlayLayer } = layout;

  try {
    expect(overlayLayer.visible).toBe(false);
    expect(conversation.scrollHeight).toBeGreaterThan(CONVERSATION_HEIGHT);

    const hitBefore = renderableAt(renderer, PROBE_X, PROBE_Y);
    expect(hitBefore).not.toBeNull();
    expect(hitBefore.id).not.toBe("overlay-layer");
    expect(isDescendantOf(hitBefore, conversation)).toBe(true);

    const before = conversation.scrollTop;
    await mockMouse.scroll(PROBE_X, PROBE_Y, "down");
    await renderOnce();

    expect(conversation.scrollTop).toBeGreaterThan(before);
  } finally {
    renderer.destroy?.();
  }
});

test("visible overlay intercepts wheel events while the menu layer is active", async () => {
  const layout = await createMainLikeLayout({ overlayVisible: true });
  const { renderer, renderOnce, mockMouse, conversation, overlayLayer } = layout;

  try {
    expect(overlayLayer.visible).toBe(true);
    expect(conversation.scrollHeight).toBeGreaterThan(CONVERSATION_HEIGHT);

    const hitBefore = renderableAt(renderer, PROBE_X, PROBE_Y);
    expect(hitBefore).not.toBeNull();
    expect(hitBefore.id).toBe("overlay-layer");

    const before = conversation.scrollTop;
    await mockMouse.scroll(PROBE_X, PROBE_Y, "down");
    await renderOnce();

    expect(conversation.scrollTop).toBe(before);
  } finally {
    renderer.destroy?.();
  }
});
