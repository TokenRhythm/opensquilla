// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  captureElementScrollAnchor,
  captureVisibleMessageAnchor,
  createScrollHandoffGuard,
  restoreElementScrollAnchor,
  restoreMessageAnchor,
  restoreTextScrollAnchor,
  stabilizeMessageAnchor,
} from './scrollAnchor'

function rect(top: number, bottom: number): DOMRect {
  return {
    top,
    bottom,
    left: 0,
    right: 800,
    width: 800,
    height: bottom - top,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect
}

function anchoredFixture() {
  const container = document.createElement('div')
  const image = document.createElement('img')
  const message = document.createElement('article')
  message.dataset.messageId = 'm-50'
  container.append(image, message)
  document.body.append(container)

  let messageContentTop = 200
  Object.defineProperty(image, 'complete', { configurable: true, value: false })
  Object.defineProperty(container, 'scrollTop', { configurable: true, value: 80, writable: true })
  container.getBoundingClientRect = () => rect(0, 600)
  message.getBoundingClientRect = () => {
    const top = messageContentTop - container.scrollTop
    return rect(top, top + 80)
  }
  return {
    container,
    image,
    setMessageContentTop: (value: number) => { messageContentTop = value },
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('message scroll anchoring', () => {
  it('preserves an element offset when live content is replaced by its settled row', () => {
    const container = document.createElement('div')
    const live = document.createElement('div')
    const settled = document.createElement('div')
    container.append(live)
    document.body.append(container)
    Object.defineProperty(container, 'scrollTop', {
      configurable: true,
      value: 1_000,
      writable: true,
    })
    container.getBoundingClientRect = () => rect(100, 700)
    live.getBoundingClientRect = () => rect(260, 340)
    settled.getBoundingClientRect = () => rect(193, 273)

    const anchor = captureElementScrollAnchor(container, live)
    live.replaceWith(settled)

    expect(restoreElementScrollAnchor(anchor, settled)).toBe(true)
    expect(container.scrollTop).toBe(933)
  })

  it('does not move a disconnected or unrelated terminal replacement', () => {
    const container = document.createElement('div')
    const live = document.createElement('div')
    const unrelated = document.createElement('div')
    container.append(live)
    document.body.append(container)
    Object.defineProperty(container, 'scrollTop', {
      configurable: true,
      value: 200,
      writable: true,
    })
    container.getBoundingClientRect = () => rect(0, 600)
    live.getBoundingClientRect = () => rect(100, 180)

    const anchor = captureElementScrollAnchor(container, live)
    expect(restoreElementScrollAnchor(anchor, unrelated)).toBe(false)
    expect(container.scrollTop).toBe(200)
  })

  it('restores the same occurrence of a visible text token after canonical rendering', () => {
    const container = document.createElement('div')
    const replacement = document.createElement('div')
    replacement.append('repeated-token gap ')
    const tokenPrefix = document.createElement('strong')
    tokenPrefix.textContent = 'repeated-'
    const tokenSuffix = document.createElement('em')
    tokenSuffix.textContent = 'token'
    replacement.append(tokenPrefix, tokenSuffix, ' final')
    container.append(replacement)
    document.body.append(container)
    Object.defineProperty(container, 'scrollTop', {
      configurable: true,
      value: 500,
      writable: true,
    })
    container.getBoundingClientRect = () => rect(100, 700)
    const rangePrototype = Range.prototype as Range & {
      getBoundingClientRect?: () => DOMRect
    }
    const previousRangeRect = rangePrototype.getBoundingClientRect
    rangePrototype.getBoundingClientRect = () => rect(180, 200)
    try {
      expect(restoreTextScrollAnchor({
        container,
        token: 'repeated-token',
        occurrence: 1,
        offsetTop: 30,
      }, replacement)).toBe(true)
      expect(container.scrollTop).toBe(550)
    } finally {
      if (previousRangeRect) rangePrototype.getBoundingClientRect = previousRangeRect
      else Reflect.deleteProperty(rangePrototype, 'getBoundingClientRect')
    }
  })

  it('cancels a terminal handoff on fresh reader input or a large baseline change', () => {
    const container = document.createElement('div')
    document.body.append(container)
    Object.defineProperty(container, 'scrollTop', {
      configurable: true,
      value: 100,
      writable: true,
    })
    const guard = createScrollHandoffGuard(container)
    container.scrollTop = 104
    expect(guard.positionChangedBeyondTolerance()).toBe(false)
    container.scrollTop = 120
    expect(guard.positionChangedBeyondTolerance()).toBe(true)
    guard.acceptCurrentPosition()
    expect(guard.positionChangedBeyondTolerance()).toBe(false)
    container.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }))
    expect(guard.isCancelled()).toBe(true)
    guard.dispose()

    const pointerGuard = createScrollHandoffGuard(container)
    container.dispatchEvent(new PointerEvent('pointerdown'))
    expect(pointerGuard.isCancelled()).toBe(true)
    pointerGuard.dispose()
  })

  it('uses the visible message position and ignores unrelated bottom growth', () => {
    const { container, setMessageContentTop } = anchoredFixture()
    const anchor = captureVisibleMessageAnchor(container)

    // A prepend moved the durable message by 200px. Any concurrent growth at
    // the live tail is deliberately absent from this calculation.
    setMessageContentTop(400)
    expect(restoreMessageAnchor(anchor)).toBe(true)
    expect(container.scrollTop).toBe(280)
  })

  it('corrects late image layout once but yields to subsequent user scroll intent', async () => {
    const { container, image, setMessageContentTop } = anchoredFixture()
    const anchor = captureVisibleMessageAnchor(container)
    setMessageContentTop(400)
    restoreMessageAnchor(anchor)
    stabilizeMessageAnchor(anchor)

    setMessageContentTop(480)
    image.dispatchEvent(new Event('load'))
    await Promise.resolve()
    expect(container.scrollTop).toBe(360)

    const secondImage = document.createElement('img')
    Object.defineProperty(secondImage, 'complete', { configurable: true, value: false })
    container.prepend(secondImage)
    const secondAnchor = captureVisibleMessageAnchor(container)
    stabilizeMessageAnchor(secondAnchor)
    container.dispatchEvent(new Event('wheel'))
    setMessageContentTop(580)
    secondImage.dispatchEvent(new Event('load'))
    await Promise.resolve()

    expect(container.scrollTop).toBe(360)
  })

  it('yields when an external control programmatically navigates the thread', async () => {
    const { container, image, setMessageContentTop } = anchoredFixture()
    const anchor = captureVisibleMessageAnchor(container)
    setMessageContentTop(400)
    restoreMessageAnchor(anchor)
    stabilizeMessageAnchor(anchor)

    container.scrollTop = 500
    container.dispatchEvent(new Event('scroll'))
    setMessageContentTop(480)
    image.dispatchEvent(new Event('load'))
    await Promise.resolve()

    expect(container.scrollTop).toBe(500)
  })
})
