// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import {
  CHAT_WHEEL_DELTA_MODE_LINE,
  CHAT_WHEEL_DELTA_MODE_PAGE,
  findNearestScrollableAncestor,
  getChatWheelDirection,
  normalizeChatWheelDelta,
  resolveChatWheelOwnership,
} from './chatScrollOwnership'

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

function wheelAt(
  target: HTMLElement,
  options: WheelEventInit = {},
): WheelEvent {
  const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, ...options })
  target.dispatchEvent(event)
  return event
}

describe('chat scroll ownership', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('normalizes pixel, line, and page wheel units', () => {
    expect(normalizeChatWheelDelta({ deltaX: 2, deltaY: -3 }, 400))
      .toEqual({ deltaX: 2, deltaY: -3 })
    expect(normalizeChatWheelDelta({
      deltaX: 2,
      deltaY: -3,
      deltaMode: CHAT_WHEEL_DELTA_MODE_LINE,
    }, 400)).toEqual({ deltaX: 32, deltaY: -48 })
    expect(normalizeChatWheelDelta({
      deltaX: 0,
      deltaY: 1,
      deltaMode: CHAT_WHEEL_DELTA_MODE_PAGE,
    }, 400)).toEqual({ deltaX: 0, deltaY: 400 })
  })

  it('filters zoom, horizontal, modified, consumed, and zero-delta gestures', () => {
    expect(getChatWheelDirection({ deltaX: 0, deltaY: -8 })).toBe('up')
    expect(getChatWheelDirection({ deltaX: 0, deltaY: 8 })).toBe('down')
    expect(getChatWheelDirection({ deltaX: 8, deltaY: 2 })).toBeNull()
    expect(getChatWheelDirection({ deltaX: 0, deltaY: -8, shiftKey: true })).toBeNull()
    expect(getChatWheelDirection({ deltaX: 0, deltaY: -8, ctrlKey: true })).toBeNull()
    expect(getChatWheelDirection({ deltaX: 0, deltaY: -8, metaKey: true })).toBeNull()
    expect(getChatWheelDirection({ deltaX: 0, deltaY: -8, defaultPrevented: true })).toBeNull()
    expect(getChatWheelDirection({ deltaX: 0, deltaY: 0 })).toBeNull()
  })

  it('selects the nearest nested scroller while it can consume the direction', () => {
    const thread = document.createElement('div')
    const nested = document.createElement('div')
    const target = document.createElement('span')
    thread.style.overflowY = 'auto'
    nested.style.overflowY = 'auto'
    nested.append(target)
    thread.append(nested)
    document.body.append(thread)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 400, scrollTop: 120 })

    let ownership: ReturnType<typeof resolveChatWheelOwnership> = null
    target.addEventListener('wheel', event => {
      ownership = resolveChatWheelOwnership(event as WheelEvent, thread)
    }, { once: true })
    wheelAt(target, { deltaY: -20 })

    expect(ownership).toMatchObject({
      owner: nested,
      direction: 'up',
      canScroll: true,
      atBoundary: false,
    })
    expect(findNearestScrollableAncestor(target, thread)).toBe(nested)
  })

  it('hands an edge gesture to the outer thread when nested scrolling is exhausted', () => {
    const thread = document.createElement('div')
    const nested = document.createElement('div')
    const target = document.createElement('span')
    thread.style.overflowY = 'auto'
    nested.style.overflowY = 'auto'
    nested.append(target)
    thread.append(nested)
    document.body.append(thread)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 400, scrollTop: 0 })

    let ownership: ReturnType<typeof resolveChatWheelOwnership> = null
    target.addEventListener('wheel', event => {
      ownership = resolveChatWheelOwnership(event as WheelEvent, thread)
    }, { once: true })
    wheelAt(target, { deltaY: -20 })

    expect(ownership).toMatchObject({
      owner: thread,
      canScroll: true,
      atBoundary: false,
    })
  })

  it('stops at a nested overscroll boundary when CSS containment is respected', () => {
    const thread = document.createElement('div')
    const nested = document.createElement('div')
    const target = document.createElement('span')
    thread.style.overflowY = 'auto'
    nested.style.overflowY = 'auto'
    nested.style.overscrollBehaviorY = 'contain'
    nested.append(target)
    thread.append(nested)
    document.body.append(thread)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 400, scrollTop: 0 })

    let ownership: ReturnType<typeof resolveChatWheelOwnership> = null
    target.addEventListener('wheel', event => {
      ownership = resolveChatWheelOwnership(event as WheelEvent, thread)
    }, { once: true })
    wheelAt(target, { deltaY: -20 })

    expect(ownership).toMatchObject({
      owner: nested,
      canScroll: false,
      atBoundary: true,
      blockedByOverscroll: true,
    })
  })

  it('recognizes shorthand overscroll containment in legacy style surfaces', () => {
    const thread = document.createElement('div')
    const nested = document.createElement('div')
    const target = document.createElement('span')
    thread.style.overflowY = 'auto'
    nested.style.overflow = 'hidden auto'
    nested.style.overscrollBehavior = 'auto contain'
    nested.append(target)
    thread.append(nested)
    document.body.append(thread)
    setScrollMetrics(thread, { clientHeight: 300, scrollHeight: 900, scrollTop: 400 })
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 400, scrollTop: 0 })

    const ownership = resolveChatWheelOwnership({
      deltaX: 0,
      deltaY: -20,
      target,
      composedPath: () => [target, nested, thread, document.body],
    }, thread)

    expect(ownership).toMatchObject({
      owner: nested,
      blockedByOverscroll: true,
    })
  })

  it('does not claim a target outside an explicit chat boundary', () => {
    const outside = document.createElement('div')
    const target = document.createElement('span')
    outside.style.overflowY = 'auto'
    outside.append(target)
    document.body.append(outside)
    setScrollMetrics(outside, { clientHeight: 100, scrollHeight: 300, scrollTop: 100 })
    const boundary = document.createElement('div')

    let ownership: ReturnType<typeof resolveChatWheelOwnership> = null
    target.addEventListener('wheel', event => {
      ownership = resolveChatWheelOwnership(event as WheelEvent, boundary)
    }, { once: true })
    wheelAt(target, { deltaY: 20 })

    expect(ownership).toMatchObject({ owner: null })
    expect(findNearestScrollableAncestor(target, boundary)).toBeNull()
  })

  it('retains the nearest edge owner when no outer container can continue', () => {
    const nested = document.createElement('div')
    const target = document.createElement('span')
    nested.style.overflowY = 'auto'
    nested.append(target)
    document.body.append(nested)
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 300, scrollTop: 0 })

    let ownership: ReturnType<typeof resolveChatWheelOwnership> = null
    target.addEventListener('wheel', event => {
      ownership = resolveChatWheelOwnership(event as WheelEvent)
    }, { once: true })
    wheelAt(target, { deltaY: -20 })

    expect(ownership).toMatchObject({
      owner: nested,
      canScroll: false,
      atBoundary: true,
      blockedByOverscroll: false,
    })
  })

  it('includes a scroller that is itself the event target without composedPath', () => {
    const nested = document.createElement('div')
    nested.style.overflowY = 'auto'
    document.body.append(nested)
    setScrollMetrics(nested, { clientHeight: 100, scrollHeight: 300, scrollTop: 80 })

    const event = {
      deltaX: 0,
      deltaY: -20,
      target: nested,
    }
    expect(findNearestScrollableAncestor(nested)).toBe(nested)
    expect(resolveChatWheelOwnership(event)).toMatchObject({
      owner: nested,
      canScroll: true,
      direction: 'up',
    })
  })
})
