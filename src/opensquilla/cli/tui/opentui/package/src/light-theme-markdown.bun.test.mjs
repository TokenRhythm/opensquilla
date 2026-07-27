// Light-theme answer-body legibility:
//   The markdown answer body is colored by the SyntaxStyle "default" token. A
//   bare SyntaxStyle.create() registers no "default", so unstyled paragraph text
//   gets fg:undefined and falls back to an invisible light foreground — the
//   answer looked blank under opensquilla-light. registerThemeStyles must give it
//   a theme-tracked color, and onThemeApplied must refresh it on a live switch.
//
// Run with: bun test src/light-theme-markdown.bun.test.mjs
import { beforeEach, test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { SyntaxStyle } from "@opentui/core";

import { registerThemeStyles } from "./syntaxTheme.mjs";
import { applyTheme, THEME, onThemeApplied, setColorMode } from "./theme.mjs";

const lum = (c) => 0.299 * c.r + 0.587 * c.g + 0.114 * c.b; // scale-free for comparisons

beforeEach(() => {
  setColorMode("truecolor");
});

test("a bare SyntaxStyle has no 'default' style — reproduces the faint-text bug", async () => {
  await createTestRenderer({ width: 10, height: 4 }); // initialize the native render lib
  const s = SyntaxStyle.create();
  // This undefined is exactly why light-theme markdown body text was invisible.
  expect(s.getStyle("default")).toBeUndefined();
});

test("registerThemeStyles gives the body a theme-tracked, legible color", async () => {
  await createTestRenderer({ width: 10, height: 4 });
  const s = SyntaxStyle.create();

  applyTheme("opensquilla-light");
  registerThemeStyles(s, THEME);
  const light = s.getStyle("default");
  expect(light).toBeDefined();
  expect(light.fg).toBeDefined();
  const lightLum = lum(light.fg);

  applyTheme("opensquilla-dark");
  registerThemeStyles(s, THEME);
  const dark = s.getStyle("default");
  const darkLum = lum(dark.fg);

  // Body text inverts with the theme: light theme -> dark text (low luminance),
  // dark theme -> light text (high luminance). They must differ and invert.
  expect(darkLum).toBeGreaterThan(lightLum);
  applyTheme("opensquilla-dark"); // leave a stable default for other tests
});

test("onThemeApplied fires listeners after THEME is repopulated, and unsubscribes", () => {
  let seen = null;
  const off = onThemeApplied((_t, name) => {
    seen = name;
  });
  applyTheme("midnight");
  expect(seen).toBe("midnight");
  // THEME must already be the new palette when the listener runs.
  expect(THEME.appBg).toBe("#0B1021");

  off();
  applyTheme("opensquilla-dark");
  expect(seen).toBe("midnight"); // listener removed -> not called again
});

test("the markdown grammar's dotted captures resolve to registered theme styles", async () => {
  // The bundled tree-sitter markdown grammar emits markup.heading.1…6,
  // markup.link.label/url, markup.raw.block and markup.list.(un)checked, and
  // the style lookup falls back only to the FIRST dotted segment ("markup",
  // unregistered) — so each emitted name must resolve on its own or headings
  // and links render as plain body text.
  await createTestRenderer({ width: 10, height: 4 });
  const s = SyntaxStyle.create();
  applyTheme("opensquilla-dark");
  registerThemeStyles(s, THEME);

  const rgb = (c) => [c.r, c.g, c.b];
  const heading = s.getStyle("markup.heading");
  for (let level = 1; level <= 6; level += 1) {
    const st = s.getStyle(`markup.heading.${level}`);
    expect(st, `markup.heading.${level}`).toBeDefined();
    expect(rgb(st.fg)).toEqual(rgb(heading.fg));
  }
  const link = s.getStyle("markup.link");
  for (const name of ["markup.link.label", "markup.link.url"]) {
    const st = s.getStyle(name);
    expect(st, name).toBeDefined();
    expect(rgb(st.fg)).toEqual(rgb(link.fg));
  }
  const raw = s.getStyle("markup.raw");
  const rawBlock = s.getStyle("markup.raw.block");
  expect(rawBlock).toBeDefined();
  expect(rgb(rawBlock.fg)).toEqual(rgb(raw.fg));
  for (const name of ["markup.list.unchecked", "markup.list.checked"]) {
    expect(s.getStyle(name), name).toBeDefined();
  }
});
