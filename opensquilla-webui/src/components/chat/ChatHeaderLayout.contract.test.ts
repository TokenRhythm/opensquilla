import { describe, expect, it } from 'vitest'

import appSource from '@/App.vue?raw'

describe('chat header layout ownership contract', () => {
  it('keeps the App-owned route header host permanently mounted', () => {
    const routeHeaderHost = appSource.match(
      /<div\s+id="app-route-header"[\s\S]*?<\/div>/,
    )?.[0]
    const routeHeaderOpeningTag = appSource.match(
      /<div\s+id="app-route-header"[^>]*>/,
    )?.[0]

    expect(routeHeaderHost).toBeDefined()
    expect(routeHeaderOpeningTag).toBeDefined()
    expect(routeHeaderHost).toContain('class="topbar-route-header"')
    expect(routeHeaderHost).toContain('data-testid="route-header-host"')
    expect(routeHeaderOpeningTag).not.toMatch(/\bv-(?:if|else-if|else|show)\b/)
    expect(routeHeaderHost).toContain('<ChatHeaderActions')
    expect(routeHeaderHost).toContain('v-if="isChatRoute"')
    expect(routeHeaderHost).toContain('v-show="chatRouteHeaderVisible"')
  })
})
