import { THEME } from "../theme.mjs";
import { TOOL_INDENT, stripTerminalControls } from "../primitives.mjs";

export function createErrorBlock(ctx) {
  const { renderer, TextRenderable, box, idPrefix } = ctx;
  let node = null;
  return {
    begin(meta) {
      node = new TextRenderable(renderer, {
        id: `${idPrefix}-err`, content: `${TOOL_INDENT}✗ ${stripTerminalControls(String(meta?.text ?? ""))}`, fg: THEME.error,
      });
      box.add(node); renderer.requestRender?.();
    },
    append() {}, update() {}, end() {},
    // Live /theme switch: re-point the error line at the updated error token.
    recolor() { if (node) node.fg = THEME.error; },
  };
}
