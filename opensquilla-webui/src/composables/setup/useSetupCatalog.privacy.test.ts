// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { localizeImageActionableDetail, useSetupCatalog } from './useSetupCatalog'
import { LEGACY_OPENROUTER_MODEL_OPTIONS } from './useSetupEnsembleForm'
import { PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS } from './useSetupProviderForm'

const rpcCall = vi.hoisted(() => vi.fn())
const waitForConnection = vi.hoisted(() => vi.fn(async () => {}))
const supportsMethod = vi.hoisted(() => vi.fn((_method: string) => true))
const pushToast = vi.hoisted(() => vi.fn())
const confirmAction = vi.hoisted(() => vi.fn(async () => true))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({
    isConnected: true,
    isConnecting: false,
    waitForConnection,
    supportsMethod,
    call: rpcCall,
  }),
}))

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

vi.mock('@/composables/useConfirm', () => ({
  useConfirm: () => ({ confirm: confirmAction }),
}))

async function mountCatalog() {
  let api!: ReturnType<typeof useSetupCatalog>
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({
    setup() {
      api = useSetupCatalog()
      return () => null
    },
  })
  app.mount(el)
  await nextTick()
  await Promise.resolve()
  await nextTick()
  return { api, app }
}

function mockConfigSequence(configs: Array<Record<string, unknown>>) {
  const queue = [...configs]
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'onboarding.catalog') return {}
    if (method === 'onboarding.status') return {}
    if (method === 'channels.status') return { channels: [] }
    if (method === 'config.get') return queue.shift() ?? configs[configs.length - 1] ?? {}
    if (method === 'config.patch.safe') return { restartRequired: false }
    throw new Error(`Unexpected RPC method: ${method}`)
  })
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  rpcCall.mockReset()
  waitForConnection.mockClear()
  supportsMethod.mockReset()
  supportsMethod.mockReturnValue(true)
  pushToast.mockClear()
  confirmAction.mockReset()
  confirmAction.mockResolvedValue(true)
  document.body.innerHTML = ''
})

describe('useSetupCatalog privacy settings', () => {
  it('moves automatic memory capture into the privacy dirty/save flow', async () => {
    mockConfigSequence([
      {
        privacy: { disable_network_observability: false },
        memory: { auto_capture_enabled: true },
      },
      {
        privacy: { disable_network_observability: false },
        memory: { auto_capture_enabled: false },
      },
    ])
    const { api, app } = await mountCatalog()

    api.setMemoryAutoCapture(false)

    expect(api.sectionDirty('privacy')).toBe(true)
    expect(api.sectionDirty('capabilities')).toBe(false)
    await api.saveDirtySections()
    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: {
        'privacy.disable_network_observability': false,
        'memory.auto_capture_enabled': false,
      },
    })
    expect(api.sectionDirty('privacy')).toBe(false)
    app.unmount()
  })

  it('saves disable_network_observability through the safe gateway config patch', async () => {
    mockConfigSequence([
      { privacy: { disable_network_observability: false } },
      { privacy: { disable_network_observability: true } },
    ])
    const { api, app } = await mountCatalog()

    api.setDisableNetworkObservability(true)
    expect(api.sectionDirty('privacy')).toBe(true)

    await api.savePrivacy()

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'privacy.disable_network_observability': true },
    })
    expect(api.sectionDirty('privacy')).toBe(false)
    expect(pushToast).toHaveBeenCalledWith('Privacy saved.')
    app.unmount()
  })

  it('keeps the privacy intent visible when dirty-bar privacy save fails alongside another section', async () => {
    const configQueue = [
      { privacy: { disable_network_observability: false }, naming: { enabled: false } },
      { privacy: { disable_network_observability: false }, naming: { enabled: true } },
    ]
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return {}
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configQueue.shift() ?? configQueue[configQueue.length - 1] ?? {}
      if (method === 'config.patch.safe') {
        const patches = params?.patches as Record<string, unknown> | undefined
        if (patches && 'privacy.disable_network_observability' in patches) {
          throw new Error('privacy patch failed')
        }
        return { restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setDisableNetworkObservability(true)
    api.setAutoSessionTitles(true)
    expect(api.sectionDirty('privacy')).toBe(true)
    expect(api.sectionDirty('behavior')).toBe(true)

    await api.saveDirtySections()

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'privacy.disable_network_observability': true },
    })
    expect(api.privacyPanel.value.disableNetworkObservability).toBe(true)
    expect(api.sectionDirty('privacy')).toBe(true)
    app.unmount()
  })

  it('shows the effective disabled state when the dedicated privacy environment switch is active', async () => {
    mockConfigSequence([
      {
        privacy: {
          disable_network_observability: false,
          network_observability_disabled_effective: true,
        },
      },
    ])
    const { api, app } = await mountCatalog()

    expect(api.privacyPanel.value.disableNetworkObservability).toBe(false)
    expect(api.privacyPanel.value.statusText).toBe(
      'Network reporting is off because an environment setting disables it.',
    )
    expect(api.sectionDirty('privacy')).toBe(false)
    app.unmount()
  })

  it('does not label an unsaved config toggle as environment-disabled', async () => {
    mockConfigSequence([
      {
        privacy: {
          disable_network_observability: true,
          network_observability_disabled_effective: true,
        },
      },
    ])
    const { api, app } = await mountCatalog()

    api.setDisableNetworkObservability(false)

    expect(api.privacyPanel.value.statusText).toBe(
      'Network reporting is on.',
    )
    expect(api.sectionDirty('privacy')).toBe(true)
    app.unmount()
  })
})

describe('useSetupCatalog capability reset', () => {
  it('confirms a draft, resets through the advertised RPC, and preserves unrelated drafts', async () => {
    let load = 0
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') {
        return {
          imageGenerationProviders: [{
            providerId: 'openrouter',
            label: 'OpenRouter',
            defaultModel: 'google/gemini-image',
            requiresApiKey: true,
          }],
        }
      }
      if (method === 'onboarding.status') {
        return load === 0
          ? {
              imageGenerationEnabled: true,
              imageGenerationConfigured: true,
              imageGenerationProvider: 'openrouter',
              imageGenerationPrimary: 'openrouter/saved-image',
              capabilityConfiguration: {
                image_generation: { resettable: true },
              },
            }
          : {
              imageGenerationEnabled: false,
              imageGenerationConfigured: false,
              capabilityConfiguration: {
                image_generation: { resettable: false },
              },
            }
      }
      if (method === 'config.get') {
        const value = load === 0
          ? {
              naming: { enabled: false },
              image_generation: {
                providers: { openrouter: { api_key_env: 'OPENROUTER_API_KEY' } },
              },
            }
          : { naming: { enabled: false }, image_generation: {} }
        load += 1
        return value
      }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.capability.reset') {
        expect(params).toEqual({ capabilityId: 'image_generation' })
        return { restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()
    api.setAutoSessionTitles(true)
    api.updateCapabilityField('image', 'apiKey', 'test-inline-key')

    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.sectionDirty('capabilities')).toBe(true)
    await api.resetCapability('image_generation')

    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining('discard its unsaved changes'),
    }))
    expect(rpcCall).toHaveBeenCalledWith('onboarding.capability.reset', {
      capabilityId: 'image_generation',
    })
    expect(api.sectionDirty('capabilities')).toBe(false)
    expect(api.sectionDirty('behavior')).toBe(true)
    app.unmount()
  })

  it('does not expose or call reset against an older gateway', async () => {
    mockConfigSequence([{}])
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.resettable('search')).toBe(false)
    await api.resetCapability('search')

    expect(confirmAction).not.toHaveBeenCalled()
    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.capability.reset', expect.anything())
    app.unmount()
  })

  it('does not reset when the confirmation is cancelled', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return { capabilityConfiguration: { search: { resettable: true } } }
      }
      if (method === 'config.get') return { search_provider: 'brave' }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    confirmAction.mockResolvedValueOnce(false)
    const { api, app } = await mountCatalog()

    await api.resetCapability('search')

    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.capability.reset', expect.anything())
    app.unmount()
  })

  it('keeps the current draft when reset fails', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          imageGenerationProviders: [{
            providerId: 'openrouter',
            label: 'OpenRouter',
            defaultModel: 'google/gemini-image',
            requiresApiKey: true,
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          imageGenerationEnabled: true,
          imageGenerationConfigured: true,
          imageGenerationProvider: 'openrouter',
          imageGenerationPrimary: 'openrouter/saved-image',
          capabilityConfiguration: { image_generation: { resettable: true } },
        }
      }
      if (method === 'config.get') return { image_generation: {} }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.capability.reset') throw new Error('reset failed')
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()
    api.updateCapabilityField('image', 'apiKey', 'test-inline-key')

    await api.resetCapability('image_generation')

    expect(api.sectionDirty('capabilities')).toBe(true)
    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining('reset failed'),
      { tone: 'danger' },
    )
    app.unmount()
  })

  it('keeps the current draft and suppresses success when the post-reset reload fails', async () => {
    let resetApplied = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        if (resetApplied) throw new Error('reload failed')
        return {
          imageGenerationProviders: [{
            providerId: 'openrouter',
            label: 'OpenRouter',
            defaultModel: 'google/gemini-image',
            requiresApiKey: true,
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          imageGenerationEnabled: true,
          imageGenerationConfigured: true,
          imageGenerationProvider: 'openrouter',
          imageGenerationPrimary: 'openrouter/saved-image',
          capabilityConfiguration: { image_generation: { resettable: true } },
        }
      }
      if (method === 'config.get') return { image_generation: {} }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.capability.reset') {
        resetApplied = true
        return { restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()
    api.updateCapabilityField('image', 'apiKey', 'test-inline-key')

    await api.resetCapability('image_generation')

    expect(api.sectionDirty('capabilities')).toBe(true)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining('reload failed'),
      { tone: 'danger' },
    )
    expect(pushToast).not.toHaveBeenCalledWith(
      expect.stringContaining('was reset'),
    )
    app.unmount()
  })
})

describe('useSetupCatalog memory capability summary', () => {
  it('describes built-in retrieval without exposing its implementation model', async () => {
    mockConfigSequence([{ memory: { embedding: { provider: 'auto' } } }])
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.memoryModeTitle)
      .toBe('Find related content in saved memory')
    expect(api.capabilitiesPanel.value.state.memoryModeDescription)
      .toBe('Runs on this device')
    expect(api.capabilitiesPanel.value.state.memoryExpandable).toBe(false)
    app.unmount()
  })

  it('distinguishes keyword-only mode from built-in embeddings', async () => {
    mockConfigSequence([{ memory: { embedding: { provider: 'none' } } }])
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.memoryModeTitle)
      .toBe('Keyword-only local retrieval')
    expect(api.capabilitiesPanel.value.state.capabilityBadgeLabel('memory_embedding'))
      .toBe('Available')
    app.unmount()
  })

  it('identifies a configured custom embedding provider without exposing its selector', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          memoryEmbeddingProviders: [{
            providerId: 'openai',
            label: 'OpenAI',
            requiresApiKey: true,
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          memoryEmbeddingConfigured: true,
          memoryEmbeddingProvider: 'openai',
        }
      }
      if (method === 'config.get') {
        return { memory: { embedding: { provider: 'openai' } } }
      }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.memoryModeTitle)
      .toBe('Custom embedding · OpenAI')
    expect(api.capabilitiesPanel.value.state.capabilityBadgeLabel('memory_embedding'))
      .toBe('Available')
    app.unmount()
  })

  it('makes a managed custom embedding expandable for its restore action', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          memoryEmbeddingProviders: [{
            providerId: 'openai',
            label: 'OpenAI',
            requiresApiKey: true,
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          memoryEmbeddingConfigured: true,
          memoryEmbeddingProvider: 'openai',
          capabilityConfiguration: {
            memory_embedding: { resettable: true },
          },
        }
      }
      if (method === 'config.get') {
        return { memory: { embedding: { provider: 'openai' } } }
      }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.memoryExpandable).toBe(true)
    app.unmount()
  })
})

describe('useSetupCatalog image model catalog', () => {
  it('loads the provider image-model catalog when the capabilities section opens', async () => {
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') {
        return {
          imageGenerationProviders: [{
            providerId: 'openrouter',
            label: 'OpenRouter Images',
            runtimeSupported: true,
            defaultModel: 'openrouter/google/fallback-image',
            suggestedModels: ['openrouter/google/fallback-image'],
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          imageGenerationProvider: 'openrouter',
          imageGenerationPrimary: 'openrouter/google/fallback-image',
        }
      }
      if (method === 'config.get') return { image_generation: {} }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.imageGeneration.models.discover') {
        expect(params).toEqual({ providerId: 'openrouter' })
        return {
          ok: true,
          providerId: 'openrouter',
          source: 'live',
          models: [{
            id: 'vendor/live-image',
            name: 'Live Image',
            contextWindow: null,
            maxOutputTokens: null,
            capabilities: [],
            pricing: null,
            capabilitySource: 'OpenRouter',
          }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('capabilities')
    await nextTick()
    await Promise.resolve()
    await nextTick()

    expect(api.capabilitiesPanel.value.options.imageModels).toEqual([
      expect.objectContaining({ id: 'vendor/live-image', name: 'Live Image' }),
    ])
    expect(api.capabilitiesPanel.value.state.imageModelSource).toBe('live')
    app.unmount()
  })

  it('uses curated image models when the gateway lacks discovery support', async () => {
    supportsMethod.mockImplementation(
      method => method !== 'onboarding.imageGeneration.models.discover',
    )
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          imageGenerationProviders: [{
            providerId: 'qwen_token_plan',
            label: 'Qwen Images',
            runtimeSupported: true,
            defaultModel: 'qwen_token_plan/wan2.7-image',
            suggestedModels: [
              'qwen_token_plan/wan2.7-image',
              'qwen_token_plan/wan2.7-image-pro',
            ],
          }],
        }
      }
      if (method === 'onboarding.status') {
        return { imageGenerationProvider: 'qwen_token_plan' }
      }
      if (method === 'config.get') return { image_generation: {} }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('capabilities')
    await nextTick()

    expect(api.capabilitiesPanel.value.options.imageModels.map(model => model.id)).toEqual([
      'wan2.7-image',
      'wan2.7-image-pro',
    ])
    expect(api.capabilitiesPanel.value.state.imageModelSource).toBe('catalog')
    expect(rpcCall).not.toHaveBeenCalledWith(
      'onboarding.imageGeneration.models.discover',
      expect.anything(),
    )
    app.unmount()
  })
})

describe('useSetupCatalog search draft validation', () => {
  it('keeps current DuckDuckGo status explicit and blocks a keyed provider without a key', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          searchProviders: [
            {
              providerId: 'duckduckgo',
              label: 'DuckDuckGo',
              runtimeSupported: true,
              requiresApiKey: false,
            },
            {
              providerId: 'brave',
              label: 'Brave Search',
              runtimeSupported: true,
              requiresApiKey: true,
            },
          ],
        }
      }
      if (method === 'onboarding.status') {
        return {
          searchConfigured: true,
          searchProvider: 'duckduckgo',
        }
      }
      if (method === 'config.get') return { search_provider: 'duckduckgo' }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateCapabilityField('search', 'provider', 'brave')
    api.onSearchProviderChange()

    expect(api.capabilitiesPanel.value.state.capabilityBadgeLabel('search'))
      .toBe('Currently available')
    expect(api.capabilitiesPanel.value.state.searchDraftStatusText)
      .toBe('Currently using DuckDuckGo; add an API key before switching to Brave Search.')
    expect(api.capabilitiesPanel.value.state.searchDraftMissingKey).toBe(true)

    await api.saveDirtySections()

    expect(rpcCall).not.toHaveBeenCalledWith(
      'onboarding.search.configure',
      expect.anything(),
    )
    expect(pushToast).toHaveBeenCalledWith(
      'Enter an API key before saving this search provider.',
      { tone: 'danger' },
    )
    app.unmount()
  })

  it('allows the provider switch after a key is entered', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          searchProviders: [
            {
              providerId: 'duckduckgo',
              label: 'DuckDuckGo',
              runtimeSupported: true,
              requiresApiKey: false,
            },
            {
              providerId: 'brave',
              label: 'Brave Search',
              runtimeSupported: true,
              requiresApiKey: true,
            },
          ],
        }
      }
      if (method === 'onboarding.status') {
        return { searchConfigured: true, searchProvider: 'duckduckgo' }
      }
      if (method === 'config.get') return { search_provider: 'duckduckgo' }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.search.configure') return {}
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateCapabilityField('search', 'provider', 'brave')
    api.onSearchProviderChange()
    api.updateCapabilityField('search', 'apiKey', 'test-search-key')

    expect(api.capabilitiesPanel.value.state.searchDraftMissingKey).toBe(false)
    await api.saveSearch({ reload: false })

    expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.search.configure',
      expect.objectContaining({
        providerId: 'brave',
        apiKey: 'test-search-key',
      }),
    )
    app.unmount()
  })
})

describe('useSetupCatalog effective model limits', () => {
  it('rediscovers on every Provider re-entry while deduplicating an in-flight request', async () => {
    let discoverCalls = 0
    let releaseFirstDiscovery!: () => void
    const firstDiscovery = new Promise<void>((resolve) => { releaseFirstDiscovery = resolve })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [{
            providerId: 'tokenrhythm',
            label: 'TokenRhythm',
            runtimeSupported: true,
            fields: [{ name: 'model', label: 'Model' }],
          }],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'qwen3.8-max' } }
      }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.models.discover') {
        discoverCalls += 1
        if (discoverCalls === 1) await firstDiscovery
        return { ok: true, source: 'live', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await vi.waitFor(() => expect(discoverCalls).toBe(1))

    api.setSection('behavior')
    await nextTick()
    api.setSection('provider')
    await nextTick()
    expect(discoverCalls).toBe(1)

    releaseFirstDiscovery()
    await vi.waitFor(() => expect(api.providerPanel.value.connection.modelSource).toBe('live'))
    await Promise.resolve()

    api.setSection('behavior')
    await nextTick()
    api.setSection('provider')
    await vi.waitFor(() => expect(discoverCalls).toBe(2))

    api.setSection('behavior')
    await nextTick()
    api.setSection('provider')
    await vi.waitFor(() => expect(discoverCalls).toBe(3))

    expect(rpcCall).toHaveBeenLastCalledWith('onboarding.models.discover', {
      providerId: 'tokenrhythm',
      model: 'qwen3.8-max',
    })
    app.unmount()
  })

  it('exposes only the effective value matching the current form identity', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [{
            providerId: 'tokenrhythm',
            label: 'TokenRhythm',
            runtimeSupported: true,
            fields: [{ name: 'model', label: 'Model' }],
          }],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'qwen3.7-max' } }
      }
      if (method === 'config.effective') {
        return {
          fields: {
            'llm.provider': { value: 'tokenrhythm', source: 'config' },
            'llm.model': { value: 'qwen3.7-max', source: 'config' },
            'llm.max_tokens': { value: 131072, source: 'catalog' },
          },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await vi.waitFor(() => {
      expect(api.providerPanel.value.effectiveMaxTokens).toEqual({
        value: 131072,
        source: 'catalog',
      })
    })

    api.updateProviderField('model', 'glm-5')
    expect(api.providerPanel.value.effectiveMaxTokens).toBeNull()

    app.unmount()
  })

  it('uses a force refresh for the manual model-catalog action', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [{
            providerId: 'tokenrhythm',
            label: 'TokenRhythm',
            runtimeSupported: true,
            fields: [{ name: 'model', label: 'Model' }],
          }],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'qwen3.7-max' } }
      }
      if (method === 'onboarding.models.discover') {
        return {
          ok: true,
          source: 'live',
          models: [],
          catalog: { lastSyncedAt: '2026-08-03T06:00:00Z', stale: false },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.refreshProviderModels()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.models.discover', {
      providerId: 'tokenrhythm',
      model: 'qwen3.7-max',
      forceRefresh: true,
    })
    expect(api.providerPanel.value.connection.catalog).toEqual({
      lastSyncedAt: '2026-08-03T06:00:00Z',
      stale: false,
    })
    app.unmount()
  })
})

describe('useSetupCatalog model strategy IA', () => {
  it('discovers the active provider model catalog when Model Strategy opens', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId: 'tokenrhythm',
              label: 'TokenRhythm',
              runtimeSupported: true,
              fields: [{ name: 'model', label: 'Model' }],
            },
          ],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' } }
      }
      if (method === 'onboarding.models.discover') {
        return {
          ok: true,
          source: 'live',
          models: [
            {
              id: 'deepseek-v4-flash',
              name: 'DeepSeek V4 Flash',
              contextWindow: 128000,
              maxOutputTokens: 16384,
              capabilities: ['chat', 'tools'],
              pricing: null,
              capabilitySource: 'provider',
            },
          ],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await nextTick()
    await Promise.resolve()
    await nextTick()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.models.discover', {
      providerId: 'tokenrhythm',
      model: 'deepseek-v4-pro',
    })
    expect(api.routerPanel.value.discoveredModelsByProvider.tokenrhythm?.models).toHaveLength(1)
    expect(api.routerPanel.value.discoveredModelsByProvider.tokenrhythm?.models[0]?.id).toBe('deepseek-v4-flash')
    app.unmount()
  })

  it('rediscovers models when config reloads while Model Strategy stays open', async () => {
    let discoverCalls = 0
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId: 'tokenrhythm',
              label: 'TokenRhythm',
              runtimeSupported: true,
              fields: [{ name: 'model', label: 'Model' }],
            },
          ],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' } }
      }
      if (method === 'onboarding.models.discover') {
        discoverCalls += 1
        return {
          ok: true,
          source: 'live',
          models: [
            {
              id: 'deepseek-v4-flash',
              name: 'DeepSeek V4 Flash',
              contextWindow: 128000,
              maxOutputTokens: 16384,
              capabilities: ['chat', 'tools'],
              pricing: null,
              capabilitySource: 'provider',
            },
          ],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await nextTick()
    await Promise.resolve()
    await nextTick()
    expect(discoverCalls).toBe(1)

    await api.loadData()

    expect(discoverCalls).toBe(2)
    expect(api.routerPanel.value.discoveredModelsByProvider.tokenrhythm?.models[0]?.id).toBe('deepseek-v4-flash')
    app.unmount()
  })

  it('does not wait for model discovery before a Model Strategy config reload completes', async () => {
    let discoverCalls = 0
    let releaseReloadDiscovery!: () => void
    const reloadDiscovery = new Promise<void>((resolve) => { releaseReloadDiscovery = resolve })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [{ providerId: 'tokenrhythm', label: 'TokenRhythm', runtimeSupported: true }],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' } }
      }
      if (method === 'onboarding.models.discover') {
        discoverCalls += 1
        if (discoverCalls === 2) await reloadDiscovery
        return { ok: true, source: 'live', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await vi.waitFor(() => expect(discoverCalls).toBe(1))

    let reloadCompleted = false
    const reload = api.loadData().then(() => { reloadCompleted = true })
    await vi.waitFor(() => expect(discoverCalls).toBe(2))
    await Promise.resolve()

    expect(reloadCompleted).toBe(true)
    releaseReloadDiscovery()
    await reload
    app.unmount()
  })

  it('discovers and isolates catalogs for every provider used by mixed router tiers', async () => {
    const requests: Array<Record<string, unknown>> = []
    const discoveryMethods: string[] = []
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId: 'tokenrhythm',
              label: 'TokenRhythm',
              runtimeSupported: true,
              fields: [
                { name: 'model', label: 'Model' },
                { name: 'api_key', label: 'API key', secret: true },
              ],
            },
            { providerId: 'openrouter', label: 'OpenRouter', runtimeSupported: true },
            { providerId: 'anthropic', label: 'Anthropic', runtimeSupported: true },
          ],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' },
          squilla_router: {
            enabled: true,
            tiers: {
              c0: { provider: ' TokenRhythm ', model: 'deepseek-v4-flash' },
              c1: { provider: 'OPENROUTER', model: 'deepseek/deepseek-v4-pro' },
              c2: { provider: 'anthropic', model: 'claude-sonnet-4' },
            },
          },
        }
      }
      if (method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover') {
        discoveryMethods.push(method)
        requests.push(params || {})
        const providerId = String(params?.providerId || '').toLowerCase()
        if (providerId === 'anthropic') return { ok: true, source: 'none', models: [] }
        return {
          ok: true,
          source: 'live',
          models: [{
            id: providerId === 'openrouter' ? 'deepseek/deepseek-v4-pro' : 'deepseek-v4-flash',
            name: 'Model',
            contextWindow: null,
            maxOutputTokens: null,
            capabilities: [],
            pricing: null,
            capabilitySource: 'provider',
          }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateProviderField('api_key', 'unsaved-selected-provider-key')
    api.setSection('modelStrategy')
    await vi.waitFor(() => expect(requests).toHaveLength(4))

    expect(requests).toContainEqual({
      providerId: 'tokenrhythm',
      model: 'deepseek-v4-pro',
    })
    expect(requests).toContainEqual({
      providerId: 'tokenrhythm',
      apiKey: 'unsaved-selected-provider-key',
      model: 'deepseek-v4-pro',
    })
    expect(requests).toContainEqual({ providerId: 'openrouter' })
    expect(requests).toContainEqual({ providerId: 'anthropic' })
    expect(requests.filter(request => request.apiKey !== undefined)).toHaveLength(1)
    expect(discoveryMethods.filter(method => method === 'onboarding.models.discover')).toHaveLength(2)
    expect(discoveryMethods.filter(method => method === 'onboarding.llmProfile.models.discover')).toHaveLength(2)

    const byProvider = api.routerPanel.value.discoveredModelsByProvider
    expect(Object.keys(byProvider).sort()).toEqual(['anthropic', 'openrouter', 'tokenrhythm'])
    expect(byProvider.tokenrhythm?.models[0]?.id).toBe('deepseek-v4-flash')
    expect(byProvider.openrouter?.models[0]?.id).toBe('deepseek/deepseek-v4-pro')
    expect(byProvider.anthropic).toEqual({ models: [], source: 'none' })
    app.unmount()
  })

  it('deduplicates only in-flight provider discovery and refreshes after it settles', async () => {
    const requests: string[] = []
    let releaseDiscoveries!: () => void
    const blocked = new Promise<void>((resolve) => { releaseDiscoveries = resolve })
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            { providerId: 'tokenrhythm', label: 'TokenRhythm', runtimeSupported: true },
            { providerId: 'openrouter', label: 'OpenRouter', runtimeSupported: true },
          ],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' },
          squilla_router: {
            enabled: true,
            tiers: {
              c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
              c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
            },
          },
        }
      }
      if (method === 'onboarding.models.discover') {
        requests.push(String(params?.providerId || ''))
        await blocked
        return { ok: true, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        throw new Error('RPC method not found')
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await vi.waitFor(() => expect(requests).toHaveLength(2))
    api.setSection('provider')
    await nextTick()
    api.setSection('modelStrategy')
    await nextTick()

    expect(requests.sort()).toEqual(['openrouter', 'tokenrhythm'])
    releaseDiscoveries()
    await vi.waitFor(() => expect(
      Object.keys(api.routerPanel.value.discoveredModelsByProvider),
    ).toHaveLength(2))

    api.setSection('provider')
    await nextTick()
    api.setSection('modelStrategy')

    await vi.waitFor(() => expect(requests).toHaveLength(4))
    expect(requests.sort()).toEqual([
      'openrouter',
      'openrouter',
      'tokenrhythm',
      'tokenrhythm',
    ])
    app.unmount()
  })

  it('exposes the Model Strategy facade panel cards', async () => {
    mockConfigSequence([
      {
        llm: { provider: 'openrouter', model: 'openrouter/auto' },
        squilla_router: { enabled: true },
        llm_ensemble: { enabled: true },
      },
    ])
    const { api, app } = await mountCatalog()

    expect(api.modelStrategyPanel.value.cards.map(card => card.id)).toEqual(['ensemble', 'router', 'single'])
    expect(api.modelStrategyPanel.value.providerLabel).toBe('openrouter')
    app.unmount()
  })

  it('marks Model Strategy dirty when selecting the single-model strategy', async () => {
    mockConfigSequence([
      {
        llm: { provider: 'openrouter', model: 'openrouter/auto' },
        squilla_router: { enabled: true },
        llm_ensemble: { enabled: true },
      },
    ])
    const { api, app } = await mountCatalog()

    api.setModelStrategy('single')

    expect(api.modelStrategyPanel.value.activeStrategy).toBe('single')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.dirtySections.value.map(s => s.id)).toContain('modelStrategy')
    app.unmount()
  })

  it('routes router readiness actions and status through Model Strategy', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          sectionDetails: {
            router: {
              status: 'missing',
              blocking: true,
              label: 'Router',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.actionItems.value).toContainEqual({ label: 'Router setup needed', section: 'modelStrategy' })
    expect(api.sectionStatus('modelStrategy')).toEqual({ label: 'Needs action', tone: 'is-warn' })
    app.unmount()
  })

  it('routes ensemble readiness actions through Model Strategy', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          sectionDetails: {
            ensemble: {
              status: 'degraded',
              actionRequired: true,
              label: 'Ensemble',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.actionItems.value).toContainEqual({ label: 'Ensemble setup needed', section: 'modelStrategy' })
    expect(api.actionItems.value).not.toContainEqual({ label: 'Ensemble setup needed', section: 'provider' })
    app.unmount()
  })

  it('filters channel-only readiness from Settings while retaining the channel count summary', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          needsOnboarding: true,
          channelCount: 2,
          sectionDetails: {
            channels: {
              status: 'degraded',
              blocking: true,
              actionRequired: true,
              label: 'Channels',
            },
          },
        }
      }
      if (method === 'config.get') {
        return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.hasSetupAction.value).toBe(false)
    expect(api.actionItems.value).toEqual([])
    api.selectInitialSection('auto')
    expect(api.section.value).toBe('provider')
    expect(api.configSummary.value).toContainEqual({ label: 'Channels', value: '2' })
    app.unmount()
  })

  it('auto-selects Model Strategy when ensemble readiness needs action', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          sectionDetails: {
            router: { status: 'ok', label: 'Router' },
            ensemble: {
              status: 'degraded',
              actionRequired: true,
              label: 'Ensemble',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectInitialSection('auto')

    expect(api.section.value).toBe('modelStrategy')
    app.unmount()
  })

  it('reports Model Strategy needs action when ensemble detail needs action', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          sectionDetails: {
            router: { status: 'ok', label: 'Router' },
            ensemble: {
              status: 'degraded',
              actionRequired: true,
              label: 'Ensemble',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.sectionStatus('modelStrategy')).toEqual({ label: 'Needs action', tone: 'is-warn' })
    app.unmount()
  })

  it('aggregates router and ensemble dirty state under Model Strategy', async () => {
    mockConfigSequence([
      {
        llm: { provider: 'openrouter', model: 'openrouter/auto' },
        squilla_router: { enabled: true },
        llm_ensemble: { enabled: true },
      },
    ])
    const { api, app } = await mountCatalog()

    api.setModelStrategy('single')
    expect(api.sectionDirty('modelStrategy')).toBe(true)

    await api.discardChanges()
    expect(api.sectionDirty('modelStrategy')).toBe(false)

    api.setEnsembleEnabled(false)
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.dirtySections.value.map(s => s.id)).toContain('modelStrategy')
    app.unmount()
  })

  it('saves dirty router and ensemble edits through the Model Strategy save path', async () => {
    let routerSaved = false
    let ensembleSaved = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return {}
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openrouter', model: 'openrouter/auto' },
          squilla_router: { enabled: !routerSaved, default_tier: 'balanced' },
          llm_ensemble: { enabled: ensembleSaved },
        }
      }
      if (method === 'onboarding.router.configure') {
        routerSaved = true
        return {}
      }
      if (method === 'onboarding.ensemble.configure') {
        ensembleSaved = true
        return {}
      }
      if (method === 'config.patch.safe') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setModelStrategy('single')
    api.setEnsembleEnabled(true)
    await api.saveDirtySections()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.router.configure', expect.any(Object))
    expect(rpcCall).toHaveBeenCalledWith('onboarding.ensemble.configure', { enabled: true })
    app.unmount()
  })

  it('owns fixed-model edits in Model Routing and patches only llm.model', async () => {
    let savedModel = 'openrouter/auto'
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return {}
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openrouter', model: savedModel },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'config.patch') {
        const patches = params?.patches as Record<string, unknown> | undefined
        savedModel = String(patches?.['llm.model'] || '')
        return { restartRequired: false }
      }
      if (method === 'config.effective') return { fields: {} }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    // The Model Service editor owns its explicit Save changes action. Its
    // model draft is mirrored into the fixed-model readout without entering
    // the settings-wide Model Routing dirty bar.
    api.updateProviderField('model', 'deepseek/deepseek-v4-pro')
    expect(api.modelStrategyPanel.value.single.model).toBe('deepseek/deepseek-v4-pro')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.sectionDirty('provider')).toBe(false)
    expect(api.providerDraftDirty.value).toBe(true)

    await api.discardChanges()
    expect(api.modelStrategyPanel.value.single.model).toBe('openrouter/auto')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.providerDraftDirty.value).toBe(false)

    api.setFixedModel('deepseek/deepseek-v4-pro')
    await api.saveDirtySections()

    expect(rpcCall).toHaveBeenCalledWith('config.patch', {
      patches: { 'llm.model': 'deepseek/deepseek-v4-pro' },
    })
    expect(rpcCall).not.toHaveBeenCalledWith(
      'onboarding.provider.configure',
      expect.anything(),
    )
    expect(api.modelStrategyPanel.value.single.model).toBe('deepseek/deepseek-v4-pro')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    app.unmount()
  })

  it('snapshots save-all work and reloads once after every dirty section is persisted', async () => {
    let configReads = 0
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        configReads += 1
        return {
          llm: { provider: 'openrouter', model: 'openrouter/auto' },
          naming: { enabled: false },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.provider.configure') return {}
      if (method === 'config.patch' || method === 'config.patch.safe') {
        return { restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateLlmTimeout(321)
    api.setAutoSessionTitles(true)
    api.setFixedModel('deepseek/deepseek-v4-pro')
    await api.saveDirtySections()

    expect(rpcCall).not.toHaveBeenCalledWith(
      'onboarding.provider.configure',
      expect.anything(),
    )
    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'naming.enabled': true },
    })
    expect(rpcCall).toHaveBeenCalledWith('config.patch', {
      patches: { 'llm.model': 'deepseek/deepseek-v4-pro' },
    })
    expect(configReads).toBe(2)
    app.unmount()
  })

  it('blocks save-all reentry and discard while a batch RPC is pending', async () => {
    let releaseBehaviorSave!: () => void
    const behaviorSaveGate = new Promise<void>(resolve => {
      releaseBehaviorSave = resolve
    })
    let configReads = 0
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        configReads += 1
        return {
          llm: { provider: 'openrouter', model: 'openrouter/auto' },
          naming: { enabled: false },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'config.patch.safe') {
        await behaviorSaveGate
        return { restartRequired: false }
      }
      if (method === 'config.patch') {
        return { restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setAutoSessionTitles(true)
    const firstSave = api.saveDirtySections()

    expect(api.saveAllPending.value).toBe(true)
    const duplicateSave = api.saveDirtySections()
    await duplicateSave
    await api.discardChanges()

    expect(rpcCall.mock.calls.filter(call => call[0] === 'config.patch.safe'))
      .toHaveLength(1)
    expect(configReads).toBe(1)

    releaseBehaviorSave()
    await firstSave

    expect(rpcCall.mock.calls.filter(call => call[0] === 'config.patch.safe'))
      .toHaveLength(1)
    expect(configReads).toBe(2)
    expect(api.saveAllPending.value).toBe(false)
    app.unmount()
  })

  it('does not commit a Model Routing draft when saving the configured provider', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openrouter', model: 'openrouter/auto' },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.provider.configure') return {}
      if (method === 'config.patch') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setFixedModel('')
    api.updateLlmTimeout(321)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      providerId: 'openrouter',
      model: 'openrouter/auto',
    }))
    expect(rpcCall).not.toHaveBeenCalledWith('config.patch', {
      patches: { 'llm.model': '' },
    })
    expect(api.modelStrategyPanel.value.single.model).toBe('')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    app.unmount()
  })

  it('validates a cleared fixed model before save-all mutates provider settings', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') return { hasConfig: true }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openrouter', model: 'openrouter/auto' },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateLlmTimeout(321)
    api.setFixedModel('')
    await api.saveDirtySections()

    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.provider.configure', expect.anything())
    expect(rpcCall).not.toHaveBeenCalledWith('config.patch', expect.anything())
    expect(pushToast).toHaveBeenCalledWith('Choose a fixed model before saving.', { tone: 'danger' })
    expect(api.providerDraftDirty.value).toBe(true)
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    app.unmount()
  })

  it('represents router and ensemble dirty state under Model Strategy', async () => {
    mockConfigSequence([
      {
        llm: { provider: 'openrouter', model: 'openrouter/auto' },
        squilla_router: { enabled: true },
        llm_ensemble: { enabled: false },
      },
    ])
    const { api, app } = await mountCatalog()

    api.setRouterMode('disabled')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.dirtySections.value.map(s => s.id)).toContain('modelStrategy')

    await api.discardChanges()
    expect(api.sectionDirty('modelStrategy')).toBe(false)

    api.setEnsembleEnabled(true)
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.dirtySections.value.map(s => s.id)).toContain('modelStrategy')
    app.unmount()
  })
})

describe('useSetupCatalog fresh-install provider semantics', () => {
  interface FreshProviderStateOptions {
    catalog?: Record<string, unknown>
    effective?: Record<string, unknown>
    allowPrimarySave?: boolean
  }

  const tokenRhythmCatalog = {
    providers: [{
      providerId: 'tokenrhythm',
      label: 'TokenRhythm',
      runtimeSupported: true,
      fields: [{ name: 'model', label: 'Model' }],
      presets: [{
        presetId: 'tokenrhythm',
        label: 'TokenRhythm',
        tiers: {
          c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
          c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' },
        },
      }],
    }],
  }

  function mockProviderState(
    status: Record<string, unknown>,
    config: Record<string, unknown>,
    options: FreshProviderStateOptions = {},
  ) {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return options.catalog ?? tokenRhythmCatalog
      if (method === 'onboarding.status') return status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return config
      if (method === 'config.effective' && options.effective) return options.effective
      if (method === 'onboarding.provider.configure' && options.allowPrimarySave) return {}
      throw new Error(`Unexpected RPC method: ${method}`)
    })
  }

  function mockFreshRuntimeDefaults(options: FreshProviderStateOptions = {}) {
    mockProviderState(
      {
        hasConfig: false,
        llmConfigured: false,
        llmSource: 'missing_env',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: false,
          source: 'missing_env',
          envKey: 'TOKENRHYTHM_API_KEY',
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
      options,
    )
  }

  function configuredProviderStatus(provider: string) {
    return {
      hasConfig: true,
      llmConfigured: true,
      llmSource: 'explicit',
      llmCredentialStatus: {
        provider,
        available: true,
        source: 'explicit',
      },
    }
  }

  async function expectMultiProviderRouting(
    provider: string,
    router: Record<string, unknown> | undefined,
    ensemble: Record<string, unknown> | undefined,
    expected: boolean,
  ) {
    mockProviderState(
      configuredProviderStatus(provider),
      {
        llm: { provider, model: 'primary-model' },
        ...(router ? { squilla_router: router } : {}),
        ...(ensemble ? { llm_ensemble: ensemble } : {}),
      },
    )

    const { api, app } = await mountCatalog()
    expect(api.providerPanel.value.routingEnabled).toBe(expected)
    app.unmount()
  }

  it('keeps runtime defaults out of configured providers', async () => {
    mockFreshRuntimeDefaults()

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([])
    app.unmount()
  })

  it('does not present a provider-resolution conflict as a configured provider', async () => {
    mockProviderState(
      {
        hasConfig: false,
        llmConfigured: false,
        llmSource: 'none',
        llmCredentialStatus: {
          provider: 'openrouter',
          available: true,
          source: 'env',
          envKey: 'OPENROUTER_API_KEY',
        },
        sectionDetails: {
          llm: {
            status: 'degraded',
            blocking: true,
            actionRequired: true,
            providerResolution: {
              status: 'conflict',
              effectiveProvider: '',
              source: 'conflicting_evidence',
              reasonCode: 'providerless_provider_conflict',
              actionRequired: true,
            },
          },
        },
      },
      {
        llm: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
      {
        catalog: {
          providers: [{
            providerId: 'openrouter',
            label: 'OpenRouter',
            runtimeSupported: true,
            fields: [{ name: 'model', label: 'Model' }],
          }],
        },
        effective: {
          fields: {
            'llm.provider': { value: 'openrouter', source: 'config' },
          },
        },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([])
    expect(api.providerPanel.value.providerSelected).toBe('')
    expect(api.hasSavedProvider.value).toBe(false)
    expect(api.sectionStatus('provider')).toEqual({
      label: 'Needs action',
      tone: 'is-warn',
    })
    app.unmount()
  })

  it('re-enables an inline provider preset without leaking materialized OpenRouter tiers', async () => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          router: { routerBinding: 'follow_primary' },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: {
          enabled: false,
          tiers: {
            c0: { provider: 'openrouter', model: 'materialized-default' },
          },
        },
        llm_ensemble: { enabled: false },
      },
    )

    const { api, app } = await mountCatalog()
    api.setModelStrategy('router')

    expect(api.routerPanel.value.providerOptions.map(row => row.providerId)).toEqual([
      'tokenrhythm',
    ])
    expect(api.routerPanel.value.tierRows).toEqual(expect.arrayContaining([
      expect.objectContaining({ provider: 'tokenrhythm', model: 'deepseek-v4-flash' }),
      expect.objectContaining({ provider: 'tokenrhythm', model: 'deepseek-v4-pro' }),
    ]))
    expect(api.routerPanel.value.tierRows.some(row => row.provider === 'openrouter')).toBe(false)
    app.unmount()
  })

  it('fills synthesized follow-primary tiers from the active direct model', async () => {
    const anthropicCatalog = {
      providers: [{
        providerId: 'anthropic',
        label: 'Anthropic',
        runtimeSupported: true,
        fields: [{ name: 'model', label: 'Model' }],
        presets: [{
          presetId: 'anthropic',
          label: 'Anthropic',
          synthesized: true,
          tiers: Object.fromEntries(['c0', 'c1', 'c2', 'c3'].map(name => [name, {
            provider: 'anthropic',
            model: '',
          }])),
        }],
      }],
    }
    mockProviderState(
      {
        ...configuredProviderStatus('anthropic'),
        sectionDetails: {
          router: { routerBinding: 'follow_primary' },
        },
      },
      {
        llm: { provider: 'anthropic', model: 'claude-sonnet-4' },
        squilla_router: {
          enabled: false,
          tiers: {
            c0: { provider: 'openrouter', model: 'materialized-default' },
          },
        },
        llm_ensemble: { enabled: false },
      },
      { catalog: anthropicCatalog },
    )

    const { api, app } = await mountCatalog()
    api.setModelStrategy('router')

    expect(api.routerPanel.value.tierRows).toHaveLength(4)
    expect(api.routerPanel.value.tierRows.every(row => (
      row.provider === 'anthropic' && row.model === 'claude-sonnet-4'
    ))).toBe(true)
    expect(api.routerPanel.value.tierRows.some(row => row.provider === 'openrouter')).toBe(false)
    app.unmount()
  })

  it('ignores an active TokenRhythm profile-status row when it only describes the fresh default', async () => {
    mockProviderState(
      {
        hasConfig: false,
        llmConfigured: false,
        llmSource: 'missing_env',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: false,
          source: 'missing_env',
          envKey: 'TOKENRHYTHM_API_KEY',
        },
        llmProfileStatus: [{
          provider: 'tokenrhythm',
          ready: false,
          credentialSource: 'missing_env',
          credentialEnv: 'TOKENRHYTHM_API_KEY',
          endpointSource: 'registry',
          reason: 'missing_credentials',
        }],
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
      {
        effective: {
          fields: {
            'llm.provider': { value: 'tokenrhythm', source: 'default' },
          },
        },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([])
    app.unmount()
  })

  it('keeps the default provider unconfigured after an unrelated config file is persisted', async () => {
    mockProviderState(
      {
        hasConfig: true,
        llmConfigured: false,
        llmSource: 'missing_env',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: false,
          source: 'missing_env',
          envKey: 'TOKENRHYTHM_API_KEY',
        },
        llmProfileStatus: [{
          provider: 'tokenrhythm',
          ready: false,
          credentialSource: 'missing_env',
          credentialEnv: 'TOKENRHYTHM_API_KEY',
          endpointSource: 'registry',
          reason: 'missing_credentials',
        }],
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        naming: { enabled: false },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
      {
        effective: {
          fields: {
            'llm.provider': { value: 'tokenrhythm', source: 'default' },
          },
        },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([])
    expect(api.providerPanel.value.providerSelected).toBe('')
    expect(api.hasSavedProvider.value).toBe(false)
    expect(api.modelStrategyPanel.value.hasSavedProvider).toBe(false)
    expect(api.hasUnsavedChanges.value).toBe(false)
    app.unmount()
  })

  it('configures TokenRhythm as the primary provider on its first fresh-install save', async () => {
    mockFreshRuntimeDefaults({ allowPrimarySave: true })
    const { api, app } = await mountCatalog()

    api.selectProvider('tokenrhythm')
    api.onProviderChange()
    api.updateProviderField('model', 'deepseek-v4-flash')
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', {
      providerId: 'tokenrhythm',
      model: 'deepseek-v4-flash',
    })
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.upsert')).toBe(false)
    app.unmount()
  })

  it('configures a non-default first selection as primary rather than a routing profile', async () => {
    const openAiCatalog = {
      providers: [
        ...tokenRhythmCatalog.providers,
        {
          providerId: 'openai',
          label: 'OpenAI',
          runtimeSupported: true,
          requiresApiKey: true,
          fields: [
            { name: 'model', label: 'Model', required: true, default: 'gpt-4.1-mini' },
            { name: 'api_key', label: 'API key', secret: true },
          ],
        },
      ],
    }
    mockFreshRuntimeDefaults({ catalog: openAiCatalog, allowPrimarySave: true })
    const { api, app } = await mountCatalog()

    api.selectProvider('openai')
    api.onProviderChange()
    api.updateProviderField('api_key', 'sk-public-test-placeholder')
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', {
      providerId: 'openai',
      model: 'gpt-4.1-mini',
      apiKey: 'sk-public-test-placeholder',
    })
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.upsert')).toBe(false)
    app.unmount()
  })

  it('leaves Model Strategy pristine for the default disabled ensemble', async () => {
    mockFreshRuntimeDefaults()

    const { api, app } = await mountCatalog()

    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.hasUnsavedChanges.value).toBe(false)
    app.unmount()
  })

  it('preserves an enabled foreign static ensemble profile without dirtying the form', async () => {
    mockProviderState(
      {
        hasConfig: true,
        llmConfigured: true,
        llmSource: 'explicit',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: true,
          source: 'explicit',
        },
        llmProfileStatus: [{
          provider: 'openrouter',
          ready: true,
          credentialSource: 'explicit',
          endpointSource: 'registry',
        }],
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        llm_profiles: { openrouter: {} },
        squilla_router: { enabled: false, cross_provider_tiers: false },
        llm_ensemble: {
          enabled: true,
          selection_mode: 'static_openrouter_b5',
        },
      },
      {
        effective: {
          fields: {
            'llm.provider': { value: 'tokenrhythm', source: 'config' },
          },
        },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.ensemblePanel.value.selectionMode).toBe('static_openrouter_b5')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.hasUnsavedChanges.value).toBe(false)
    app.unmount()
  })

  it('surfaces the configured and effective failure policies reported by the Gateway', async () => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          ensemble: {
            configuredAllFailedPolicy: 'error',
            effectiveAllFailedPolicy: 'fallback_single',
            policyDeprecated: true,
          },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: { enabled: false, cross_provider_tiers: false },
        llm_ensemble: {
          enabled: true,
          selection_mode: 'static_tokenrhythm_b5',
          all_failed_policy: 'fallback_single',
        },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.ensemblePanel.value.configuredAllFailedPolicy).toBe('error')
    expect(api.ensemblePanel.value.effectiveAllFailedPolicy).toBe('fallback_single')
    expect(api.ensemblePanel.value.policyDeprecated).toBe(true)
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    app.unmount()
  })

  it('does not present ordinary router enablement as multi-provider routing', async () => {
    mockProviderState(
      {
        hasConfig: true,
        llmConfigured: true,
        llmSource: 'explicit',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: true,
          source: 'explicit',
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.routingEnabled).toBe(false)
    app.unmount()
  })

  it('keeps an env-only effective provider visible as Active before config is persisted', async () => {
    mockProviderState(
      {
        hasConfig: false,
        llmConfigured: true,
        llmSource: 'env',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: true,
          source: 'env',
          envKey: 'TOKENRHYTHM_API_KEY',
        },
      },
      {
        llm: {
          provider: 'tokenrhythm',
          model: 'deepseek-v4-flash',
          api_key_env: 'TOKENRHYTHM_API_KEY',
        },
        squilla_router: { enabled: true, cross_provider_tiers: false },
        llm_ensemble: { enabled: false },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([
      expect.objectContaining({ providerId: 'tokenrhythm', active: true, ready: true }),
    ])
    app.unmount()
  })

  it('presents explicitly enabled cross-provider tiers as multi-provider routing', async () => {
    mockProviderState(
      {
        hasConfig: true,
        llmConfigured: true,
        llmSource: 'explicit',
        llmCredentialStatus: {
          provider: 'tokenrhythm',
          available: true,
          source: 'explicit',
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
        squilla_router: { enabled: true, cross_provider_tiers: true },
        llm_ensemble: { enabled: false },
      },
    )

    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.routingEnabled).toBe(true)
    app.unmount()
  })

  it.each([
    { role: 'dormant_draft', expected: false },
    { role: 'dynamic_member', expected: true },
  ])('consumes the server C3 provider role $role', async ({ role, expected }) => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          router: {
            routerBinding: 'custom',
            routerProviderRoles: { c0: 'direct', c3: role },
          },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'primary-model' },
        squilla_router: {
          enabled: true,
          tiers: {
            c0: { provider: 'tokenrhythm', model: 'fast-model' },
            c3: {
              provider: 'openrouter',
              model: 'saved-c3-model',
              ensemble_enabled: true,
            },
          },
        },
        llm_ensemble: { enabled: false, selection_mode: 'static_tokenrhythm_b5' },
      },
    )

    const { api, app } = await mountCatalog()
    expect(api.modelStrategyPanel.value.router.hasMixedTierProviders).toBe(expected)
    expect(api.modelStrategyPanel.value.router.routerProviderRoles).toEqual({
      c0: 'direct',
      c3: role,
    })
    app.unmount()
  })

  it('loads saved C3 fusion readiness from the Router status and invalidates it on edit', async () => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          router: {
            routerBinding: 'custom',
            routerProviderRoles: { c0: 'dynamic_member', c3: 'dynamic_member' },
            tierEnsembleStatuses: {
              c0: {
                selectionMode: 'static_tokenrhythm_b5',
                activationTiers: ['c0'],
                tierSelectionModes: { c0: 'static_tokenrhythm_b5' },
                runtimeStatus: 'blocked',
                configurationReady: false,
                blockedReason: 'other_tier_not_ready',
                blockedTierCandidates: [],
                fixedFallbackReady: true,
                fixedFallbackBlockedReason: null,
              },
              c3: {
                selectionMode: 'router_dynamic',
                activationTiers: ['c3'],
                tierSelectionModes: { c3: 'router_dynamic' },
                runtimeStatus: 'conditional',
                configurationReady: null,
                blockedReason: null,
                blockedTierCandidates: [],
                fixedFallbackReady: true,
                fixedFallbackBlockedReason: null,
              },
            },
          },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'primary-model' },
        squilla_router: {
          enabled: true,
          tiers: {
            c0: { provider: 'tokenrhythm', model: 'fast-model' },
            c3: {
              provider: 'tokenrhythm',
              model: 'quality-model',
              ensemble_enabled: true,
            },
          },
        },
        llm_ensemble: { enabled: false, selection_mode: 'router_dynamic' },
      },
    )

    const { api, app } = await mountCatalog()
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toMatchObject({
      selectionMode: 'router_dynamic',
      activationTiers: ['c3'],
      tierSelectionModes: { c3: 'router_dynamic' },
      runtimeStatus: 'conditional',
      fixedFallbackReady: true,
    })
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(true)

    api.updateTierField('c3', 'model', 'edited-quality-model')
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(false)
    app.unmount()
  })

  it('does not fall back to a singular C3 status when the per-tier map omits C3', async () => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          router: {
            routerBinding: 'custom',
            routerProviderRoles: { c3: 'dynamic_member' },
            tierEnsembleStatuses: {
              c0: {
                selectionMode: 'router_dynamic',
                activationTiers: ['c0'],
                tierSelectionModes: { c0: 'router_dynamic' },
                runtimeStatus: 'conditional',
              },
            },
            tierEnsembleStatus: {
              selectionMode: 'router_dynamic',
              activationTiers: ['c3'],
              tierSelectionModes: { c3: 'router_dynamic' },
              runtimeStatus: 'conditional',
            },
          },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'primary-model' },
        squilla_router: {
          enabled: true,
          tiers: {
            c3: {
              provider: 'tokenrhythm',
              model: 'quality-model',
              ensemble_enabled: true,
            },
          },
        },
        llm_ensemble: { enabled: false, selection_mode: 'router_dynamic' },
      },
    )

    const { api, app } = await mountCatalog()
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toBeNull()
    app.unmount()
  })

  it('invalidates saved C3 readiness when an independent image-model row is edited', async () => {
    mockProviderState(
      {
        ...configuredProviderStatus('tokenrhythm'),
        sectionDetails: {
          router: {
            routerBinding: 'custom',
            routerProviderRoles: { c3: 'dormant_draft', image_model: 'direct' },
            tierEnsembleStatuses: {
              c3: {
                selectionMode: 'static_tokenrhythm_b5',
                activationTiers: ['c3'],
                tierSelectionModes: { c3: 'static_tokenrhythm_b5' },
                runtimeStatus: 'blocked',
                configurationReady: false,
                blockedReason: 'fixed_fallback:missing_credential:tokenrhythm',
                blockedTierCandidates: [],
                fixedFallbackReady: false,
                fixedFallbackBlockedReason: 'fixed_fallback:missing_credential:tokenrhythm',
              },
            },
          },
        },
      },
      {
        llm: { provider: 'tokenrhythm', model: 'primary-model' },
        squilla_router: {
          enabled: true,
          tiers: {
            c3: {
              provider: 'tokenrhythm',
              model: 'quality-model',
              ensemble_enabled: true,
            },
            image_model: {
              provider: 'tokenrhythm',
              model: 'vision-model',
              supports_image: true,
            },
          },
        },
        llm_ensemble: { enabled: false, selection_mode: 'static_tokenrhythm_b5' },
      },
    )

    const { api, app } = await mountCatalog()
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toMatchObject({
      runtimeStatus: 'blocked',
      fixedFallbackReady: false,
    })
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(true)

    api.updateTierField('image_model', 'model', 'vision-model-v2')
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(false)
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toMatchObject({
      runtimeStatus: 'blocked',
      fixedFallbackReady: false,
    })
    app.unmount()
  })

  it.each([
    {
      name: 'ignores a stale cross-provider flag while the router is disabled',
      router: { enabled: false, cross_provider_tiers: true },
    },
    {
      name: 'fails closed when an older Gateway omits cross_provider_tiers',
      router: {
        enabled: true,
        tiers: {
          c0: { provider: 'tokenrhythm', model: 'fast-model' },
          c1: { provider: 'openrouter', model: 'vendor/balanced-model' },
        },
      },
    },
  ])('$name', async ({ router }) => {
    await expectMultiProviderRouting('tokenrhythm', router, { enabled: false }, false)
  })

  it.each([
    {
      name: 'keeps a same-provider custom lineup off',
      candidates: [
        { provider: 'tokenrhythm', model: 'draft-a' },
        { provider: 'tokenrhythm', model: 'draft-b' },
      ],
      modelOptions: undefined,
      expected: false,
    },
    {
      name: 'detects a custom lineup routed wholly to a secondary provider',
      candidates: [
        { provider: 'deepseek', model: 'deepseek-chat' },
        { provider: 'deepseek', model: 'deepseek-reasoner' },
      ],
      modelOptions: undefined,
      expected: true,
    },
    {
      name: 'ignores disabled foreign custom candidates',
      candidates: [
        { provider: 'tokenrhythm', model: 'draft-a' },
        { provider: 'tokenrhythm', model: 'draft-b' },
        { provider: 'deepseek', model: 'deepseek-chat', enabled: false },
      ],
      modelOptions: undefined,
      expected: false,
    },
    {
      name: 'counts a foreign custom aggregator as cross-provider execution',
      candidates: [
        { provider: 'tokenrhythm', model: 'draft-a' },
        { provider: 'tokenrhythm', model: 'draft-b' },
        { provider: 'gemini', model: 'gemini-flash', role: 'aggregator' },
      ],
      modelOptions: undefined,
      expected: true,
    },
    {
      name: 'ignores legacy model_options in custom mode',
      candidates: [
        { provider: 'tokenrhythm', model: 'draft-a' },
        { provider: 'tokenrhythm', model: 'draft-b' },
      ],
      modelOptions: ['vendor/ignored-model'],
      expected: false,
    },
  ])('$name', async ({ candidates, modelOptions, expected }) => {
    await expectMultiProviderRouting(
      'tokenrhythm',
      { enabled: false, cross_provider_tiers: false },
      {
        enabled: true,
        selection_mode: 'custom_b5',
        candidates,
        ...(modelOptions ? { model_options: modelOptions } : {}),
      },
      expected,
    )
  })

  it.each([
    {
      name: 'keeps the OpenRouter static lineup off for an OpenRouter primary',
      provider: 'openrouter',
      selectionMode: 'static_openrouter_b5',
      expected: false,
    },
    {
      name: 'detects the OpenRouter static lineup for a different primary',
      provider: 'tokenrhythm',
      selectionMode: 'static_openrouter_b5',
      expected: true,
    },
    {
      name: 'keeps the TokenRhythm static lineup off for a TokenRhythm primary',
      provider: 'tokenrhythm',
      selectionMode: 'static_tokenrhythm_b5',
      expected: false,
    },
    {
      name: 'detects the TokenRhythm static lineup for a different primary',
      provider: 'openrouter',
      selectionMode: 'static_tokenrhythm_b5',
      expected: true,
    },
  ])('$name', async ({ provider, selectionMode, expected }) => {
    await expectMultiProviderRouting(
      provider,
      { enabled: false, cross_provider_tiers: false },
      { enabled: true, selection_mode: selectionMode },
      expected,
    )
  })

  it('ignores stale candidate and model-option fields in a static lineup', async () => {
    await expectMultiProviderRouting(
      'openrouter',
      { enabled: false, cross_provider_tiers: false },
      {
        enabled: true,
        selection_mode: 'static_openrouter_b5',
        candidates: [
          { provider: 'deepseek', model: 'deepseek-chat' },
          { provider: 'gemini', model: 'gemini-flash' },
        ],
        model_options: ['vendor/ignored-model'],
      },
      false,
    )
  })

  it.each([
    {
      name: 'detects a foreign router_dynamic candidate',
      router: { enabled: false, cross_provider_tiers: false },
      candidates: [
        { provider: 'deepseek', model: 'deepseek-chat' },
      ],
      modelOptions: [] as string[],
      expected: true,
    },
    {
      name: 'detects a foreign router tier in the router_dynamic pool',
      router: {
        enabled: false,
        cross_provider_tiers: false,
        tiers: { c1: { provider: 'gemini', model: 'gemini-flash' } },
      },
      candidates: [],
      modelOptions: [] as string[],
      expected: true,
    },
    {
      name: 'maps slash-prefixed router_dynamic model options to OpenRouter',
      router: { enabled: false, cross_provider_tiers: false },
      candidates: [],
      modelOptions: ['vendor/routed-model'],
      expected: true,
    },
    {
      name: 'maps plain router_dynamic model options to the active provider',
      router: { enabled: false, cross_provider_tiers: false },
      candidates: [],
      modelOptions: ['same-provider-model'],
      expected: false,
    },
    {
      name: 'ignores the retired default option list in explicit router_dynamic mode',
      router: { enabled: false, cross_provider_tiers: false },
      candidates: [],
      modelOptions: [...LEGACY_OPENROUTER_MODEL_OPTIONS],
      expected: false,
    },
  ])('$name', async ({ router, candidates, modelOptions, expected }) => {
    await expectMultiProviderRouting(
      'tokenrhythm',
      router,
      {
        enabled: true,
        selection_mode: 'router_dynamic',
        candidates,
        model_options: modelOptions,
      },
      expected,
    )
  })

  it('treats a pre-selection-mode Gateway response as legacy router_dynamic', async () => {
    await expectMultiProviderRouting(
      'tokenrhythm',
      { enabled: false },
      {
        enabled: true,
        model_options: [...LEGACY_OPENROUTER_MODEL_OPTIONS],
      },
      true,
    )
  })

  it('fails closed for an unknown enabled ensemble selection mode', async () => {
    await expectMultiProviderRouting(
      'tokenrhythm',
      { enabled: false },
      {
        enabled: true,
        selection_mode: 'future_unknown_mode',
        candidates: [
          { provider: 'deepseek', model: 'deepseek-chat' },
          { provider: 'gemini', model: 'gemini-flash' },
        ],
        model_options: ['vendor/routed-model'],
      },
      false,
    )
  })

  it('keeps a disabled foreign static ensemble out of routing state', async () => {
    await expectMultiProviderRouting(
      'tokenrhythm',
      { enabled: false },
      { enabled: false, selection_mode: 'static_openrouter_b5' },
      false,
    )
  })
})

describe('useSetupCatalog configured provider management', () => {
  const customProvider = {
    providerId: 'custom',
    label: 'Custom OpenAI-compatible endpoint',
    runtimeSupported: true,
    requiresApiKey: false,
    defaultModel: '',
    fields: [
      { name: 'model', label: 'Model', required: true, default: '' },
      { name: 'base_url', label: 'Base URL', required: true, default: '' },
    ],
  }
  const providers = [
    {
      providerId: 'openai',
      label: 'OpenAI',
      runtimeSupported: true,
      requiresApiKey: true,
      defaultDirectModel: 'gpt-4.1-mini',
      defaultModel: 'gpt-4.1-mini',
      fields: [{ name: 'model', label: 'Model', required: true, default: 'gpt-4.1-mini' }],
    },
    {
      providerId: 'deepseek',
      label: 'DeepSeek',
      runtimeSupported: true,
      requiresApiKey: true,
      defaultDirectModel: 'deepseek-chat',
      defaultModel: 'deepseek-chat',
      fields: [
        { name: 'model', label: 'Model', required: true, default: 'deepseek-chat' },
        { name: 'api_key', label: 'API key', secret: true },
        { name: 'api_key_env', label: 'API key env' },
      ],
    },
    { providerId: 'gemini', label: 'Google Gemini', runtimeSupported: true },
  ]

  function statusWithDeepSeek() {
    return {
      hasConfig: true,
      llmConfigured: true,
      llmSource: 'explicit',
      llmCredentialStatus: {
        provider: 'openai',
        available: true,
        source: 'explicit',
      },
      llmProfileStatus: [
        {
          provider: 'openai',
          ready: true,
          credentialSource: 'explicit',
          endpointSource: 'registry',
        },
        {
          provider: 'deepseek',
          ready: true,
          credentialSource: 'env',
          credentialEnv: 'DEEPSEEK_API_KEY',
          endpointSource: 'registry',
        },
        {
          provider: 'legacy-provider',
          ready: false,
          credentialSource: 'none',
          endpointSource: 'registry',
          reason: 'missing_credentials',
        },
      ],
    }
  }

  function configWithProfiles(...providerIds: string[]) {
    return {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_profiles: Object.fromEntries(providerIds.map(providerId => [providerId, {}])),
    }
  }

  function statusWithProfileBackedImage(
    source: 'llm_fallback' | 'env' | 'none',
  ) {
    const available = source !== 'none'
    const owner = source === 'llm_fallback' ? 'profile' : source === 'env' ? 'image' : 'none'
    return {
      ...statusWithDeepSeek(),
      imageGenerationEnabled: true,
      imageGenerationConfigured: available,
      imageGenerationProvider: 'deepseek',
      imageGenerationPrimary: 'deepseek/image-model',
      imageGenerationSource: source,
      imageGenerationEnvKey: source === 'env' ? 'DEEPSEEK_API_KEY' : '',
      imageGenerationState: {
        mode: 'custom',
        storedEnabled: true,
        effective: {
          enabled: true,
          available,
          providerId: 'deepseek',
          primary: 'deepseek/image-model',
          credentialSource: source,
          credentialOwner: owner,
        },
        credentialOptions: [{
          providerId: 'deepseek',
          available,
          source,
          owner,
          envKey: source === 'env' ? 'DEEPSEEK_API_KEY' : '',
        }],
      },
    }
  }

  it('uses persisted profile discovery for clean refreshes and post-probe refreshes', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [] }
      }
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 19 }
      if (method === 'onboarding.llmProfile.models.discover') {
        return { ok: true, source: 'live', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.models.discover',
      { providerId: 'deepseek' },
    ))
    expect(api.providerDraftDirty.value).toBe(false)
    rpcCall.mockClear()

    await api.probeProviderConnection()
    await api.refreshProviderModels()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.probe', {
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    expect(rpcCall.mock.calls.filter(([method, params]) => (
      method === 'onboarding.llmProfile.models.discover'
      && (params as Record<string, unknown>).forceRefresh === true
    ))).toHaveLength(2)
    expect(rpcCall.mock.calls.some(([method]) => (
      method === 'onboarding.llmProfile.draft.models.discover'
      || method === 'onboarding.llmProfile.draft.probe'
    ))).toBe(false)
    app.unmount()
  })

  it('marks only unsaved provider/model candidates as awaiting an effective limit', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, customProvider] }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [] }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        return { ok: true, source: 'live', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    // Missing effective metadata is unknown, not evidence that the saved
    // primary deployment is waiting to be persisted.
    expect(api.providerPanel.value.effectiveMaxTokens).toBeNull()
    expect(api.providerPanel.value.effectiveMaxTokensPending).toBe(false)

    api.updateProviderField('model', 'gpt-4.1')
    expect(api.providerPanel.value.effectiveMaxTokensPending).toBe(true)

    api.selectConfiguredProvider('deepseek')
    expect(api.providerPanel.value.effectiveMaxTokensPending).toBe(false)
    api.updateProviderField('model', 'deepseek-reasoner')
    expect(api.providerPanel.value.effectiveMaxTokensPending).toBe(true)

    await api.requestAddProvider('custom')
    api.updateProviderField('model', 'new-custom-model')
    expect(api.providerPanel.value.effectiveMaxTokensPending).toBe(true)
    app.unmount()
  })

  it('activates a configured provider selected for fixed-model mode on save', async () => {
    let activeProvider = 'openai'
    let activeModel = 'gpt-4.1-mini'
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmCredentialStatus: {
            provider: activeProvider,
            available: true,
            source: 'explicit',
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: activeProvider, model: activeModel },
          llm_profiles: activeProvider === 'openai'
            ? { deepseek: { model: 'deepseek-chat' } }
            : { openai: { model: 'gpt-4.1-mini' } },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.llmProfile.activate') {
        activeProvider = String(params?.providerId || '')
        activeModel = String(params?.model || '')
        return { changed: true, restartRequired: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.modelStrategyPanel.value.single.providerId).toBe('openai')
    api.setFixedProvider('deepseek')

    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'deepseek',
      providerLabel: 'DeepSeek',
      model: 'deepseek-chat',
    })
    expect(api.sectionDirty('modelStrategy')).toBe(true)

    await api.saveDirtySections()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.activate', {
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    expect(rpcCall).not.toHaveBeenCalledWith('config.patch', expect.anything())
    expect(api.modelStrategyPanel.value.single.providerId).toBe('deepseek')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    app.unmount()
  })

  it('loads the saved active provider catalog without probing or dirtying the editor', async () => {
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        expect(params).toEqual({ providerId: 'openai', model: 'gpt-4.1-mini' })
        return {
          ok: true,
          source: 'live',
          models: [{ id: 'gpt-4.1-mini', name: 'GPT-4.1 mini' }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await vi.waitFor(() => expect(api.providerPanel.value.connection.models).toHaveLength(1))
    expect(api.providerPanel.value.connection).toMatchObject({
      phase: 'unverified',
      modelSource: 'live',
    })
    expect(api.sectionDirty('provider')).toBe(false)
    expect(rpcCall.mock.calls.some(call => String(call[0]).includes('.probe'))).toBe(false)
    app.unmount()
  })

  it('loads a selected saved profile catalog through the profile deployment RPC', async () => {
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'gpt-4.1-mini', name: 'GPT-4.1 mini' }] }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        expect(params).toEqual({ providerId: 'deepseek' })
        return { ok: true, source: 'live', models: [{ id: 'deepseek-chat', name: 'DeepSeek Chat' }] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    await vi.waitFor(() => expect(api.providerPanel.value.connection.models[0]?.id).toBe('deepseek-chat'))

    expect(api.providerPanel.value.connection).toMatchObject({
      phase: 'unverified',
      modelSource: 'live',
    })
    expect(api.sectionDirty('provider')).toBe(false)
    expect(rpcCall.mock.calls.some(call => String(call[0]).includes('.probe'))).toBe(false)
    app.unmount()
  })

  it.each([
    ['an empty provider catalog', async () => ({ ok: true, source: 'none', models: [] })],
    ['an unavailable discovery method', async () => { throw new Error('unknown method') }],
  ])('keeps manual model entry available for %s', async (_label, discover) => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') return discover()
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await vi.waitFor(() => expect(
      rpcCall.mock.calls.some(call => call[0] === 'onboarding.models.discover'),
    ).toBe(true))
    expect(api.providerPanel.value.connection.models).toEqual([])
    expect(api.providerPanel.value.connection.modelSource).toBe('none')
    expect(api.providerPanel.value.connection.phase).toBe('unverified')

    api.updateProviderField('model', 'manually-entered-model')
    expect(api.providerPanel.value.providerFieldValue({ name: 'model', label: 'Model' }))
      .toBe('manually-entered-model')
    app.unmount()
  })

  it('does not expose route-only deployment status as a deletable configured profile', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openai', model: 'gpt-4.1-mini' },
          llm_profiles: {},
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
          },
          llm_ensemble: {
            enabled: true,
            selection_mode: 'custom_b5',
            candidates: [
              { provider: 'gemini', model: 'gemini-2.5-flash', role: 'aggregator' },
            ],
          },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    // llmProfileStatus is deployment readiness for Router/Ensemble and can
    // legitimately contain providers that have no persisted llm_profile.
    // Only the active provider and actual llm_profiles belong in Model Service.
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId)).toEqual([
      'openai',
    ])
    expect(api.routerPanel.value.providerOptions.map(row => row.providerId)).toEqual(['openai'])
    await api.removeProviderProfile('deepseek')
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.remove')).toBe(false)
    app.unmount()
  })

  it('offers only persisted providers when historical router tiers name a removed provider', async () => {
    const tokenRhythm = {
      providerId: 'tokenrhythm',
      label: 'TokenRhythm',
      runtimeSupported: true,
      requiresApiKey: true,
      defaultDirectModel: 'deepseek-v4-flash',
      defaultModel: 'deepseek-v4-flash',
      fields: [{ name: 'model', label: 'Model', required: true, default: 'deepseek-v4-flash' }],
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, tokenRhythm] }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'explicit',
          llmCredentialStatus: {
            provider: 'deepseek',
            available: true,
            source: 'explicit',
          },
          llmProfileStatus: [
            {
              provider: 'deepseek',
              ready: true,
              credentialSource: 'explicit',
              endpointSource: 'registry',
            },
            {
              provider: 'tokenrhythm',
              ready: true,
              credentialSource: 'explicit',
              endpointSource: 'registry',
            },
          ],
          sectionDetails: {
            router: { routerBinding: 'custom' },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'deepseek', model: 'deepseek-chat' },
          llm_profiles: { tokenrhythm: {} },
          squilla_router: {
            enabled: true,
            preset_binding: 'custom',
            tiers: {
              c0: { provider: 'openrouter', model: 'legacy-model' },
              c1: { provider: 'deepseek', model: 'deepseek-chat' },
              image_model: { provider: 'deepseek', model: 'deepseek-vision' },
            },
          },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId)).toEqual([
      'deepseek',
      'tokenrhythm',
    ])
    expect(api.routerPanel.value.providerOptions.map(row => row.providerId)).toEqual([
      'deepseek',
      'tokenrhythm',
    ])
    expect(api.routerPanel.value.tierRows).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'c0', provider: 'openrouter', model: 'legacy-model' }),
      expect.objectContaining({ name: 'c1', provider: 'deepseek', model: 'deepseek-chat' }),
      expect.objectContaining({ name: 'image_model', provider: 'deepseek', model: 'deepseek-vision' }),
    ]))
    // The dedicated image route is independent and must never be imported as
    // a text proposer when the user converts a legacy dynamic plan.
    expect(api.ensemblePanel.value.tierCandidates).toEqual([
      expect.objectContaining({ provider: 'deepseek', model: 'deepseek-chat', source: 'tier' }),
    ])
    app.unmount()
  })

  it('keeps persisted profiles visible when an older gateway omits profile status', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'explicit',
          llmCredentialStatus: {
            provider: 'openai',
            available: true,
            source: 'explicit',
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders).toEqual([
      expect.objectContaining({ providerId: 'openai', active: true }),
      expect.objectContaining({
        providerId: 'deepseek',
        active: false,
        ready: false,
        reason: 'profile_status_unavailable',
      }),
    ])
    app.unmount()
  })

  it('treats additive profile status without activation fields as compatibility-unknown', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.configuredProviders.find(row => row.providerId === 'deepseek'))
      .toMatchObject({
        ready: true,
        primaryEligible: false,
        primaryBlockReason: 'profile_status_unavailable',
      })
    app.unmount()
  })

  it('does not discard the shared model draft when the selected provider is clicked again', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateProviderField('model', 'unsaved-model')
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.sectionDirty('provider')).toBe(false)
    expect(api.providerDraftDirty.value).toBe(true)

    await api.requestSelectConfiguredProvider('openai')

    expect(confirmAction).not.toHaveBeenCalled()
    expect(api.providerPanel.value.providerFieldValue({ name: 'model', label: 'Model' }))
      .toBe('unsaved-model')
    expect(api.providerDraftDirty.value).toBe(true)
    app.unmount()
  })

  it('hydrates and safely round-trips a saved profile endpoint and proxy', async () => {
    const endpointProvider = {
      ...customProvider,
      fields: [
        ...(customProvider.fields || []),
        { name: 'proxy', label: 'Proxy', default: '' },
      ],
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, endpointProvider] }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmProfileStatus: [
            ...(statusWithDeepSeek().llmProfileStatus || []),
            {
              provider: 'custom',
              ready: true,
              credentialSource: 'profile_env',
              credentialEnv: 'CUSTOM_PROFILE_KEY',
              endpointSource: 'profile',
              reason: '',
            },
          ],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('custom'),
          llm_profiles: {
            custom: {
              api_key_env: 'CUSTOM_PROFILE_KEY',
              base_url: 'https://llm.example.test/v1',
              proxy: 'http://proxy.example.test:8080',
            },
          },
        }
      }
      if (method === 'onboarding.llmProfile.upsert') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('custom')
    expect(api.providerPanel.value.providerFieldValue({ name: 'base_url', label: 'Base URL' }))
      .toBe('https://llm.example.test/v1')
    expect(api.providerPanel.value.providerFieldValue({ name: 'proxy', label: 'Proxy' }))
      .toBe('http://proxy.example.test:8080')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'env',
      available: true,
      envKey: 'CUSTOM_PROFILE_KEY',
      apiKeyEnvValue: 'CUSTOM_PROFILE_KEY',
    })
    expect(api.providerPanel.value.configuredProviders.find(row => row.providerId === 'custom'))
      .toMatchObject({ ready: true, reason: '' })
    expect(api.sectionDirty('provider')).toBe(false)

    api.updateProviderField('proxy', 'http://proxy-b.example.test:8080')
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.upsert', {
      providerId: 'custom',
      baseUrl: 'https://llm.example.test/v1',
      proxy: 'http://proxy-b.example.test:8080',
      keepCurrentSecret: true,
    })
    app.unmount()
  })

  it('limits routing provider choices to configured profiles without promoting historical references', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek', 'legacy-provider'),
          squilla_router: {
            enabled: true,
            tiers: {
              c0: { provider: 'deepseek', model: 'deepseek-chat' },
              c1: { provider: 'historical-unknown', model: 'archived-model' },
            },
          },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.routerPanel.value.providerOptions.map(row => row.providerId).sort()).toEqual([
      'deepseek',
      'legacy-provider',
      'openai',
    ])
    expect(api.routerPanel.value.providerOptions.map(row => row.providerId)).not.toContain('gemini')
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId)).toEqual([
      'openai',
      'deepseek',
      'legacy-provider',
    ])
    app.unmount()
  })

  it('upserts and reloads a non-primary profile without replacing the active provider', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.upsert') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    expect(api.sectionDirty('provider')).toBe(false)
    api.updateProviderField('model', 'deepseek-reasoner')
    expect(api.providerDraftDirty.value).toBe(true)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.upsert', {
      providerId: 'deepseek',
      model: 'deepseek-reasoner',
      keepCurrentSecret: true,
    })
    expect(api.providerPanel.value.providerSelected).toBe('deepseek')
    expect(api.providerPanel.value.selectedStoredProfile).toBe(true)
    expect(api.providerPanel.value.editingPrimary).toBe(false)
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId)).toContain('deepseek')
    app.unmount()
  })

  it('preserves dirty settings sections while rebasing a saved non-primary profile', async () => {
    let saved = false
    const configForState = () => ({
      ...configWithProfiles('deepseek'),
      naming: { enabled: false },
      privacy: { disable_network_observability: false },
      search_provider: 'duckduckgo',
      search_max_results: 10,
      memory: {
        auto_capture_enabled: true,
        embedding: {
          provider: 'auto',
          remote: { model: saved ? 'server-refreshed-model' : '' },
        },
      },
      image_generation: { size: '1024x1024', output_format: 'png' },
      audio: { enabled: false, tts: { voice: '' } },
    })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configForState()
      if (method === 'onboarding.llmProfile.upsert') {
        saved = true
        return { changed: true }
      }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setAutoSessionTitles(true)
    api.setDisableNetworkObservability(true)
    api.setMemoryAutoCapture(false)
    api.updateCapabilityField('search', 'maxResults', 25)
    api.updateCapabilityField('image', 'size', '512x512')
    api.updateCapabilityField('audio', 'ttsVoice', 'draft-voice')
    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.sectionDirty('privacy')).toBe(true)
    expect(api.sectionDirty('capabilities')).toBe(true)

    await api.requestSelectConfiguredProvider('deepseek')
    api.updateProviderField('model', 'deepseek-reasoner')
    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.behaviorPanel.value.autoSessionTitles).toBe(true)
    expect(api.privacyPanel.value).toMatchObject({
      disableNetworkObservability: true,
      memoryAutoCapture: false,
    })
    expect(api.capabilitiesPanel.value.form).toMatchObject({
      searchMaxResults: 25,
      memoryModel: 'server-refreshed-model',
      imageSize: '512x512',
      audioTtsVoice: 'draft-voice',
    })
    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.sectionDirty('privacy')).toBe(true)
    expect(api.sectionDirty('capabilities')).toBe(true)
    expect(api.providerDraftDirty.value).toBe(false)
    expect(api.providerPanel.value.providerSelected).toBe('deepseek')
    app.unmount()
  })

  it('preserves active-primary routing drafts and rebases Provider-owned fields after save', async () => {
    let saved = false
    const configForState = () => ({
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_request_timeout_seconds: saved ? 321 : 120,
      models: saved
        ? { openai: { 'draft-provider-model': { context_window: 65536 } } }
        : {},
      llm_profiles: { deepseek: { model: 'deepseek-chat' } },
      squilla_router: {
        enabled: true,
        preset_binding: 'custom',
        tiers: {
          c0: { provider: 'openai', model: 'gpt-4.1-mini' },
          c1: { provider: 'openai', model: 'gpt-4.1-mini' },
        },
      },
      llm_ensemble: {
        enabled: false,
        selection_mode: 'custom_b5',
        candidates: [
          { provider: 'openai', model: 'gpt-4.1-mini' },
          { provider: 'openai', model: 'gpt-4.1' },
        ],
        min_successful_proposers: 1,
      },
    })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          sectionDetails: { router: { routerBinding: 'custom' } },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configForState()
      if (method === 'onboarding.provider.configure') {
        saved = true
        return { changed: true }
      }
      if (method === 'config.patch') return { restartRequired: false }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateTierField('c0', 'model', 'draft-router-model')
    api.setEnsembleMinSuccessful(2)
    api.updateProviderField('model', 'draft-provider-model')
    api.updateLlmTimeout(321)
    api.updateContextWindow('65536')
    expect(api.providerDraftDirty.value).toBe(true)

    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.routerPanel.value.tierRows.find(row => row.name === 'c0')?.model)
      .toBe('draft-router-model')
    expect(api.ensemblePanel.value.minSuccessfulProposers).toBe(2)
    expect(api.modelStrategyPanel.value.single.model).toBe('draft-provider-model')
    expect(api.providerPanel.value.llmTimeoutSeconds).toBe(321)
    expect(api.providerPanel.value.contextWindowTokens).toBe('65536')
    expect(api.providerDraftDirty.value).toBe(false)
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.providerPanel.value.providerSelected).toBe('openai')
    expect(api.providerPanel.value.editingPrimary).toBe(true)
    expect(api.providerPanel.value.selectedStoredProfile).toBe(false)
    app.unmount()
  })

  it('rebases active-primary context against a retained Model Routing model draft', async () => {
    let saved = false
    const configForState = () => ({
      ...configWithProfiles('deepseek'),
      llm_request_timeout_seconds: saved ? 321 : 120,
      models: {
        openai: {
          'gpt-4.1-mini': { context_window: 8192 },
          'draft-router-model': { context_window: saved ? 65536 : 32768 },
        },
      },
      squilla_router: { enabled: false },
      llm_ensemble: { enabled: false },
    })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configForState()
      if (method === 'onboarding.provider.configure') {
        saved = true
        return { changed: true }
      }
      if (method === 'config.patch') return { restartRequired: false }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setFixedModel('draft-router-model')
    expect(api.providerPanel.value.contextWindowTokens).toBe('32768')
    api.updateLlmTimeout(321)

    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'openai',
      model: 'draft-router-model',
    })
    expect(api.providerPanel.value.contextWindowTokens).toBe('65536')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.providerDraftDirty.value).toBe(false)
    app.unmount()
  })

  it('keeps active context scoped and restores a foreign fixed identity on cancel', async () => {
    const config = {
      ...configWithProfiles('deepseek'),
      llm_request_timeout_seconds: 120,
      models: {
        openai: { 'gpt-4.1-mini': { context_window: 8192 } },
        deepseek: { 'deepseek-reasoner': { context_window: 131072 } },
      },
      squilla_router: { enabled: false },
      llm_ensemble: { enabled: false },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return config
      if (method === 'onboarding.provider.configure') return { changed: true }
      if (method === 'config.patch') return { restartRequired: false }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setFixedProvider('deepseek')
    api.setFixedModel('deepseek-reasoner')
    api.updateLlmTimeout(321)
    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'deepseek',
      model: 'deepseek-reasoner',
    })
    // The active Provider editor still describes OpenAI's persisted model,
    // never the foreign fixed-fallback identity.
    expect(api.providerPanel.value.contextWindowTokens).toBe('8192')

    await api.requestSelectConfiguredProvider('openai')
    api.updateProviderField('model', 'gpt-provider-draft')
    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'openai',
      model: 'gpt-provider-draft',
    })

    api.cancelProviderEdit()

    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'deepseek',
      model: 'deepseek-reasoner',
    })
    expect(api.providerPanel.value.contextWindowTokens).toBe('8192')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    app.unmount()
  })

  it('preserves a settings draft created while the Provider reload is in flight', async () => {
    let configReads = 0
    let releaseReload!: () => void
    const reloadGate = new Promise<void>(resolve => {
      releaseReload = resolve
    })
    const config = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      naming: { enabled: false },
      squilla_router: { enabled: false },
      llm_ensemble: { enabled: false },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        configReads += 1
        if (configReads > 1) await reloadGate
        return config
      }
      if (method === 'onboarding.provider.configure') return { changed: true }
      if (method === 'config.patch') return { restartRequired: false }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateLlmTimeout(321)
    const save = api.saveProvider()
    await vi.waitFor(() => expect(configReads).toBe(2))
    api.setAutoSessionTitles(true)
    releaseReload()
    await expect(save).resolves.toBe(true)

    expect(api.behaviorPanel.value.autoSessionTitles).toBe(true)
    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.providerDraftDirty.value).toBe(false)
    app.unmount()
  })

  it('keeps a verified new provider as a draft until Save changes persists it', async () => {
    let saved = false
    const status = {
      ...statusWithDeepSeek(),
      llmProfileStatus: statusWithDeepSeek().llmProfileStatus.filter(
        profile => profile.provider === 'openai',
      ),
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return saved ? statusWithDeepSeek() : status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles(...(saved ? ['deepseek'] : []))
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'deepseek-chat' }] }
      }
      if (method === 'onboarding.provider.probe') return { ok: true, latencyMs: 18 }
      if (method === 'onboarding.llmProfile.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'deepseek-chat' }] }
      }
      if (method === 'onboarding.llmProfile.upsert') {
        saved = true
        return { changed: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.requestAddProvider('deepseek')
    api.updateProviderField('api_key', 'draft-secret')
    api.updateProviderField('model', 'deepseek-chat')
    api.probeProviderConnection()

    await vi.waitFor(() => expect(api.providerPanel.value.connection.phase).toBe('verified'))
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.upsert')).toBe(false)
    expect(api.providerDraftDirty.value).toBe(true)

    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.upsert', {
      providerId: 'deepseek',
      apiKey: 'draft-secret',
      model: 'deepseek-chat',
      keepCurrentSecret: false,
    })
    app.unmount()

    const reopened = await mountCatalog()
    expect(reopened.api.providerPanel.value.configuredProviders.map(row => row.providerId))
      .toContain('deepseek')
    reopened.app.unmount()
  })

  it('keeps router and ensemble drafts when a new routing provider is saved', async () => {
    let saved = false
    const baseConfig = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      squilla_router: {
        enabled: true,
        tiers: {
          c0: { provider: 'openai', model: 'gpt-4.1-mini' },
          c1: { provider: 'openai', model: 'gpt-4.1-mini' },
        },
      },
      llm_ensemble: {
        enabled: false,
        selection_mode: 'custom_b5',
        candidates: [
          { provider: 'openai', model: 'gpt-4.1-mini' },
          { provider: 'openai', model: 'gpt-4.1' },
        ],
        min_successful_proposers: 1,
      },
    }
    const initialStatus = {
      ...statusWithDeepSeek(),
      llmProfileStatus: statusWithDeepSeek().llmProfileStatus.filter(
        profile => profile.provider === 'openai',
      ),
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return saved ? statusWithDeepSeek() : initialStatus
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return saved
          ? { ...baseConfig, llm_profiles: { deepseek: { model: 'deepseek-chat' } } }
          : { ...baseConfig, llm_profiles: {} }
      }
      if (method === 'onboarding.llmProfile.upsert') {
        saved = true
        return { changed: true }
      }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateTierField('c0', 'model', 'draft-router-model')
    api.setEnsembleMinSuccessful(2)
    api.setFixedModel('draft-fallback-model')
    expect(api.sectionDirty('modelStrategy')).toBe(true)

    await api.requestAddProvider('deepseek')
    api.updateProviderField('api_key', 'test-secret')
    api.updateProviderField('model', 'deepseek-chat')
    await api.saveProvider()

    expect(api.routerPanel.value.tierRows.find(row => row.name === 'c0')?.model)
      .toBe('draft-router-model')
    expect(api.ensemblePanel.value.minSuccessfulProposers).toBe(2)
    expect(api.modelStrategyPanel.value.single.model).toBe('draft-fallback-model')
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    expect(api.providerDraftDirty.value).toBe(false)
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId))
      .toContain('deepseek')
    expect(api.providerPanel.value.providerSelected).toBe('deepseek')
    expect(api.providerPanel.value.selectedStoredProfile).toBe(true)
    expect(api.providerPanel.value.editingPrimary).toBe(false)
    app.unmount()
  })

  it.each([
    {
      name: 'blocked to ready',
      before: {
        runtimeStatus: 'blocked',
        configurationReady: false,
        blockedReason: 'missing_credentials:deepseek',
      },
      after: {
        runtimeStatus: 'ready',
        configurationReady: true,
        blockedReason: '',
      },
    },
    {
      name: 'ready to blocked',
      before: {
        runtimeStatus: 'ready',
        configurationReady: true,
        blockedReason: '',
      },
      after: {
        runtimeStatus: 'blocked',
        configurationReady: false,
        blockedReason: 'missing_credentials:deepseek',
      },
    },
  ])('refreshes C3 runtime readiness after a stored profile save: $name', async ({ before, after }) => {
    let saved = false
    const config = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_profiles: { deepseek: { model: 'deepseek-chat' } },
      squilla_router: {
        enabled: true,
        tiers: {
          c3: {
            provider: 'deepseek',
            model: 'deepseek-chat',
            ensemble_enabled: true,
          },
        },
      },
      llm_ensemble: { enabled: false, selection_mode: 'router_dynamic' },
    }
    const runtimeStatus = (state: typeof before) => ({
      ...statusWithDeepSeek(),
      sectionDetails: {
        router: {
          routerBinding: 'custom',
          routerProviderRoles: { c3: 'dynamic_member' },
          tierEnsembleStatuses: {
            c3: {
              selectionMode: 'router_dynamic',
              activationTiers: ['c3'],
              tierSelectionModes: { c3: 'router_dynamic' },
              runtimeStatus: state.runtimeStatus,
              configurationReady: state.configurationReady,
              blockedReason: state.blockedReason,
              blockedTierCandidates: [],
              fixedFallbackReady: true,
              fixedFallbackBlockedReason: '',
            },
          },
        },
      },
    })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return runtimeStatus(saved ? after : before)
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return config
      if (method === 'onboarding.llmProfile.upsert') {
        saved = true
        return { changed: true }
      }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toMatchObject(before)
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(true)
    expect(api.sectionDirty('modelStrategy')).toBe(false)

    await api.requestSelectConfiguredProvider('deepseek')
    api.updateProviderField('api_key', 'replacement-secret')
    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.modelStrategyPanel.value.router.tierEnsembleStatus).toMatchObject(after)
    expect(api.modelStrategyPanel.value.router.tierEnsembleStatusFresh).toBe(true)
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.providerPanel.value.providerSelected).toBe('deepseek')
    expect(api.providerPanel.value.selectedStoredProfile).toBe(true)
    expect(api.providerPanel.value.editingPrimary).toBe(false)
    app.unmount()
  })

  it('keeps draft Provider roles coherent when only the ensemble mode is dirty', async () => {
    const config = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_profiles: { deepseek: { model: 'deepseek-chat' } },
      squilla_router: {
        enabled: true,
        preset_binding: 'custom',
        tiers: {
          c0: { provider: 'openai', model: 'gpt-4.1-mini' },
          c3: {
            provider: 'deepseek',
            model: 'deepseek-chat',
            ensemble_enabled: true,
          },
        },
      },
      llm_ensemble: { enabled: true, selection_mode: 'static_openrouter_b5' },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          sectionDetails: {
            router: {
              routerBinding: 'custom',
              routerProviderRoles: { c0: 'dormant_draft', c3: 'dormant_draft' },
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return config
      if (method === 'onboarding.llmProfile.upsert') return { changed: true }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setEnsembleSelectionMode('router_dynamic')
    expect(api.routerPanel.value.routerProviderRoles).toEqual({
      c0: 'dynamic_member',
      c3: 'dynamic_member',
    })

    await api.requestSelectConfiguredProvider('deepseek')
    api.updateProviderField('model', 'deepseek-reasoner')
    await expect(api.saveProvider()).resolves.toBe(true)

    expect(api.ensemblePanel.value.selectionMode).toBe('router_dynamic')
    expect(api.routerPanel.value.routerProviderRoles).toEqual({
      c0: 'dynamic_member',
      c3: 'dynamic_member',
    })
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    app.unmount()
  })

  it('replaces the active provider through the legacy configure RPC on an older Gateway', async () => {
    let activeProvider = 'openai'
    let activeModel = 'gpt-4.1-mini'
    supportsMethod.mockImplementation(method => method !== 'onboarding.llmProfile.upsert')
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmCredentialStatus: {
            provider: activeProvider,
            available: true,
            source: 'explicit',
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: activeProvider, model: activeModel },
          llm_profiles: {},
          naming: { enabled: false },
          privacy: { disable_network_observability: false },
          search_provider: 'duckduckgo',
          search_max_results: 10,
          memory: {
            auto_capture_enabled: true,
            embedding: {
              provider: 'auto',
              remote: {
                model: activeProvider === 'deepseek' ? 'server-refreshed-model' : '',
              },
            },
          },
          image_generation: { size: '1024x1024', output_format: 'png' },
          audio: { enabled: false, tts: { voice: '' } },
        }
      }
      if (method === 'onboarding.provider.probe') return { ok: true, latencyMs: 18 }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'deepseek-chat' }] }
      }
      if (method === 'onboarding.provider.configure') {
        activeProvider = String(params?.providerId || '')
        activeModel = String(params?.model || 'deepseek-chat')
        return { changed: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateProviderField('model', 'old-primary-draft')
    api.setAutoSessionTitles(true)
    api.setDisableNetworkObservability(true)
    api.setMemoryAutoCapture(false)
    api.updateCapabilityField('search', 'maxResults', 25)
    api.updateCapabilityField('image', 'size', '512x512')
    api.updateCapabilityField('audio', 'ttsVoice', 'draft-voice')
    await api.requestAddProvider('deepseek')
    api.updateProviderField('api_key', 'draft-secret')
    expect(api.providerPanel.value.profileSaveSupported).toBe(false)
    expect(api.modelStrategyPanel.value.profileSaveSupported).toBe(false)

    await expect(api.saveProvider()).resolves.toBe(false)
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.provider.configure')).toBe(false)

    await api.probeProviderConnection()
    expect(api.providerPanel.value.connection.phase).toBe('verified')

    await expect(api.saveProvider()).resolves.toBe(true)
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.upsert')).toBe(false)
    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', {
      providerId: 'deepseek',
      apiKey: 'draft-secret',
    })
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId))
      .toEqual(['deepseek'])
    expect(api.providerDraftDirty.value).toBe(false)
    expect(api.modelStrategyPanel.value.single).toMatchObject({
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    expect(api.sectionDirty('modelStrategy')).toBe(false)
    expect(api.behaviorPanel.value.autoSessionTitles).toBe(true)
    expect(api.privacyPanel.value).toMatchObject({
      disableNetworkObservability: true,
      memoryAutoCapture: false,
    })
    expect(api.capabilitiesPanel.value.form).toMatchObject({
      searchMaxResults: 25,
      memoryModel: 'server-refreshed-model',
      imageSize: '512x512',
      audioTtsVoice: 'draft-voice',
    })
    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.sectionDirty('privacy')).toBe(true)
    expect(api.sectionDirty('capabilities')).toBe(true)
    app.unmount()
  })

  it('sends an explicit empty model when clearing a saved profile override', async () => {
    const saved = configWithProfiles('deepseek')
    saved.llm_profiles.deepseek = { model: 'deepseek-reasoner' }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return saved
      if (method === 'onboarding.llmProfile.upsert') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    api.updateProviderField('model', '')
    expect(api.providerDraftDirty.value).toBe(true)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.upsert', {
      providerId: 'deepseek',
      model: '',
      keepCurrentSecret: true,
    })
    app.unmount()
  })

  it('saves the registry env default without materializing the inherited model default', async () => {
    const providersWithEnvDefault = providers.map(provider => (
      provider.providerId === 'deepseek'
        ? {
            ...provider,
            fields: (provider.fields || []).map(field => (
              field.name === 'api_key_env'
                ? { ...field, default: 'DEEPSEEK_API_KEY' }
                : field
            )),
          }
        : provider
    ))
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: providersWithEnvDefault }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles()
      if (method === 'onboarding.provider.probe') {
        return {
          ok: false,
          failureKind: 'auth_invalid',
          message: 'Environment variable DEEPSEEK_API_KEY is not visible',
        }
      }
      if (method === 'onboarding.llmProfile.upsert') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.requestAddProvider('deepseek')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      apiKeyEnvValue: 'DEEPSEEK_API_KEY',
      draftCredentialSource: '',
      probeReady: false,
    })
    api.probeProviderConnection()
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.provider.probe')).toBe(false)

    // A registry default is only a suggestion. Explicitly choosing/entering
    // the reference makes the draft probe-eligible so the Gateway can report
    // whether that environment variable is actually visible.
    api.updateProviderField('api_key_env', 'DEEPSEEK_API_KEY')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      draftCredentialSource: 'env',
      probeReady: true,
    })
    const modelField = [
      ...api.providerPanel.value.providerCoreFields,
      ...api.providerPanel.value.providerAdvancedFields,
    ].find(field => field.name === 'model')!
    expect(api.providerPanel.value.providerFieldValue(modelField)).toBe('deepseek-chat')
    api.probeProviderConnection()
    await vi.waitFor(() => expect(api.providerPanel.value.connection).toMatchObject({
      phase: 'key_invalid',
      failureKind: 'auth_invalid',
      detail: 'Environment variable DEEPSEEK_API_KEY is not visible',
    }))
    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.probe', {
      providerId: 'deepseek',
      apiKeyEnv: 'DEEPSEEK_API_KEY',
      model: 'deepseek-chat',
    })
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.upsert', {
      providerId: 'deepseek',
      apiKey: '',
      apiKeyEnv: 'DEEPSEEK_API_KEY',
      keepCurrentSecret: false,
    })
    app.unmount()
  })

  it('tests the current saved-profile editor draft, including unsaved values', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.draft.probe') return { ok: true, latencyMs: 21 }
      if (method === 'onboarding.llmProfile.draft.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'deepseek-chat', name: 'DeepSeek Chat' }] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    api.updateProviderField('api_key', 'draft-secret')
    api.updateProviderField('model', 'draft-deepseek-model')
    api.probeProviderConnection()
    const expectedDraft = {
      providerId: 'deepseek',
      apiKey: 'draft-secret',
      model: 'draft-deepseek-model',
      keepCurrentSecret: false,
    }
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.draft.probe',
      expectedDraft,
    ))
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.draft.models.discover',
      { ...expectedDraft, forceRefresh: true },
    ))
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.probe')).toBe(false)
    expect(api.providerPanel.value.connection.phase).toBe('verified')
    expect(api.providerPanel.value.connection.models[0]?.id).toBe('deepseek-chat')
    app.unmount()
  })

  it('tests a configured-provider row against its saved deployment without changing editor state', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 19 }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.providerSelected).toBe('openai')
    await api.probeConfiguredProvider('deepseek')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.probe', {
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    expect(api.providerPanel.value.providerSelected).toBe('openai')
    expect(api.providerPanel.value.configuredProviderProbes.deepseek).toMatchObject({
      phase: 'verified',
      latencyMs: 19,
    })
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.draft.probe')).toBe(false)
    app.unmount()
  })

  it('tests a saved profile model before catalog or routed defaults', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          llm_profiles: { deepseek: { model: 'deepseek-saved-direct' } },
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-routed-model' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 17 }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.probeConfiguredProvider('deepseek')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.probe', {
      providerId: 'deepseek',
      model: 'deepseek-saved-direct',
    })
    app.unmount()
  })

  it('uses the provider direct default before a routed model for legacy profiles', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-routed-model' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 13 }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.probeConfiguredProvider('deepseek')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.probe', {
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    app.unmount()
  })

  it('invalidates saved-provider probe evidence after a successful reload', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 11 }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.probeConfiguredProvider('deepseek')
    expect(api.providerPanel.value.configuredProviderProbes.deepseek?.phase).toBe('verified')

    await api.loadData()
    expect(api.providerPanel.value.configuredProviderProbes.deepseek).toBeUndefined()
    app.unmount()
  })

  it('discards a saved-provider probe that finishes after a reload', async () => {
    let finishProbe!: (value: { ok: boolean; latencyMs: number }) => void
    const pendingProbe = new Promise<{ ok: boolean; latencyMs: number }>((resolve) => {
      finishProbe = resolve
    })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.probe') return pendingProbe
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    const probe = api.probeConfiguredProvider('deepseek')
    await vi.waitFor(() => expect(
      api.providerPanel.value.configuredProviderProbes.deepseek?.phase,
    ).toBe('probing'))

    await api.loadData()
    expect(api.providerPanel.value.configuredProviderProbes.deepseek).toBeUndefined()
    finishProbe({ ok: true, latencyMs: 9 })
    await probe
    expect(api.providerPanel.value.configuredProviderProbes.deepseek).toBeUndefined()
    app.unmount()
  })

  it('uses stored-profile discovery when the selected secondary provider opens Model Routing', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        return {
          ok: true,
          source: 'live',
          models: [{ id: 'deepseek-chat', name: 'DeepSeek Chat' }],
        }
      }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    api.setSection('modelStrategy')

    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.models.discover',
      { providerId: 'deepseek' },
    ))
    expect(rpcCall).toHaveBeenCalledWith('onboarding.models.discover', {
      providerId: 'openai',
      model: 'gpt-4.1-mini',
    })
    expect(api.routerPanel.value.discoveredModelsByProvider.deepseek?.models[0]?.id)
      .toBe('deepseek-chat')
    app.unmount()
  })

  it('does not disguise profile discovery failures as an old-Gateway fallback', async () => {
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        throw new Error('401 unauthorized profile deployment')
      }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'wrong-deployment' }] }
      }
      throw new Error(`Unexpected RPC method: ${method} ${String(params?.providerId || '')}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.models.discover',
      { providerId: 'deepseek' },
    ))
    await vi.waitFor(() => expect(
      api.routerPanel.value.discoveredModelsByProvider.deepseek,
    ).toEqual({ models: [], source: 'none' }))
    expect(rpcCall.mock.calls.some(([method, params]) => (
      method === 'onboarding.models.discover'
      && (params as Record<string, unknown> | undefined)?.providerId === 'deepseek'
    ))).toBe(false)
    app.unmount()
  })

  it('uses the legacy discovery endpoint only when the profile method is unavailable', async () => {
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        throw new Error('RPC method not found')
      }
      if (method === 'onboarding.models.discover') {
        return {
          ok: true,
          source: 'live',
          models: [{ id: String(params?.providerId) === 'deepseek' ? 'legacy-model' : 'active-model' }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.setSection('modelStrategy')
    await vi.waitFor(() => expect(
      api.routerPanel.value.discoveredModelsByProvider.deepseek?.models[0]?.id,
    ).toBe('legacy-model'))
    expect(rpcCall).toHaveBeenCalledWith('onboarding.models.discover', {
      providerId: 'deepseek',
    })
    app.unmount()
  })

  it('probes a model-less stored profile with a representative routed model', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, customProvider] }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmProfileStatus: [
            ...(statusWithDeepSeek().llmProfileStatus || []),
            {
              provider: 'custom',
              ready: true,
              credentialSource: 'not_required',
              endpointSource: 'profile',
            },
          ],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('custom'),
          squilla_router: {
            enabled: true,
            tiers: { c0: { provider: 'custom', model: 'local-chat-model' } },
          },
        }
      }
      if (method === 'onboarding.llmProfile.probe') return { ok: true, latencyMs: 17 }
      if (method === 'onboarding.llmProfile.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('custom')
    api.probeProviderConnection()

    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.llmProfile.probe',
      { providerId: 'custom', model: 'local-chat-model' },
    ))
    expect(api.providerPanel.value.connection.phase).toBe('verified')
    app.unmount()
  })

  it('blocks a stored profile probe when no representative model exists', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, customProvider] }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmProfileStatus: [
            ...(statusWithDeepSeek().llmProfileStatus || []),
            {
              provider: 'custom',
              ready: true,
              credentialSource: 'not_required',
              endpointSource: 'profile',
            },
          ],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return configWithProfiles('custom')
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('custom')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      probeReady: false,
      probeDisabledReason: 'Complete required fields before verifying: Model.',
    })
    api.probeProviderConnection()

    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.draft.probe')).toBe(false)
    app.unmount()
  })

  it('activates with the provider default and turns off an incompatible custom Router', async () => {
    const status = {
      ...statusWithDeepSeek(),
      sectionDetails: {
        router: { routerMode: 'custom', routerBinding: 'custom' },
      },
      llmProfileStatus: (statusWithDeepSeek().llmProfileStatus || []).map(profile => (
        profile.provider === 'deepseek'
          ? { ...profile, primaryEligible: true, primaryBlockReason: '' }
          : profile
      )),
    }
    const saved = {
      ...configWithProfiles('deepseek'),
      squilla_router: {
        enabled: true,
        preset_binding: 'custom',
        cross_provider_tiers: false,
        tiers: {
          c0: { provider: 'openai', model: 'gpt-4.1-mini' },
          c1: { provider: 'deepseek', model: 'deepseek-chat' },
        },
      },
      llm_ensemble: { enabled: false },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return saved
      if (method === 'onboarding.models.discover') return { ok: true, source: 'none', models: [] }
      if (method === 'onboarding.llmProfile.activate') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.routerEnabled).toBe(true)
    expect(api.providerPanel.value.routerBinding).toBe('custom')
    await api.activateProvider('deepseek')
    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.activate', {
      providerId: 'deepseek',
      routerAction: 'disable',
    })
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.models.discover'))
      .toBe(false)
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('Model Routing was turned off'))
    app.unmount()
  })

  it('serializes activation and ignores provider edits or mutations until reload completes', async () => {
    let resolveActivation!: () => void
    const activationRequest = new Promise<{ changed: boolean }>(resolve => {
      resolveActivation = () => resolve({ changed: true })
    })
    const status = statusWithDeepSeek()
    status.llmProfileStatus = status.llmProfileStatus.map(profile => (
      profile.provider === 'deepseek'
        ? { ...profile, primaryEligible: true, primaryBlockReason: '' }
        : profile
    ))
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.activate') return activationRequest
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    const firstActivation = api.activateProvider('deepseek')
    await vi.waitFor(() => expect(api.providerPanel.value.activation).toMatchObject({
      providerId: 'deepseek',
      phase: 'activating',
    }))

    api.selectConfiguredProvider('deepseek')
    api.updateProviderField('model', 'must-not-be-applied')
    api.updateLlmTimeout(7)
    api.probeProviderConnection()
    const duplicateActivation = api.activateProvider('deepseek')
    await api.removeProviderProfile('deepseek')
    await api.saveProvider()

    expect(api.providerPanel.value.providerSelected).toBe('openai')
    expect(api.providerPanel.value.providerFieldValue({ name: 'model', label: 'Model' }))
      .toBe('gpt-4.1-mini')
    expect(api.providerPanel.value.llmTimeoutSeconds).not.toBe(7)
    expect(rpcCall.mock.calls.filter(call => call[0] === 'onboarding.llmProfile.activate'))
      .toHaveLength(1)
    expect(rpcCall.mock.calls.some(call => [
      'onboarding.provider.configure',
      'onboarding.provider.probe',
      'onboarding.llmProfile.remove',
    ].includes(String(call[0])))).toBe(false)

    resolveActivation()
    await Promise.all([firstActivation, duplicateActivation])
    expect(api.providerPanel.value.activation.phase).toBe('idle')
    app.unmount()
  })

  it('treats a missing router binding as legacy and never guesses follow-primary', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: { enabled: true, tiers: {} },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.routerBinding).toBe('legacy')
    app.unmount()
  })

  it('falls back to the persisted router binding when additive status is absent', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          ...configWithProfiles('deepseek'),
          squilla_router: {
            enabled: true,
            preset_binding: 'custom',
            tiers: { c0: { provider: 'deepseek', model: 'deepseek-chat' } },
          },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: true, source: 'none', models: [] }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.routerBinding).toBe('custom')
    app.unmount()
  })

  it('removes an unused profile and surfaces backend refusal for referenced profiles', async () => {
    let refuse = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.llmProfile.remove') {
        if (refuse) throw new Error('profile is still referenced by router tier c0')
        return { changed: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderProfile('deepseek')
    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.remove', { providerId: 'deepseek' })
    expect(confirmAction).toHaveBeenCalledWith({
      title: 'Remove provider?',
      body: 'Remove DeepSeek from configured providers? Referenced providers cannot be removed.',
      primaryLabel: 'Remove provider',
    })
    expect(pushToast).toHaveBeenCalledWith('DeepSeek was removed.')

    refuse = true
    await api.removeProviderProfile('deepseek')
    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining('profile is still referenced'),
      { tone: 'danger' },
    )
    app.unmount()
  })

  it('warns that image generation switches from a removed profile to environment credentials', async () => {
    let removed = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return statusWithProfileBackedImage(removed ? 'env' : 'llm_fallback')
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return removed ? configWithProfiles() : configWithProfiles('deepseek')
      }
      if (method === 'onboarding.llmProfile.remove') {
        removed = true
        return { changed: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderProfile('deepseek')

    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining(
        'Image generation is currently reusing this provider\'s credential.',
      ),
    }))
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      'DeepSeek was removed from Model Service. Image generation remains available through DEEPSEEK_API_KEY.',
      { tone: 'warn' },
    )
    app.unmount()
  })

  it('warns when removing a profile leaves the retained image route without credentials', async () => {
    let removed = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return statusWithProfileBackedImage(removed ? 'none' : 'llm_fallback')
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return removed ? configWithProfiles() : configWithProfiles('deepseek')
      }
      if (method === 'onboarding.llmProfile.remove') {
        removed = true
        return { changed: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderProfile('deepseek')

    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining(
        'Image generation is currently reusing this provider\'s credential.',
      ),
    }))
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast).toHaveBeenCalledWith(
      'DeepSeek was removed. Image generation kept its provider and model, but now needs a credential.',
      { tone: 'warn' },
    )
    app.unmount()
  })

  it('removes the active provider with one atomic RPC and never chains activate then remove', async () => {
    const status = {
      ...statusWithDeepSeek(),
      llmProfileStatus: statusWithDeepSeek().llmProfileStatus.map(profile => (
        profile.provider === 'deepseek'
          ? { ...profile, primaryEligible: true, primaryBlockReason: '' }
          : profile
      )),
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.active.remove') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderProfile('openai')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.active.remove', {
      providerId: 'openai',
      replacementProviderId: 'deepseek',
    })
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.activate'))
      .toBe(false)
    expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.llmProfile.remove'))
      .toBe(false)
    app.unmount()
  })

  it('requests the OpenRouter image default when active removal promotes that profile', async () => {
    const openrouter = {
      providerId: 'openrouter',
      label: 'OpenRouter',
      runtimeSupported: true,
      requiresApiKey: true,
      defaultDirectModel: 'openai/gpt-4.1-mini',
      fields: [{ name: 'model', label: 'Model', required: true }],
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [...providers, openrouter] }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmCredentialStatus: {
            provider: 'openai',
            available: true,
            source: 'explicit',
          },
          llmProfileStatus: [
            {
              provider: 'openai',
              ready: true,
              credentialSource: 'explicit',
              primaryEligible: false,
              primaryBlockReason: 'already_active',
            },
            {
              provider: 'openrouter',
              ready: true,
              credentialSource: 'profile',
              primaryEligible: true,
              primaryBlockReason: '',
            },
          ],
          imageGenerationState: { mode: 'unconfigured' },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openai', model: 'gpt-4.1-mini' },
          llm_profiles: {
            openrouter: {
              model: 'openai/gpt-4.1-mini',
              base_url: 'https://openrouter.ai/api/v1',
            },
          },
        }
      }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.active.remove') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderProfile('openai')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.active.remove', {
      providerId: 'openai',
      replacementProviderId: 'openrouter',
      imageGenerationIntent: 'enable_provider_default',
    })
    app.unmount()
  })

  it('does not offer a two-RPC fallback when active removal is unsupported', async () => {
    supportsMethod.mockImplementation(method => (
      method !== 'onboarding.llmProfile.active.remove'
    ))
    const status = {
      ...statusWithDeepSeek(),
      llmProfileStatus: statusWithDeepSeek().llmProfileStatus.map(profile => (
        profile.provider === 'deepseek'
          ? { ...profile, primaryEligible: true, primaryBlockReason: '' }
          : profile
      )),
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return status
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.primaryProviderRemovalSupported).toBe(false)
    await api.removeProviderProfile('openai')

    expect(confirmAction).not.toHaveBeenCalled()
    expect(rpcCall.mock.calls.some(call => [
      'onboarding.llmProfile.active.remove',
      'onboarding.llmProfile.activate',
      'onboarding.llmProfile.remove',
    ].includes(String(call[0])))).toBe(false)
    app.unmount()
  })

  it('clears the active provider credential without removing its deployment settings', async () => {
    let credentialCleared = false
    const savedConfig = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini', base_url: 'https://example.invalid/v1' },
      squilla_router: {
        enabled: true,
        tiers: { c0: { provider: 'openai', model: 'gpt-4.1-mini' } },
      },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: !credentialCleared,
          llmSource: credentialCleared ? 'none' : 'explicit',
          llmCredentialStatus: {
            provider: 'openai',
            available: !credentialCleared,
            source: credentialCleared ? 'none' : 'explicit',
            masked: credentialCleared ? '' : 'sk-•••1234',
            revealAllowed: !credentialCleared,
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return savedConfig
      if (method === 'config.effective') {
        return { fields: { 'llm.provider': { value: 'openai', source: 'config' } } }
      }
      if (method === 'onboarding.models.discover') return { ok: true, source: 'none', models: [] }
      if (method === 'onboarding.provider.credential.clear') {
        credentialCleared = true
        return { credentialCleared: true, provider: 'openai', active: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.credentialPanel?.source).toBe('explicit')
    await api.removeProviderCredential()

    expect(confirmAction).toHaveBeenCalledOnce()
    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.credential.clear', {
      providerId: 'openai',
    })
    expect(api.providerPanel.value.providerSelected).toBe('openai')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'none',
      available: false,
      removable: false,
      probeReady: false,
    })
    expect(api.providerPanel.value.providerFieldValue({ name: 'model', label: 'Model' }))
      .toBe('gpt-4.1-mini')
    expect(api.routerPanel.value.routerMode).not.toBe('disabled')
    expect(api.routerPanel.value.tierRows.find(row => row.name === 'c0')).toMatchObject({
      provider: 'openai',
      model: 'gpt-4.1-mini',
    })
    app.unmount()
  })

  it('warns when a system environment variable remains active after config credential removal', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'env',
          llmCredentialStatus: {
            provider: 'openai',
            available: true,
            source: 'env',
            envKey: 'OPENAI_API_KEY',
            masked: 'sk-•••1234',
            revealAllowed: false,
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'openai', model: 'gpt-4.1-mini' } }
      }
      if (method === 'config.effective') return { fields: {} }
      if (method === 'onboarding.models.discover') return { ok: true, source: 'none', models: [] }
      if (method === 'onboarding.provider.credential.clear') {
        return {
          entry: {
            externalCredentialActive: true,
            credentialEnv: 'OPENAI_API_KEY',
          },
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    await api.removeProviderCredential()

    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining('OPENAI_API_KEY'),
      { tone: 'warn' },
    )
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'env',
      available: true,
      removable: false,
    })
    app.unmount()
  })

  it('allows clearing a saved profile env reference when the environment variable is missing', async () => {
    let credentialCleared = false
    const savedConfig = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_profiles: {
        deepseek: {
          model: 'deepseek-chat',
          api_key_env: 'DEEPSEEK_MISSING_KEY',
        },
      },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmProfileStatus: [
            {
              provider: 'deepseek',
              ready: false,
              credentialSource: credentialCleared ? 'none' : 'profile_env',
              credentialEnv: credentialCleared ? '' : 'DEEPSEEK_MISSING_KEY',
              endpointSource: 'registry',
              reason: 'missing_credentials',
            },
          ],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return savedConfig
      if (method === 'config.effective') return { fields: {} }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      if (method === 'onboarding.llmProfile.credential.clear') {
        credentialCleared = true
        return { credentialCleared: true, provider: 'deepseek', active: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'missing_env',
      available: false,
      removable: true,
    })

    await api.removeProviderCredential()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.credential.clear', {
      providerId: 'deepseek',
    })
    app.unmount()
  })

  it('clears a routing profile credential and keeps that provider selected', async () => {
    let credentialCleared = false
    const savedConfig = {
      llm: { provider: 'openai', model: 'gpt-4.1-mini' },
      llm_profiles: {
        deepseek: { model: 'deepseek-chat' },
      },
    }
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          ...statusWithDeepSeek(),
          llmProfileStatus: [
            {
              provider: 'deepseek',
              ready: !credentialCleared,
              credentialSource: credentialCleared ? 'none' : 'profile_env',
              credentialEnv: credentialCleared ? '' : 'DEEPSEEK_API_KEY',
              endpointSource: 'registry',
              reason: credentialCleared ? 'missing_credentials' : '',
            },
          ],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return savedConfig
      if (method === 'config.effective') return { fields: {} }
      if (
        method === 'onboarding.models.discover'
        || method === 'onboarding.llmProfile.models.discover'
      ) return { ok: true, source: 'none', models: [] }
      if (method === 'onboarding.llmProfile.credential.clear') {
        credentialCleared = true
        return { credentialCleared: true, provider: 'deepseek', active: false }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('deepseek')
    expect(api.providerPanel.value.credentialPanel?.source).toBe('env')
    await api.removeProviderCredential()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.credential.clear', {
      providerId: 'deepseek',
    })
    expect(api.providerPanel.value.providerSelected).toBe('deepseek')
    expect(api.providerPanel.value.selectedStoredProfile).toBe(true)
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'none',
      available: false,
      removable: false,
    })
    expect(api.providerPanel.value.providerFieldValue({ name: 'model', label: 'Model' }))
      .toBe('deepseek-chat')
    expect(api.providerPanel.value.configuredProviders.map(row => row.providerId))
      .toEqual(['openai', 'deepseek'])
    app.unmount()
  })

  it('refreshes credential status without discarding unrelated settings drafts', async () => {
    let credentialCleared = false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: !credentialCleared,
          llmSource: credentialCleared ? 'none' : 'explicit',
          llmCredentialStatus: {
            provider: 'openai',
            available: !credentialCleared,
            source: credentialCleared ? 'none' : 'explicit',
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openai', model: 'gpt-4.1-mini' },
          naming: { enabled: false },
          squilla_router: { enabled: false },
          llm_ensemble: { enabled: false },
        }
      }
      if (method === 'config.effective') {
        return { fields: { 'llm.provider': { value: 'openai', source: 'config' } } }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.provider.credential.clear') {
        credentialCleared = true
        return { credentialCleared: true, provider: 'openai', active: true }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.updateLlmTimeout(321)
    api.setAutoSessionTitles(true)
    api.setFixedModel('gpt-4.1')
    await api.removeProviderCredential()

    expect(confirmAction).toHaveBeenCalledOnce()
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      source: 'none',
      removing: false,
    })
    expect(api.providerPanel.value.credentialRemovalPending).toBe(false)
    expect(api.providerPanel.value.llmTimeoutSeconds).toBe(321)
    expect(api.modelStrategyPanel.value.single.model).toBe('gpt-4.1')
    expect(api.providerDraftDirty.value).toBe(true)
    expect(api.sectionDirty('behavior')).toBe(true)
    expect(api.sectionDirty('modelStrategy')).toBe(true)
    app.unmount()
  })

  it('uses the canonical fixed model for configured-primary probes and invalidates stale verdicts', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') return statusWithDeepSeek()
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return configWithProfiles('deepseek')
      if (method === 'config.effective') {
        return { fields: { 'llm.provider': { value: 'openai', source: 'config' } } }
      }
      if (method === 'onboarding.provider.probe') return { ok: true }
      if (method === 'onboarding.models.discover') {
        return { ok: true, source: 'live', models: [{ id: 'gpt-4.1' }] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.probeProviderConnection()
    await vi.waitFor(() => expect(api.providerPanel.value.connection.phase).toBe('verified'))

    api.setFixedModel('gpt-4.1')
    expect(api.providerPanel.value.connection.phase).toBe('unverified')
    api.probeProviderConnection()

    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.provider.probe',
      expect.objectContaining({ providerId: 'openai', model: 'gpt-4.1' }),
    ))
    const probeCalls = rpcCall.mock.calls.filter(call => call[0] === 'onboarding.provider.probe')
    expect(probeCalls[probeCalls.length - 1]?.[1]).toMatchObject({
      providerId: 'openai',
      model: 'gpt-4.1',
    })
    app.unmount()
  })
})

describe('useSetupCatalog provider credential reveal', () => {
  it('reveals the saved provider key through the dedicated RPC', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId: 'deepseek',
              label: 'DeepSeek',
              runtimeSupported: true,
              requiresApiKey: true,
              envKey: 'DEEPSEEK_API_KEY',
              fields: [{ name: 'model', label: 'Model' }],
            },
          ],
        }
      }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'explicit',
          llmCredentialStatus: {
            provider: 'deepseek',
            available: true,
            source: 'explicit',
            envKey: 'DEEPSEEK_API_KEY',
            masked: 'sk-••••1234',
            revealAllowed: true,
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'deepseek', model: 'deepseek-chat' } }
      if (method === 'onboarding.provider.credential.reveal') return { ok: true, apiKey: 'sk-real-value' }
      throw new Error(`Unexpected RPC method: ${method}`)
    })

    const { api, app } = await mountCatalog()

    vi.useFakeTimers()
    await api.revealProviderCredential()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.credential.reveal', { providerId: 'deepseek' })
    const credentialPanel = api.providerPanel.value.credentialPanel as { masked: string; revealed: string }
    expect(credentialPanel.revealed).toBe('sk-real-value')
    const callsAfterReveal = rpcCall.mock.calls.length

    api.providerPanel.value.credentialPanel?.onHideReveal?.()
    await nextTick()

    const hiddenCredentialPanel = api.providerPanel.value.credentialPanel as { masked: string; revealed: string }
    expect(hiddenCredentialPanel.masked).toBe('sk-••••1234')
    expect(hiddenCredentialPanel.revealed).toBe('')
    expect(rpcCall).toHaveBeenCalledTimes(callsAfterReveal)

    await api.revealProviderCredential()

    vi.advanceTimersByTime(PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS)
    await nextTick()

    const expiredCredentialPanel = api.providerPanel.value.credentialPanel as { masked: string; revealed: string }
    expect(expiredCredentialPanel.masked).toBe('sk-••••1234')
    expect(expiredCredentialPanel.revealed).toBe('')
    app.unmount()
  })
})

describe('useSetupCatalog optional provider credentials', () => {
  function mockSavedProviderForSave(options: {
    providerId?: string
    acceptsApiKey?: boolean
    requiresApiKey?: boolean
    additionalProviderId?: string
    baseUrl?: string
  } = {}) {
    const providerId = options.providerId || 'custom'
    const acceptsApiKey = options.acceptsApiKey ?? true
    const requiresApiKey = options.requiresApiKey ?? false
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId,
              label: providerId === 'custom' ? 'Custom OpenAI-compatible endpoint' : 'Test provider',
              runtimeSupported: true,
              acceptsApiKey,
              requiresApiKey,
              envKey: `${providerId.toUpperCase()}_API_KEY`,
              fields: [
                { name: 'model', label: 'Model', required: true },
                { name: 'api_key', label: 'API key', secret: true },
                { name: 'api_key_env', label: 'API key environment variable' },
                ...(options.baseUrl
                  ? [{ name: 'base_url', label: 'Base URL', required: true }]
                  : []),
              ],
            },
            ...(options.additionalProviderId
              ? [{
                  providerId: options.additionalProviderId,
                  label: 'Other optional provider',
                  runtimeSupported: true,
                  acceptsApiKey: true,
                  requiresApiKey: false,
                  envKey: `${options.additionalProviderId.toUpperCase()}_API_KEY`,
                  fields: [{ name: 'model', label: 'Model', required: true }],
                }]
              : []),
          ],
        }
      }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'explicit',
          llmCredentialStatus: {
            provider: providerId,
            available: true,
            source: 'explicit',
            envKey: `${providerId.toUpperCase()}_API_KEY`,
            masked: 'sk-••••1234',
            revealAllowed: true,
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: {
            provider: providerId,
            model: 'test-model',
            ...(options.baseUrl ? { base_url: options.baseUrl } : {}),
          },
        }
      }
      if (method === 'onboarding.provider.configure') return {}
      if (method === 'onboarding.llmProfile.upsert') return {}
      throw new Error(`Unexpected RPC method: ${method}`)
    })
  }

  it.each([
    ['custom', 'Custom OpenAI-compatible endpoint', 'CUSTOM_LLM_API_KEY'],
    ['custom_anthropic', 'Custom Anthropic-compatible endpoint', 'CUSTOM_ANTHROPIC_API_KEY'],
  ])(
    'exposes an optional key and blocks %s probes until required fields exist',
    async (providerId, label, envKey) => {
      rpcCall.mockImplementation(async (method: string) => {
        if (method === 'onboarding.catalog') {
          return {
            providers: [
              {
                providerId,
                label,
                runtimeSupported: true,
                acceptsApiKey: true,
                requiresApiKey: false,
                envKey,
                fields: [
                  { name: 'model', label: 'Model id', required: true, default: '' },
                  { name: 'api_key', label: 'API key', required: false, secret: true },
                  { name: 'base_url', label: 'Base URL', required: true, default: '' },
                ],
              },
            ],
          }
        }
        if (method === 'onboarding.status') {
          return { hasConfig: false, llmConfigured: false, llmCredentialStatus: {} }
        }
        if (method === 'channels.status') return { channels: [] }
        if (method === 'config.get') return {}
        if (method === 'onboarding.provider.probe') {
          return { ok: false, failureKind: 'transport_transient', message: 'offline' }
        }
        throw new Error(`Unexpected RPC method: ${method}`)
      })
      const { api, app } = await mountCatalog()

      api.selectProvider(providerId)
      api.onProviderChange()

      let credential = api.providerPanel.value.credentialPanel
      expect(credential).toMatchObject({
        acceptsApiKey: true,
        requiresApiKey: false,
        source: 'not_required',
        probeReady: false,
      })
      expect(credential?.probeDisabledReason).toBe(
        'Complete required fields before verifying: Model, Base URL.',
      )

      api.probeProviderConnection()
      expect(rpcCall.mock.calls.some(call => call[0] === 'onboarding.provider.probe')).toBe(false)

      api.updateProviderField('model', 'test-model')
      api.updateProviderField('base_url', 'https://custom.example.test/v1')
      credential = api.providerPanel.value.credentialPanel
      expect(credential?.probeReady).toBe(true)
      expect(credential?.probeDisabledReason).toBe('')

      api.probeProviderConnection()
      await Promise.resolve()
      expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.probe', {
        providerId,
        baseUrl: 'https://custom.example.test/v1',
        model: 'test-model',
      })
      app.unmount()
    },
  )

  it('falls back conservatively to requiresApiKey when an older gateway omits acceptsApiKey', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId: 'custom',
              label: 'Custom endpoint',
              runtimeSupported: true,
              requiresApiKey: false,
              fields: [{ name: 'model', label: 'Model' }],
            },
            {
              providerId: 'openrouter',
              label: 'OpenRouter',
              runtimeSupported: true,
              requiresApiKey: true,
              fields: [{ name: 'model', label: 'Model' }],
            },
          ],
        }
      }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmSource: 'not_required',
          llmCredentialStatus: {
            provider: 'custom',
            available: true,
            source: 'not_required',
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'custom', model: 'test-model' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      acceptsApiKey: false,
      requiresApiKey: false,
    })

    api.selectProvider('openrouter')
    api.onProviderChange()
    expect(api.providerPanel.value.credentialPanel).toMatchObject({
      acceptsApiKey: true,
      requiresApiKey: true,
    })
    app.unmount()
  })

  it('sends preserveApiKey only for the active saved explicit optional credential', async () => {
    mockSavedProviderForSave()
    const { api, app } = await mountCatalog()

    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', {
      providerId: 'custom',
      model: 'test-model',
      preserveApiKey: true,
    })
    app.unmount()
  })

  it('reuses the same preservation intent when applying a provider preset', async () => {
    mockSavedProviderForSave()
    const { api, app } = await mountCatalog()

    await api.applyProviderPreset()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', {
      providerId: 'custom',
      model: 'test-model',
      preserveApiKey: true,
      presetId: 'custom',
    })
    app.unmount()
  })

  it('preserves an optional key for a same-origin endpoint path change', async () => {
    mockSavedProviderForSave({ baseUrl: 'https://llm.example.test/v1' })
    const { api, app } = await mountCatalog()

    api.updateProviderField('base_url', 'https://llm.example.test/openai/v1')
    await api.saveProvider()

    const configureCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.provider.configure')
    expect(configureCall?.[1]).toMatchObject({
      baseUrl: 'https://llm.example.test/openai/v1',
      preserveApiKey: true,
    })
    app.unmount()
  })

  it('does not preserve an optional key across endpoint origins', async () => {
    mockSavedProviderForSave({ baseUrl: 'https://llm-a.example.test/v1' })
    const { api, app } = await mountCatalog()

    api.updateProviderField('base_url', 'https://llm-b.example.test/v1')
    await api.saveProvider()

    const configureCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.provider.configure')
    const payload = configureCall?.[1] as Record<string, unknown>
    expect(payload).toMatchObject({ baseUrl: 'https://llm-b.example.test/v1' })
    expect(payload).not.toHaveProperty('preserveApiKey')
    app.unmount()
  })

  it.each([
    ['api_key', 'sk-replacement', 'apiKey'],
    ['api_key_env', 'CUSTOM_REPLACEMENT_KEY', 'apiKeyEnv'],
  ])('does not send preserveApiKey with an explicit %s replacement', async (field, value, wireField) => {
    mockSavedProviderForSave()
    const { api, app } = await mountCatalog()

    api.updateProviderField(field, value)
    await api.saveProvider()

    const configureCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.provider.configure')
    const payload = configureCall?.[1] as Record<string, unknown>
    expect(payload).toMatchObject({ providerId: 'custom', model: 'test-model', [wireField]: value })
    expect(payload).not.toHaveProperty('preserveApiKey')
    app.unmount()
  })

  it.each(['api_key', 'api_key_env'])(
    'treats whitespace-only %s input as unchanged when preserving an optional key',
    async field => {
      mockSavedProviderForSave()
      const { api, app } = await mountCatalog()

      api.updateProviderField(field, '   ')
      await api.saveProvider()

      const configureCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.provider.configure')
      expect(configureCall?.[1]).toEqual({
        providerId: 'custom',
        model: 'test-model',
        preserveApiKey: true,
      })
      app.unmount()
    },
  )

  it('does not send preserveApiKey for a provider whose key is required', async () => {
    mockSavedProviderForSave({ providerId: 'deepseek', requiresApiKey: true })
    const { api, app } = await mountCatalog()

    await api.saveProvider()

    const configureCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.provider.configure')
    const payload = configureCall?.[1] as Record<string, unknown>
    expect(payload).toMatchObject({ providerId: 'deepseek', model: 'test-model' })
    expect(payload).not.toHaveProperty('preserveApiKey')
    app.unmount()
  })

  it('adds a different provider as a persistent routing profile without carrying the primary secret', async () => {
    mockSavedProviderForSave({ additionalProviderId: 'custom-alt' })
    const { api, app } = await mountCatalog()

    api.selectProvider('custom-alt')
    api.onProviderChange()
    api.updateProviderField('model', 'other-model')
    await api.saveProvider()

    const upsertCall = rpcCall.mock.calls.find(call => call[0] === 'onboarding.llmProfile.upsert')
    const payload = upsertCall?.[1] as Record<string, unknown>
    expect(payload).toMatchObject({ providerId: 'custom-alt', keepCurrentSecret: false })
    expect(payload).toHaveProperty('model', 'other-model')
    expect(payload).not.toHaveProperty('preserveApiKey')
    app.unmount()
  })
})

describe('useSetupCatalog context-window override', () => {
  const CATALOG = {
    providers: [
      {
        providerId: 'ollama',
        label: 'Ollama',
        runtimeSupported: true,
        requiresApiKey: false,
        deployment: 'local',
        fields: [{ name: 'model', label: 'Model' }],
      },
      {
        providerId: 'vllm',
        label: 'vLLM',
        runtimeSupported: true,
        requiresApiKey: false,
        deployment: 'local',
        fields: [{ name: 'model', label: 'Model' }],
      },
    ],
  }

  function mockCatalog(config: Record<string, unknown>) {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return CATALOG
      if (method === 'onboarding.status') {
        return { hasConfig: true, llmConfigured: true, llmSource: 'explicit' }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return config
      if (method === 'onboarding.provider.configure') return {}
      if (method === 'config.patch') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
  }

  it('reseeds the context-window field from the saved override when the provider switches', async () => {
    mockCatalog({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: {
        ollama: { 'qwen3:8b': { context_window: 16384 } },
        vllm: { 'meta/llama-4': { context_window: 65536 } },
      },
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.contextWindowTokens).toBe('16384')

    // Switch provider (select + change, mirroring the panel's @change handler).
    api.selectProvider('vllm')
    api.onProviderChange()

    // resetForProvider clears the model field, so the new provider has no saved
    // override for an empty model → field reseeds to blank, not the stale 16384.
    expect(api.providerPanel.value.contextWindowTokens).toBe('')
    app.unmount()
  })

  it('reseeds from the per-model override when the model field changes', async () => {
    mockCatalog({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: {
        ollama: {
          'qwen3:8b': { context_window: 16384 },
          'qwen3:32b': { context_window: 40960 },
        },
      },
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.contextWindowTokens).toBe('16384')

    api.updateProviderField('model', 'qwen3:32b')
    expect(api.providerPanel.value.contextWindowTokens).toBe('40960')

    api.updateProviderField('model', 'qwen3:unlisted')
    expect(api.providerPanel.value.contextWindowTokens).toBe('')
    app.unmount()
  })

  it('saves the context-window patch under the currently-selected provider and form model', async () => {
    mockCatalog({ llm: { provider: 'ollama', model: 'qwen3:8b' } })
    const { api, app } = await mountCatalog()

    api.updateContextWindow('32768')
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('config.patch', {
      patch: { models: { ollama: { 'qwen3:8b': { context_window: 32768 } } } },
    })
    app.unmount()
  })

  it('skips the context-window patch when the form model is empty', async () => {
    mockCatalog({ llm: { provider: 'ollama', model: '' } })
    const { api, app } = await mountCatalog()

    api.updateContextWindow('32768')
    await api.saveProvider()

    const deepPatchCalls = rpcCall.mock.calls.filter(
      (call: unknown[]) => call[0] === 'config.patch' && 'patch' in ((call[1] as Record<string, unknown>) || {}),
    )
    expect(deepPatchCalls).toHaveLength(0)
    app.unmount()
  })
})

describe('useSetupCatalog providerIsLocal', () => {
  function mockLocalCatalog(providerId: string, deployment: string) {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [
            {
              providerId,
              label: providerId,
              runtimeSupported: true,
              requiresApiKey: false,
              deployment,
              fields: [{ name: 'model', label: 'Model' }],
            },
          ],
        }
      }
      if (method === 'onboarding.status') return { hasConfig: true, llmConfigured: true, llmSource: 'explicit' }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: providerId, model: 'm' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
  }

  it('treats a custom-deployment provider as local (mirrors backend LOCAL_RUNTIME_PROVIDERS)', async () => {
    // 'custom' is budgeted at the 8192 local default backend-side, so the panel's
    // small-window warning must fire — a non-'local' deployment tag must not
    // suppress the known-local-id match.
    mockLocalCatalog('custom', 'custom')
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.providerIsLocal).toBe(true)
    app.unmount()
  })

  it('treats a hosted provider as non-local', async () => {
    mockLocalCatalog('openai', 'hosted')
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.providerIsLocal).toBe(false)
    app.unmount()
  })
})

describe('useSetupCatalog image status localization', () => {
  const missingEnvText = (name: string) => (
    `Missing environment variable ${name}. Paste a key below, or set that variable and restart the gateway.`
  )

  it('localizes every known backend missing-env detail shape', () => {
    // onboarding/status.py: _with_provider(provider, _source_detail('missing_env', key))
    expect(localizeImageActionableDetail('openai (env key not visible: OPENAI_API_KEY)'))
      .toBe(missingEnvText('OPENAI_API_KEY'))
    // onboarding/status.py: bare _source_detail shape (no provider wrap)
    expect(localizeImageActionableDetail('env key not visible: OPENROUTER_API_KEY'))
      .toBe(missingEnvText('OPENROUTER_API_KEY'))
    // onboarding/next_steps.py: _missing_env_warning
    expect(localizeImageActionableDetail(
      'Image generation provider: $GEMINI_API_KEY is not set in this shell. '
      + 'The config saved the environment-variable reference, but this feature '
      + 'will not work until the gateway is started with that variable set.',
    )).toBe(missingEnvText('GEMINI_API_KEY'))
    expect(localizeImageActionableDetail('missing environment variable IMAGE_API_KEY'))
      .toBe(missingEnvText('IMAGE_API_KEY'))
  })

  it('passes unrecognised details through unchanged so no information is lost', () => {
    expect(localizeImageActionableDetail('openai (unknown image provider)'))
      .toBe('openai (unknown image provider)')
    expect(localizeImageActionableDetail('invalid image provider/model reference'))
      .toBe('invalid image provider/model reference')
    expect(localizeImageActionableDetail(
      'openai (invalid image endpoint; use an absolute http:// or https:// URL)',
    )).toBe('openai (invalid image endpoint; use an absolute http:// or https:// URL)')
  })

  it('renders a blocking missing-env detail localized through the capabilities panel', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          imageGenerationEnabled: true,
          sectionDetails: {
            image_generation: {
              status: 'missing',
              blocking: true,
              label: 'Image generation',
              detail: 'openai (env key not visible: OPENAI_API_KEY)',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.imageStatusText)
      .toBe(missingEnvText('OPENAI_API_KEY'))
    app.unmount()
  })

  it('renders an unrecognised action-required detail verbatim through the capabilities panel', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return {}
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          imageGenerationEnabled: true,
          sectionDetails: {
            image_generation: {
              status: 'degraded',
              actionRequired: true,
              label: 'Image generation',
              detail: 'openai (endpoint/provider mismatch: configured openrouter official endpoint)',
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return { llm: { provider: 'openrouter', model: 'openrouter/auto' } }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.state.imageStatusText)
      .toBe('openai (endpoint/provider mismatch: configured openrouter official endpoint)')
    app.unmount()
  })
})

describe('useSetupCatalog image-generation onboarding intent', () => {
  const openRouterProvider = {
    providerId: 'openrouter',
    label: 'OpenRouter',
    runtimeSupported: true,
    requiresApiKey: true,
    defaultBaseUrl: 'https://openrouter.ai/api/v1',
    fields: [{
      name: 'model',
      label: 'Model',
      required: true,
      default: 'openai/gpt-4.1-mini',
    }, {
      name: 'base_url',
      label: 'Base URL',
      required: true,
      default: 'https://openrouter.ai/api/v1',
    }],
  }

  function mockFreshOpenRouter(mode: 'unconfigured' | 'custom' | 'disabled') {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [openRouterProvider] }
      if (method === 'onboarding.status') {
        return {
          hasConfig: false,
          llmConfigured: false,
          imageGenerationState: { mode },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return {}
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.provider.configure') return { changed: true }
      if (method === 'config.patch') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
  }

  it('omits the additive intent when an older Gateway has no image state contract', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [openRouterProvider] }
      if (method === 'onboarding.status') return { hasConfig: false, llmConfigured: false }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') return {}
      if (method === 'onboarding.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      if (method === 'onboarding.provider.configure') return { changed: true }
      if (method === 'config.patch') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectProvider('openrouter')
    api.onProviderChange()
    expect(api.providerPanel.value.imageGenerationOffer).toBe(false)
    await api.saveProvider()

    const configureCall = rpcCall.mock.calls.find(
      call => call[0] === 'onboarding.provider.configure',
    )
    expect(configureCall?.[1]).not.toHaveProperty('imageGenerationIntent')
    app.unmount()
  })

  it('defaults OpenRouter image setup on and sends the atomic configure intent', async () => {
    mockFreshOpenRouter('unconfigured')
    const { api, app } = await mountCatalog()

    api.selectProvider('openrouter')
    api.onProviderChange()

    expect(api.providerPanel.value).toMatchObject({
      imageGenerationOffer: true,
      imageGenerationOptIn: true,
    })
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      providerId: 'openrouter',
      imageGenerationIntent: 'enable_provider_default',
    }))
    app.unmount()
  })

  it('lets the user preserve image setup from the default OpenRouter offer', async () => {
    mockFreshOpenRouter('unconfigured')
    const { api, app } = await mountCatalog()

    api.selectProvider('openrouter')
    api.onProviderChange()
    api.setProviderImageGenerationOptIn(false)
    expect(api.providerPanel.value.imageGenerationOptIn).toBe(false)

    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      imageGenerationIntent: 'preserve',
    }))
    app.unmount()
  })

  it('lets an existing OpenRouter primary save only the default image intent', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers: [openRouterProvider] }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmCredentialStatus: {
            provider: 'openrouter',
            available: true,
            source: 'explicit',
          },
          imageGenerationState: { mode: 'unconfigured' },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: {
            provider: 'openrouter',
            model: 'openai/gpt-4.1-mini',
            base_url: 'https://openrouter.ai/api/v1',
          },
        }
      }
      if (method === 'onboarding.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      if (method === 'onboarding.provider.configure') return { changed: true }
      if (method === 'config.patch') return { restartRequired: false }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.providerPanel.value.imageGenerationOffer).toBe(true)
    expect(api.providerDraftDirty.value).toBe(false)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      providerId: 'openrouter',
      imageGenerationIntent: 'enable_provider_default',
    }))
    app.unmount()
  })

  it('preserves image setup when the OpenRouter draft uses a custom endpoint', async () => {
    mockFreshOpenRouter('unconfigured')
    const { api, app } = await mountCatalog()

    api.selectProvider('openrouter')
    api.onProviderChange()
    expect(api.providerPanel.value.imageGenerationOffer).toBe(true)

    api.updateProviderField('base_url', 'https://proxy.example.test/v1')
    expect(api.providerPanel.value.imageGenerationOffer).toBe(false)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      providerId: 'openrouter',
      imageGenerationIntent: 'preserve',
    }))
    app.unmount()
  })

  it('preserves image setup for a same-origin OpenRouter draft with the wrong API path', async () => {
    mockFreshOpenRouter('unconfigured')
    const { api, app } = await mountCatalog()

    api.selectProvider('openrouter')
    api.onProviderChange()
    api.updateProviderField('base_url', 'https://openrouter.ai/compatible/v1')

    expect(api.providerPanel.value.imageGenerationOffer).toBe(false)
    await api.saveProvider()

    expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
      providerId: 'openrouter',
      imageGenerationIntent: 'preserve',
    }))
    app.unmount()
  })

  it.each(['custom', 'disabled'] as const)(
    'preserves an explicitly %s image configuration',
    async mode => {
      mockFreshOpenRouter(mode)
      const { api, app } = await mountCatalog()

      api.selectProvider('openrouter')
      api.onProviderChange()
      expect(api.providerPanel.value.imageGenerationOffer).toBe(false)
      await api.saveProvider()

      expect(rpcCall).toHaveBeenCalledWith('onboarding.provider.configure', expect.objectContaining({
        imageGenerationIntent: 'preserve',
      }))
      app.unmount()
    },
  )

  it('sends the OpenRouter default intent when activating a stored profile', async () => {
    const providers = [
      {
        providerId: 'openai',
        label: 'OpenAI',
        runtimeSupported: true,
        requiresApiKey: true,
        fields: [{ name: 'model', label: 'Model', required: true }],
      },
      openRouterProvider,
    ]
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmCredentialStatus: {
            provider: 'openai',
            available: true,
            source: 'explicit',
          },
          llmProfileStatus: [{
            provider: 'openrouter',
            ready: true,
            credentialSource: 'profile',
            primaryEligible: true,
            primaryBlockReason: '',
          }],
          imageGenerationState: { mode: 'unconfigured' },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openai', model: 'gpt-4.1-mini' },
          llm_profiles: { openrouter: { model: 'openai/gpt-4.1-mini' } },
        }
      }
      if (method === 'onboarding.models.discover') return { ok: false, source: 'none', models: [] }
      if (method === 'onboarding.llmProfile.activate') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('openrouter')
    await api.activateProvider('openrouter')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.activate', {
      providerId: 'openrouter',
      imageGenerationIntent: 'enable_provider_default',
    })
    app.unmount()
  })

  it('omits the activation intent for an older Gateway without the state contract', async () => {
    const providers = [
      {
        providerId: 'openai',
        label: 'OpenAI',
        runtimeSupported: true,
        requiresApiKey: true,
        fields: [{ name: 'model', label: 'Model', required: true }],
      },
      openRouterProvider,
    ]
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') return { providers }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          llmCredentialStatus: {
            provider: 'openai',
            available: true,
            source: 'explicit',
          },
          llmProfileStatus: [{
            provider: 'openrouter',
            ready: true,
            credentialSource: 'profile',
            primaryEligible: true,
            primaryBlockReason: '',
          }],
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: { provider: 'openai', model: 'gpt-4.1-mini' },
          llm_profiles: { openrouter: { model: 'openai/gpt-4.1-mini' } },
        }
      }
      if (method === 'onboarding.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      if (method === 'onboarding.llmProfile.activate') return { changed: true }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    api.selectConfiguredProvider('openrouter')
    await api.activateProvider('openrouter')

    expect(rpcCall).toHaveBeenCalledWith('onboarding.llmProfile.activate', {
      providerId: 'openrouter',
    })
    app.unmount()
  })

  it('keeps a standalone TokenRhythm recommendation for a custom OpenRouter endpoint', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          providers: [openRouterProvider],
          imageGenerationProviders: [{
            providerId: 'tokenrhythm',
            label: 'TokenRhythm Images',
            runtimeSupported: true,
            requiresApiKey: true,
            defaultModel: 'tokenrhythm/qwen-image-2.0',
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          hasConfig: true,
          llmConfigured: true,
          imageGenerationState: {
            mode: 'unconfigured',
            recommendation: {
              providerId: 'tokenrhythm',
              reason: 'recommended_standalone',
              canReuseCredential: false,
              actionRequired: true,
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return {
          llm: {
            provider: 'openrouter',
            model: 'openai/gpt-4.1-mini',
            base_url: 'https://proxy.example.test/v1',
          },
        }
      }
      if (method === 'onboarding.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      if (method === 'onboarding.imageGeneration.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.options.imageRecommendation).toMatchObject({
      providerId: 'tokenrhythm',
      actionRequired: true,
    })
    app.unmount()
  })

  it('shows only a server-recommended catalog provider and dirties on explicit use', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.catalog') {
        return {
          imageGenerationProviders: [{
            providerId: 'tokenrhythm',
            label: 'TokenRhythm Images',
            runtimeSupported: true,
            requiresApiKey: true,
            defaultModel: 'tokenrhythm/qwen-image-2.0',
          }],
        }
      }
      if (method === 'onboarding.status') {
        return {
          llmConfigured: true,
          imageGenerationState: {
            mode: 'unconfigured',
            recommendation: {
              providerId: 'tokenrhythm',
              reason: 'recommended_standalone',
              canReuseCredential: false,
              actionRequired: true,
            },
          },
        }
      }
      if (method === 'channels.status') return { channels: [] }
      if (method === 'config.get') {
        return { llm: { provider: 'openai', model: 'gpt-4.1-mini' } }
      }
      if (method === 'onboarding.imageGeneration.models.discover') {
        return { ok: false, source: 'none', models: [] }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const { api, app } = await mountCatalog()

    expect(api.capabilitiesPanel.value.options.imageRecommendation).toMatchObject({
      providerId: 'tokenrhythm',
      label: 'TokenRhythm Images',
    })
    expect(api.capabilitiesPanel.value.form.imageProvider).toBe('')
    expect(api.sectionDirty('capabilities')).toBe(false)

    api.useImageRecommendation('tokenrhythm')

    expect(api.capabilitiesPanel.value.form.imageProvider).toBe('tokenrhythm')
    expect(api.sectionDirty('capabilities')).toBe(true)
    app.unmount()
  })
})
