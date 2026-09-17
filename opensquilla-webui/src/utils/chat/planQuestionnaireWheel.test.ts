// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import {
  handoffPlanQuestionnaireTouch,
  handoffPlanQuestionnaireWheel,
} from './planQuestionnaireWheel'

function setScrollMetrics(
  element: HTMLElement,
  values: { clientHeight: number, scrollHeight: number, scrollTop: number },
) {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: values.clientHeight },
    scrollHeight: { configurable: true, value: values.scrollHeight },
    scrollTop: { configurable: true, value: values.scrollTop, writable: true },
  })
}

function wheelFrom(
  target: HTMLElement,
  thread: HTMLElement,
  deltaY: number,
  options: Partial<WheelEventInit> = {},
): { event: WheelEvent, forwarded: boolean } {
  const event = new WheelEvent('wheel', {
    bubbles: true,
    cancelable: true,
    deltaY,
    ...options,
  })
  // happy-dom currently ignores modifier fields in WheelEventInit even
  // though browser engines populate them. Mirror the native readonly fields
  // so the filtering path is exercised deterministically.
  for (const key of ['ctrlKey', 'metaKey', 'shiftKey'] as const) {
    if (options[key] !== undefined) {
      Object.defineProperty(event, key, { configurable: true, value: options[key] })
    }
  }
  let forwarded = false
  target.addEventListener('wheel', current => {
    forwarded = handoffPlanQuestionnaireWheel(current as WheelEvent, thread)
  }, { once: true })
  target.dispatchEvent(event)
  return { event, forwarded }
}

function touchFrom(
  target: HTMLElement,
  thread: HTMLElement,
  startY: number,
  endY: number,
): { event: TouchEvent, forwarded: boolean } {
  const event = new Event('touchmove', { bubbles: true, cancelable: true }) as TouchEvent
  Object.defineProperty(event, 'touches', {
    configurable: true,
    value: [{ identifier: 1, clientX: 10, clientY: endY }],
  })
  let forwarded = false
  target.addEventListener('touchmove', current => {
    forwarded = handoffPlanQuestionnaireTouch(current as TouchEvent, thread, {
      identifier: 1,
      x: 10,
      y: startY,
    })
  }, { once: true })
  target.dispatchEvent(event)
  return { event, forwarded }
}

describe('handoffPlanQuestionnaireWheel', () => {
  let thread: HTMLElement
  let card: HTMLElement
  let body: HTMLElement
  let choice: HTMLElement
  let intro: HTMLElement

  beforeEach(() => {
    document.body.innerHTML = ''
    thread = document.createElement('div')
    card = document.createElement('article')
    body = document.createElement('div')
    choice = document.createElement('label')
    intro = document.createElement('p')
    thread.style.overflowY = 'auto'
    card.className = 'clarify-card'
    body.className = 'clarify-card__body'
    body.style.overflowY = 'auto'
    intro.className = 'clarify-card__intro--long'
    intro.style.overflowY = 'auto'
    intro.style.overscrollBehaviorY = 'contain'
    body.appendChild(choice)
    card.append(intro, body)
    document.body.append(thread, card)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(body, { clientHeight: 100, scrollHeight: 300, scrollTop: 80 })
    setScrollMetrics(intro, { clientHeight: 100, scrollHeight: 400, scrollTop: 120 })
  })

  it('leaves the wheel with the questionnaire while its body can scroll', () => {
    const { event, forwarded } = wheelFrom(choice, thread, -40)

    expect(forwarded).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(thread.scrollTop).toBe(400)
  })

  it('continues upward scrolling in the conversation at the questionnaire top edge', () => {
    body.scrollTop = 0

    const { event, forwarded } = wheelFrom(choice, thread, -40)

    expect(forwarded).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(thread.scrollTop).toBe(360)
  })

  it('continues downward scrolling in the conversation at the questionnaire bottom edge', () => {
    body.scrollTop = 200

    const { forwarded } = wheelFrom(choice, thread, 40)

    expect(forwarded).toBe(true)
    expect(thread.scrollTop).toBe(440)
  })

  it('forwards wheel gestures over the questionnaire header and footer', () => {
    const header = document.createElement('header')
    card.prepend(header)

    const { forwarded } = wheelFrom(header, thread, -20)

    expect(forwarded).toBe(true)
    expect(thread.scrollTop).toBe(380)
  })

  it('keeps a long clarification intro as the wheel owner while it can scroll', () => {
    const before = thread.scrollTop

    const { event, forwarded } = wheelFrom(intro, thread, -40)

    expect(forwarded).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(thread.scrollTop).toBe(before)
  })

  it('hands a long clarification intro to the thread at its edge', () => {
    intro.scrollTop = 0

    const { event, forwarded } = wheelFrom(intro, thread, -40)

    expect(forwarded).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(thread.scrollTop).toBe(360)
  })

  it('hands a single-finger questionnaire edge drag to the thread', () => {
    body.scrollTop = 0
    const { event, forwarded } = touchFrom(choice, thread, 200, 160)

    expect(forwarded).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(thread.scrollTop).toBe(360)
  })

  it('uses incremental touch deltas instead of replaying the whole drag', () => {
    body.scrollTop = 0
    const start = { identifier: 1, x: 10, y: 200 }
    const move = (clientY: number) => {
      const event = new Event('touchmove', { bubbles: true, cancelable: true }) as TouchEvent
      Object.defineProperty(event, 'touches', {
        configurable: true,
        value: [{ identifier: 1, clientX: 10, clientY }],
      })
      let forwarded = false
      choice.addEventListener('touchmove', current => {
        forwarded = handoffPlanQuestionnaireTouch(current as TouchEvent, thread, start)
      }, { once: true })
      choice.dispatchEvent(event)
      return forwarded
    }

    expect(move(190)).toBe(true)
    expect(move(180)).toBe(true)
    expect(start.y).toBe(180)
    expect(thread.scrollTop).toBe(380)
  })

  it('uses line and page delta modes when forwarding to the thread', () => {
    body.scrollTop = 0
    const line = wheelFrom(choice, thread, -2, { deltaMode: WheelEvent.DOM_DELTA_LINE })
    expect(line.forwarded).toBe(true)
    expect(thread.scrollTop).toBe(368)

    body.scrollTop = 200
    const page = wheelFrom(choice, thread, 1, { deltaMode: WheelEvent.DOM_DELTA_PAGE })
    expect(page.forwarded).toBe(true)
    // A page-sized handoff is clamped to the thread's real scroll range.
    expect(thread.scrollTop).toBe(600)
  })

  it.each([
    ['horizontal', { deltaX: 40 }],
    ['shift', { shiftKey: true }],
    ['ctrl zoom', { ctrlKey: true }],
    ['meta zoom', { metaKey: true }],
  ])('does not forward %s gestures', (_label, options) => {
    body.scrollTop = 0
    const { event, forwarded } = wheelFrom(choice, thread, -40, options)

    expect(forwarded).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(thread.scrollTop).toBe(400)
  })
})
