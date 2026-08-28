// Parse a Rich-rendered notice line (captured from the Python side's stdout in
// opentui mode) into clean text plus a single semantic severity level, so the
// host can render command notices INSIDE its frame in the active theme's colors
// instead of letting raw ANSI bleed onto the alternate screen.
//
// The Python redirect forwards one console line per notice.write message, still
// carrying Rich's ANSI styling. We strip the control bytes (no more "compact:" ->
// "act:" clipping) and classify the line's dominant foreground color into a
// theme-agnostic level. main.mjs maps the level onto the LIVE THEME tokens, so
// every theme — including light — gets legible, on-brand notices.

import { TOOL_INDENT } from "./primitives.mjs";

// level -> { glyph, token } where token names a THEME color read at render time.
export const NOTICE_LEVELS = Object.freeze({
  error: { glyph: "✗", token: "error" }, // ✗
  warn: { glyph: "⚠", token: "warning" }, // ⚠
  success: { glyph: "✓", token: "success" }, // ✓
  info: { glyph: "›", token: "routeText" }, // ›
  accent: { glyph: "›", token: "brandAccent" }, // › (brand-tinted)
  detail: { glyph: "·", token: "detailText" }, // ·
});

// Box-drawing / block ranges: a line with these is a table/panel border, so it
// renders without a glyph (the glyph only fronts plain status lines).
const BOX_DRAWING = /[─-╿▀-▟]/;

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
// CSI ... <final byte>, e.g. SGR (m) and cursor controls. The parameter class
// [0-?] covers the private-parameter bytes < = > ? too, matching
// primitives.stripTerminalControls, so e.g. ESC[>4;2m is fully removed.
const CSI_RE = new RegExp(ESC + "\\[[0-?]*[ -/]*[@-~]", "g");
// OSC ... (BEL or ST) — hyperlinks etc.
const OSC_RE = new RegExp(ESC + "\\][\\s\\S]*?(?:" + BEL + "|" + ESC + "\\\\)", "g");
// SGR specifically, for color scanning.
const SGR_RE = new RegExp(ESC + "\\[([0-9;]*)m", "g");

// xterm 256-color index -> [r,g,b].
function xterm256ToRgb(n) {
  if (n < 16) {
    const base = [
      [0, 0, 0], [128, 0, 0], [0, 128, 0], [128, 128, 0],
      [0, 0, 128], [128, 0, 128], [0, 128, 128], [192, 192, 192],
      [128, 128, 128], [255, 0, 0], [0, 255, 0], [255, 255, 0],
      [0, 0, 255], [255, 0, 255], [0, 255, 255], [255, 255, 255],
    ];
    return base[n] ?? [128, 128, 128];
  }
  if (n >= 232) {
    const v = 8 + (n - 232) * 10;
    return [v, v, v];
  }
  const c = n - 16;
  const r = Math.floor(c / 36);
  const g = Math.floor((c % 36) / 6);
  const b = c % 6;
  const step = (x) => (x === 0 ? 0 : 55 + x * 40);
  return [step(r), step(g), step(b)];
}

// Standard 8/16-color SGR code -> level (no RGB hue math needed).
function basicCodeLevel(code) {
  switch (code) {
    case 31: case 91: return "error"; // red
    case 32: case 92: return "success"; // green
    case 33: case 93: return "warn"; // yellow
    case 34: case 94: case 36: case 96: return "info"; // blue / cyan
    case 35: case 95: return "accent"; // magenta -> brand-ish
    case 37: case 97: case 90: return "detail"; // white / gray
    default: return null;
  }
}

// RGB -> level by hue (handles Rich truecolor like the #F56600 brand accent).
function rgbLevel([r, g, b]) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max - min < 28) return "detail"; // near-neutral / gray
  const d = max - min;
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h = (((h * 60) % 360) + 360) % 360;
  if (h < 18 || h >= 330) return "error"; // red
  if (h < 40) return "accent"; // orange (brand)
  if (h < 70) return "warn"; // yellow
  if (h < 170) return "success"; // green
  if (h < 270) return "info"; // cyan / blue
  return "accent"; // magenta / purple
}

// Level priority when a line has several colored segments: the most "important"
// wins so a leading dim word never hides a trailing error.
const PRIORITY = { error: 5, warn: 4, success: 3, accent: 2, info: 1, detail: 0 };

// Scan SGR (...m) sequences for the dominant foreground level. Returns null when
// nothing is colored (Rich emitted plain text) so the caller can default.
function dominantLevel(raw) {
  let best = null;
  SGR_RE.lastIndex = 0;
  let m;
  while ((m = SGR_RE.exec(raw)) !== null) {
    const params = m[1].split(";").map((p) => (p === "" ? 0 : Number(p)));
    for (let i = 0; i < params.length; i++) {
      const code = params[i];
      let level = null;
      if (code === 38 || code === 48 || code === 58) {
        // Extended color: 38 = foreground, 48 = background, 58 = underline.
        // All three carry ;5;N or ;2;R;G;B arguments that must be consumed so
        // they are never re-read as standalone codes (a gray background like
        // 48;2;31;31;31 would otherwise classify as basic red/error) — but
        // only the foreground one feeds the level.
        if (params[i + 1] === 5) {
          if (code === 38) level = rgbLevel(xterm256ToRgb(params[i + 2] ?? 0));
          i += 2;
        } else if (params[i + 1] === 2) {
          if (code === 38) {
            level = rgbLevel([params[i + 2] ?? 0, params[i + 3] ?? 0, params[i + 4] ?? 0]);
          }
          i += 4;
        }
      } else {
        level = basicCodeLevel(code);
      }
      if (level && (best === null || PRIORITY[level] > PRIORITY[best])) best = level;
    }
  }
  return best;
}

// Remove every escape sequence (OSC links, CSI/SGR including private-parameter
// forms, charset selects), then sweep residual control bytes — lone/truncated
// escapes, BEL, backspace, \r — the same sweep as
// primitives.stripTerminalControls, so a line flushed mid-sequence can never
// write raw controls into the terminal grid.
function stripAnsi(raw) {
  return String(raw)
    .replace(OSC_RE, "")
    .replace(CSI_RE, "")
    .replace(new RegExp(`${ESC}[()][0-9A-Za-z]`, "g"), "")
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");
}

// Parse one captured notice line -> { text, level } (level always present).
export function parseNotice(raw) {
  return { text: stripAnsi(raw), level: dominantLevel(raw) ?? "detail" };
}

export function isStructuredLine(text) {
  return BOX_DRAWING.test(text);
}

// The full render recipe for one notice line, shared by main.mjs's notice
// handler and its tests: blank spacer lines drop entirely (null); table/panel
// border lines render as-is so their box-drawing stays aligned; plain status
// lines get their severity glyph. token names the THEME color the caller reads
// at render time (and re-reads on a live theme switch).
export function noticeContent(raw) {
  const { text, level } = parseNotice(String(raw ?? ""));
  if (!text.trim()) return null;
  const spec = NOTICE_LEVELS[level] ?? NOTICE_LEVELS.detail;
  const content = isStructuredLine(text)
    ? `${TOOL_INDENT}${text}`
    : `${TOOL_INDENT}${spec.glyph} ${text}`;
  return { content, token: spec.token };
}

// Re-point already-rendered loose transcript nodes ({ node, token } entries:
// notice lines, plus the scrollback lines that share the registry) at a live
// theme object after an in-place theme switch — renderables capture their fg
// VALUE at creation, so without this a dark/light flip leaves them unreadable.
export function recolorNoticeNodes(nodes, theme) {
  for (const { node, token } of nodes) node.fg = theme[token] ?? theme.detailText;
}
