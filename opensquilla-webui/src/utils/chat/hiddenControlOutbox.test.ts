import { describe, expect, it, vi } from 'vitest'

import {
  getHiddenControlRequestSnapshot,
  hiddenControlDispatchAttempted,
  hiddenControlReceiptReplayEligible,
  listHiddenControls,
  markHiddenControlDispatchDefinitelyRejected,
  markHiddenControlDispatchAttempted,
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

  it('persists whether an ingress dispatch was attempted', () => {
    const storage = memoryStorage()
    const item = {
      sessionKey: 'agent:main:chat-1',
      clientRequestId: 'stable-request-attempted',
      providerText: '/meta meta-paper-write -- durable request',
      displayText: 'Start document',
    }
    expect(persistHiddenControl(item, storage)).toBe(true)
    expect(hiddenControlDispatchAttempted(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toBe(false)

    expect(markHiddenControlDispatchAttempted(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toBe(true)
    expect(hiddenControlDispatchAttempted(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toBe(true)
  })

  it('preserves the hidden request fingerprint and revokes replay after definite rejection', () => {
    const storage = memoryStorage()
    const item = {
      sessionKey: 'agent:main:chat-1',
      clientRequestId: 'stable-request-fingerprint',
      providerText: '/meta meta-paper-write -- durable request',
      displayText: 'Start document',
      requestSnapshot: {
        intent: 'new_chat',
        initialRoutingMode: 'ensemble' as const,
        source: { elevated: 'enabled', runMode: 'safe' as const },
      },
    }
    expect(persistHiddenControl(item, storage)).toBe(true)
    expect(getHiddenControlRequestSnapshot(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toEqual(item.requestSnapshot)

    expect(markHiddenControlDispatchAttempted(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toBe(true)
    expect(hiddenControlReceiptReplayEligible(
      item.sessionKey,
      item.clientRequestId,
      false,
      storage,
    )).toBe(true)

    expect(markHiddenControlDispatchDefinitelyRejected(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toBe(true)
    expect(hiddenControlReceiptReplayEligible(
      item.sessionKey,
      item.clientRequestId,
      false,
      storage,
    )).toBe(false)
    expect(getHiddenControlRequestSnapshot(
      item.sessionKey,
      item.clientRequestId,
      storage,
    )).toEqual(item.requestSnapshot)
  })

  it('preserves legacy dispatch state as unknown until a capable Gateway can recover it', () => {
    const storage = memoryStorage()
    const item = {
      sessionKey: 'cron:legacy-job:run:legacy-run',
      clientRequestId: 'legacy-hidden-request',
      providerText: '/meta meta-paper-write -- durable request',
      displayText: 'Start document',
      createdAtMs: Date.now(),
    }
    storage.setItem(
      'opensquilla.chat.hiddenControlOutbox:v1',
      JSON.stringify([item]),
    )

    expect(listHiddenControls(item.sessionKey, storage)[0]?.dispatchAttempted).toBeNull()
    expect(hiddenControlReceiptReplayEligible(
      item.sessionKey,
      item.clientRequestId,
      false,
      storage,
    )).toBe(false)
    expect(hiddenControlReceiptReplayEligible(
      item.sessionKey,
      item.clientRequestId,
      true,
      storage,
    )).toBe(true)

    expect(persistHiddenControl(item, storage)).toBe(true)
    expect(listHiddenControls(item.sessionKey, storage)[0]?.dispatchAttempted).toBeNull()
  })
})
