import { describe, expect, it, vi } from 'vitest'
import { createV4ProviderConfiguration } from './providerConfigurationV4'
import { ProviderConfigurationError } from '@/modules/providerConfiguration'

describe('recommended Router adapter', () => {
  const events = { subscribe: vi.fn(() => ({ close() {} })) }
  it.each([undefined, false])('requires explicit support (%s) and never falls back to two writes', async supported => {
    const request = vi.fn()
    const adapter = createV4ProviderConfiguration({ request, ...(supported === undefined ? {} : { supports: () => supported }) }, events)
    expect(adapter.resetRecommendedSupported).toBe(false)
    await expect(adapter.resetRecommended!({ providerId: 'openai' })).rejects.toMatchObject({ code: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })
  it('submits the exact atomic reset envelope with reject-only timeout behavior', async () => {
    const request = vi.fn().mockResolvedValue({ mode: 'direct', patched: [], restart_required: false })
    const adapter = createV4ProviderConfiguration({ request, supports: () => true }, events)
    expect(adapter.resetRecommendedSupported).toBe(true)
    await adapter.resetRecommended!({ providerId: 'openai', activateRouter: false })
    expect(request).toHaveBeenCalledExactlyOnceWith('models.routing.resetRecommended', { providerId: 'openai', activateRouter: false }, expect.objectContaining({ timeoutAction: 'reject', abortAction: 'reject' }))
  })
  it('preserves only validated conflict details, never prose-derived actions', async () => {
    const details = { reason: 'router_provider_conflict', providerId: 'openai', conflictProviders: ['other'], allowedRouterActions: ['use_recommended'] }
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('opaque'), { code: 'ROUTER_PROVIDER_CONFLICT', details }))
    const adapter = createV4ProviderConfiguration({ request, supports: () => true }, events)
    await expect(adapter.setRouting('router')).rejects.toMatchObject({ details })
    request.mockRejectedValue(new Error('ROUTER_PROVIDER_CONFLICT use_recommended'))
    try { await adapter.setRouting('router') } catch (error) {
      expect(error).toBeInstanceOf(ProviderConfigurationError)
      expect((error as ProviderConfigurationError).details).toBeUndefined()
    }
  })
  it('does not retry invalid/unknown reset responses', async () => {
    const request = vi.fn().mockResolvedValue({ unexpected: true })
    const adapter = createV4ProviderConfiguration({ request, supports: () => true }, events)
    await expect(adapter.resetRecommended!({ providerId: 'openai', activateRouter: true })).rejects.toThrow('invalid response')
    expect(request).toHaveBeenCalledTimes(1)
  })
})

describe('remote recommendation adapter', () => {
  const events = { subscribe: vi.fn(() => ({ close() {} })) }
  it('requires all three capabilities and preserves the reviewed provider, kind and revision', async () => {
    const request = vi.fn().mockResolvedValue({ mode: 'router', patched: ['squilla_router.tiers'], restart_required: false })
    const adapter = createV4ProviderConfiguration({ request, supports: () => true }, events)
    expect(adapter.recommendationsSupported).toBe(true)
    await adapter.applyRecommendation!({ providerId: 'tokenrhythm', kind: 'router', revision: 'reviewed-content' })
    expect(request).toHaveBeenCalledExactlyOnceWith('models.recommendations.apply', { providerId: 'tokenrhythm', kind: 'router', revision: 'reviewed-content' }, expect.objectContaining({ timeoutAction: 'reject', abortAction: 'reject' }))
  })
  it('rejects unsupported or malformed apply commands before sending', async () => {
    const request = vi.fn()
    const adapter = createV4ProviderConfiguration({ request, supports: () => false }, events)
    expect(adapter.recommendationsSupported).toBe(false)
    await expect(adapter.applyRecommendation!({ providerId: 'tokenrhythm', kind: 'ensemble', revision: 'one' })).rejects.toMatchObject({ code: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })
  it('validates passive recommendation reads and acknowledgement responses', async () => {
    const request = vi.fn().mockResolvedValueOnce({ plans: [] }).mockResolvedValueOnce({ acknowledged: true }).mockResolvedValueOnce({ plans: [{ providerId: 'other' }] })
    const adapter = createV4ProviderConfiguration({ request, supports: () => true }, events)
    expect(await adapter.recommendations!()).toEqual({ plans: [] })
    expect(await adapter.acknowledgeRecommendation!({ providerId: 'openrouter', kind: 'ensemble', revision: 'one' })).toEqual({ acknowledged: true })
    await expect(adapter.recommendations!()).rejects.toThrow('Invalid model recommendations response')
    expect(request.mock.calls.map(call => call[0])).toEqual(['models.recommendations.get', 'models.recommendations.acknowledge', 'models.recommendations.get'])
  })
})
