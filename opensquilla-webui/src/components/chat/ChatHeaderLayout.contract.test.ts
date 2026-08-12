import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import appSource from '@/App.vue?raw'

const baseCssSource = readFileSync(new URL('../../assets/base.css', import.meta.url), 'utf8')
const chatViewCssSource = readFileSync(
  new URL('../../styles/chat-view.css', import.meta.url),
  'utf8',
)

describe('chat header layout ownership contract', () => {
  it('keeps the route header Teleport host permanently mounted in App', () => {
    const routeHeaderHost = appSource.match(
      /<div\s+id="app-route-header"[\s\S]*?<\/div>/,
    )?.[0]

    expect(routeHeaderHost).toBeDefined()
    expect(routeHeaderHost).toContain('class="topbar-route-header"')
    expect(routeHeaderHost).toContain('data-testid="route-header-host"')
    expect(routeHeaderHost).not.toMatch(/\bv-(?:if|else-if|else|show)\b/)
  })

  it('keeps the chat topbar three-column grid owned by App chrome', () => {
    expect(baseCssSource).toContain('.topbar--chat')
    const chatTopbarRule = baseCssSource.match(/\.topbar--chat\s*\{[\s\S]*?\}/)?.[0]

    expect(chatTopbarRule).toBeDefined()
    expect(chatTopbarRule).toContain('display: grid')
    expect(chatTopbarRule).toMatch(
      /grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)\s+auto/,
    )
  })

  it('does not recreate obsolete width reservation or sidebar-driven header layout', () => {
    expect(chatViewCssSource).not.toContain('--topbar-right-reserve')
    expect(chatViewCssSource).not.toMatch(/\.chat-header-(?:left|right)\b/)
    expect(chatViewCssSource).not.toMatch(/\.main--sidebar-compact\s+\.chat-header\b/)
  })
})
