import { describe, expect, it } from 'vitest'

import codeDetailsSource from './ActivityCodeDetails.vue?raw'
import toolDetailsSource from './ActivityToolDetails.vue?raw'

function ruleFor(source: string, selector: string): string {
  const start = source.indexOf(`${selector} {`)
  expect(start, `${selector} rule`).toBeGreaterThanOrEqual(0)
  return source.slice(start, source.indexOf('}', start) + 1)
}

describe('tool detail scroll windows', () => {
  it('lets a wheel gesture at the inner boundary continue the transcript scroll', () => {
    // `overscroll-behavior: contain` swallows the gesture at either inner
    // boundary, so the transcript never moves while the pointer stays over the
    // expanded details. Both the generic window and the code/diff viewport sit
    // inside the chat column and must hand the remainder to it.
    expect(ruleFor(toolDetailsSource, '.activity-tool-details__window'))
      .toContain('overscroll-behavior: auto')
    expect(ruleFor(codeDetailsSource, '.activity-code-details__viewport'))
      .toContain('overscroll-behavior: auto')
  })
})
