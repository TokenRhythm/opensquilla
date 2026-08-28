import { THEME } from "../theme.mjs";
import { stripTerminalControls } from "../primitives.mjs";

export function createUsageBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  let node = null;
  return {
    begin(meta) {
      node = new TextRenderable(renderer, {
        id: `${idPrefix}-usage`, content: `  · ${stripTerminalControls(String(meta?.text ?? ""))}`, fg: THEME.muted,
      });
      box.add(node); renderer.requestRender?.();
    },
    append() {}, update() {}, end() {},
    // Live /theme switch: re-point the summary at the updated muted token.
    recolor() { if (node) node.fg = THEME.muted; },
  };
}
