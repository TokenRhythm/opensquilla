// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  captureElementScrollAnchor,
  createScrollHandoffGuard,
  restoreElementScrollAnchor,
  restoreTextScrollAnchor,
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

afterEach(() => {
  document.body.innerHTML = ''
})

describe('semantic scroll handoff', () => {
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

})
