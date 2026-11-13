import { describe, expect, it, vi } from 'vitest'
import { jevDecision, jevSnapshot, jevEnsemble, jevHandoffDecision } from '@/testing/jevRouting'
import { createV4JevRouting } from './jevRoutingV4'

function harness(value: unknown = jevSnapshot()) {
  const request = vi.fn().mockResolvedValue(value)
  let event: (value: unknown) => void = () => {}
  const close = vi.fn()
  const api = createV4JevRouting({ request }, {
    subscribe: (_name, handler) => { event = handler; return { close } },
  })
  return { api, request, event: (payload: unknown) => event(payload), close }
}

describe('JEV Gateway adapter', () => {
  it('uses typed bounded operations and validates returned snapshots', async () => {
    const h = harness()
    const key = jevSnapshot().sessionKey
    expect(await h.api.get(key)).toEqual(jevSnapshot())
    await h.api.configure(key, { enabled: true, mode: 'trust', budgetPerHour: 2 })
    await h.api.respond(key, 'decision-synthetic', false)
    expect(h.request.mock.calls.map(call => call.slice(0, 2))).toEqual([
      ['jev.get', { sessionKey: key }],
      ['jev.configure', { sessionKey: key, enabled: true, mode: 'trust', budgetPerHour: 2 }],
      ['jev.respond', { sessionKey: key, decisionId: 'decision-synthetic', accepted: false }],
    ])
    expect(h.request.mock.calls[0]![2]).toMatchObject({ timeoutMs: 10_000, timeoutAction: 'reject', recoveryClass: 'safe-read' })
    expect(h.request.mock.calls[2]![2]).toMatchObject({ recoveryClass: 'mutation' })
    h.api.dispose()
  })

  it.each([
    { ...jevSnapshot(), mode: 'unexpected' },
    { ...jevSnapshot(), recommendation: jevDecision({ sessionKey: 'other' }) },
    { ...jevSnapshot(), decisions: [jevDecision({ sessionKey: 'other' })] },
    { ...jevSnapshot(), budgetPerHour: 900 },
    { ...jevSnapshot(), sessionKey: 'other' },
  ])('rejects invalid or cross-session snapshots (%j)', async value => {
    const h = harness(value)
    await expect(h.api.get(jevSnapshot().sessionKey)).rejects.toThrow('invalid decision snapshot')
    h.api.dispose()
  })

  it('ignores malformed live events and closes listeners on disposal', () => {
    const h = harness()
    const listener = vi.fn()
    h.api.subscribe(listener)
    h.event({ sessionKey: 'synthetic' })
    h.event(jevSnapshot({ recommendation: jevDecision({ sessionKey: 'other' }) }))
    expect(listener).not.toHaveBeenCalled()
    h.event(jevSnapshot({ recommendation: jevDecision() }))
    expect(listener).toHaveBeenCalledExactlyOnceWith(jevSnapshot({ recommendation: jevDecision() }))
    h.api.dispose()
    h.api.dispose()
    h.event(jevSnapshot())
    expect(listener).toHaveBeenCalledOnce()
    expect(h.close).toHaveBeenCalledOnce()
  })

  it('does not request unsupported operations from an older Gateway', async () => {
    const request = vi.fn()
    const api = createV4JevRouting({ request, supports: () => false }, { subscribe: () => ({ close: vi.fn() }) })
    expect(api.available()).toBe(false)
    await expect(api.get('synthetic')).rejects.toThrow('unavailable')
    expect(request).not.toHaveBeenCalled()
    api.dispose()
  })

  it('validates configuration inputs before sending a mutation', async () => {
    const h = harness()
    await expect(h.api.configure(jevSnapshot().sessionKey, { budgetPerHour: -1 })).rejects.toThrow('Invalid JEV request')
    expect(h.request).not.toHaveBeenCalled()
    h.api.dispose()
  })
})

describe('JEV draft transport', () => {
  it('uses a bounded ephemeral request with cancellation and validates the result', async () => {
    const h = harness(jevSnapshot({ draftId: 'opaque-draft', status: 'checking' }))
    const signal = new AbortController().signal
    expect(h.api.previewAvailable?.()).toBe(true)
    await h.api.preview!(jevSnapshot().sessionKey, 'opaque-draft', 'Synthetic draft', signal)
    expect(h.request).toHaveBeenCalledExactlyOnceWith('jev.preview', {
      sessionKey: jevSnapshot().sessionKey, draftId: 'opaque-draft', text: 'Synthetic draft',
    }, expect.objectContaining({ recoveryClass: 'ephemeral', signal, cancelOnAbort: true, abortAction: 'reject' }))
    h.api.dispose()
  })

  it('keeps ordinary recommendations usable when the Gateway does not support draft preview', async () => {
    const request = vi.fn().mockResolvedValue(jevSnapshot())
    const api = createV4JevRouting({ request, supports: method => method !== 'jev.preview' }, { subscribe: () => ({ close: vi.fn() }) })
    expect(api.available()).toBe(true)
    expect(api.previewAvailable?.()).toBe(false)
    await expect(api.preview!('synthetic', 'draft', 'Synthetic text')).rejects.toThrow('unavailable')
    expect(request).not.toHaveBeenCalled()
    await api.get(jevSnapshot().sessionKey)
    expect(request).toHaveBeenCalledOnce()
    api.dispose()
  })

  it('sends an empty draft as an ephemeral invalidation without a model inference payload', async () => {
    const h = harness(jevSnapshot({ draftId: 'invalidated-draft' }))
    await h.api.preview!(jevSnapshot().sessionKey, 'invalidated-draft', '')
    expect(h.request).toHaveBeenCalledExactlyOnceWith('jev.preview', {
      sessionKey: jevSnapshot().sessionKey, draftId: 'invalidated-draft', text: '',
    }, expect.objectContaining({ recoveryClass: 'ephemeral' }))
    h.api.dispose()
  })

  it('rejects oversized unsent text and invalid settings before transport', async () => {
    const h = harness()
    await expect(h.api.preview!('synthetic', 'draft', 'x'.repeat(12001))).rejects.toThrow('Invalid JEV request')
    await expect(h.api.configure('synthetic', { loopInterval: 4 as 3 })).rejects.toThrow('Invalid JEV request')
    await expect(h.api.configure('synthetic', { autoSwitchSeconds: -1 })).rejects.toThrow('Invalid JEV request')
    expect(h.request).not.toHaveBeenCalled()
    h.api.dispose()
  })
})

describe('JEV expanded action contracts', () => {
  it.each([
    jevSnapshot({ ensembleAvailable: true, ensemblePlan: jevEnsemble(), recommendation: jevDecision({
      action: 'ensemble', target: null, ensemble: jevEnsemble(),
    }) }),
    jevSnapshot({ recommendation: jevHandoffDecision() }),
  ])('accepts action metadata with a null single-model target', async value => {
    const h = harness(value)
    expect(await h.api.get(value.sessionKey)).toEqual(value)
    h.api.dispose()
  })

  it('rejects a handoff describing another source and allows a longer bounded response', async () => {
    const h = harness(jevSnapshot({ recommendation: jevHandoffDecision({ handoff: {
      sourceSessionKey: 'another-session', contextTokens: 64000, contextWindowTokens: 80000,
    } }) }))
    await expect(h.api.get(jevSnapshot().sessionKey)).rejects.toThrow('invalid decision snapshot')
    h.request.mockResolvedValue(jevSnapshot())
    await h.api.respond(jevSnapshot().sessionKey, 'decision-synthetic', true)
    expect(h.request.mock.calls[h.request.mock.calls.length - 1]?.[2]).toMatchObject({ timeoutMs: 90_000, recoveryClass: 'mutation' })
    h.api.dispose()
  })
})
