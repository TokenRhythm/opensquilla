import { test, expect } from "bun:test";
import { createHash } from "node:crypto";
import { createTestRenderer } from "@opentui/core/testing";
import {
  ASCIIFontRenderable,
  BoxRenderable,
  ScrollBoxRenderable,
  TextRenderable,
} from "@opentui/core";

import {
  createWelcomeView,
  welcomeLogoMode,
} from "./welcomeView.mjs";
import { THEME } from "./theme.mjs";

function frameText(frame) {
  return frame.lines.map((line) => line.spans.map((span) => span.text).join("")).join("\n");
}

function spanFgIs(span, hex) {
  const n = parseInt(hex.slice(1), 16);
  const want = [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  const fg = span.fg;
  if (!fg) return false;
  return [fg.r, fg.g, fg.b].every((channel, index) => Math.abs(channel - want[index]) < 0.004);
}

async function harness(width, height) {
  const setup = await createTestRenderer({ width, height });
  const conversationBox = new ScrollBoxRenderable(setup.renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height,
    scrollY: true,
    viewportCulling: false,
  });
  setup.renderer.root.add(conversationBox);
  const welcome = createWelcomeView({
    renderer: setup.renderer,
    BoxRenderable,
    TextRenderable,
    ASCIIFontRenderable,
    conversationBox,
    contentWidth: () => width,
  });
  return { ...setup, conversationBox, welcome };
}

test("logo typography responds to realistic terminal geometry", () => {
  expect(welcomeLogoMode(115, 42, 115)).toBe("block");
  // A normal 24-row coding terminal must retain the original six-row brand
  // face. Height only forces plain text when the whole surface is truly short.
  expect(welcomeLogoMode(115, 24, 115)).toBe("block");
  expect(welcomeLogoMode(100, 24, 100)).toBe("block");
  expect(welcomeLogoMode(99, 24, 99)).toBe("tiny");
  expect(welcomeLogoMode(80, 24, 80)).toBe("tiny");
  expect(welcomeLogoMode(44, 24, 44)).toBe("plain");
  expect(welcomeLogoMode(160, 17, 160)).toBe("plain");
  // A wide terminal with a context rail still chooses from transcript width.
  expect(welcomeLogoMode(160, 40, 124)).toBe("block");
  // The 132-column rail breakpoint leaves exactly the 102 cells required by
  // the block wordmark, so making the terminal wider never downgrades the logo.
  expect(welcomeLogoMode(131, 40, 131)).toBe("block");
  expect(welcomeLogoMode(132, 40, 102)).toBe("block");
});

test("115-column empty session matches the approved brand-anchor composition", async () => {
  const { renderer, renderOnce, captureSpans, welcome } = await harness(115, 42);
  try {
    await renderOnce();
    const text = frameText(captureSpans());
    expect(welcome.snapshot()).toMatchObject({ mounted: true, eligible: true, mode: "block" });
    expect(text).toContain("Build with your agent. Stay in the flow.");
    // Runtime identity and controls belong to fixed shell chrome. The welcome
    // reference contains no duplicate context or shortcut rows below the line.
    expect(text).not.toContain("Gateway connected");
    expect(text).not.toContain("/help commands");
    // The wordmark itself is terminal-native display type, not the small plain
    // fallback: it occupies multiple visible glyph rows above the tagline.
    const taglineRow = text.split("\n").findIndex((line) => line.includes("Build with your agent"));
    expect(taglineRow).toBeGreaterThanOrEqual(8);
    const spans = captureSpans().lines.flatMap((line) => line.spans);
    expect(spans.some((span) => spanFgIs(span, THEME.brandAccent))).toBe(true);
    expect(spans.some((span) => spanFgIs(span, THEME.brandShadow))).toBe(true);
  } finally {
    renderer.destroy?.();
  }
});

test("100x24 restores the approved filled block wordmark", async () => {
  const { renderer, renderOnce, captureSpans, welcome } = await harness(100, 24);
  try {
    await renderOnce();
    const text = frameText(captureSpans());
    expect(welcome.snapshot()).toMatchObject({ mounted: true, mode: "block" });
    expect(text).toContain("██████");
    expect(text).not.toContain("╭━━━╮");
    const taglineRow = text.split("\n").findIndex((line) => line.includes("Build with your agent"));
    expect(taglineRow).toBeGreaterThanOrEqual(8);
    const logoRows = text.split("\n")
      .slice(0, taglineRow)
      .filter((line) => /[█╗║╝]/u.test(line) && line.trim().length > 20);
    expect(logoRows).toHaveLength(6);
    expect(logoRows.every((line) => line.startsWith(" "))).toBe(true);
    const wordmarkHash = createHash("sha256")
      .update(logoRows.map((line) => line.trimEnd()).join("\n"))
      .digest("hex");
    expect(wordmarkHash).toBe("55aedb233862a90bb52ca22b08c1bc3794e46dfd68b0c89956ca9f851ce6714c");
    // The approved final A remains complete at the exact breakpoint; if the
    // ScrollBox/inset steals one more cell these right-edge glyphs are first to
    // disappear, recreating the visibly undersized/cropped regression.
    expect(logoRows.map((line) => line.slice(0, -1).trimEnd().at(-1))).toEqual(["╗", "╗", "║", "║", "║", "╝"]);
  } finally {
    renderer.destroy?.();
  }
});

test("80x24 keeps the narrow fallback and reset/resume lifecycle", async () => {
  const { renderer, renderOnce, captureSpans, welcome } = await harness(80, 24);
  try {
    await renderOnce();
    let text = frameText(captureSpans());
    expect(welcome.snapshot()).toMatchObject({ mounted: true, mode: "tiny" });
    expect(text).toContain("Build with your agent");
    expect(text).not.toContain("Ctrl+O details");

    welcome.syncHistory({ messages: [{ id: "m1", role: "user", text: "resumed" }] });
    await renderOnce();
    expect(welcome.snapshot()).toMatchObject({ mounted: false, eligible: false });
    expect(frameText(captureSpans())).not.toContain("Build with your agent");

    // Mimic history.replace's clear before /new remounts the empty state.
    for (const child of (renderer.root.getRenderable?.("conversation")?.getChildren?.() ?? [])) {
      renderer.root.getRenderable?.("conversation")?.remove?.(child);
    }
    welcome.syncHistory({ messages: [] });
    await renderOnce();
    text = frameText(captureSpans());
    expect(welcome.snapshot()).toMatchObject({ mounted: true, eligible: true, mode: "tiny" });
    expect(text).toContain("Build with your agent");
  } finally {
    renderer.destroy?.();
  }
});

test("an empty welcome remains visible across a collapsed-to-wide resize", async () => {
  const setup = await createTestRenderer({ width: 18, height: 24 });
  const { renderer, renderOnce, captureSpans } = setup;
  const conversationBox = new ScrollBoxRenderable(renderer, {
    id: "conversation-resize",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: 18,
    stickyScroll: true,
    stickyStart: "bottom",
    scrollY: true,
    viewportCulling: false,
  });
  renderer.root.add(conversationBox);
  const welcome = createWelcomeView({
    renderer,
    BoxRenderable,
    TextRenderable,
    ASCIIFontRenderable,
    conversationBox,
    contentWidth: () => renderer.terminalWidth,
  });
  try {
    await renderOnce();
    expect(welcome.snapshot().mode).toBe("plain");
    expect(frameText(captureSpans())).toContain("OpenSquilla");

    renderer.resize(160, 40);
    conversationBox.height = 34;
    welcome.relayout();
    await renderOnce();
    const wide = frameText(captureSpans());
    expect(welcome.snapshot()).toMatchObject({ mounted: true, mode: "block" });
    expect(wide).toContain("Build with your agent. Stay in the flow.");
    expect(wide).not.toContain("Type a request");

    renderer.resize(18, 24);
    conversationBox.height = 18;
    welcome.relayout();
    await renderOnce();
    expect(welcome.snapshot()).toMatchObject({ mounted: true, mode: "plain" });
    expect(frameText(captureSpans())).toContain("OpenSquilla");
  } finally {
    renderer.destroy?.();
  }
});
