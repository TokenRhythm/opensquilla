// WCAG-AA legibility guard for every theme. For each theme, every (foreground
// token, background surface) pair the host actually renders must clear its
// contrast target: 4.5:1 for text, 3:1 for non-text borders/frames. This keeps
// all themes — light ones included — legible, and prevents a palette tweak or a
// new theme from silently shipping invisible text.
//
// Run with: bun test src/theme-contrast.bun.test.mjs
import { test, expect } from "bun:test";

import { STATUS, THEME, THEME_NAMES, applyTheme } from "./theme.mjs";
import { contrastRatio } from "./contrast.mjs";

// { fg token, bg surface, min ratio, where it shows }
const PAIRS = [
  { fg: "text", bg: "appBg", min: 4.5, role: "answer body" },
  { fg: "muted", bg: "appBg", min: 4.5, role: "secondary text" },
  { fg: "detailText", bg: "appBg", min: 4.5, role: "metadata / fallback block" },
  { fg: "promptAccent", bg: "promptSurface", min: 4.5, role: "prompt role label" },
  { fg: "promptText", bg: "promptSurface", min: 4.5, role: "prompt text" },
  { fg: "thinkingAccent", bg: "appBg", min: 4.5, role: "reasoning text" },
  { fg: "routeText", bg: "appBg", min: 4.5, role: "info notice / link" },
  { fg: "success", bg: "appBg", min: 4.5, role: "success notice" },
  { fg: "warning", bg: "appBg", min: 4.5, role: "warn notice" },
  { fg: "error", bg: "appBg", min: 4.5, role: "error notice" },
  { fg: "brandAccent", bg: "appBg", min: 4.5, role: "accent notice / header" },
  { fg: "brandAccentSoft", bg: "appBg", min: 4.5, role: "inline code" },
  { fg: "text", bg: "footerBg", min: 4.5, role: "composer text / router model" },
  { fg: "muted", bg: "footerBg", min: 4.5, role: "placeholder" },
  { fg: "detailText", bg: "footerBg", min: 4.5, role: "router secondary" },
  { fg: "routeText", bg: "footerBg", min: 4.5, role: "router route" },
  { fg: "metricPositive", bg: "footerBg", min: 4.5, role: "router savings" },
  { fg: "warning", bg: "footerBg", min: 4.5, role: "router context" },
  { fg: "error", bg: "footerBg", min: 4.5, role: "router error" },
  { fg: "brandAccent", bg: "footerBg", min: 3.0, role: "composer border" },
  { fg: "composerDisabledBorder", bg: "footerBg", min: 3.0, role: "disabled composer border" },
  { fg: "text", bg: "overlayBg", min: 4.5, role: "picker active row" },
  { fg: "muted", bg: "overlayBg", min: 4.5, role: "picker rows" },
  { fg: "detailText", bg: "overlayBg", min: 4.5, role: "picker hint" },
  { fg: "brandAccentSoft", bg: "overlayBg", min: 4.5, role: "picker marker / inline code" },
  { fg: "brandAccent", bg: "overlayBg", min: 3.0, role: "picker frame" },
];

// The run-state vocabulary toolBlock draws inside the card (on appBg). Checked
// by NAME so remapping a STATUS token in toStatus can never dodge the guard by
// aliasing away from an already-tested palette field.
const STATUS_PAIRS = [
  { fg: "running", bg: "appBg", min: 4.5, role: "running tool row" },
  { fg: "ok", bg: "appBg", min: 4.5, role: "completed tool row" },
  { fg: "error", bg: "appBg", min: 4.5, role: "failed tool row" },
  { fg: "warn", bg: "appBg", min: 4.5, role: "warned tool row" },
  { fg: "queued", bg: "appBg", min: 4.5, role: "queued tool row" },
  { fg: "detail", bg: "appBg", min: 4.5, role: "tool result preview" },
  { fg: "detailError", bg: "appBg", min: 4.5, role: "failed tool result preview" },
];

test("contrastRatio matches known WCAG anchors", () => {
  expect(contrastRatio("#000000", "#FFFFFF")).toBeCloseTo(21, 0);
  expect(contrastRatio("#FFFFFF", "#FFFFFF")).toBeCloseTo(1, 5);
  expect(contrastRatio("#595959", "#FFFFFF")).toBeGreaterThan(4.5); // dark gray passes
  expect(contrastRatio("#AAAAAA", "#FFFFFF")).toBeLessThan(4.5); // light gray fails (function discriminates)
});

for (const name of THEME_NAMES) {
  test(`theme "${name}" keeps every rendered pair legible (WCAG AA)`, () => {
    applyTheme(name);
    const failures = [];
    for (const { fg, bg, min, role } of PAIRS) {
      const r = contrastRatio(THEME[fg], THEME[bg]);
      if (r < min) {
        failures.push(`${role}: ${fg}(${THEME[fg]}) on ${bg}(${THEME[bg]}) = ${r.toFixed(2)} < ${min}`);
      }
    }
    for (const { fg, bg, min, role } of STATUS_PAIRS) {
      const r = contrastRatio(STATUS[fg], THEME[bg]);
      if (r < min) {
        failures.push(`${role}: STATUS.${fg}(${STATUS[fg]}) on ${bg}(${THEME[bg]}) = ${r.toFixed(2)} < ${min}`);
      }
    }
    applyTheme("opensquilla-dark"); // leave a stable default for other tests
    expect(failures).toEqual([]);
  });
}
