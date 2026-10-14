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
    const error = ruleBody('.activity-tool-details__line--error')
    expect(error).toContain('color: var(--warn);')
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

describe('ActivityToolDetails bounded window treatment', () => {
  it('bounds the detail window without allowing content to escape', () => {
    const window = ruleBody('.activity-tool-details--bounded')
    expect(window).toContain('max-height: min(20rem, 45vh);')
    expect(window).toContain('overflow: hidden;')
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
