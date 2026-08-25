// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest'

import {
  applyProgrammaticScroll,
  clearProgrammaticScroll,
  consumeProgrammaticScroll,
} from './scrollMutation'

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

    expect(consumeProgrammaticScroll(thread)).toEqual({
      expectedScrollTop: 180,
      matched: true,
    })
    expect(consumeProgrammaticScroll(thread)).toBeNull()
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

    expect(consumeProgrammaticScroll(thread)).toEqual({
      expectedScrollTop: 180,
      matched: false,
    })
    expect(consumeProgrammaticScroll(thread)).toBeNull()
  })

  it('keeps only the latest application correction while scroll events coalesce', () => {
    const thread = container(24)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 120
    })
    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 240
    })

    expect(consumeProgrammaticScroll(thread)).toEqual({
      expectedScrollTop: 240,
      matched: true,
    })
  })

  it('clears a pending application correction explicitly', () => {
    const thread = container(24)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 180
    })
    clearProgrammaticScroll(thread)

    expect(consumeProgrammaticScroll(thread)).toBeNull()
  })

  it('retains the pending target when reader movement coalesces with the correction', () => {
    const thread = container(1_000)

    applyProgrammaticScroll(thread, () => {
      thread.scrollTop = 1_100
    })
    thread.scrollTop = 1_092

    expect(consumeProgrammaticScroll(thread)).toEqual({
      expectedScrollTop: 1_100,
      matched: false,
    })
    expect(consumeProgrammaticScroll(thread)).toBeNull()
  })
})
