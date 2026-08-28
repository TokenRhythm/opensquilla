import { textWidth } from "./primitives.mjs";
import { SURFACE_Z_INDEX } from "./screenMode.mjs";

export const HELD_OUTPUT_MESSAGE = "↓ new output · End to follow";

// Render the held-output notice as an opaque root overlay, not a bare text
// node. A TextRenderable only paints the cells occupied by its glyphs; while a
// turn is streaming, transcript cells can consequently show through between
// those glyphs and produce an interleaved line. The filled BoxRenderable owns
// the whole rectangle and makes the notice one atomic framebuffer surface.
export function createHeldOutputIndicator({
  renderer,
  BoxRenderable,
  TextRenderable,
  bottom,
  theme,
}) {
  const node = new BoxRenderable(renderer, {
    id: "scroll-held-indicator",
    position: "absolute",
    left: 1,
    bottom,
    zIndex: SURFACE_Z_INDEX.footerIndicator,
    width: textWidth(HELD_OUTPUT_MESSAGE) + 2,
    height: 1,
    paddingLeft: 1,
    paddingRight: 1,
    flexDirection: "row",
    backgroundColor: theme.appBg,
    shouldFill: true,
    visible: false,
  });
  const label = new TextRenderable(renderer, {
    id: "scroll-held-indicator-label",
    content: HELD_OUTPUT_MESSAGE,
    fg: theme.warning,
    backgroundColor: theme.appBg,
    wrapMode: "none",
  });
  node.add(label);

  return {
    node,
    setVisible(visible) {
      node.visible = Boolean(visible);
    },
    setBottom(nextBottom) {
      node.bottom = nextBottom;
    },
    applyTheme(nextTheme) {
      node.backgroundColor = nextTheme.appBg;
      label.backgroundColor = nextTheme.appBg;
      label.fg = nextTheme.warning;
    },
  };
}
