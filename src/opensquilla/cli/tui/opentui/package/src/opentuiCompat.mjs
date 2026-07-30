// Transcript viewport culling remains disabled until the real-terminal
// framebuffer gate proves that an OpenTUI release is safe for dynamic-height
// turns. A wheel gesture therefore only needs a normal dirty render. Forcing a
// complete terminal repaint here makes every scroll frame clear and redraw the
// alternate screen, which is visible as a flash in Terminal.app and embedded
// terminals.
//
// Keep this compatibility boundary even though requestRender() is public: a
// future OpenTUI upgrade can change the narrow invalidation strategy here
// without leaking renderer internals into the product components.
export function invalidateConversationViewport(renderer, scrollBox) {
  if (typeof scrollBox?.requestRender === "function") {
    scrollBox.requestRender();
    return;
  }
  renderer?.requestRender?.();
}

// OpenTUI recalculates Yoga immediately before painting a frame. Transcript
// mutations need to restore their semantic anchor *after* that calculation but
// *before* the new frame reaches the terminal; restoring from the previous
// frame's height either jumps the held viewport or paints one visibly wrong
// frame first. Keep the one OpenTUI-specific pre-paint hook here.
//
// `setFrameCallback` is on OpenTUI 0.4.x's public type surface but is not yet
// covered by its standard documentation. Keep that pre-paint bridge and the
// synchronous Yoga/scrollbar ordering workaround inside this versioned adapter:
// ScrollBox's public scrollTop setter would otherwise clamp against the prior
// frame's range. Product components never reach into those host details.
export function scheduleConversationLayoutCommit(
  renderer,
  scrollBox,
  callback,
  {
    scheduleFallback = (fn) => setTimeout(fn, 0),
    cancelFallback = clearTimeout,
  } = {},
) {
  if (typeof callback !== "function") return () => {};
  let active = true;
  let fallback = null;

  const prepareLayout = () => {
    renderer?.root?.calculateLayout?.();
    const contentLayout = scrollBox?.content?.getLayoutNode?.()?.getComputedLayout?.();
    const viewportLayout = scrollBox?.viewport?.getLayoutNode?.()?.getComputedLayout?.();
    const bar = scrollBox?.verticalScrollBar;
    if (bar && Number.isFinite(Number(contentLayout?.height))) {
      bar.scrollSize = Math.max(0, Number(contentLayout.height));
    }
    if (bar && Number.isFinite(Number(viewportLayout?.height))) {
      bar.viewportSize = Math.max(0, Number(viewportLayout.height));
    }
  };

  const run = () => {
    if (!active) return;
    active = false;
    if (fallback !== null) cancelFallback(fallback);
    fallback = null;
    prepareLayout();
    callback();
  };

  if (
    typeof renderer?.setFrameCallback === "function"
    && typeof renderer?.removeFrameCallback === "function"
  ) {
    const onFrame = () => {
      renderer.removeFrameCallback(onFrame);
      run();
    };
    renderer.setFrameCallback(onFrame);
    renderer.requestRender?.();
    return () => {
      if (!active) return;
      active = false;
      renderer.removeFrameCallback(onFrame);
    };
  }

  // Synthetic/unit renderers do not expose frame callbacks. Their geometry is
  // synchronous, so a scheduled fallback preserves the same coalescing
  // contract without depending on production renderer internals.
  fallback = scheduleFallback(run);
  return () => {
    if (!active) return;
    active = false;
    if (fallback !== null) cancelFallback(fallback);
    fallback = null;
  };
}

// Keep wheel ownership on OpenTUI's public surface. `onMouseScroll` observes the
// event before ScrollBox's built-in handler; a zero multiplier prevents that
// handler from applying a second movement after the application scroller has
// queued its semantic 3-row tick. This avoids overriding protected
// `onMouseEvent`, which is not an application API in OpenTUI 0.4.x.
const APPLICATION_OWNS_SCROLL_ACCELERATION = Object.freeze({
  tick: () => 0,
  reset: () => {},
});

export function installConversationWheelHandler(scrollBox, handleWheel) {
  if (!scrollBox || typeof handleWheel !== "function") return false;
  scrollBox.scrollAcceleration = APPLICATION_OWNS_SCROLL_ACCELERATION;
  scrollBox.onMouseScroll = (event) => {
    handleWheel(event);
    // The event is fully owned by the transcript scroller. Stop bubbling to a
    // parent ScrollBox while leaving OpenTUI's own protected handler intact;
    // its zero acceleration makes that handler a harmless state sync.
    event?.stopPropagation?.();
    event?.preventDefault?.();
  };
  return true;
}
