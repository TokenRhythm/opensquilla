export const ALTERNATE_SCREEN = "alternate-screen";

// Root-surface paint order is a product contract, not an incidental insertion
// order. Transcript content is clipped below fixed chrome; the opaque footer
// owns every composer row; transient overlays remain above both.
export const SURFACE_Z_INDEX = Object.freeze({
  transcript: 0,
  header: 200,
  footer: 300,
  footerIndicator: 310,
  contextRail: 400,
  overlay: 1000,
});
// The production host owns one terminal lifecycle: a full-screen alternate
// buffer with mouse interaction. Keep this option factory centralized so
// construction and handshake tests assert the same fixed product contract.
export function rendererOptions() {
  return {
    screenMode: ALTERNATE_SCREEN,
    useMouse: true,
  };
}

export function assertRendererScreenMode(renderer) {
  if (renderer?.screenMode !== ALTERNATE_SCREEN) {
    throw new Error(
      `OpenTUI screen mode mismatch: expected=${ALTERNATE_SCREEN} actual=${renderer?.screenMode ?? "unknown"}`,
    );
  }
}

function positiveDimension(value, fallback = 1) {
  const dimension = Number(value);
  if (Number.isFinite(dimension) && dimension > 0) return Math.floor(dimension);
  return fallback;
}

// OpenTUI documents `width`/`height` as the current render-surface dimensions,
// and its root renderable fills that surface. Keep those public properties as
// the canonical layout source. terminalWidth/terminalHeight are compatibility
// fallbacks for test doubles and pre-initialized renderers only. Every
// OpenSquilla layout and cursor calculation must read this one snapshot.
export function rendererViewportSnapshot(renderer) {
  return Object.freeze({
    width: positiveDimension(renderer?.width, positiveDimension(renderer?.terminalWidth)),
    height: positiveDimension(renderer?.height, positiveDimension(renderer?.terminalHeight)),
  });
}

export function createRendererViewportState(renderer) {
  let epoch = 0;
  let current = Object.freeze({ ...rendererViewportSnapshot(renderer), epoch, reason: "startup" });

  return {
    current: () => current,
    refresh(reason = "layout", dimensions = null) {
      const measured = rendererViewportSnapshot(renderer);
      epoch += 1;
      current = Object.freeze({
        width: positiveDimension(dimensions?.width, measured.width),
        height: positiveDimension(dimensions?.height, measured.height),
        epoch,
        reason: String(reason),
      });
      return current;
    },
  };
}

export function rendererLayoutHeight(renderer) {
  return rendererViewportSnapshot(renderer).height;
}
