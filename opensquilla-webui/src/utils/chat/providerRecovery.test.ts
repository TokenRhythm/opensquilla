import { describe, expect, it } from 'vitest'
import { normalizeProviderRecovery, providerHttpStatus, providerRetrySeconds, providerRecoveryIdentity } from './providerRecovery'
import { normalizeTurnOutcome } from './turnOutcome'
import { providerActivityLabel, projectAssistantActivityTimeline } from './assistantActivity'

describe('provider recovery facts', () => {
  const wait = (overrides = {}) => normalizeProviderRecovery({ phase: 'retry_wait', reason: 'rate_limited', started_at: 10_000, retry_after_ms: 35_000, retry_attempt: 1, http_status: 429, ...overrides })!
  it('only accepts an explicit bounded integer HTTP status', () => {
    for (const value of [undefined, null, '429', true, 429.5, 399, 600, Infinity]) expect(providerHttpStatus(value)).toBeUndefined()
    expect(providerHttpStatus(429)).toBe(429)
    expect(normalizeTurnOutcome({ turn_id: 't', status: 'failed', http_status: 429 })?.httpStatus).toBe(429)
    expect(normalizeTurnOutcome({ turn_id: 't', status: 'failed', http_status: '429' })?.httpStatus).toBeNull()
    expect(normalizeTurnOutcome({ turn_id: 't', status: 'failed', http_status: 429, outcome: { http_status: 503 } })?.httpStatus).toBeNull()
    expect(normalizeTurnOutcome({ turn_id: 't', status: 'failed', code: 429 })?.httpStatus).toBeUndefined()
  })
  it('uses server start rather than arrival and never counts below zero', () => {
    expect(providerRetrySeconds(wait(), 20_000)).toBe(25)
    expect(providerRetrySeconds(wait(), 45_000)).toBeNull()
    expect(providerRetrySeconds(wait({ started_at: undefined }), 20_000)).toBeNull()
    expect(providerRetrySeconds(wait({ started_at: 100_000 }), 20_000)).toBeNull()
    expect(providerRetrySeconds(wait({ retry_after_ms: 900_001 }), 20_000)).toBeNull()
    expect(providerRetrySeconds(wait({ retry_after_ms: '35000' }), 20_000)).toBeNull()
  })
  it('keeps a heartbeat identity and distinguishes the next wait plan', () => {
    expect(providerRecoveryIdentity(wait())).toBe(providerRecoveryIdentity(wait()))
    expect(providerRecoveryIdentity(wait())).not.toBe(providerRecoveryIdentity(wait({ started_at: 20_000 })))
    expect(providerRecoveryIdentity(wait())).not.toBe(providerRecoveryIdentity(wait({ retry_attempt: 2 })))
  })
  it('clears current HTTP provenance on retry, fallback, and normal activity', () => {
    for (const phase of ['retrying', 'fallback', 'requesting', 'reasoning']) expect(wait({ phase }).httpStatus).toBeUndefined()
  })
  it('accepts bounded model identifiers without inventing an unknown source', () => {
    const normalize = (extra: Record<string, unknown>) => normalizeProviderRecovery({ phase: 'fallback', ...extra })!
    expect(normalize({ model: ' vendor/model-b ', from_model: ' 模型 A ' })).toMatchObject({ model: 'vendor/model-b', fromModel: '模型 A' })
    // The Gateway limit counts Unicode code points, not UTF-16 units.
    expect(normalize({ model: '🚀'.repeat(256) }).model).toBe('🚀'.repeat(256))
    expect(normalize({ model: 'model-b' }).fromModel).toBeUndefined()
    expect(normalize({ model: 'model-b', fromModel: 'model-a' }).fromModel).toBe('model-a')
    expect(normalizeProviderRecovery({ phase: 'requesting', model: 'model-b', from_model: 'model-a' })?.fromModel).toBeUndefined()
    for (const value of [null, 123, '', 'a'.repeat(257), 'a\nb', 'a\u202eb', 'a\u0085b']) {
      expect(normalize({ model: value, from_model: value })).not.toHaveProperty('model')
      expect(normalize({ model: value, from_model: value })).not.toHaveProperty('fromModel')
    }
  })
  it('renders factual fallback names in live and settled rows, without claiming success', () => {
    const step = { key: 'fallback', at: 1000, isCurrent: true, label: { code: 'chat.activity.provider.fallback' as const, params: {} }, providerRecovery: normalizeProviderRecovery({ phase: 'fallback', model: 'model-b', from_model: 'model-a' }) }
    expect(providerActivityLabel(step)).toEqual({ code: 'chat.activity.provider.fallbackModels', params: { from: 'model-a', to: 'model-b' } })
    expect(providerActivityLabel({ ...step, isCurrent: false })).toEqual({ code: 'chat.activity.provider.fallbackModelsPast', params: { from: 'model-a', to: 'model-b' } })
    expect(providerActivityLabel({ ...step, providerRecovery: normalizeProviderRecovery({ phase: 'fallback', model: 'model-b' }) })).toEqual({ code: 'chat.activity.provider.fallbackModel', params: { model: 'model-b' } })
    expect(providerActivityLabel({ ...step, providerRecovery: undefined }).code).toBe('chat.activity.provider.fallback')
    expect(providerActivityLabel({ ...step, providerRecovery: normalizeProviderRecovery({ phase: 'fallback', model: 'model-b', from_model: 'model-b' }) }).code).toBe('chat.activity.provider.fallbackModel')
  })
  it('preserves separate fallback legs even with equal timestamps and legacy order', () => {
    const recovery = (from: string, to: string) => normalizeProviderRecovery({ phase: 'fallback', from_model: from, model: to, started_at: 1000 })!
    const first = recovery('model-a', 'model-b')
    const second = recovery('model-b', 'model-c')
    expect(providerRecoveryIdentity(first)).not.toBe(providerRecoveryIdentity(second))
    const projection = projectAssistantActivityTimeline([], { lifecycle: 'working', statusHistory: [first, first, second].map(providerRecovery => ({ action: 'provider:fallback', label: '', at: 1000, providerRecovery })) })
    expect(projection.statusSteps).toHaveLength(2)
    expect(new Set(projection.statusSteps.map(step => step.key)).size).toBe(2)
    expect(providerActivityLabel(projection.statusSteps[0]!).code).toBe('chat.activity.provider.fallbackModelsPast')
    expect(providerActivityLabel(projection.statusSteps[1]!).params).toEqual({ from: 'model-b', to: 'model-c' })
  })
  it('renders active wait, elapsed wait, overload and settled history distinctly', () => {
    const step = { key: 'p', at: 99_999, isCurrent: true, providerRecovery: wait(), label: { code: 'chat.activity.provider.rateLimited' as const, params: { seconds: 35 } } }
    expect(providerActivityLabel(step, 20_000)).toEqual({ code: 'chat.activity.provider.rateLimited', params: { seconds: 25 } })
    expect(providerActivityLabel(step, 45_000).code).toBe('chat.activity.provider.rateLimitedWaiting')
    expect(providerActivityLabel(step, 20_000, true).code).toBe('chat.activity.provider.rateLimitedWaiting')
    expect(providerActivityLabel({ ...step, isCurrent: false }, 20_000).code).toBe('chat.activity.provider.rateLimitedPast')
    expect(providerActivityLabel({ ...step, providerRecovery: wait({ reason: 'provider_overloaded' }) }, 20_000).code).toBe('chat.activity.provider.overloaded')
    expect(providerActivityLabel({ ...step, providerRecovery: undefined }, 20_000).code).toBe('chat.activity.provider.rateLimitedWaiting')
  })
  it('keeps identical-duration separate waits instead of losing a new attempt', () => {
    const projection = projectAssistantActivityTimeline([], { lifecycle: 'working', statusHistory: [
      { action: 'provider:rate_limited:35', label: '', at: 10_000, providerRecovery: wait() },
      { action: 'provider:rate_limited:35', label: '', at: 20_000, providerRecovery: wait({ started_at: 20_000 }) },
    ] })
    expect(projection.statusSteps).toHaveLength(2)
    expect(projection.statusSteps[1]?.isCurrent).toBe(true)
  })
})
