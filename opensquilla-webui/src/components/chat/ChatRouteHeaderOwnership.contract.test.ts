import { describe, expect, it } from 'vitest'

import appSource from '@/App.vue?raw'
import chatViewSource from '@/views/ChatView.vue?raw'

describe('chat route header ownership contract', () => {
  it('keeps the only ChatHeaderActions instance in the permanent App host', () => {
    const routeHeaderHost = appSource.match(
      /<div\s+id="app-route-header"[\s\S]*?<\/div>/,
    )?.[0]

    expect(routeHeaderHost).toBeDefined()
    expect(routeHeaderHost).toContain('data-testid="route-header-host"')
    expect(routeHeaderHost).toContain('<ChatHeaderActions')
    expect(routeHeaderHost).toMatch(/<ChatHeaderActions[\s\S]*?\bv-show=/)
    expect(appSource.match(/<ChatHeaderActions\b/g)).toHaveLength(1)
  })

  it('does not let ChatView patch App-owned DOM through Teleport', () => {
    expect(chatViewSource).not.toMatch(/<Teleport\b/)
    expect(chatViewSource).not.toMatch(/<ChatHeaderActions\b/)
    expect(chatViewSource).not.toContain("from '@/components/chat/ChatHeaderActions.vue'")
  })
})
