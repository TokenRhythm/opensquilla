// Theme registry + live-switch regression tests.
//
// Guarantees: every theme supplies a complete, valid render-token set; theme
// resolution is forgiving (unknown -> default, case/space-insensitive); a
// non-default theme actually paints its own background when rendered; and the
// color-degradation policy holds (mode detection precedence, ANSI-16
// quantization in compat16, forced grayscale in mono).
//
// Run with: bun test src/theme.bun.test.mjs
import { test, expect, afterEach } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import {
  STATUS,
  THEME,
  THEME_NAMES,
  PALETTES,
  DEFAULT_THEME,
  activeColorMode,
  applyTheme,
  applyThemeExplicit,
  detectColorMode,
  quantizeToBasic16,
  resolveThemeName,
  activeThemeName,
  setColorMode,
} from "./theme.mjs";
import { createComposer } from "./composer.mjs";

// Color-mode tests flip module-level state that EVERY suite reads: THEME is
// populated at import time and shared across all test files in one bun run.
// Restore truecolor AND re-apply the default theme after every test — even one
// that failed mid-flip — so no other suite ever observes degraded tokens.
afterEach(() => {
  setColorMode("truecolor");
  applyTheme(DEFAULT_THEME);
});

// The render tokens every block/composer consumer relies on.
const REQUIRED_TOKENS = [
  "brandAccent", "brandShadow", "brandAccentSoft", "text", "muted", "detailText",
  "appBg", "overlayBg", "footerBg", "composerBorder", "composerDisabledBorder",
  "answerFrame", "thinkingAccent", "routeText", "promptAccent", "promptSurface", "promptText",
  "success", "warning", "error", "metricPositive",
];
const HEX = /^#[0-9a-fA-F]{6}$/;

test("every theme supplies a complete, valid-hex render-token set", () => {
  expect(THEME_NAMES.length).toBeGreaterThanOrEqual(6);
  for (const name of THEME_NAMES) {
    applyTheme(name);
    for (const token of REQUIRED_TOKENS) {
      expect(THEME[token], `${name}.${token}`).toBeDefined();
      expect(THEME[token], `${name}.${token}=${THEME[token]}`).toMatch(HEX);
    }
  }
  applyTheme(DEFAULT_THEME);
});

test("every semantic palette defines the OpenSquilla base tokens", () => {
  const base = ["bg", "bgSurface", "bgElevated", "text", "textMuted", "textDim",
    "accent", "accentSecondary", "ok", "warn", "danger", "info", "queued"];
  for (const name of THEME_NAMES) {
    for (const token of base) {
      expect(PALETTES[name][token], `${name}.${token}`).toMatch(HEX);
    }
  }
});

test("theme resolution is forgiving and defaults safely", () => {
  expect(resolveThemeName("midnight")).toBe("midnight");
  expect(resolveThemeName(" MIDNIGHT ")).toBe("midnight"); // trim + case-insensitive
  expect(resolveThemeName("nope")).toBe(DEFAULT_THEME);
  expect(resolveThemeName(undefined)).toBe(DEFAULT_THEME);
  expect(resolveThemeName("")).toBe(DEFAULT_THEME);
});

test("applyTheme mutates the live THEME and tracks the active name", () => {
  applyTheme("opensquilla-dark");
  expect(THEME.appBg).toBe("#121212");
  expect(THEME.brandShadow).toBe("#6A300C");
  expect(activeThemeName()).toBe("opensquilla-dark");
  applyTheme("midnight");
  expect(THEME.appBg).toBe("#0B1021");
  expect(THEME.brandAccent).toBe("#EC6A1A");
  expect(activeThemeName()).toBe("midnight");
  applyTheme(DEFAULT_THEME);
});

test("every truecolor theme keeps the wordmark fill and shadow distinct", () => {
  setColorMode("truecolor");
  for (const name of THEME_NAMES) {
    applyTheme(name);
    expect(THEME.brandShadow, `${name}.brandShadow`).not.toBe(THEME.brandAccent);
  }
});

test("a non-default theme paints its own footer background when rendered", async () => {
  applyTheme("ember"); // bgSurface (footerBg) = #1F1810 -> rgba(31,24,16)
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width: 60, height: 12 });
  const inputBox = new BoxRenderable(renderer, {
    id: "input-region", position: "absolute", left: 0, right: 0, bottom: 0, height: 6,
    backgroundColor: THEME.footerBg,
  });
  renderer.root.add(inputBox);
  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    zIndex: 1000, shouldFill: false, visible: false,
  });
  renderer.root.add(overlayLayer);
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, height: 6,
  });
  renderer.root.add(conversationBox);
  const composer = createComposer({
    renderer, BoxRenderable, TextRenderable, conversationBox, inputBox, overlayLayer,
    footerHeight: 6, sendHostMessage: () => {},
  });
  try { composer.install(); } catch { composer.rerender(); }
  await renderOnce();
  const frame = captureSpans();
  // ember footerBg = #1F1810 = rgb(31,24,16); read the footer margin cell bg.
  const line = frame.lines[12 - 3];
  let col = 0, bg = null;
  for (const span of line.spans) {
    const w = Math.max(1, span.width || 1);
    for (let i = 0; i < w; i += 1) { if (col === 0) bg = span.bg; col += 1; }
  }
  expect(bg).not.toBeNull();
  expect(bg.a).toBeGreaterThan(0); // opaque
  expect(Math.round(bg.r * 255)).toBe(31);
  expect(Math.round(bg.g * 255)).toBe(24);
  expect(Math.round(bg.b * 255)).toBe(16);
  renderer.destroy?.();
  applyTheme(DEFAULT_THEME);
});

test("detectColorMode: override beats NO_COLOR beats TERM=dumb beats capability sniffing", () => {
  // a. OPENSQUILLA_TUI_COLOR wins over everything — the NO_COLOR spec defers
  //    to user-level configuration. Values are trimmed + case-insensitive.
  expect(detectColorMode({ OPENSQUILLA_TUI_COLOR: "truecolor", NO_COLOR: "1", TERM: "dumb" })).toBe("truecolor");
  expect(detectColorMode({ OPENSQUILLA_TUI_COLOR: " 16 ", NO_COLOR: "1" })).toBe("compat16");
  expect(detectColorMode({ OPENSQUILLA_TUI_COLOR: "MONO", COLORTERM: "truecolor" })).toBe("mono");
  // ...but an unknown override value is ignored, falling through to detection.
  expect(detectColorMode({ OPENSQUILLA_TUI_COLOR: "8bit", COLORTERM: "truecolor" })).toBe("truecolor");
  // b. NO_COLOR: any non-empty value forces mono; empty does not count (spec).
  expect(detectColorMode({ NO_COLOR: "1", COLORTERM: "truecolor", TERM: "xterm-256color" })).toBe("mono");
  expect(detectColorMode({ NO_COLOR: "", COLORTERM: "truecolor" })).toBe("truecolor");
  // c. TERM=dumb is mono even when COLORTERM lies about capability.
  expect(detectColorMode({ TERM: "dumb" })).toBe("mono");
  expect(detectColorMode({ TERM: "dumb", COLORTERM: "truecolor" })).toBe("mono");
  // d. COLORTERM advertising truecolor/24bit wins over a legacy TERM.
  expect(detectColorMode({ COLORTERM: "truecolor", TERM: "xterm" })).toBe("truecolor");
  expect(detectColorMode({ COLORTERM: "24bit", TERM: "vt100" })).toBe("truecolor");
  // e. modern TERM families are truecolor (256color entries degrade one layer
  //    down via the engine's native 256-color palette sync).
  expect(detectColorMode({ TERM: "xterm-256color" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "xterm-kitty" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "xterm-ghostty" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "wezterm" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "alacritty" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "tmux-256color" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "xterm-direct" })).toBe("truecolor");
  expect(detectColorMode({ TERM: "screen-256color" })).toBe("truecolor");
  // f. legacy 16-color families — including BARE xterm/screen (no suffix).
  expect(detectColorMode({ TERM: "xterm" })).toBe("compat16");
  expect(detectColorMode({ TERM: "screen" })).toBe("compat16");
  expect(detectColorMode({ TERM: "vt100" })).toBe("compat16");
  expect(detectColorMode({ TERM: "vt220" })).toBe("compat16");
  expect(detectColorMode({ TERM: "ansi" })).toBe("compat16");
  expect(detectColorMode({ TERM: "linux" })).toBe("compat16");
  // g. unset/unknown TERM defaults to truecolor — the host already assumes a
  //    modern terminal for the alternate screen and mouse tracking.
  expect(detectColorMode({})).toBe("truecolor");
  expect(detectColorMode({ TERM: "someterm" })).toBe("truecolor");
});

test("quantizeToBasic16 snaps to the nearest canonical ANSI-16 value", () => {
  // Exact basic-16 inputs are fixed points (idempotence).
  for (const exact of ["#000000", "#800000", "#008000", "#808000", "#000080", "#800080",
    "#008080", "#C0C0C0", "#808080", "#FF0000", "#00FF00", "#FFFF00", "#0000FF",
    "#FF00FF", "#00FFFF", "#FFFFFF"]) {
    expect(quantizeToBasic16(exact)).toBe(exact);
  }
  expect(quantizeToBasic16("#F81005")).toBe("#FF0000"); // near-pure red
  expect(quantizeToBasic16("#7F7F80")).toBe("#808080"); // near-gray
  expect(quantizeToBasic16("#121212")).toBe("#000000"); // dark theme bg -> black
  expect(quantizeToBasic16("#ECECEC")).toBe("#FFFFFF"); // near-white text -> white
  expect(quantizeToBasic16("#EC6A1A")).toBe("#FF0000"); // brand orange stays on the warm axis
});

test("PALETTES.monochrome is strictly grayscale with distinct semantic lightness steps", () => {
  expect(THEME_NAMES).toContain("monochrome");
  for (const [token, hex] of Object.entries(PALETTES.monochrome)) {
    const n = parseInt(hex.slice(1), 16);
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    expect(g, `monochrome.${token}=${hex}`).toBe(r);
    expect(b, `monochrome.${token}=${hex}`).toBe(r);
  }
  // Hue is gone, so lightness is the only channel separating the run states:
  // every semantic slot must sit on its own step.
  const semantic = ["accent", "accentSecondary", "ok", "warn", "danger", "info", "queued"];
  const steps = semantic.map((token) => PALETTES.monochrome[token]);
  expect(new Set(steps).size).toBe(semantic.length);
});

test("compat16 mode quantizes every THEME and STATUS value to the ANSI-16 set", () => {
  const BASIC16_HEX = new Set(["#000000", "#800000", "#008000", "#808000", "#000080",
    "#800080", "#008080", "#C0C0C0", "#808080", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF"]);
  setColorMode("compat16");
  // Every theme, because live /theme switches route through applyTheme too —
  // a mid-session switch must stay quantized without any extra wiring.
  for (const name of THEME_NAMES) {
    applyTheme(name);
    for (const [token, value] of Object.entries(THEME)) {
      expect(BASIC16_HEX.has(value), `${name}.THEME.${token}=${value}`).toBe(true);
    }
    for (const [token, value] of Object.entries(STATUS)) {
      expect(BASIC16_HEX.has(value), `${name}.STATUS.${token}=${value}`).toBe(true);
    }
  }
});

test("mono mode forces monochrome regardless of the requested theme name", () => {
  setColorMode("mono");
  expect(applyTheme("ember")).toBe("monochrome"); // /theme + picker path
  expect(activeThemeName()).toBe("monochrome");
  expect(THEME.brandAccent).toBe(PALETTES.monochrome.accent);
  expect(applyTheme(undefined)).toBe("monochrome"); // the startup call shape (no env theme)
  // applyThemeExplicit is the direct-user-request bypass: user-level
  // configuration outranks NO_COLOR, so an explicit ask gets the real palette.
  expect(applyThemeExplicit("ember")).toBe("ember");
  expect(THEME.brandAccent).toBe(PALETTES.ember.accent);
});

test("setColorMode restores truecolor behavior and ignores unknown modes", () => {
  setColorMode("mono");
  applyTheme("midnight");
  expect(THEME.appBg).toBe(PALETTES.monochrome.bg);
  setColorMode("truecolor");
  applyTheme("midnight");
  expect(THEME.appBg).toBe("#0B1021"); // full-fidelity midnight again
  expect(activeColorMode()).toBe("truecolor");
  setColorMode("nonsense"); // must not wedge the host into an unknown state
  expect(activeColorMode()).toBe("truecolor");
});
