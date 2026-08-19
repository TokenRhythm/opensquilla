const expectedProgrammaticScrollTop = new WeakMap<HTMLElement, number>()

/**
 * Records a chat-thread scroll position that was produced by the application
 * itself. Browser scroll events carry no reliable source information: native
 * scrollbar drags, middle-button auto-scroll, and a `scrollTop` assignment can
 * all reach the same listener. The next matching event is therefore consumed
 * by the view, while any different position remains reader-owned.
 */
export function applyProgrammaticScroll(
  container: HTMLElement,
  mutate: () => void,
): void {
  mutate()
  // Record the clamped value rather than the requested target. A short thread
  // or a viewport resize can make the browser choose a smaller legal maximum.
  expectedProgrammaticScrollTop.set(container, container.scrollTop)
}

/**
 * Returns whether this scroll event belongs to the most recent application
 * correction. A mismatch immediately releases the marker so a real reader
 * gesture is never swallowed by a stale correction.
 */
export function consumeProgrammaticScroll(
  container: HTMLElement,
  tolerancePx = 1,
): boolean {
  const expected = expectedProgrammaticScrollTop.get(container)
  if (expected === undefined) return false
  expectedProgrammaticScrollTop.delete(container)
  return Math.abs(container.scrollTop - expected) <= tolerancePx
}
