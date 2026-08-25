import { describe, expect, it } from 'vitest'

import { PendingMutationRequestIds } from './mutationRequestIdentity'

describe('PendingMutationRequestIds', () => {
  it('freezes an exact pending payload until its outcome is known', () => {
    const coordinator = new PendingMutationRequestIds(2)
    const key = 'same logical update'
    const requestId = coordinator.idFor(key, 'save')
    const payload = coordinator.freeze(key, requestId, {
      documentId: 'document-1',
      replacement: 'private source',
      clientRequestId: requestId,
    })
    coordinator.markPending(key, requestId)

    expect(Object.isFrozen(payload)).toBe(true)
    expect(coordinator.idFor(key, 'save')).toBe(requestId)
    expect(coordinator.pendingPayload(key, requestId)).toEqual(payload)
    expect(() => coordinator.freeze(key, requestId, {
      documentId: 'document-1',
      replacement: 'different source',
      clientRequestId: requestId,
    })).toThrow('no longer matches')
  })

  it('releases applied requests and retires not-applied requests', () => {
    const coordinator = new PendingMutationRequestIds(2)
    const appliedKey = 'applied'
    const appliedId = coordinator.idFor(appliedKey, 'save')
    coordinator.release(appliedKey, appliedId)
    expect(coordinator.idFor(appliedKey, 'save')).not.toBe(appliedId)

    const rejectedKey = 'not-applied'
    const rejectedId = coordinator.idFor(rejectedKey, 'save')
    coordinator.markNotApplied(rejectedKey, rejectedId)
    expect(coordinator.idFor(rejectedKey, 'save')).not.toBe(rejectedId)
  })

  it('does not evict a request whose outcome is pending', () => {
    const coordinator = new PendingMutationRequestIds(1)
    const pendingId = coordinator.idFor('pending', 'save')
    coordinator.freeze('pending', pendingId, { clientRequestId: pendingId })
    coordinator.markPending('pending', pendingId)

    expect(() => coordinator.idFor('another', 'save')).toThrow(
      'waiting for confirmation',
    )
    expect(coordinator.idFor('pending', 'save')).toBe(pendingId)
  })
})
