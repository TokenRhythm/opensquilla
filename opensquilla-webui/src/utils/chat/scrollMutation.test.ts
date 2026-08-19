// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest'

import { applyProgrammaticScroll, consumeProgrammaticScroll } from './scrollMutation'

function container(top = 0): HTMLElement {
  const element = document.createElement('div')
  Object.defineProperty(element, 'scrollTop', {
    configurable: true,
    writable: true,
    value: top,
  })
  return element
}

describe('chat scroll mutation ownership', () => {
  it('consumes the next scroll position written by the application', () => {
    const thread = container(24)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 180
    })

    expect(consumeProgrammaticScroll(thread)).toBe(true)
    expect(consumeProgrammaticScroll(thread)).toBe(false)
  })

  it('does not swallow a reader move after an application write had no event', () => {
    const thread = container(24)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 180
    })
    // A no-op DOM write does not necessarily emit `scroll`. If the next event
    // instead comes from a native scrollbar drag, its different position must
    // disable live following rather than consume the stale marker.
    thread.scrollTop = 96

    expect(consumeProgrammaticScroll(thread)).toBe(false)
    expect(consumeProgrammaticScroll(thread)).toBe(false)
  })

  it('keeps only the latest application correction while scroll events coalesce', () => {
    const thread = container(24)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 120
    })
    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 240
    })

    expect(consumeProgrammaticScroll(thread)).toBe(true)
  })
})
