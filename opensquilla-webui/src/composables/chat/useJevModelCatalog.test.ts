import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import type { ProviderConfiguration, ProviderStatusResult } from '@/modules/providerConfiguration'
import type { SetupDiscoveryResult, SetupWorkflow } from '@/modules/setupWorkflow'
import { useJevModelCatalog } from './useJevModelCatalog'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function catalog(id = 'synthetic-decision-model', overrides: Partial<SetupDiscoveryResult> = {}): SetupDiscoveryResult {
  return {
    ok: true,
    source: 'live',
    models: [{ id, name: id, capabilities: ['chat'] }],
    catalog: { cacheHit: true, stale: false, lastSyncedAt: '2026-01-01T00:00:00Z' },
    ...overrides,
  }
}

function providerStatus(activeProvider: string | null): ProviderStatusResult {
  return { activeProvider, providerResolution: {}, providers: [], count: 0 }
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
}

const cleanups: (() => void)[] = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()) })

function harness(options: {
  active?: boolean
  provider?: string
  activeProvider?: string | null
  workflow?: null
  providers?: null
} = {}) {
  const primary = vi.fn<SetupWorkflow['provider']['discoverPrimaryModels']>()
    .mockResolvedValue(catalog())
  const profile = vi.fn<SetupWorkflow['profile']['discoverProfileModels']>()
    .mockResolvedValue(catalog('synthetic-profile-model'))
  const draft = vi.fn()
  const reveal = vi.fn()
  const status = vi.fn<ProviderConfiguration['status']>()
    .mockResolvedValue(providerStatus(options.activeProvider === undefined ? 'tokenrhythm' : options.activeProvider))
  const workflow = options.workflow === null ? null : {
    provider: { discoverPrimaryModels: primary },
    profile: { discoverProfileModels: profile, discoverDraftProfileModels: draft },
  } as unknown as SetupWorkflow
  const providers = options.providers === null ? null : {
    status, credentials: { reveal },
  } as unknown as ProviderConfiguration
  const provider = ref(options.provider ?? 'tokenrhythm')
  const active = ref(options.active ?? true)
  const connectionEpoch = ref(1)
  const scope = effectScope()
  const api = scope.run(() => useJevModelCatalog({
    workflow, providers, provider, active, connectionEpoch,
  }))!
  cleanups.push(() => { api.dispose(); scope.stop() })
  return { api, provider, active, connectionEpoch, primary, profile, draft, reveal, status }
}

describe('JEV model catalog discovery', () => {
  it('loads the active provider catalog automatically using stored provider identity only', async () => {
    const h = harness()
    await h.api.load()
    expect(h.status).toHaveBeenCalledOnce()
    expect(h.status).toHaveBeenCalledWith({ probeModels: false, signal: expect.any(AbortSignal) })
    expect(h.primary).toHaveBeenCalledOnce()
    expect(h.primary).toHaveBeenCalledWith(
      { providerId: 'tokenrhythm', cacheOnly: true }, { signal: expect.any(AbortSignal) },
    )
    expect(h.profile).not.toHaveBeenCalled()
    expect(h.draft).not.toHaveBeenCalled()
    expect(h.reveal).not.toHaveBeenCalled()
    expect(h.api.catalog.value.models.map(model => model.id)).toEqual(['synthetic-decision-model'])
    expect(h.api.catalog.value.discovering).toBe(false)
  })

  it('discovers a non-primary decision provider through its stored profile', async () => {
    const h = harness({ activeProvider: 'openrouter' })
    await h.api.load()
    expect(h.primary).not.toHaveBeenCalled()
    expect(h.profile).toHaveBeenCalledWith(
      { providerId: 'tokenrhythm', cacheOnly: true }, { signal: expect.any(AbortSignal) },
    )
    expect(h.api.catalog.value.models[0]?.id).toBe('synthetic-profile-model')
  })

  it.each([
    { cacheHit: true, stale: true }, { cacheHit: false, stale: false },
  ])('refreshes a missing or stale cache using provider-only parameters: %j', async state => {
    const h = harness({ active: false })
    h.primary.mockResolvedValueOnce(catalog('cached-model', {
      catalog: { ...state, lastSyncedAt: '2025-01-01T00:00:00Z' },
    })).mockResolvedValueOnce(catalog('refreshed-model'))
    h.active.value = true
    await nextTick()
    await h.api.load()
    expect(h.primary.mock.calls.map(([params]) => params)).toEqual([
      { providerId: 'tokenrhythm', cacheOnly: true }, { providerId: 'tokenrhythm' },
    ])
    expect(h.api.catalog.value.models.map(model => model.id)).toEqual(['refreshed-model'])
    expect(h.draft).not.toHaveBeenCalled()
    expect(h.reveal).not.toHaveBeenCalled()
  })

  it('treats a fresh empty catalog as a successful result and avoids a redundant live request', async () => {
    const h = harness({ active: false })
    h.primary.mockResolvedValue(catalog('', { models: [] }))
    h.active.value = true
    await nextTick()
    await h.api.load()
    expect(h.primary).toHaveBeenCalledOnce()
    expect(h.api.catalog.value.models).toEqual([])
    expect(h.api.catalog.value.discoverError).toBeFalsy()
    expect(h.api.catalog.value.discovering).toBe(false)
  })

  it('forces a live refresh explicitly after an initial cached result', async () => {
    const h = harness()
    await h.api.load()
    await h.api.load(true)
    expect(h.primary.mock.calls.map(([params]) => params)).toEqual([
      { providerId: 'tokenrhythm', cacheOnly: true },
      { providerId: 'tokenrhythm', forceRefresh: true },
    ])
  })

  it('joins concurrent catalog loads while discovery is in flight', async () => {
    const pending = deferred<SetupDiscoveryResult>()
    const h = harness({ active: false })
    h.primary.mockReturnValue(pending.promise)
    h.active.value = true
    await settle()
    const first = h.api.load()
    const second = h.api.load()
    expect(h.status).toHaveBeenCalledOnce()
    expect(h.primary).toHaveBeenCalledOnce()
    expect(h.api.catalog.value.discovering).toBe(true)
    pending.resolve(catalog())
    await Promise.all([first, second])
    expect(h.primary).toHaveBeenCalledOnce()
    expect(h.api.catalog.value.discovering).toBe(false)
  })

  it.each([
    { active: false }, { workflow: null }, { providers: null }, { provider: '' },
  ])('does not issue optional discovery requests when unavailable: %j', async options => {
    const h = harness(options)
    await settle()
    await h.api.load()
    expect(h.status).not.toHaveBeenCalled()
    expect(h.primary).not.toHaveBeenCalled()
    expect(h.profile).not.toHaveBeenCalled()
    expect(h.api.catalog.value.models).toEqual([])
  })

  it('aborts old provider discovery and ignores its late result after the provider changes', async () => {
    const pending = deferred<SetupDiscoveryResult>()
    const h = harness({ active: false })
    h.primary.mockReturnValue(pending.promise)
    h.active.value = true
    await settle()
    const signal = h.primary.mock.calls[0]?.[1]?.signal
    h.provider.value = 'openrouter'
    await nextTick()
    await h.api.load()
    expect(signal?.aborted).toBe(true)
    expect(h.profile.mock.calls[0]?.[0]).toEqual({ providerId: 'openrouter', cacheOnly: true })
    pending.resolve(catalog('obsolete-provider-model'))
    await settle()
    expect(h.api.catalog.value.models.map(model => model.id)).toEqual(['synthetic-profile-model'])
  })

  it('does not select a discovery route from a late provider-status response', async () => {
    const pending = deferred<ProviderStatusResult>()
    const h = harness({ active: false })
    h.status.mockReturnValueOnce(pending.promise)
    h.active.value = true
    await settle()
    const signal = h.status.mock.calls[0]?.[0]?.signal
    h.provider.value = 'openrouter'
    await nextTick()
    await h.api.load()
    pending.resolve(providerStatus('tokenrhythm'))
    await settle()
    expect(signal?.aborted).toBe(true)
    expect(h.primary).not.toHaveBeenCalled()
    expect(h.profile).toHaveBeenCalledOnce()
    expect(h.api.catalog.value.models[0]?.id).toBe('synthetic-profile-model')
  })

  it('clears and fences in-flight models when the picker becomes inactive', async () => {
    const pending = deferred<SetupDiscoveryResult>()
    const h = harness({ active: false })
    h.primary.mockReturnValue(pending.promise)
    h.active.value = true
    await settle()
    const signal = h.primary.mock.calls[0]?.[1]?.signal
    h.active.value = false
    await nextTick()
    expect(signal?.aborted).toBe(true)
    expect(h.api.catalog.value.models).toEqual([])
    pending.resolve(catalog('closed-picker-model'))
    await settle()
    expect(h.api.catalog.value.models).toEqual([])
    expect(h.api.catalog.value.discovering).toBeFalsy()
  })

  it('reloads after reconnect and prevents an older connection from restoring its catalog', async () => {
    const pending = deferred<SetupDiscoveryResult>()
    const h = harness({ active: false })
    h.primary.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(catalog('reconnected-model'))
    h.active.value = true
    await settle()
    const signal = h.primary.mock.calls[0]?.[1]?.signal
    h.connectionEpoch.value += 1
    await nextTick()
    await h.api.load()
    pending.resolve(catalog('old-connection-model'))
    await settle()
    expect(signal?.aborted).toBe(true)
    expect(h.status).toHaveBeenCalledTimes(2)
    expect(h.api.catalog.value.models.map(model => model.id)).toEqual(['reconnected-model'])
  })

  it('surfaces discovery failures locally without throwing out of load', async () => {
    const h = harness({ active: false })
    h.primary.mockRejectedValue(new Error('Synthetic provider unavailable'))
    h.active.value = true
    await nextTick()
    await expect(h.api.load()).resolves.toBeUndefined()
    expect(h.api.catalog.value.discoverError).toBeTruthy()
    expect(h.api.catalog.value.discovering).toBe(false)
    expect(h.api.catalog.value.models).toEqual([])
  })

  it('keeps cached options visible when a live refresh returns a structured failure', async () => {
    const h = harness({ active: false })
    h.primary.mockResolvedValueOnce(catalog('cached-model', {
      catalog: { cacheHit: true, stale: true, lastSyncedAt: '2025-01-01T00:00:00Z' },
    })).mockResolvedValueOnce({ ok: false, detail: 'Synthetic discovery unavailable', source: 'none' })
    h.active.value = true
    await nextTick()
    await h.api.load()
    expect(h.api.catalog.value.models.map(model => model.id)).toEqual(['cached-model'])
    expect(h.api.catalog.value.discoverError).toBeTruthy()
    expect(h.api.catalog.value.discovering).toBe(false)
  })

  it('does not discover models if provider status cannot be resolved', async () => {
    const h = harness({ active: false })
    h.status.mockRejectedValue(new Error('Synthetic status unavailable'))
    h.active.value = true
    await nextTick()
    await expect(h.api.load()).resolves.toBeUndefined()
    expect(h.primary).not.toHaveBeenCalled()
    expect(h.profile).not.toHaveBeenCalled()
    expect(h.api.catalog.value.discoverError).toBeTruthy()
  })

  it('rejects models returned under a different provider identity', async () => {
    const h = harness({ active: false })
    h.primary.mockResolvedValue(catalog('wrong-provider-model', { providerId: 'openrouter' }))
    h.active.value = true
    await nextTick()
    await h.api.load()
    expect(h.api.catalog.value.models).toEqual([])
    expect(h.api.catalog.value.discoverError).toBeTruthy()
    expect(h.api.catalog.value.discovering).toBe(false)
  })

  it('aborts on disposal and never starts or publishes further requests', async () => {
    const pending = deferred<SetupDiscoveryResult>()
    const h = harness({ active: false })
    h.primary.mockReturnValue(pending.promise)
    h.active.value = true
    await settle()
    const signal = h.primary.mock.calls[0]?.[1]?.signal
    h.api.dispose()
    expect(signal?.aborted).toBe(true)
    pending.resolve(catalog('disposed-model'))
    await settle()
    const before = h.status.mock.calls.length
    h.connectionEpoch.value += 1
    await nextTick()
    await h.api.load()
    expect(h.status).toHaveBeenCalledTimes(before)
    expect(h.api.catalog.value.models).toEqual([])
  })
})
