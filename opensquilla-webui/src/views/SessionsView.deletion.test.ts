import { describe, expect, it } from 'vitest'

import sessionsViewSource from './SessionsView.vue?raw'

describe('SessionsView deletion contract', () => {
  it('removes deleted session keys from the local approval snapshot immediately', () => {
    const applyStart = sessionsViewSource.indexOf('function applyLocalDeletedSessions')
    const applyEnd = sessionsViewSource.indexOf(
      'function handleLocalSessionsDeleted',
      applyStart,
    )
    const applyDelete = sessionsViewSource.slice(applyStart, applyEnd)

    expect(applyDelete).toContain(
      'pendingApprovals.value = pendingApprovals.value.filter(key => !keys.has(key))',
    )
  })

  it('resets session pagination after the RPC connection recovers', () => {
    const handlerStart = sessionsViewSource.indexOf('function handleConnectionState')
    const handlerEnd = sessionsViewSource.indexOf('// ---------------------------------------------------------------------------', handlerStart)
    const handler = sessionsViewSource.slice(handlerStart, handlerEnd)
    const activationStart = sessionsViewSource.indexOf('onActivated(() =>')
    const activationEnd = sessionsViewSource.indexOf('onDeactivated(', activationStart)
    const activation = sessionsViewSource.slice(activationStart, activationEnd)

    expect(handler).toContain("if (state === 'connected') scheduleSessionRefresh()")
    expect(activation).toContain("rpc.on('_state', handleConnectionState)")
  })
})
