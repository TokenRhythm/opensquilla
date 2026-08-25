/**
 * Small, DOM-only helpers for deciding which chat scroll container owns a
 * wheel gesture.  The conversation view has a number of intentionally nested
 * scrollers (reasoning, tool output, questionnaires, and popovers); making
 * this decision in one place keeps those surfaces from each inventing subtly
 * different delta and edge rules.
 */

export const CHAT_WHEEL_DELTA_MODE_PIXEL = 0
export const CHAT_WHEEL_DELTA_MODE_LINE = 1
export const CHAT_WHEEL_DELTA_MODE_PAGE = 2
export const CHAT_WHEEL_LINE_HEIGHT_PX = 16
export const CHAT_WHEEL_EPSILON_PX = 0.5

export type ChatWheelDirection = 'up' | 'down'

/** The part of WheelEvent used by the helper (also convenient in unit tests). */
export interface ChatWheelEventLike {
  deltaX: number
  deltaY: number
  deltaMode?: number
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
  defaultPrevented?: boolean
  target?: EventTarget | null
  composedPath?: () => EventTarget[]
}

export interface NormalizedChatWheelDelta {
  deltaX: number
  deltaY: number
}

export interface ChatWheelOwnership {
  direction: ChatWheelDirection
  deltaX: number
  deltaY: number
  /** The nearest container that can consume this direction, if any. */
  owner: HTMLElement | null
  /** True when the owner has room in the wheel direction. */
  canScroll: boolean
  /** True when the nearest owner is at its directional edge. */
  atBoundary: boolean
  /** True when CSS overscroll behavior intentionally stops handoff. */
  blockedByOverscroll: boolean
}

export interface ResolveChatWheelOwnershipOptions {
  /** Height used to expand DOM_DELTA_PAGE values. */
  pageHeight?: number
  /** Tolerance for fractional scroll positions and tiny trackpad noise. */
  epsilonPx?: number
  /**
   * Respect `overscroll-behavior-y: contain|none` on nested scrollers.  A
   * caller that performs an explicit sibling handoff may disable this so the
   * gesture can continue in the conversation at the nested edge.
   */
  respectOverscrollBehavior?: boolean
}

export interface ScrollableAncestorOptions {
  epsilonPx?: number
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * Convert pixel, line, and page wheel units to a common pixel scale.  Browser
 * engines disagree on line height and often emit fractional pixel deltas, so
 * the chat boundary uses a stable 16px line estimate and the caller's actual
 * viewport height for page mode.
 */
export function normalizeChatWheelDelta(
  event: Pick<ChatWheelEventLike, 'deltaX' | 'deltaY' | 'deltaMode'>,
  pageHeight = 0,
): NormalizedChatWheelDelta | null {
  const rawX = finiteNumber(event.deltaX)
  const rawY = finiteNumber(event.deltaY)
  if (rawX === null || rawY === null) return null

  const mode = event.deltaMode ?? CHAT_WHEEL_DELTA_MODE_PIXEL
  const safePageHeight = finiteNumber(pageHeight)
  const multiplier = mode === CHAT_WHEEL_DELTA_MODE_LINE
    ? CHAT_WHEEL_LINE_HEIGHT_PX
    : mode === CHAT_WHEEL_DELTA_MODE_PAGE
      ? (safePageHeight !== null && safePageHeight > 0 ? safePageHeight : 1)
      : 1

  return {
    deltaX: rawX * multiplier,
    deltaY: rawY * multiplier,
  }
}

function isPredominantlyHorizontal(
  delta: NormalizedChatWheelDelta,
  shiftKey: boolean,
  epsilonPx: number,
): boolean {
  // Shift+wheel is the conventional horizontal-scroll modifier.  Some
  // browsers report only deltaY for it, so the modifier itself is enough to
  // keep the transcript from stealing the gesture.
  if (shiftKey) return true
  const horizontal = Math.abs(delta.deltaX) > epsilonPx
  return horizontal && Math.abs(delta.deltaX) >= Math.abs(delta.deltaY)
}

/**
 * Return the vertical direction of a chat wheel gesture, or null when the
 * gesture is not transcript navigation (zoom, horizontal, zero, or already
 * consumed by another control).
 */
export function getChatWheelDirection(
  event: ChatWheelEventLike,
  pageHeight = 0,
  epsilonPx = CHAT_WHEEL_EPSILON_PX,
): ChatWheelDirection | null {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey) return null
  const delta = normalizeChatWheelDelta(event, pageHeight)
  return delta ? directionForNormalizedDelta(event, delta, epsilonPx) : null
}

function directionForNormalizedDelta(
  event: Pick<ChatWheelEventLike, 'shiftKey'>,
  delta: NormalizedChatWheelDelta,
  epsilonPx: number,
): ChatWheelDirection | null {
  if (Math.abs(delta.deltaY) <= epsilonPx) return null
  if (isPredominantlyHorizontal(delta, Boolean(event.shiftKey), epsilonPx)) return null
  return delta.deltaY < 0 ? 'up' : 'down'
}

/**
 * Whether a wheel event is eligible for vertical chat ownership.  This is a
 * named predicate for callers that only need filtering and not an owner.
 */
export function isChatVerticalWheel(
  event: ChatWheelEventLike,
  pageHeight = 0,
  epsilonPx = CHAT_WHEEL_EPSILON_PX,
): boolean {
  return getChatWheelDirection(event, pageHeight, epsilonPx) !== null
}

function isHTMLElement(value: EventTarget | null | undefined): value is HTMLElement {
  return typeof HTMLElement !== 'undefined' && value instanceof HTMLElement
}

function parentElement(value: EventTarget | null | undefined): HTMLElement | null {
  if (isHTMLElement(value)) return value
  if (typeof Node !== 'undefined' && value instanceof Node) {
    return value.parentElement
  }
  return null
}

function ancestorPath(target: EventTarget | null): EventTarget[] {
  const path: EventTarget[] = []
  // Include the event target itself when it is an HTMLElement.  Wheel events
  // dispatched directly on a nested scroller (and older WebViews that do not
  // expose composedPath()) otherwise skip the actual owner and start at its
  // parent.  Text-node targets still begin at their containing element.
  let current = isHTMLElement(target) ? target : parentElement(target)
  while (current) {
    path.push(current)
    current = current.parentElement
  }
  return path
}

function computedStyleFor(element: HTMLElement): CSSStyleDeclaration | null {
  if (typeof getComputedStyle === 'function') {
    try {
      return getComputedStyle(element)
    } catch {
      // Detached or partially mocked elements can reject style computation;
      // the inline declaration remains a safe best-effort fallback.
    }
  }
  return null
}

function secondShorthandToken(value: string): string {
  const tokens = value.trim().split(/\s+/).filter(Boolean)
  return tokens.length > 1 ? tokens[1] ?? '' : tokens[0] ?? ''
}

function overflowYFor(element: HTMLElement): string {
  const inline = element.style.overflowY || element.style.overflow
  if (inline) return secondShorthandToken(inline)
  const style = computedStyleFor(element)
  return secondShorthandToken(style?.overflowY || style?.overflow || '')
}

function overscrollBehaviorYFor(element: HTMLElement): string {
  const inline = element.style.overscrollBehaviorY || element.style.overscrollBehavior
  if (inline) return secondShorthandToken(inline)
  const style = computedStyleFor(element)
  return secondShorthandToken(
    style?.overscrollBehaviorY || style?.overscrollBehavior || '',
  )
}

function hasVerticalOverflow(element: HTMLElement, epsilonPx: number): boolean {
  const overflowY = overflowYFor(element)
  if (!['auto', 'scroll', 'overlay'].includes(overflowY)) return false
  return element.scrollHeight > element.clientHeight + epsilonPx
}

function canScrollInDirection(
  element: HTMLElement,
  direction: ChatWheelDirection,
  epsilonPx: number,
): boolean {
  const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight)
  // Safari can expose a negative rubber-band scrollTop at the top. Clamp it
  // for ownership decisions so bounce does not transfer the gesture upward.
  const scrollTop = Math.max(0, element.scrollTop)
  return direction === 'up'
    ? scrollTop > epsilonPx
    : scrollTop < maxScrollTop - epsilonPx
}

function isOverscrollBlocking(element: HTMLElement): boolean {
  const value = overscrollBehaviorYFor(element).trim().toLowerCase()
  return value === 'contain' || value === 'none'
}

/**
 * Find the nearest ancestor with a real vertical scroll range.  `boundary`,
 * when supplied, is inclusive and prevents accidentally selecting the page
 * when a chat sub-surface is being handed off.
 */
export function findNearestScrollableAncestor(
  target: EventTarget | null,
  boundary: HTMLElement | null = null,
  options: ScrollableAncestorOptions = {},
): HTMLElement | null {
  const epsilonPx = options.epsilonPx ?? CHAT_WHEEL_EPSILON_PX
  const path = ancestorPath(target)
  if (boundary && !path.includes(boundary)) return null
  for (const value of path) {
    const current = value as HTMLElement
    if (hasVerticalOverflow(current, epsilonPx)) return current
    if (current === boundary) break
  }
  return null
}

function pathForEvent(event: ChatWheelEventLike): EventTarget[] {
  if (typeof event.composedPath === 'function') {
    const path = event.composedPath()
    if (path.length > 0) return path
  }
  return ancestorPath(event.target ?? null)
}

/**
 * Resolve the nearest scroll owner for a vertical wheel event.  A null result
 * means the event was filtered (horizontal/zoom/etc.) or no scrollable
 * ancestor exists.  A non-null owner at its edge lets callers decide whether
 * to hand off to an outer sibling; `blockedByOverscroll` marks the CSS
 * containment case where the handoff should stop.
 */
export function resolveChatWheelOwnership(
  event: ChatWheelEventLike,
  boundary: HTMLElement | null = null,
  options: ResolveChatWheelOwnershipOptions = {},
): ChatWheelOwnership | null {
  const pageHeight = options.pageHeight ?? 0
  const epsilonPx = options.epsilonPx ?? CHAT_WHEEL_EPSILON_PX
  const delta = normalizeChatWheelDelta(event, pageHeight)
  const direction = event.defaultPrevented || event.ctrlKey || event.metaKey || !delta
    ? null
    : directionForNormalizedDelta(event, delta, epsilonPx)
  if (!delta || !direction) return null

  const respectOverscrollBehavior = options.respectOverscrollBehavior ?? true
  const path = pathForEvent(event)
  const boundaryIndex = boundary
    ? path.findIndex(target => target === boundary)
    : path.length
  // A supplied boundary must actually contain the event target.  Without
  // this check a sibling/page path could accidentally claim ownership.
  if (boundary && boundaryIndex < 0) {
    return {
      direction,
      deltaX: delta.deltaX,
      deltaY: delta.deltaY,
      owner: null,
      canScroll: false,
      atBoundary: false,
      blockedByOverscroll: false,
    }
  }

  let nearestEdgeOwner: HTMLElement | null = null
  for (const [index, target] of path.entries()) {
    if (!isHTMLElement(target)) continue
    if (boundary && index > boundaryIndex) break

    if (hasVerticalOverflow(target, epsilonPx)) {
      const canScroll = canScrollInDirection(target, direction, epsilonPx)
      if (canScroll) {
        return {
          direction,
          deltaX: delta.deltaX,
          deltaY: delta.deltaY,
          owner: target,
          canScroll: true,
          atBoundary: false,
          blockedByOverscroll: false,
        }
      }

      const blockedByOverscroll = respectOverscrollBehavior && isOverscrollBlocking(target)
      if (blockedByOverscroll || target === boundary) {
        return {
          direction,
          deltaX: delta.deltaX,
          deltaY: delta.deltaY,
          owner: target,
          canScroll: false,
          atBoundary: true,
          blockedByOverscroll,
        }
      }
      nearestEdgeOwner ??= target
    }

    // The boundary is inclusive but nothing outside the chat surface may own
    // its wheel.  Stop after inspecting it.
    if (target === boundary) break
  }

  return {
    direction,
    deltaX: delta.deltaX,
    deltaY: delta.deltaY,
    owner: nearestEdgeOwner,
    canScroll: false,
    atBoundary: nearestEdgeOwner !== null,
    blockedByOverscroll: false,
  }
}
