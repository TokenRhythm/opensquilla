import {
  resolveChatWheelOwnership,
} from './chatScrollOwnership'

const QUESTIONNAIRE_SCROLL_EPSILON_PX = 0.5

function questionnaireBoundaryForTarget(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null
  return target.closest<HTMLElement>('.clarify-card')
}

/**
 * Preserve the questionnaire's own scrolling until it reaches an edge, then
 * continue the same wheel gesture in the sibling conversation scroller.
 */
export function handoffPlanQuestionnaireWheel(
  event: WheelEvent,
  thread: HTMLElement | null,
): boolean {
  if (!thread) return false

  const boundary = questionnaireBoundaryForTarget(event.target)
  if (!boundary) return false

  const ownership = resolveChatWheelOwnership(
    event,
    boundary,
    {
      pageHeight: thread.clientHeight,
      // This handler is the explicit sibling handoff for the dock.  The
      // nested questionnaire's own CSS containment should not strand the
      // reader at an edge when the conversation can continue scrolling.
      respectOverscrollBehavior: false,
    },
  )
  // The dock is a sibling of `.chat-thread`, so the generic ancestor walk can
  // decide whether the card's own body/intro owns the gesture, but it cannot
  // discover the thread as an ancestor.  Keep the inner scroller untouched
  // while it has room; only then perform the explicit sibling handoff below.
  if (ownership?.owner && ownership.canScroll) return false

  const direction = ownership?.direction
  const deltaY = ownership?.deltaY
  if (!direction || deltaY === undefined) return false
  const maxScrollTop = Math.max(0, thread.scrollHeight - thread.clientHeight)
  const threadCanScroll = direction === 'up'
    ? thread.scrollTop > QUESTIONNAIRE_SCROLL_EPSILON_PX
    : thread.scrollTop < maxScrollTop - QUESTIONNAIRE_SCROLL_EPSILON_PX
  if (!threadCanScroll) return false

  event.preventDefault()
  thread.scrollTop = Math.min(maxScrollTop, Math.max(0, thread.scrollTop + deltaY))
  return true
}

/**
 * Touch equivalent of the dock's wheel handoff. Touchmove cannot rely on the
 * browser's scroll-chain propagation because the questionnaire is a sibling
 * overlay, so an edge gesture is explicitly transferred to the thread.
 */
export function handoffPlanQuestionnaireTouch(
  event: TouchEvent,
  thread: HTMLElement | null,
  /** Mutable per-gesture cursor; updated to the latest touch point. */
  start: { identifier: number, x: number, y: number },
): boolean {
  if (!thread || event.touches.length !== 1) return false
  const touch = Array.from(event.touches).find(item => item.identifier === start.identifier)
  if (!touch) return false
  const deltaX = touch.clientX - start.x
  const deltaY = start.y - touch.clientY
  // `start` is the mutable per-gesture cursor owned by the dock handler. A
  // touchmove delta is incremental, unlike a wheel event's delta; advance the
  // cursor even when the nested questionnaire consumes the move so a later
  // edge handoff cannot replay the whole gesture distance.
  start.x = touch.clientX
  start.y = touch.clientY
  if (Math.abs(deltaY) <= 2 || Math.abs(deltaX) >= Math.abs(deltaY)) return false

  const boundary = questionnaireBoundaryForTarget(event.target)
  if (!boundary) return false
  const ownership = resolveChatWheelOwnership({
    deltaX: 0,
    deltaY: deltaY > 0 ? -1 : 1,
    defaultPrevented: event.defaultPrevented,
    target: event.target,
    composedPath: () => event.composedPath(),
  }, boundary, {
    pageHeight: thread.clientHeight,
    respectOverscrollBehavior: false,
  })
  if (ownership?.owner && ownership.canScroll) return false

  const maxScrollTop = Math.max(0, thread.scrollHeight - thread.clientHeight)
  const threadCanScroll = deltaY > 0
    ? thread.scrollTop > QUESTIONNAIRE_SCROLL_EPSILON_PX
    : thread.scrollTop < maxScrollTop - QUESTIONNAIRE_SCROLL_EPSILON_PX
  if (!threadCanScroll) return false

  event.preventDefault()
  const transfer = deltaY > 0 ? -Math.abs(deltaY) : Math.abs(deltaY)
  thread.scrollTop = Math.min(maxScrollTop, Math.max(0, thread.scrollTop + transfer))
  return true
}
