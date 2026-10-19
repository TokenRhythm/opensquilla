import { describe, expect, it } from 'vitest'
import type { DeliveryWalRecord } from './pendingInputWal'
import { retiredMissingSessionCandidate } from './retiredMissingSession'

function record(id: string, createdAt = 1): DeliveryWalRecord {
  return { schemaVersion: 2, revision: 1, ownerRequestId: id, deliveryIdentity: 'owner', requestSessionKey: 'missing',
    phase: 'not-sent', retired: true, createdAt, updatedAt: createdAt,
    request: { kind: 'send', request: { kind: 'new-turn', params: { sessionKey: 'missing', message: 'draft',
      clientRequestId: id, clientMessageId: `message-${id}` } } } }
}

describe('retired missing session recovery proof', () => {
  it('recognizes the scoped persisted fence after handoff consumption or reload', () => {
    const latest = record('latest', 2)
    expect(retiredMissingSessionCandidate([record('old'), latest], 'owner', 'missing')).toBe(latest)
  })
  it.each(['prepared', 'submitting', 'unknown'] as const)('waits while another request is %s', phase => {
    const unresolved = { ...record('unresolved', 0), phase, retired: undefined }
    expect(retiredMissingSessionCandidate([record('retired'), unresolved], 'owner', 'missing')).toBeNull()
  })
  it.each([1, 2])('does not revive a retired draft after positive acceptance at %s', createdAt => {
    const accepted = { ...record('accepted', createdAt), phase: 'accepted' as const, retired: undefined }
    expect(retiredMissingSessionCandidate([record('retired'), accepted], 'owner', 'missing')).toBeNull()
  })
  it('does not use another identity or session as recovery proof', () => {
    expect(retiredMissingSessionCandidate([record('retired')], 'other', 'missing')).toBeNull()
    expect(retiredMissingSessionCandidate([record('retired')], 'owner', 'other')).toBeNull()
  })
  it('does not convert a plain not-sent result or an accepted result into retirement', () => {
    expect(retiredMissingSessionCandidate([{ ...record('rejected'), retired: undefined }], 'owner', 'missing')).toBeNull()
    expect(retiredMissingSessionCandidate([{ ...record('accepted'), phase: 'accepted' }], 'owner', 'missing')).toBeNull()
  })
})
