import { describe, expect, it } from 'vitest'

import {
  listPendingMetaDiscards,
  persistPendingMetaDiscard,
  removePendingMetaDiscard,
  type MetaDiscardStorage,
} from './metaDiscardOutbox'

function memoryStorage(): MetaDiscardStorage {
  const values = new Map<string, string>()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) },
  }
}

describe('meta discard outbox', () => {
  it('survives a reopen and is removed only after explicit confirmation', () => {
    const storage = memoryStorage()
    expect(persistPendingMetaDiscard({
      sessionKey: 'agent:main:webchat:reopen',
      clientRequestId: 'discard-after-reopen',
    }, storage)).toBe(true)

    expect(listPendingMetaDiscards(undefined, storage)).toMatchObject([{
      sessionKey: 'agent:main:webchat:reopen',
      clientRequestId: 'discard-after-reopen',
    }])
    removePendingMetaDiscard(
      'agent:main:webchat:reopen',
      'discard-after-reopen',
      storage,
    )
    expect(listPendingMetaDiscards(undefined, storage)).toEqual([])
  })

  it('does not silently expire after seven days or evict an older cancellation', () => {
    const storage = memoryStorage()
    const olderThanServerRetention = Date.now() - 8 * 24 * 60 * 60 * 1000
    expect(persistPendingMetaDiscard({
      sessionKey: 'agent:main:webchat:old',
      clientRequestId: 'old-cancellation',
      createdAtMs: olderThanServerRetention,
    }, storage)).toBe(true)
    for (let index = 1; index < 20; index += 1) {
      expect(persistPendingMetaDiscard({
        sessionKey: `agent:main:webchat:${index}`,
        clientRequestId: `cancel-${index}`,
      }, storage)).toBe(true)
    }
    expect(persistPendingMetaDiscard({
      sessionKey: 'agent:main:webchat:overflow',
      clientRequestId: 'must-not-evict',
    }, storage)).toBe(false)

    expect(listPendingMetaDiscards(undefined, storage).map(item => item.clientRequestId))
      .toContain('old-cancellation')
  })

  it('survives a local clock moving backwards', () => {
    const storage = memoryStorage()
    expect(persistPendingMetaDiscard({
      sessionKey: 'agent:main:webchat:clock-skew',
      clientRequestId: 'future-clock-cancellation',
      createdAtMs: Date.now() + 24 * 60 * 60 * 1000,
    }, storage)).toBe(true)

    expect(listPendingMetaDiscards(undefined, storage)).toMatchObject([{
      clientRequestId: 'future-clock-cancellation',
    }])
  })
})
