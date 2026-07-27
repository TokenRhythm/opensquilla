import { describe, expect, it } from 'vitest'
import appSource from '@/App.vue?raw'
import chatViewSource from './ChatView.vue?raw'

describe('pending approval navigation wiring', () => {
  it('turns the topbar approval button into a session route plus exact-card focus request', () => {
    expect(appSource).toContain('appStore.requestApprovalFocus(oldest)')
    expect(appSource).toContain(
      `switchToSession(oldest.sessionKey, 'approval.openBlockedSession')`,
    )
  })

  it('clears stale global approval attention while the gateway is disconnected', () => {
    expect(appSource).toContain("if (state !== 'connected')")
    expect(appSource).toContain('appStore.setPendingApprovals([])')
  })

  it('keeps new pending approvals at the bottom and handles exact focus requests', () => {
    expect(chatViewSource).toContain('focusPendingApprovalCard')
    expect(chatViewSource).toContain('livePendingInterruptParts')
    expect(chatViewSource).toMatch(/autoScroll\.value\s*=\s*true/)
    expect(chatViewSource).toContain('scrollToBottom()')
  })
})
