import { describe, expect, it } from 'vitest'

import {
  persistDeferredMetaDraft,
  takeDeferredMetaDrafts,
  type MetaDraftStorage,
} from './metaDraftOutbox'

function memoryStorage(): MetaDraftStorage {
  const values = new Map<string, string>()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

describe('meta draft outbox', () => {
  it('defers exact requests by session, deduplicates, and consumes only the target', () => {
    const storage = memoryStorage()
    const first = '/meta meta-paper-write -- Keep this paper request'
    const second = '/meta meta-short-drama -- Keep this short-drama request'

    expect(persistDeferredMetaDraft({ sessionKey: 'session-a', launchText: first }, storage))
      .toBe(true)
    expect(persistDeferredMetaDraft({ sessionKey: 'session-a', launchText: first }, storage))
      .toBe(true)
    expect(persistDeferredMetaDraft({ sessionKey: 'session-b', launchText: second }, storage))
      .toBe(true)

    expect(takeDeferredMetaDrafts('session-a', storage)).toEqual([first])
    expect(takeDeferredMetaDrafts('session-a', storage)).toEqual([])
    expect(takeDeferredMetaDrafts('session-b', storage)).toEqual([second])
  })

  it('rejects non-meta and expired payloads', () => {
    const storage = memoryStorage()
    expect(persistDeferredMetaDraft({
      sessionKey: 'session-a',
      launchText: 'ordinary draft',
    }, storage)).toBe(false)
    expect(persistDeferredMetaDraft({
      sessionKey: 'session-a',
      launchText: '/meta meta-paper-write',
      createdAtMs: Date.now() - 8 * 24 * 60 * 60 * 1000,
    }, storage)).toBe(false)
  })
})
