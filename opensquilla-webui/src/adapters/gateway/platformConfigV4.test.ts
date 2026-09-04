import { describe, expect, it, vi } from 'vitest'
import { createV4AppSettings } from './appSettingsV4'
import { createV4ProviderConfiguration } from './providerConfigurationV4'
import { createV4SetupWorkflow } from './setupWorkflowV4'
import { createV4MigrationOperations } from './migrationOperationsV4'

function rpc() {
  const source = { request: vi.fn(async <T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> => {
    if (method === 'config.get') return (params?.path ? 'dark' : { theme: 'dark' }) as T
    if (method === 'config.effective') return { fields: { theme: { value: 'dark', source: 'config' } } } as T
    if (method === 'config.patch.safe') return { patched: ['theme'], restartRequired: true } as T
    if (method === 'config.patch') return { patched: ['llm.model'], restartRequired: false } as T
    if (method === 'telemetry.consent.set') return {
      scope: params?.scope,
      enabled: params?.enabled,
      noticeVersion: params?.enabled ? `${params?.scope}-v1` : null,
      consentedAtUtc: params?.enabled ? '2026-09-03T00:00:00Z' : null,
    } as T
    if (method === 'models.list') return { models: [], errors: [] } as T
    if (method === 'onboarding.catalog') return { providers: [{ providerId: 'openai', label: 'OpenAI' }] } as T
    if (method === 'models.routing.get') return { mode: 'direct', provider: 'openai' } as T
    if (method === 'models.routing.set') return { mode: 'ensemble', provider: 'openai', patched: ['squilla_router.mode'], restart_required: false } as T
    if (method === 'providers.status') return {
      activeProvider: 'openai',
      providerResolution: {
        status: 'explicit', effectiveProvider: 'openai', source: 'config',
        reasonCode: 'provider_explicit', actionRequired: false, actionRecommended: false,
      },
      providers: [{
        providerId: 'openai', active: true, configured: true, buildable: true,
        model: 'gpt-4o', requiresApiKey: true, apiKeyEnv: 'OPENAI_API_KEY',
        apiKeyConfigured: true, apiKeyShape: 'ok', baseUrlConfigured: false,
        error: null,
        modelProbe: { attempted: false, status: 'skipped', count: 0, error: null, failureKind: null },
        latency: null,
      }],
      count: 1,
    } as T
    if (method === 'onboarding.status') return { ready: true } as T
    if (method === 'onboarding.router.configure') return {
      changed: true,
      restartRequired: false,
      configPath: '/tmp/config.toml',
      entry: { mode: 'recommended' },
      warnings: [],
    } as T
    if (method === 'migration.sources.list') return {
      schemaVersion: 1,
      mode: 'preview_only',
      capabilities: { discover: true, preview: true, apply: false, manualSource: false },
      candidates: [{ candidateId: 'opaque', sourceKind: 'cli-home', version: null, estimatedActivityAt: null, sessionCount: 1, sizeBytes: 2, previouslyImported: false }],
    } as T
    if (method === 'migration.sources.preview') return {
      schemaVersion: 1,
      mode: 'preview_only',
      candidate: { candidateId: 'opaque', sourceKind: 'cli-home', version: null, estimatedActivityAt: null, sessionCount: 1, sizeBytes: 2, previouslyImported: false },
      previewStatus: 'available',
      targetAction: 'copy',
      summary: { sessionCount: 1, itemCounts: { planned: 1, skipped: 0, error: 0 }, pausedJobCount: 0, diskRequiredBytes: 2, diskFreeBytes: 10 },
      blockers: [],
      notices: [],
      execution: { canApply: false, supportedBy: ['desktop'] },
    } as T
    return {} as T
  }) }
  return source as { request<T = unknown>(method: string, params?: Record<string, unknown>, options?: unknown): Promise<T> }
}

describe('Platform configuration adapters', () => {
  it('maps config operations to AppSettings domain values', async () => {
    const source = rpc()
    const settings = createV4AppSettings(source)
    expect(await settings.read('theme')).toBe('dark')
    expect(await settings.readAll()).toEqual({ theme: 'dark' })
    expect(await settings.readEffective()).toEqual({ fields: { theme: { value: 'dark', source: 'config' } } })
    expect(source.request).toHaveBeenCalledWith(
      'config.get',
      undefined,
      expect.objectContaining({
        timeoutMs: 10_000,
        timeoutAction: 'reconnect',
        abortAction: 'reject',
      }),
    )
    expect(await settings.patchSafe([{ path: 'theme', value: 'light' }])).toEqual({ patched: ['theme'], restartRequired: true })
    expect(source.request).toHaveBeenCalledWith(
      'config.patch.safe',
      { patches: { theme: 'light' } },
      expect.objectContaining({ timeoutAction: 'reject', abortAction: 'reject' }),
    )
    await settings.merge({ llm: { model: 'gpt-4' } })
    expect(source.request).toHaveBeenCalledWith('config.patch', { patch: { llm: { model: 'gpt-4' } } }, expect.any(Object))
    await expect(settings.setTelemetryConsent('reliability', true)).resolves.toEqual({
      scope: 'reliability',
      enabled: true,
      noticeVersion: 'reliability-v1',
      consentedAtUtc: '2026-09-03T00:00:00Z',
    })
    expect(source.request).toHaveBeenCalledWith(
      'telemetry.consent.set',
      { scope: 'reliability', enabled: true },
      expect.objectContaining({ timeoutAction: 'reject', abortAction: 'reject' }),
    )
  })

  it('normalizes provider and setup snapshots without exposing transport details', async () => {
    const source = rpc()
    const providers = createV4ProviderConfiguration(source)
    expect(await providers.catalog()).toEqual([{ providerId: 'openai', label: 'OpenAI' }])
    expect(await providers.list()).toEqual({ models: [], errors: [] })
    expect(await providers.status()).toMatchObject({
      activeProvider: 'openai',
      providers: [{
        providerId: 'openai',
        configured: true,
        modelProbe: { attempted: false, status: 'skipped', count: 0 },
      }],
      count: 1,
    })
    expect(await providers.get()).toMatchObject({ mode: 'direct', provider: 'openai' })
    await expect(providers.setRouting('unknown' as never)).rejects.toThrow('Unsupported routing mode')
    await providers.setRouting('ensemble')
    expect(source.request).toHaveBeenCalledWith('models.routing.set', { mode: 'ensemble' }, expect.any(Object))
    const setup = createV4SetupWorkflow(source)
    expect(await setup.status()).toEqual({ ready: true })
    await expect(setup.capability.configureRouter({ mode: 'recommended' })).resolves.toMatchObject({
      changed: true,
      restartRequired: false,
    })
    expect(source.request).toHaveBeenCalledWith('onboarding.router.configure', { mode: 'recommended' }, expect.any(Object))
  })

  it('keeps migration discovery and preview read-only behind the domain seam', async () => {
    const source = rpc()
    const migration = createV4MigrationOperations(source)
    await expect(migration.listSources()).resolves.toMatchObject({
      mode: 'preview_only',
      candidates: [{ id: 'opaque', sourceKind: 'cli-home' }],
    })
    await expect(migration.preview('opaque')).resolves.toMatchObject({
      targetAction: 'copy',
      execution: { canApply: false },
    })
    expect(source.request).toHaveBeenCalledWith('migration.sources.list', {}, expect.any(Object))
    expect(source.request).toHaveBeenCalledWith('migration.sources.preview', { candidateId: 'opaque' }, expect.any(Object))
  })
})
