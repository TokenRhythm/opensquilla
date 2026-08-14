// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import { handoffPlanQuestionnaireWheel } from './planQuestionnaireWheel'

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
): { event: WheelEvent, forwarded: boolean } {
  const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY })
  let forwarded = false
  target.addEventListener('wheel', current => {
    forwarded = handoffPlanQuestionnaireWheel(current as WheelEvent, thread)
  }, { once: true })
  target.dispatchEvent(event)
  return { event, forwarded }
}

describe('handoffPlanQuestionnaireWheel', () => {
  let thread: HTMLElement
  let body: HTMLElement
  let choice: HTMLElement

  beforeEach(() => {
    document.body.innerHTML = ''
    thread = document.createElement('div')
    body = document.createElement('div')
    choice = document.createElement('label')
    body.className = 'clarify-card__body'
    body.appendChild(choice)
    document.body.append(thread, body)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(body, { clientHeight: 100, scrollHeight: 300, scrollTop: 80 })
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
    document.body.appendChild(header)

    const { forwarded } = wheelFrom(header, thread, -20)

    expect(forwarded).toBe(true)
    expect(thread.scrollTop).toBe(380)
  })
})
