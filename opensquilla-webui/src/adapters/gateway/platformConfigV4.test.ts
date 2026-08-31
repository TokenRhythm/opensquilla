import { describe, expect, it, vi } from 'vitest'
import { createV4AppSettings } from './appSettingsV4'
import { createV4ProviderConfiguration } from './providerConfigurationV4'
import { createV4SetupWorkflow } from './setupWorkflowV4'

function rpc() {
  const source = { request: vi.fn(async <T = unknown>(method: string): Promise<T> => {
    if (method === 'config.get') return { config: { theme: 'dark' }, revision: 2 } as T
    if (method === 'config.effective') return { values: { theme: 'dark' } } as T
    if (method === 'config.patch.safe') return { values: { theme: 'light' }, restartRequired: true } as T
    if (method === 'models.list') return { providers: [{ provider_id: 'openai', label: 'OpenAI', models: ['gpt-4'] }] } as T
    if (method === 'models.routing.get') return { mode: 'direct', provider: 'openai' } as T
    if (method === 'models.routing.set') return { mode: 'ensemble', provider: 'openai' } as T
    if (method === 'onboarding.catalog') return { providers: [] } as T
    if (method === 'onboarding.status') return { ready: true } as T
    return {} as T
  }) }
  return source as { request<T = unknown>(method: string, params?: Record<string, unknown>, options?: unknown): Promise<T> }
}

describe('Platform configuration adapters', () => {
  it('maps config operations to AppSettings domain values', async () => {
    const source = rpc()
    const settings = createV4AppSettings(source)
    expect(await settings.get('theme')).toEqual({ values: { theme: 'dark' }, revision: 2 })
    expect(await settings.patchSafe([{ path: 'theme', value: 'light' }])).toEqual({ values: { theme: 'light' }, restartRequired: true })
    expect(source.request).toHaveBeenCalledWith('config.patch.safe', { patches: [{ path: 'theme', value: 'light' }] }, expect.any(Object))
  })

  it('normalizes provider and setup snapshots without exposing transport details', async () => {
    const source = rpc()
    const providers = createV4ProviderConfiguration(source)
    expect(await providers.list()).toEqual([{ provider_id: 'openai', label: 'OpenAI', models: ['gpt-4'], id: 'openai' }])
    expect(await providers.getRouting()).toMatchObject({ mode: 'direct', provider: 'openai' })
    const setup = createV4SetupWorkflow(source)
    expect(await setup.status()).toEqual({ ready: true })
  })
})
