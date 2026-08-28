// Multi-theme palette for the OpenTUI footer host.
//
// Each theme is an OpenSquilla SEMANTIC palette using the same token names as the
// web UI design system (src/assets/base.css): bg / surfaces, text tints, the
// brand accent family, and the run-state hues (ok / warn / danger / info /
// queued). The host's render tokens are DERIVED from these (see toTokens), so
// every theme is internally consistent and on-brand by construction — adding a
// theme is just supplying one semantic palette.
//
// opensquilla-dark / -light follow the web UI palette, nudged only where a token
// would otherwise fail WCAG AA on the TUI's elevated surfaces (the picker/menu
// overlay); the rest are distinct on-brand aesthetics that keep the orange accent
// family and the semantic run-state structure. Every foreground clears WCAG AA on
// the surfaces it renders on — enforced by theme-contrast.bun.test.mjs.
//
// Color degradation: a color MODE is resolved once at import (detectColorMode)
// and applied inside applyTheme, so live /theme switches degrade the same way
// the startup theme does. Three modes:
//   truecolor — tokens pass through unchanged (the default);
//   compat16  — every token is quantized to the nearest canonical ANSI-16 RGB
//               value (quantizeToBasic16) for 16-color-only terminals;
//   mono      — the grayscale "monochrome" palette is forced for NO_COLOR and
//               TERM=dumb.
// The engine below us already owns the 256-color case: @opentui/core's native
// renderer detects rgb/ansi256 capability and, on ansi256-only terminals, syncs
// a native 256-color palette — so this layer only handles NO_COLOR, 16-color,
// and the explicit OPENSQUILLA_TUI_COLOR override.
//
// NO_COLOR in a cell renderer: the host paints opaque backgrounds on every
// surface, so "no color" cannot mean "emit no SGR at all" — the alternate
// screen would be an unreadable terminal-default-on-terminal-default smear.
// We honor the INTENT instead: no hue carries information (the glyph vocabulary
// ✓ ✗ ⚠ › · already encodes the semantics), everything renders as grayscale.
// Per the NO_COLOR spec, user-level configuration (OPENSQUILLA_TUI_COLOR)
// takes precedence over NO_COLOR.

import process from "node:process";

function darkenHex(hex, factor = 0.45) {
  const value = parseInt(String(hex).replace("#", ""), 16);
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  return `#${channels
    .map((channel) => Math.round(channel * factor).toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase()}`;
}

export const PALETTES = Object.freeze({
  // Canonical — verbatim from opensquilla-webui/src/assets/base.css.
  "opensquilla-dark": {
    bg: "#121212", bgSurface: "#1A1A1B", bgElevated: "#232325",
    text: "#ECECEC", textMuted: "#A6A6A8", textDim: "#8C8C8E",
    accent: "#EC6A1A", accentSecondary: "#FF8A4C",
    ok: "#39D7A2", warn: "#E8B23A", danger: "#FF6B6B", info: "#56C2E6", queued: "#8C7DF2",
  },
  "opensquilla-light": {
    bg: "#F7F7F8", bgSurface: "#FFFFFF", bgElevated: "#F0F0F2",
    text: "#18181A", textMuted: "#56565A", textDim: "#6C6C70",
    accent: "#B0440A", accentSecondary: "#B14E1D",
    ok: "#0E7A52", warn: "#8A6410", danger: "#C2382E", info: "#1E6E8C", queued: "#5A48C0",
  },
  // Deep indigo night — cool and calm, brand orange for warmth.
  midnight: {
    bg: "#0B1021", bgSurface: "#121831", bgElevated: "#1A2342",
    text: "#DCE3F2", textMuted: "#93A0C0", textDim: "#828DAD",
    accent: "#EC6A1A", accentSecondary: "#FF9A52",
    ok: "#4FD6B0", warn: "#F0C674", danger: "#FF6B8A", info: "#6AB7FF", queued: "#A78BFA",
  },
  // Warm charcoal/amber — cozy, orange-forward.
  ember: {
    bg: "#16110C", bgSurface: "#1F1810", bgElevated: "#2A2015",
    text: "#F5ECE0", textMuted: "#C2AD92", textDim: "#9A856C",
    accent: "#FF7A1A", accentSecondary: "#FFA45C",
    ok: "#8BD88F", warn: "#F6C177", danger: "#FF7B6B", info: "#E0A878", queued: "#C9A0FF",
  },
  // Cool neutral gray-blue — understated and professional.
  slate: {
    bg: "#15181C", bgSurface: "#1C2026", bgElevated: "#252B33",
    text: "#E2E6EB", textMuted: "#99A2AE", textDim: "#89929B",
    accent: "#EC6A1A", accentSecondary: "#FF8A4C",
    ok: "#5FB89A", warn: "#D7B86A", danger: "#E47C7C", info: "#6FA8C9", queued: "#9384C7",
  },
  // Maximum contrast on pure black — accessibility-first.
  "high-contrast": {
    bg: "#000000", bgSurface: "#0B0B0B", bgElevated: "#151515",
    text: "#FFFFFF", textMuted: "#D0D0D0", textDim: "#ABABAB",
    accent: "#FF8A1F", accentSecondary: "#FFB266",
    ok: "#3DF0A8", warn: "#FFD24A", danger: "#FF5C5C", info: "#5AC8FF", queued: "#BB9CFF",
  },
  // Nordic polar night — recognizable cool palette with the brand accent
  // (brightened so accent text clears 4.5:1 on bgSurface).
  nord: {
    bg: "#2E3440", bgSurface: "#3B4252", bgElevated: "#434C5E",
    text: "#ECEFF4", textMuted: "#C0C7D4", textDim: "#B6BDC9",
    accent: "#FF9446", accentSecondary: "#FFA86A",
    ok: "#A3BE8C", warn: "#EBCB8B", danger: "#D89FA5", info: "#88C0D0", queued: "#B690AF",
  },
  // Near-monochrome — the orange accent is the only saturated hue.
  mono: {
    bg: "#0F0F0F", bgSurface: "#181818", bgElevated: "#222222",
    text: "#EAEAEA", textMuted: "#9A9A9A", textDim: "#8A8A8A",
    accent: "#EC6A1A", accentSecondary: "#FF8A4C",
    ok: "#A7C2B0", warn: "#C9B98A", danger: "#CE9A9A", info: "#9FB3C0", queued: "#B0A6C8",
  },
  // Strict grayscale (every value r==g==b) — forced in mono color mode
  // (NO_COLOR / TERM=dumb), pickable everywhere. Built on the opensquilla-dark
  // lightness ladder; with hue gone, the semantic slots get DISTINCT lightness
  // steps instead (danger brightest → queued dimmest), each clearing WCAG AA on
  // all three surfaces. Hue loss is acceptable by design: the glyph vocabulary
  // (✓ ✗ ⚠ › ·) already carries the run-state semantics.
  monochrome: {
    bg: "#121212", bgSurface: "#1A1A1A", bgElevated: "#232323",
    text: "#ECECEC", textMuted: "#A6A6A6", textDim: "#8C8C8C",
    accent: "#E0E0E0", accentSecondary: "#C4C4C4",
    ok: "#B8B8B8", warn: "#D0D0D0", danger: "#F5F5F5", info: "#A0A0A0", queued: "#949494",
  },
});

export const DEFAULT_THEME = "opensquilla-dark";
export const THEME_NAMES = Object.freeze(Object.keys(PALETTES));

// One coherent run-state color vocabulary for in-card tool rows. Derived from
// the SAME semantic palette as THEME (no new palette values) and repopulated in
// place on every /theme switch, exactly like THEME.
function toStatus(p) {
  return {
    running: p.accentSecondary, // a tool/turn in flight (soft brand orange)
    ok: p.ok,
    error: p.danger,
    warn: p.warn,
    queued: p.queued, // reserved for a future per-tool queued/permission signal
    detail: p.textDim, // tool result preview + secondary lines
    detailError: p.danger, // a result line under a failed tool
  };
}

// Derive the host's render tokens from an OpenSquilla semantic palette. This is
// the single source of how a theme maps onto the UI, so all themes are coherent.
function toTokens(p) {
  return {
    // Brand
    brandAccent: p.accent,
    // OpenTUI's approved block wordmark has a second color channel for its
    // outline/drop-shadow geometry. Keep it materially darker than the fill;
    // collapsing both channels into brandAccent turns the segmented mark into
    // a visually heavy solid slab.
    brandShadow: darkenHex(p.accent),
    brandAccentSoft: p.accentSecondary,
    // Neutrals
    text: p.text,
    muted: p.textMuted,
    detailText: p.textDim,
    // Opaque surfaces (the UI owns its background on every surface)
    appBg: p.bg,
    overlayBg: p.bgElevated,
    footerBg: p.bgSurface,
    // Chrome
    composerBorder: p.accent,
    composerDisabledBorder: p.textDim,
    // Roles
    answerFrame: p.accent,
    thinkingAccent: p.queued,
    routeText: p.info,
    // User turns are the transcript's strongest causal boundary. A dedicated
    // surface + brand rail + explicit role label distinguish them from model
    // process rows without relying on color alone.
    promptAccent: p.accent,
    promptSurface: p.bgSurface,
    promptText: p.text,
    // Semantic status triad + savings metric
    success: p.ok,
    warning: p.warn,
    error: p.danger,
    metricPositive: p.ok,
  };
}

export function resolveThemeName(name) {
  const n = String(name ?? "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(PALETTES, n) ? n : DEFAULT_THEME;
}

// ---- Color degradation ------------------------------------------------------

// Resolve the terminal's color mode from the environment. Precedence (highest
// first — the explicit override outranks NO_COLOR because the NO_COLOR spec
// defers to user-level configuration):
//   a. OPENSQUILLA_TUI_COLOR: "truecolor" | "16" | "mono" (unknown values are
//      ignored and fall through to detection);
//   b. NO_COLOR set to any non-empty value -> mono;
//   c. TERM=dumb -> mono;
//   d. COLORTERM advertising truecolor/24bit -> truecolor;
//   e. TERM families known to speak truecolor (direct/256color entries degrade
//      via the engine's native 256-color palette sync; tmux passes truecolor
//      through when the outer terminal supports it — assume modern) -> truecolor;
//   f. legacy 16-color TERM families -> compat16;
//   g. default -> truecolor. The host already assumes a modern terminal for the
//      alternate screen and mouse tracking, so an unknown TERM is not punished.
export function detectColorMode(env = process.env) {
  const override = String(env.OPENSQUILLA_TUI_COLOR ?? "").trim().toLowerCase();
  if (override === "truecolor") return "truecolor";
  if (override === "16") return "compat16";
  if (override === "mono") return "mono";
  if (String(env.NO_COLOR ?? "") !== "") return "mono";
  const term = String(env.TERM ?? "").trim().toLowerCase();
  if (term === "dumb") return "mono";
  const colorterm = String(env.COLORTERM ?? "").toLowerCase();
  if (colorterm.includes("truecolor") || colorterm.includes("24bit")) return "truecolor";
  if (/direct|256color|kitty|ghostty|wezterm|alacritty|iterm|tmux/.test(term)) return "truecolor";
  if (/^(vt100|vt220|ansi|linux)/.test(term) || term === "xterm" || term === "screen") {
    return "compat16";
  }
  return "truecolor";
}

// The 16 canonical ANSI RGB values — indices 0-15 of the xterm 256-color cube.
// Invariant: this is the SAME base-16 table ansiNotice.mjs's xterm256ToRgb uses
// to classify inbound notice colors; it is duplicated (not imported) on purpose
// so outbound quantization never couples to inbound notice parsing. Keep the
// two tables identical.
const BASIC16 = Object.freeze([
  [0, 0, 0], [128, 0, 0], [0, 128, 0], [128, 128, 0],
  [0, 0, 128], [128, 0, 128], [0, 128, 128], [192, 192, 192],
  [128, 128, 128], [255, 0, 0], [0, 255, 0], [255, 255, 0],
  [0, 0, 255], [255, 0, 255], [0, 255, 255], [255, 255, 255],
]);

// Nearest canonical ANSI-16 value by plain squared-Euclidean RGB distance —
// simple, deterministic (first match wins a tie), and it keeps the brand orange
// on the warm axis (red) across the shipped palettes instead of collapsing it
// into olive the way green-weighted metrics do. Exact basic-16 inputs are
// fixed points. Exported for the quantization tests.
export function quantizeToBasic16(hex) {
  const n = parseInt(String(hex).replace("#", ""), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  let best = BASIC16[0];
  let bestDist = Infinity;
  for (const [cr, cg, cb] of BASIC16) {
    const dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2;
    if (dist < bestDist) {
      bestDist = dist;
      best = [cr, cg, cb];
    }
  }
  return `#${best.map((v) => v.toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}

// The active color mode, resolved ONCE at import so every applyTheme call —
// startup and live /theme switches alike — degrades identically. setColorMode
// exists for the tests (and a future /color command); unknown modes are ignored
// so a bad caller can never wedge the host into an unrenderable state.
let colorMode = detectColorMode(process.env);

export function setColorMode(mode) {
  if (mode === "truecolor" || mode === "compat16" || mode === "mono") colorMode = mode;
  return colorMode;
}

export function activeColorMode() {
  return colorMode;
}

// Live, mutable active-theme token set. Blocks and the composer read THEME.<token>
// at render time, so applyTheme() repopulates this object IN PLACE and a re-render
// recolors the UI without recreating any imports.
export const THEME = {};
// Live run-state colors (see toStatus); repopulated in place alongside THEME.
export const STATUS = {};
let activeTheme = DEFAULT_THEME;

// Listeners fired AFTER THEME is repopulated, so host-owned derived state that is
// NOT a THEME token (e.g. the markdown SyntaxStyle) can refresh on every live
// /theme switch. A listener must never throw — theming must not be breakable.
const _themeListeners = new Set();

export function onThemeApplied(listener) {
  _themeListeners.add(listener);
  return () => _themeListeners.delete(listener);
}

export function applyTheme(name, { explicit = false } = {}) {
  // mono mode overrides the requested palette with the grayscale one. The
  // startup call and the /theme + picker paths all route through here without
  // the flag, so under NO_COLOR they conservatively land on monochrome too —
  // applyThemeExplicit below is the direct-user-request bypass.
  const resolved = colorMode === "mono" && !explicit ? "monochrome" : resolveThemeName(name);
  const tokens = toTokens(PALETTES[resolved]);
  const status = toStatus(PALETTES[resolved]);
  if (colorMode === "compat16") {
    // Quantize AFTER derivation so every consumer — including a live /theme
    // switch — only ever sees basic-16 values on a 16-color terminal.
    for (const key of Object.keys(tokens)) tokens[key] = quantizeToBasic16(tokens[key]);
    for (const key of Object.keys(status)) status[key] = quantizeToBasic16(status[key]);
  }
  Object.assign(THEME, tokens);
  Object.assign(STATUS, status);
  activeTheme = resolved;
  for (const listener of _themeListeners) {
    try {
      listener(THEME, resolved);
    } catch {
      // A faulty listener must not break theme application.
    }
  }
  return resolved;
}

// Direct user selection: bypasses the mono forcing, so a user who explicitly
// asks for a palette gets it even under NO_COLOR (user-level configuration
// outranks NO_COLOR, per its spec). Not wired to the /theme command yet — the
// current command and picker intentionally stay on the conservative path above.
export function applyThemeExplicit(name) {
  return applyTheme(name, { explicit: true });
}

export function activeThemeName() {
  return activeTheme;
}

// Populate THEME at import so every consumer sees a complete token set.
applyTheme(DEFAULT_THEME);

export const STATUS_PULSE_FRAMES = Object.freeze({
  thinking: ["∙", "•", "●", "•"],
  tool: ["◌", "◔", "◑", "◕"],
  output: ["◇", "◆", "◇", "◆"],
  // A distinct half-filled "waiting" pulse, reserved for a future queued state.
  queued: ["◍", "◌", "◍", "◌"],
});
