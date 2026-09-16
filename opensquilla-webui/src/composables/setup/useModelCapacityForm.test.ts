// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import type { ModelCapacity, ModelCapacityTarget, ProviderConfiguration } from '@/modules/providerConfiguration'
import { capacityKey, parseCapacity, useModelCapacityForm } from './useModelCapacityForm'

export const target = { provider: 'custom', model: 'example.vendor/unknown.v1:latest' }
export function capacityRow(item: ModelCapacityTarget = target): ModelCapacity {
  return { ...item, localRuntime: false,
    contextWindow: { automatic: 8192, automaticSource: 'default', override: null, value: 8192, source: 'default', editable: true },
    maxOutputTokens: { automatic: 8192, automaticSource: 'default', override: null, value: 8192, source: 'default', editable: true },
  }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await nextTick() }
function form() {
  const resolveCapacity = vi.fn(async (items: readonly ModelCapacityTarget[]) => ({ models: items.map(capacityRow) }))
  const api = useModelCapacityForm({ capacitySupported: true, resolveCapacity } as unknown as ProviderConfiguration)
  return { api, resolveCapacity }
}

describe('model capacity drafts', () => {
  it('batches and deduplicates reads while keeping providers isolated', async () => {
    const { api, resolveCapacity } = form()
    api.ensure(target); api.ensure({ ...target }); api.ensure({ ...target, provider: 'custom_anthropic' })
    await flush()
    expect(resolveCapacity).toHaveBeenCalledTimes(1)
    expect(resolveCapacity.mock.calls[0]![0]).toHaveLength(2)
    expect(api.rows.size).toBe(2)
  })
  it('does not probe old gateways and retries failed reads explicitly', async () => {
    const { api, resolveCapacity } = form()
    resolveCapacity.mockRejectedValueOnce(new Error('offline'))
    api.ensure(target); await flush()
    expect(api.failed.has(capacityKey(target))).toBe(true)
    api.ensure(target); await flush()
    expect(api.rows.size).toBe(1)
    const old = useModelCapacityForm({ resolveCapacity } as unknown as ProviderConfiguration)
    old.ensure(target); await flush()
    expect(resolveCapacity).toHaveBeenCalledTimes(2)
  })
  it('shows a retryable error when a partial response omits a requested model', async () => {
    const { api, resolveCapacity } = form()
    resolveCapacity.mockResolvedValueOnce({ models: [] })
    api.ensure(target); await flush()
    expect(api.pending.size).toBe(0)
    expect(api.failed.has(capacityKey(target))).toBe(true)
    api.ensure(target); await flush()
    expect(api.rows.has(capacityKey(target))).toBe(true)
  })
  it('shares drafts, retains other models and persists punctuation as literal keys', async () => {
    const { api } = form()
    const other = { ...target, model: '__proto__' }
    api.ensure(target); api.ensure(other); await flush()
    api.update(target, { contextWindow: '262144', maxOutputTokens: '65536' }, 'modelStrategy')
    api.update(other, { contextWindow: '32000', maxOutputTokens: '' }, 'modelStrategy')
    expect(api.values({ ...target }).contextWindow).toBe('262144')
    const patch = api.patch('modelStrategy')!
    expect(JSON.parse(JSON.stringify(patch))).toEqual({ models: { custom: {
      [target.model]: { context_window: 262144, max_output_tokens: 65536 },
      ['__proto__']: { context_window: 32000 },
    } } })
    expect(Object.getPrototypeOf({})).not.toHaveProperty('context_window')
  })
  it('retains failed scopes and acknowledges only successful writes', async () => {
    const { api } = form()
    const other = { ...target, provider: 'custom_anthropic' }
    api.ensure(target); api.ensure(other); await flush()
    api.update(target, { contextWindow: '32000', maxOutputTokens: '' }, 'provider:custom')
    api.update(other, { contextWindow: '64000', maxOutputTokens: '' }, 'modelStrategy')
    await api.save('provider:custom', vi.fn(async () => true))
    await expect(api.save('modelStrategy', vi.fn(async () => { throw new Error('disk full') }))).rejects.toThrow('disk full')
    expect(api.dirty('provider:custom')).toBe(false)
    expect(api.values(other).contextWindow).toBe('64000')
    expect(api.dirty('modelStrategy')).toBe(true)
  })
  it('restores only capacity fields and retains invalid inputs until corrected', async () => {
    const { api } = form()
    api.rows.set(capacityKey(target), { ...capacityRow(), contextWindow: { ...capacityRow().contextWindow, override: 32000, value: 32000, source: 'override' } })
    api.update(target, { contextWindow: '-5', maxOutputTokens: '' }, 'modelStrategy')
    expect(api.valid('modelStrategy')).toBe(false)
    expect(api.values(target).contextWindow).toBe('-5')
    expect(() => api.patch('modelStrategy')).toThrow()
    api.update(target, { contextWindow: '', maxOutputTokens: '' }, 'modelStrategy')
    expect(api.patch('modelStrategy')).toEqual({ models: { custom: { [target.model]: { context_window: null } } } })
    api.discard('modelStrategy')
    expect(api.values(target).contextWindow).toBe('32000')
  })
  it('cannot override a server-controlled output limit', async () => {
    const { api } = form()
    const row = capacityRow({ provider: 'openai_codex', model: 'gpt-example' })
    row.maxOutputTokens.editable = false
    api.rows.set(capacityKey(row), row)
    api.update(row, { contextWindow: '131072', maxOutputTokens: '1000' }, 'modelStrategy')
    expect(api.patch('modelStrategy')).toEqual({ models: { openai_codex: { 'gpt-example': { context_window: 131072 } } } })
  })
  it('rejects stale reads after endpoint or save invalidation', async () => {
    const { api, resolveCapacity } = form()
    let finish!: (value: { models: ModelCapacity[] }) => void
    resolveCapacity.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    api.ensure(target); await Promise.resolve()
    api.invalidate(); finish({ models: [capacityRow()] }); await flush()
    expect(api.rows.size).toBe(0)
    api.ensure(target); await flush()
    expect(api.rows.size).toBe(1)
  })
  it.each(['0', '-1', '1.5', '2e5', 'Infinity', 'NaN', '9007199254740992'])('rejects invalid value %s', value => {
    expect(() => parseCapacity(value)).toThrow()
  })
})
