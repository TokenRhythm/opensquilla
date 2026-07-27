import { THEME } from "../theme.mjs";
import { stripTerminalControls } from "../primitives.mjs";

// A stream delta can end mid-escape-sequence; stripping it in isolation would
// leak the sequence's payload as visible text. Returns the index where the
// FIRST possibly-unterminated sequence starts — everything before it is fully
// decidable and safe to strip — or -1 when the text ends clean (matching
// stripTerminalControls' grammar). Scanning front-to-back matters: an OSC/DCS
// terminated by ST (ESC \) whose delta boundary lands between the ST's ESC
// and its backslash leaves the buffer ending in a bare ESC, and classifying
// only that LAST ESC would flush the still-open opener through the strip in
// isolation, leaking its payload permanently. A sequence is held back only
// while more data could still complete it; once its fate is fixed by bytes
// already present (complete or malformed alike), the strip treats it the same
// at any split point, so it never needs holding.
function pendingEscapeStart(s) {
  for (let i = s.indexOf("\x1b"); i !== -1; i = s.indexOf("\x1b", i + 1)) {
    const kind = s[i + 1];
    if (kind === undefined) return i; // a bare trailing ESC could open any sequence
    if (kind === "[") {
      // CSI: incomplete until a byte beyond the param/intermediate run exists
      // (a final byte completes it; anything else can never match).
      const run = /^[0-?]*[ -/]*/.exec(s.slice(i + 2))[0];
      if (i + 2 + run.length >= s.length) return i;
    } else if (kind === "]") {
      // OSC: terminated by BEL or ST; the payload cannot contain ESC, so the
      // first BEL/ESC decides — unless it is a trailing ESC awaiting ST's "\".
      const end = /[\x07\x1b]/.exec(s.slice(i + 2));
      if (!end) return i;
      if (end[0] === "\x1b" && i + 2 + end.index === s.length - 1) return i;
    } else if (kind === "P") {
      // DCS: terminated only by ST, so the payload's first ESC decides —
      // unless it trails the buffer, still awaiting the "\".
      const end = s.indexOf("\x1b", i + 2);
      if (end === -1 || end === s.length - 1) return i;
    }
    // Any other second byte is decided already: a two-char sequence the strip
    // removes whole, or not a sequence at all (the stray ESC just drops).
  }
  return -1;
}

// Bound the held-back prefix so a malformed never-terminating sequence cannot
// swallow the rest of the stream.
const CARRY_MAX = 1024;

// The answer is just the streamed markdown body now: its card chrome (header
// rule, left border, footer) is owned by the turn, which wraps the whole
// assistant turn in ONE card so narration and tool calls share a continuous
// gutter. begin() mounts the markdown into the turn's shared card body.
export function createAnswerBlock(ctx) {
  const { renderer, MarkdownRenderable, syntaxStyle, box, idPrefix } = ctx;
  let md = null;
  let text = "";
  let stripped = ""; // control-stripped accumulation — grown per delta, never re-stripped
  let carry = "";    // trailing bytes that may be an unterminated escape prefix
  return {
    get text() { return text; },
    begin() {
      md = new MarkdownRenderable(renderer, { id: `${idPrefix}-md`, content: "", streaming: true, conceal: true, syntaxStyle, fg: THEME.text, tableOptions: { style: "columns" }, internalBlockMode: "top-level", width: "100%" });
      box.add(md);
      renderer.requestRender?.();
    },
    // Strip incrementally: only the new delta (plus any carried escape prefix)
    // runs through stripTerminalControls, so each append costs O(delta) instead
    // of re-stripping the whole accumulated answer.
    append(delta) {
      const chunk = String(delta);
      text += chunk;
      const pending = carry + chunk;
      const esc = pendingEscapeStart(pending);
      const cut = esc !== -1 && pending.length - esc <= CARRY_MAX ? esc : pending.length;
      carry = pending.slice(cut);
      stripped += stripTerminalControls(pending.slice(0, cut));
      if (md) md.content = stripped;
      renderer.requestRender?.();
    },
    update(patch) {
      if (!Object.prototype.hasOwnProperty.call(patch ?? {}, "text")) return;
      text = String(patch.text ?? "");
      carry = "";
      stripped = stripTerminalControls(text);
      if (md) md.content = stripped;
      renderer.requestRender?.();
    },
    end() {
      // Flush the carry: whatever never terminated is still control-stripped
      // (a lone ESC drops, its visible tail renders) — same as a full re-strip.
      stripped += stripTerminalControls(carry);
      carry = "";
      if (md) { md.content = stripped; md.streaming = false; }
      renderer.requestRender?.();
    },
    // Live /theme switch: re-point the body fg at the (in-place updated) THEME
    // and explicitly rebuild the highlighted spans against the re-registered
    // syntaxStyle. Chunk colors are resolved at build time, so re-registering
    // styles repaints nothing by itself, and the fg assignment only forces a
    // rebuild when the two themes' text colors happen to differ.
    recolor() {
      if (!md) return;
      md.fg = THEME.text;
      md.refreshStyles?.();
    },
  };
}
