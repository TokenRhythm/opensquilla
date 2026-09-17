import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const themedWorlds = ['ember', 'miami', 'synthwave', 'terminal', 'vapor']

describe('world theme sidebar navigation chrome', () => {
  it.each(themedWorlds)('keeps the %s new-task row out of elevated theme effects', (theme) => {
    const source = readFileSync(new URL(`./${theme}/world.css`, import.meta.url), 'utf8')

    expect(source).not.toContain('.sidebar-new-session')
  })
})
