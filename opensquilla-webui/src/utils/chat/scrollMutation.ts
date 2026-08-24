const expectedProgrammaticScrollTop = new WeakMap<HTMLElement, number>()

export interface ProgrammaticScrollConsumption {
  expectedScrollTop: number
  matched: boolean
}

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
 * Forget a pending application-owned position without consuming a scroll
 * event. Chat session hand-offs reuse the same outer element, so a correction
 * queued for the previous session must not be allowed to classify the first
 * native event in the next one as programmatic.
 */
export function clearProgrammaticScroll(container: HTMLElement): void {
  expectedProgrammaticScrollTop.delete(container)
}

/**
 * Returns the consumed application target and whether this event reached it.
 * A mismatch immediately releases the marker so a real reader gesture is never
 * swallowed by a stale correction.
 */
export function consumeProgrammaticScroll(
  container: HTMLElement,
  tolerancePx = 1,
): ProgrammaticScrollConsumption | null {
  const expected = expectedProgrammaticScrollTop.get(container)
  if (expected === undefined) return null
  expectedProgrammaticScrollTop.delete(container)
  return {
    expectedScrollTop: expected,
    matched: Math.abs(container.scrollTop - expected) <= tolerancePx,
  }
}
