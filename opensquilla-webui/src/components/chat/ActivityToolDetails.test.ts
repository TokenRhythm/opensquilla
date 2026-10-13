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
  it('uses one scroll container without nested cards or a shadow layer', () => {
    const window = ruleBody('.activity-tool-details--bounded')
    expect(window).toContain('max-height: min(20rem, 45vh);')
    expect(window).toContain('border: 1px solid var(--border);')
    expect(window).not.toContain('box-shadow:');
    expect(window).toContain('overflow: hidden;')

    const section = ruleBody('.activity-tool-details__section')
    expect(section).toContain('border: 0;')
    expect(section).toContain('background: transparent;')
    const preview = ruleBody('.activity-tool-details__preview')
    expect(preview).toContain('border: 0;')
    expect(preview).toContain('background: transparent;')
  })

  it('does not mask retained content with a fade or a second disclosure', () => {
    expect(activityToolDetailsSource).not.toContain('activity-tool-details__title')
    expect(activityToolDetailsSource).not.toContain('activity-tool-details__fade')
    expect(activityToolDetailsSource).not.toContain('shared.runTrace.viewFull')
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
