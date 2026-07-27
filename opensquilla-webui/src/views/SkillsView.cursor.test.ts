import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./SkillsView.vue', import.meta.url), 'utf8')

function ruleBody(selectors: RegExp): string {
  const match = source.match(new RegExp(`${selectors.source}\\s*\\{([^}]*)\\}`))
  expect(match, `Missing CSS rule matching ${selectors.source}`).not.toBeNull()
  return match?.[1] ?? ''
}

describe('Skills registry cursor boundaries', () => {
  it('does not leave a default-cursor gap between inputs and buttons', () => {
    const rowRule = ruleBody(/\.sk-registry__head,\s*\.sk-github-install/)

    // Keep vertical spacing when controls wrap, but make their horizontal
    // hit-test boundaries adjacent so the cursor changes only once.
    expect(rowRule).toContain('gap: var(--sp-2) 0;')
  })
})
