import { describe, expect, it } from 'vitest'

import activityToolDetailsSource from './ActivityToolDetails.vue?raw'

function ruleBody(selector: string): string {
  const selectorStart = activityToolDetailsSource.indexOf(selector)
  expect(selectorStart).toBeGreaterThanOrEqual(0)

  const blockStart = activityToolDetailsSource.indexOf('{', selectorStart)
  const blockEnd = activityToolDetailsSource.indexOf('}', blockStart)
  return activityToolDetailsSource.slice(blockStart + 1, blockEnd)
}

describe('ActivityToolDetails text hierarchy', () => {
  it('keeps compact detail text on AA contrast tokens', () => {
    expect(ruleBody('.activity-tool-details__summary')).toContain(
      'color: var(--text-muted);',
    )
    expect(ruleBody('.activity-tool-details__line--target')).toContain(
      'color: var(--text-muted);',
    )
    expect(ruleBody('.activity-tool-details__line--code')).toContain(
      'color: var(--text-muted);',
    )
    expect(ruleBody('.activity-tool-details__fallback')).toContain(
      'color: var(--text-muted);',
    )
    const error = ruleBody('.activity-tool-details__line--error')
    expect(error).toContain('color: var(--text-muted);')
    expect(error).not.toContain('var(--danger)')
  })
})

describe('ActivityToolDetails narrow-width layout', () => {
  it('lets the summary wrap instead of clipping at max-content width', () => {
    const summary = ruleBody('.activity-tool-details__summary')
    expect(summary).toContain('flex-wrap: wrap;')
    expect(summary).not.toContain('width: max-content;')
    expect(summary).not.toContain('white-space: nowrap;')
  })

  it('keeps the error line shrinkable and wrappable', () => {
    const error = ruleBody('.activity-tool-details__line--error')
    expect(error).not.toContain('flex: 0 0 auto;')
    expect(error).toContain('flex: 1 1 auto;')
    expect(error).toContain('white-space: normal;')
    expect(error).toContain('overflow: visible;')
  })
})

describe('ActivityToolDetails interaction affordances', () => {
  it('marks interactive target and code lines with a resting underline', () => {
    const resting = ruleBody(
      '.activity-tool-details__summary--interactive .activity-tool-details__line--target',
    )
    expect(resting).toContain('text-decoration: underline;')
    expect(resting).toContain('text-decoration-style: dotted;')
  })

  it('gives the fallback affordance a real focus ring', () => {
    const focused = ruleBody('.activity-tool-details__fallback:focus-visible')
    expect(focused).toContain('box-shadow: var(--focus-ring);')
  })
})

describe('ActivityToolDetails bounded window treatment', () => {
  it('uses one borderless shadow container instead of nested cards', () => {
    const window = ruleBody('.activity-tool-details--bounded')
    expect(window).toContain('max-height: min(20rem, 52vh);')
    expect(window).toContain('border: 0;')
    expect(window).toContain('box-shadow: var(--shadow-md);')
    expect(window).toContain('overflow: hidden;')

    const section = ruleBody('.activity-tool-details__section')
    expect(section).toContain('border: 0;')
    expect(section).toContain('background: transparent;')
    const preview = ruleBody('.activity-tool-details__preview')
    expect(preview).toContain('border: 0;')
    expect(preview).toContain('background: transparent;')
  })

  it('keeps the duplicate category title out and provides a bottom fade', () => {
    expect(activityToolDetailsSource).not.toContain('activity-tool-details__title')
    expect(ruleBody('.activity-tool-details__fade')).toContain(
      'background: linear-gradient(to bottom, transparent, var(--bg-elevated));',
    )
  })
})

describe('ActivityToolDetails byte units', () => {
  it('labels 1024-based sizes with binary units', () => {
    expect(activityToolDetailsSource).toContain("['KiB', 'MiB', 'GiB']")
    expect(activityToolDetailsSource).not.toContain("['KB', 'MB', 'GB']")
  })
})

describe('ActivityToolDetails localized facts', () => {
  it('formats projected exit codes through i18n', () => {
    expect(activityToolDetailsSource).toContain(
      "t('shared.runTrace.activityExitCode', { code: formatNumber(line.code) })",
    )
  })
})
