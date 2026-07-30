import { THEME } from "./theme.mjs";
import { clipToCells } from "./primitives.mjs";
import { destroyRenderable } from "./renderableLifecycle.mjs";
import { rendererViewportSnapshot } from "./screenMode.mjs";

// The approved filled/shadowed block wordmark is 98 cells wide. Its font owns
// the leading shadow geometry visible in the reference. The container keeps
// the same one-cell inset as the fixed identity header so every logo row and
// the tagline share one optical left edge.
export const WELCOME_BLOCK_MIN_COLUMNS = 100;
export const WELCOME_TINY_MIN_COLUMNS = 46;
export const WELCOME_DISPLAY_MIN_ROWS = 18;

export function welcomeLogoMode(terminalWidth, terminalHeight, contentWidth = terminalWidth) {
  const width = Math.max(1, Number(contentWidth) || Number(terminalWidth) || 80);
  const height = Math.max(1, Number(terminalHeight) || 24);
  // There is one approved display identity: OpenTUI's filled `block` face.
  // Do not substitute slick/grid faces at intermediate widths — they change
  // the brand from solid pixel type to outlined decorative lettering. Tiny is
  // only a geometric fallback when the 98-cell source asset cannot fit.
  if (height >= WELCOME_DISPLAY_MIN_ROWS && width >= WELCOME_BLOCK_MIN_COLUMNS) return "block";
  if (height >= WELCOME_DISPLAY_MIN_ROWS && width >= WELCOME_TINY_MIN_COLUMNS) return "tiny";
  return "plain";
}

function hasHistory(message) {
  return Array.isArray(message?.messages)
    && message.messages.some((item) => item && typeof item === "object" && !Array.isArray(item));
}

/**
 * Branded empty-session introduction that lives in the transcript.
 *
 * It is intentionally not a permanent dashboard: it appears for an empty
 * canonical history, scrolls away with the first work, disappears on resume,
 * and returns after /new or /reset replaces history with an empty snapshot.
 */
export function createWelcomeView({
  renderer,
  BoxRenderable,
  TextRenderable,
  ASCIIFontRenderable,
  conversationBox,
  contentWidth = null,
  viewport = () => rendererViewportSnapshot(renderer),
}) {
  let eligible = true;
  let node = null;
  let logo = null;
  let tagline = null;
  let mode = null;
  let sequence = 0;

  const mounted = () => (conversationBox.getChildren?.() ?? []).some((child) => child.id === node?.id);

  function availableCells() {
    return Math.max(1, Number(contentWidth?.()) || viewport().width);
  }

  function build() {
    mode = welcomeLogoMode(viewport().width, viewport().height, availableCells());
    const narrow = availableCells() < 64;
    const id = `welcome-${sequence++}`;
    node = new BoxRenderable(renderer, {
      id,
      flexDirection: "column",
      marginTop: viewport().height >= 28 ? 2 : 1,
      // Keep the approved mark and tagline on the identity header's one-cell
      // inset. The previous two-cell wide-screen inset made the brand anchor
      // visibly smaller than the selected reference.
      paddingLeft: mode === "block" ? 1 : narrow ? 1 : 2,
      paddingRight: mode === "block" ? 0 : 1,
      // The selected reference is deliberately just a brand anchor and one
      // promise line. Session/model/gateway context already lives in the fixed
      // header/rail/footer; repeating it here diluted the first-screen rhythm.
      minHeight: mode === "block" ? 10 : mode === "tiny" ? 5 : 3,
      paddingBottom: 1,
      backgroundColor: THEME.appBg,
    });

    if (mode === "plain" || !ASCIIFontRenderable) {
      logo = new TextRenderable(renderer, {
        id: `${id}-logo`,
        content: "OpenSquilla",
        fg: THEME.brandAccent,
        wrapMode: "none",
      });
    } else {
      logo = new ASCIIFontRenderable(renderer, {
        id: `${id}-logo`,
        text: "OpenSquilla",
        font: mode,
        // `block` is a two-channel face: c1 is the orange fill and c2 is the
        // darker outline/shadow visible in the selected brand reference.
        color: [THEME.brandAccent, THEME.brandShadow],
        backgroundColor: THEME.appBg,
        selectable: true,
      });
    }
    tagline = new TextRenderable(renderer, {
      id: `${id}-tagline`,
      content: "Build with your agent. Stay in the flow.",
      fg: THEME.text,
      wrapMode: "none",
      marginTop: mode === "block" ? 1 : 0,
    });
    node.add(logo);
    node.add(tagline);
    refreshText();
  }

  function refreshText() {
    if (!node) return;
    const padding = availableCells() < 64 ? 2 : 4;
    const cells = Math.max(1, availableCells() - padding);
    tagline.content = clipToCells("Build with your agent. Stay in the flow.", cells);
  }

  function mount() {
    if (!eligible || mounted()) return;
    build();
    conversationBox.add(node, 0);
    renderer.requestRender?.();
  }

  function unmount() {
    if (mounted()) destroyRenderable(conversationBox, node);
    node = null;
    logo = null;
    tagline = null;
    renderer.requestRender?.();
  }

  mount();

  return {
    mount,
    unmount,
    syncHistory(message) {
      eligible = !hasHistory(message);
      if (eligible) mount();
      else unmount();
    },
    relayout() {
      if (!eligible) return;
      const nextMode = welcomeLogoMode(
        viewport().width,
        viewport().height,
        availableCells(),
      );
      if (!mounted() || nextMode !== mode) {
        unmount();
        mount();
        return;
      }
      // Height-only resizes and same-mode width changes still alter the welcome
      // spacing. Update those live properties instead of leaving the old
      // margin/padding baked into the node until the next logo-mode change.
      const narrow = availableCells() < 64;
      node.marginTop = viewport().height >= 28 ? 2 : 1;
      node.paddingLeft = mode === "block" ? 1 : narrow ? 1 : 2;
      node.paddingRight = mode === "block" ? 0 : 1;
      tagline.marginTop = mode === "block" ? 1 : 0;
      refreshText();
      renderer.requestRender?.();
    },
    recolor() {
      if (!node) return;
      node.backgroundColor = THEME.appBg;
      if (mode === "plain") logo.fg = THEME.brandAccent;
      else {
        logo.color = [THEME.brandAccent, THEME.brandShadow];
        logo.backgroundColor = THEME.appBg;
      }
      tagline.fg = THEME.text;
      renderer.requestRender?.();
    },
    snapshot: () => ({ eligible, mounted: mounted(), mode }),
  };
}
