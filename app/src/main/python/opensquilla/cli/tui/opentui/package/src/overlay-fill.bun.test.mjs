// Renderer-level regression test for the completion-menu opaque background.
//
// The completion menu floats over the conversation. If its BoxRenderable has no
// backgroundColor it defaults to transparent (alpha 0), the conversation behind
// it bleeds through, and the menu rows collide with backdrop text on screen.
// The tmux/PTY text-snapshot harness cannot see cell background colour, so this
// bug slipped past it. Here we render with the real @opentui core test renderer
// and inspect captured span backgrounds (RGBA), which DOES expose the alpha.
//
// Must run under bun: @opentui/core/testing needs bun FFI and cannot load under
// `node --test`. Run with: bun test src/overlay-fill.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { THEME } from "./theme.mjs";

const WIDTH = 60;
const HEIGHT = 20;
const MENU_BOTTOM = 6;
const MENU_HEIGHT = 5;

// Build a backdrop of conversation text plus an overlay-mounted menu box. When
// `withBackground` is false the menu omits backgroundColor, reproducing the
// transparent-bleed bug for the contrast case.
async function renderMenu(withBackground) {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
  const { renderer, renderOnce, captureSpans } = setup;

  const backdrop = new BoxRenderable(renderer, {
    id: "backdrop",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
  });
  renderer.root.add(backdrop);
  for (let row = 0; row < HEIGHT - 2; row += 1) {
    backdrop.add(
      new TextRenderable(renderer, {
        id: `backdrop-${row}`,
        content: "BACKDROP".repeat(8),
        fg: THEME.text,
      }),
    );
  }

  const overlay = new BoxRenderable(renderer, {
    id: "overlay-layer",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 1000,
    shouldFill: false,
  });
  renderer.root.add(overlay);

  const menuOptions = {
    id: "completion-menu",
    position: "absolute",
    left: 1,
    right: 20,
    bottom: MENU_BOTTOM,
    height: MENU_HEIGHT,
    borderStyle: "rounded",
    borderColor: THEME.composerBorder,
    title: " commands ",
    titleAlignment: "left",
    flexDirection: "column",
    paddingLeft: 1,
    paddingRight: 1,
  };
  if (withBackground) menuOptions.backgroundColor = THEME.overlayBg;
  const menu = new BoxRenderable(renderer, menuOptions);
  menu.add(new TextRenderable(renderer, { id: "menu-row-0", content: "/compact", fg: THEME.text }));
  overlay.add(menu);

  await renderOnce();
  const frame = captureSpans();
  renderer.destroy?.();
  return frame;
}

function bgAt(frame, rowIndex, colIndex) {
  const line = frame.lines[rowIndex];
  if (!line) return null;
  let col = 0;
  for (const span of line.spans) {
    const width = Math.max(1, span.width || 1);
    for (let i = 0; i < width; i += 1) {
      if (col === colIndex) return span.bg;
      col += 1;
    }
  }
  return null;
}

// Find a row that contains the menu's vertical border (│) and return a column
// inside that border so we probe the menu body, not the backdrop.
function menuBodyProbe(frame) {
  for (let row = 0; row < frame.lines.length; row += 1) {
    const line = frame.lines[row];
    const flat = line.spans.map((span) => span.text).join("");
    const left = flat.indexOf("│");
    const right = flat.lastIndexOf("│");
    if (left >= 0 && right > left) {
      return { row, col: left + 2 };
    }
  }
  return null;
}

test("completion menu paints an opaque background over the conversation", async () => {
  const frame = await renderMenu(true);
  const probe = menuBodyProbe(frame);
  expect(probe).not.toBeNull();
  const bg = bgAt(frame, probe.row, probe.col);
  expect(bg).not.toBeNull();
  // The decisive check: the menu body cell is opaque, so the backdrop cannot
  // bleed through and collide with the menu rows.
  expect(bg.a).toBeGreaterThan(0);
});

test("a menu without backgroundColor leaves a transparent body (contrast case)", async () => {
  const frame = await renderMenu(false);
  const probe = menuBodyProbe(frame);
  expect(probe).not.toBeNull();
  const bg = bgAt(frame, probe.row, probe.col);
  expect(bg).not.toBeNull();
  // Proves the assertion above actually discriminates: drop the fix and the
  // body is transparent (alpha 0), which is the bug.
  expect(bg.a).toBe(0);
});
