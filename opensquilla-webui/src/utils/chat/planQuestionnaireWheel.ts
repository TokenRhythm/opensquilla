const WHEEL_LINE_HEIGHT_PX = 16

function questionnaireBodyForTarget(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null
  return target.closest<HTMLElement>('.clarify-card__body')
}

function questionnaireBodyCanScroll(body: HTMLElement, deltaY: number): boolean {
  const maxScrollTop = Math.max(0, body.scrollHeight - body.clientHeight)
  if (deltaY < 0) return body.scrollTop > 0
  return body.scrollTop < maxScrollTop
}

function wheelDeltaPixels(event: WheelEvent, pageHeight: number): number {
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) {
    return event.deltaY * WHEEL_LINE_HEIGHT_PX
  }
  if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
    return event.deltaY * pageHeight
  }
  return event.deltaY
}

/**
 * Preserve the questionnaire's own scrolling until it reaches an edge, then
 * continue the same wheel gesture in the sibling conversation scroller.
 */
export function handoffPlanQuestionnaireWheel(
  event: WheelEvent,
  thread: HTMLElement | null,
): boolean {
  if (!thread || event.ctrlKey || event.defaultPrevented || event.deltaY === 0) return false

  const questionnaireBody = questionnaireBodyForTarget(event.target)
  if (questionnaireBody && questionnaireBodyCanScroll(questionnaireBody, event.deltaY)) {
    return false
  }

  event.preventDefault()
  thread.scrollTop += wheelDeltaPixels(event, thread.clientHeight)
  return true
}
