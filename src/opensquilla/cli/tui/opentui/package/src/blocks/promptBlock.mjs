import { THEME } from "../theme.mjs";
import { stripTerminalControls } from "../primitives.mjs";

// A prompt starts the causal unit of a turn, so it must remain scannable after
// the transcript fills with thinking, tools, answers, steer notices, and queued
// follow-ups. It uses a low-contrast surface, the brand rail, and an explicit
// `you` role label. The label makes attribution survive monochrome terminals;
// the surface/rail create the visual boundary in normal color modes.
//
// The prompt stays compact (one row for a one-line query) rather than becoming
// a second assistant-style card. A real Box border/background owns the whole
// width, so soft-wrapped and multi-line input keeps the same prompt surface.
export function createPromptBlock(ctx) {
  const { renderer, BoxRenderable, TextRenderable, box, idPrefix } = ctx;
  let body = null;
  let label = null;
  let content = null;
  const nodes = []; // every prompt text node, so a live /theme can recolor them
  return {
    begin(meta) {
      body = new BoxRenderable(renderer, {
        id: `${idPrefix}-body`, width: "100%", flexDirection: "row",
        border: ["left"], borderColor: THEME.promptAccent,
        backgroundColor: THEME.promptSurface,
        paddingLeft: 1, paddingRight: 1, flexShrink: 0,
      });
      box.add(body);
      label = new TextRenderable(renderer, {
        id: `${idPrefix}-label`, content: "you", fg: THEME.promptAccent,
        width: 5, flexShrink: 0, wrapMode: "none",
      });
      content = new BoxRenderable(renderer, {
        id: `${idPrefix}-content`, flexDirection: "column", flexGrow: 1, flexShrink: 1,
        backgroundColor: THEME.promptSurface,
      });
      body.add(label);
      body.add(content);
      stripTerminalControls(String(meta?.text ?? "")).split("\n").forEach((line, i) => {
        const n = new TextRenderable(renderer, {
          id: `${idPrefix}-l${i}`, content: line || " ", fg: THEME.promptText,
        });
        content.add(n);
        nodes.push(n);
      });
      renderer.requestRender?.();
    },
    append() {}, update() {}, end() {},
    // Live /theme switch: re-point the rail and every line at the updated
    // tokens. Nothing here is width-dependent, so no relayout is needed.
    recolor() {
      for (const n of nodes) n.fg = THEME.promptText;
      if (label) label.fg = THEME.promptAccent;
      if (body) {
        body.borderColor = THEME.promptAccent;
        body.backgroundColor = THEME.promptSurface;
      }
      if (content) content.backgroundColor = THEME.promptSurface;
    },
  };
}
