// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  captureVisibleMessageAnchor,
  restoreMessageAnchor,
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
