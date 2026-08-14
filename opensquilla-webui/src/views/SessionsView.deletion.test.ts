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
})
