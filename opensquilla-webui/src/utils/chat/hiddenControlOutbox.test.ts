import { describe, expect, it, vi } from 'vitest'

import {
  listHiddenControls,
  persistHiddenControl,
  removeHiddenControl,
} from './hiddenControlOutbox'

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
}

describe('hidden control durable outbox', () => {
  it('survives a remount and a queue delay longer than the old 15-minute TTL', () => {
    const storage = memoryStorage()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    expect(persistHiddenControl({
      sessionKey: 'agent:main:chat-1',
      clientRequestId: 'stable-request-1',
      providerText: '/meta meta-paper-write -- durable request',
      displayText: '/meta meta-paper-write -- durable request',
    }, storage)).toBe(true)

    vi.setSystemTime(new Date('2026-01-01T00:20:00Z'))
    expect(listHiddenControls('agent:main:chat-1', storage)).toEqual([expect.objectContaining({
      clientRequestId: 'stable-request-1',
      providerText: '/meta meta-paper-write -- durable request',
    })])
    vi.useRealTimers()
  })

  it('keeps one immutable payload per session and client request id', () => {
    const storage = memoryStorage()
    const original = {
      sessionKey: 'agent:main:chat-1',
      clientRequestId: 'stable-request-2',
      providerText: '/meta-replay 0123456789abcdef0123456789abcdef',
      displayText: 'Retry failed step',
    }
    expect(persistHiddenControl(original, storage)).toBe(true)
    expect(persistHiddenControl(original, storage)).toBe(true)
    expect(persistHiddenControl({
      ...original,
      providerText: '/meta meta-short-drama',
    }, storage)).toBe(false)
    expect(listHiddenControls(original.sessionKey, storage)).toHaveLength(1)
    expect(listHiddenControls(original.sessionKey, storage)[0]?.providerText)
      .toBe(original.providerText)

    removeHiddenControl(original.sessionKey, original.clientRequestId, storage)
    expect(listHiddenControls(original.sessionKey, storage)).toEqual([])
  })
})


it('freezes model/provider/routing in the hidden-control identity and keeps legacy records readable', () => {
  const storage = memoryStorage()
  const original = {
    sessionKey: 'agent:main:chat-1', clientRequestId: 'model-request', providerText: '/meta launch', displayText: 'Launch',
    initialSettings: { intent: 'new_chat' as const, initialRoutingMode: 'direct' as const,
      initialModel: 'model-a', initialProvider: 'provider-a' },
  }
  expect(persistHiddenControl(original, storage)).toBe(true)
  expect(persistHiddenControl(original, storage)).toBe(true)
  expect(persistHiddenControl({ ...original,
    initialSettings: { ...original.initialSettings, initialModel: 'model-b' },
  }, storage)).toBe(false)
  expect(listHiddenControls(original.sessionKey, storage)[0]?.initialSettings).toEqual(original.initialSettings)
  expect(persistHiddenControl({ ...original, clientRequestId: 'legacy', initialSettings: undefined }, storage)).toBe(true)
  expect(listHiddenControls(original.sessionKey, storage)[1]).not.toHaveProperty('initialSettings')
})
