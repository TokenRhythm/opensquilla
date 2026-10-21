import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type Ref } from 'vue'
import { useChatFeatureToggles } from './useChatFeatureToggles'
import source from './useChatFeatureToggles.ts?raw'
import chatViewSource from '@/views/ChatView.vue?raw'
import type { RpcCallOptions } from '@/lib/rpc'
import type {
  ModelRoutingCapabilitiesByMode,
  ModelRoutingMode,
} from '@/types/modelRouting'
import {
  ProviderConfigurationError,
  type ModelRoutingSnapshot,
} from '@/modules/providerConfiguration'

type RpcResult = Record<string, unknown> | Error | Promise<unknown>

const CAPABILITIES_BY_MODE: ModelRoutingCapabilitiesByMode = {
  direct: {
    image_input: { admission: 'allowed', reason: 'model_vision_supported' },
  },
  router: {
    image_input: { admission: 'allowed', reason: 'router_image_route_available' },
  },
  ensemble: {
    image_input: { admission: 'blocked', reason: 'ensemble_mode_unsupported' },
  },
}

const EFFECTIVE_CAPABILITIES_BY_MODE: ModelRoutingCapabilitiesByMode = {
  ...CAPABILITIES_BY_MODE,
  ensemble: {
    image_input: { admission: 'allowed', reason: 'ensemble_mode_unsupported' },
  },
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function createHarness(options: {
  configGetResults?: RpcResult[]
  routingGetResults?: RpcResult[]
  patchResults?: RpcResult[]
  readCallOptions?: RpcCallOptions
  hasRpcMethod?: (method: string) => boolean
  connectionEpoch?: Readonly<Ref<unknown>>
  connectionAvailable?: Readonly<Ref<boolean>>
} = {}) {
  const configGetResults = [...(options.configGetResults ?? [{}])]
  const routingGetResults = [...(options.routingGetResults ?? [])]
  const patchResults = [...(options.patchResults ?? [])]
  const ready = vi.fn(async () => {})
  let routingChangedListener: ((snapshot: ModelRoutingSnapshot) => void) | null = null
  const subscribeChanged = vi.fn((listener: (snapshot: ModelRoutingSnapshot) => void) => {
    routingChangedListener = listener
    return {
      close() {
        if (routingChangedListener === listener) routingChangedListener = null
      },
    }
  })
  const setGlobalElevatedMode = vi.fn()
  const loadCurrentSessionUsage = vi.fn()
  const call = vi.fn(async (method: string, _params?: Record<string, unknown>): Promise<unknown> => {
    if (method === 'config.get') {
      const result = configGetResults.shift() ?? {}
      if (result instanceof Error) throw result
      return await Promise.resolve(result)
    }
    if (method === 'models.routing.get') {
      if (options.hasRpcMethod?.('models.routing.get') === false) {
        throw new ProviderConfigurationError(
          'unsupported',
          'Model routing is unsupported.',
        )
      }
      const result = routingGetResults.shift()
      if (result === undefined) throw new Error('canonical routing unavailable')
      if (result instanceof Error) throw result
      return await Promise.resolve(result)
    }
    if (method === 'config.patch.safe' || method === 'models.routing.set') {
      const result = patchResults.shift()
      if (result instanceof Error) throw result
      await Promise.resolve(result)
      return { ok: true }
    }
    throw new Error(`Unexpected RPC method: ${method}`)
  })
  const rpcRequest = call as unknown as (
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<unknown>
  const api = useChatFeatureToggles({
    appSettings: {
      readAll: vi.fn(async () => {
        return await rpcRequest('config.get', undefined, options.readCallOptions) as import('@/modules/appSettings').SettingsObject
      }),
      read: vi.fn(async () => null),
      readEffective: vi.fn(async () => ({ fields: {} })),
      patch: vi.fn(async () => ({})),
      patchSafe: vi.fn(async (changes: readonly { path: string; value: unknown }[]) => {
        const patches = Object.fromEntries(changes.map(change => [change.path, change.value]))
        await call('config.patch.safe', { patches })
        return {}
      }),
      merge: vi.fn(async () => ({})),
      setTelemetryConsent: vi.fn(async (scope, enabled) => ({
        scope,
        enabled,
        noticeVersion: enabled ? `${scope}-v1` : null,
        consentedAtUtc: enabled ? '2026-09-03T00:00:00Z' : null,
      })),
    },
    modelRouting: {
      get: vi.fn(async () => await rpcRequest('models.routing.get', undefined, options.readCallOptions) as import('@/modules/providerConfiguration').ModelRoutingSnapshot),
      setRouting: vi.fn(async (mode: string) => {
        await rpcRequest('models.routing.set', { mode })
        return { mode: mode as import('@/modules/providerConfiguration').RoutingMode }
      }),
      subscribeChanged,
    },
    readOptions: options.readCallOptions,
    connectionEpoch: options.connectionEpoch,
    connectionAvailable: options.connectionAvailable,
    setGlobalElevatedMode,
    loadCurrentSessionUsage,
  })
  return {
    api,
    rpc: { ready, call, subscribeChanged },
    emitRouting: (snapshot: ModelRoutingSnapshot) => routingChangedListener?.(snapshot),
    setGlobalElevatedMode,
    loadCurrentSessionUsage,
  }
}


function routingCalls(rpc: ReturnType<typeof createHarness>['rpc']) {
  return rpc.call.mock.calls.filter(([method]) => method === 'models.routing.set')
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useChatFeatureToggles default model display', () => {
  const primaryConfig = { llm: { model: 'Model/CaseSensitive', provider: 'tokenrhythm' } }
  const primaryModel = { model: 'Model/CaseSensitive', provider: 'tokenrhythm' }

  it('reads the direct default from the existing config request even when global routing is enabled', async () => {
    const { api, rpc } = createHarness({
      configGetResults: [{
        llm: { model: ' Model/CaseSensitive ', provider: ' TokenRhythm ' },
        squilla_router: { enabled: true, rollout_phase: 'full' },
      }],
      routingGetResults: [{ mode: 'router', model: 'not-a-default', provider: 'other' }],
    })
    expect(api.defaultModelForAgent('main')).toBeNull()
    await api.loadFeatureToggles()
    expect(api.defaultModelForAgent('main')).toEqual(primaryModel)
    expect(api.defaultModelForAgent('')).toEqual(primaryModel)
    expect(api.defaultModelForAgent('DEFAULT')).toEqual(primaryModel)
    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(rpc.call.mock.calls.map(([method]) => method)).toEqual(['config.get', 'models.routing.get'])
  })

  it('never invents a provider for a different enabled agent override', async () => {
    const { api } = createHarness({
      configGetResults: [{
        ...primaryConfig,
        agents: [
          { id: 'Writer', model: 'writer-specific' },
          { id: 'same', model: 'Model/CaseSensitive' },
          { id: 'disabled', model: 'another-model', enabled: false },
          { id: 'inherited', model: null },
        ],
      }],
    })
    await api.loadFeatureToggles()
    expect(api.defaultModelForAgent(' writer ')).toBeNull()
    expect(api.defaultModelForAgent('same')).toEqual(primaryModel)
    expect(api.defaultModelForAgent('disabled')).toEqual(primaryModel)
    expect(api.defaultModelForAgent('inherited')).toEqual(primaryModel)
    expect(api.defaultModelForAgent('main')).toEqual(primaryModel)
  })

  it.each([
    {},
    { llm: {} },
    { llm: { model: 'known-model' } },
    { llm: { model: '', provider: 'tokenrhythm' } },
    { llm: { model: 'known-model', provider: 123 } },
  ])('keeps an incomplete default unknown for %j', async (config) => {
    const { api } = createHarness({ configGetResults: [config] })
    await api.loadFeatureToggles()
    expect(api.defaultModelForAgent('main')).toBeNull()
  })

  it('clears the previous default while a settings refresh is pending and adopts the new value', async () => {
    const nextConfig = deferred<Record<string, unknown>>()
    const { api } = createHarness({ configGetResults: [primaryConfig, nextConfig.promise] })
    await api.loadFeatureToggles()
    expect(api.defaultModelForAgent('main')).toEqual(primaryModel)
    const refresh = api.loadFeatureToggles()
    expect(api.defaultModelForAgent('main')).toBeNull()
    nextConfig.resolve({ llm: { model: 'deepseek-chat', provider: 'deepseek' } })
    await refresh
    expect(api.defaultModelForAgent('main')).toEqual({ model: 'deepseek-chat', provider: 'deepseek' })
  })

  it('keeps the default unknown after a failed config refresh', async () => {
    const { api } = createHarness({ configGetResults: [primaryConfig, new Error('config unavailable')] })
    await api.loadFeatureToggles()
    await api.loadFeatureToggles()
    expect(api.defaultModelForAgent('main')).toBeNull()
  })

  it('rejects late default config from an old connection and clears values immediately on loss', async () => {
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const connectionAvailable = ref(true)
      const oldConfig = deferred<Record<string, unknown>>()
      const nextModel = { model: 'new-model', provider: 'openrouter' }
      const { api } = scope.run(() => createHarness({
        configGetResults: [primaryConfig, oldConfig.promise, { llm: nextModel }],
        connectionEpoch,
        connectionAvailable,
      }))!
      await api.loadFeatureToggles()
      expect(api.defaultModelForAgent('main')).toEqual(primaryModel)
      connectionAvailable.value = false
      expect(api.defaultModelForAgent('main')).toBeNull()
      connectionAvailable.value = true
      const oldRead = api.loadFeatureToggles()
      connectionEpoch.value = 2
      oldConfig.resolve(primaryConfig)
      await oldRead
      expect(api.defaultModelForAgent('main')).toBeNull()
      await api.loadFeatureToggles()
      expect(api.defaultModelForAgent('main')).toEqual(nextModel)
      connectionEpoch.value = 3
      expect(api.defaultModelForAgent('main')).toBeNull()
    } finally {
      scope.stop()
    }
  })

  it('does not let an older config response replace a newer settings read', async () => {
    const oldConfig = deferred<Record<string, unknown>>()
    const nextModel = { model: 'new-model', provider: 'openrouter' }
    const { api } = createHarness({ configGetResults: [oldConfig.promise, { llm: nextModel }] })
    const oldRead = api.loadFeatureToggles()
    await api.loadFeatureToggles()
    oldConfig.resolve(primaryConfig)
    await oldRead
    expect(api.defaultModelForAgent('main')).toEqual(nextModel)
  })

  it('passes agent-specific defaults to both draft and existing selectors and refreshes all authoritative state', () => {
    expect(chatViewSource).toContain(':default-model="composerDefaultModel"')
    expect(chatViewSource).toContain('isProvisionalDraftSession() ? draftAgentId() : agentIdFromSessionKey(sessionKey.value)')
    expect(chatViewSource).toContain('@refresh-models="refreshComposerModels"')
    expect(chatViewSource).toMatch(/Promise\.allSettled\(\[\s*newTaskModel\.refresh\(\), loadFeatureToggles\(\), chatSessionModel\.refresh\(\),\s*chatSessionRouting\.load\(\)/)
    expect(chatViewSource).toContain('connectionEpoch: computed(() => gatewayAccess.subscriptionEpoch)')
    expect(chatViewSource).toContain('connectionAvailable: computed(() => gatewayAccess.isAvailable && gatewayAccess.isAuthenticated)')
  })
})

describe('useChatFeatureToggles router visual effects', () => {
  it('persists router visual effects without a legacy browser global', () => {
    const setItem = vi.fn()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem,
    })
    const { api } = createHarness()

    api.setRouterVisualEffectsEnabled(false)

    expect(api.routerVisualEffectsEnabled.value).toBe(false)
    expect(setItem).toHaveBeenCalledWith('opensquilla.routerFx', JSON.stringify({
      enabled: false,
      variant: 'default',
    }))
    expect(source).not.toContain('SavingsFX')
  })
})

describe('useChatFeatureToggles tier refresh', () => {
  function prepareRefreshEvents() {
    vi.useFakeTimers()
    vi.stubGlobal('document', {
      visibilityState: 'visible',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    vi.stubGlobal('window', {
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
  }

  function configFor(prefix: string) {
    return {
      squilla_router: {
        enabled: true,
        rollout_phase: 'full',
        tiers: {
          c3: { model: `${prefix}-quality` },
          c1: { model: `${prefix}-balanced` },
          c0: { model: `${prefix}-fast` },
          c2: { model: `${prefix}-reasoning` },
        },
      },
    }
  }

  it('refreshes saved tier models in the retained chat without a focus or visibility event', async () => {
    prepareRefreshEvents()
    const { api, emitRouting, rpc } = createHarness({
      configGetResults: [configFor('original'), configFor('replacement')],
      routingGetResults: [{ mode: 'router' }, { mode: 'router' }],
    })
    await api.loadFeatureToggles()
    const cleanup = api.bindFeatureRefresh()

    emitRouting({ mode: 'router' })
    await vi.advanceTimersByTimeAsync(120)

    expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(2)
    expect(api.routerSlots.value).toEqual(['c0', 'c1', 'c2', 'c3'])
    expect(api.routerModels.value).toEqual({
      c0: 'replacement-fast',
      c1: 'replacement-balanced',
      c2: 'replacement-reasoning',
      c3: 'replacement-quality',
    })
    expect(api.routerTierConfigs.value.c2?.model).toBe('replacement-reasoning')
    cleanup()
  })

  it('keeps explicit providers for tiers using the same model name', async () => {
    const { api } = createHarness({ configGetResults: [{ squilla_router: {
      enabled: true,
      rollout_phase: 'full',
      tiers: {
        c0: { provider: ' Provider_A ', model: 'shared-model' },
        c1: { provider: 'Provider_B', model: 'shared-model' },
        c2: { model: 'unassigned-model' },
      },
    } }] })
    await api.loadFeatureToggles()

    expect(api.routerTierConfigs.value.c0?.provider).toBe('provider_a')
    expect(api.routerTierConfigs.value.c1?.provider).toBe('provider_b')
    expect(api.routerTierConfigs.value.c2).not.toHaveProperty('provider')
  })

  it.each([true, false])('prefers canonical tiers over legacy aliases with canonical-first=%s', async (canonicalFirst) => {
    const canonical = { c0: { provider: 'provider_a', model: 'canonical-fast' } }
    const legacy = { t0: { provider: 'provider_b', model: 'retired-fast' } }
    const { api } = createHarness({ configGetResults: [{ squilla_router: {
      tiers: canonicalFirst ? { ...canonical, ...legacy } : { ...legacy, ...canonical },
    } }] })
    await api.loadFeatureToggles()

    expect(api.routerSlots.value).toEqual(['c0'])
    expect(api.routerModels.value).toEqual({ c0: 'canonical-fast' })
    expect(api.routerTierConfigs.value.c0?.provider).toBe('provider_a')
  })

  it('retires a pending pre-save config read before the event refresh debounce expires', async () => {
    prepareRefreshEvents()
    const staleConfig = deferred<Record<string, unknown>>()
    const { api, emitRouting, rpc } = createHarness({
      configGetResults: [configFor('original'), staleConfig.promise, configFor('replacement')],
      routingGetResults: [{ mode: 'router' }, { mode: 'direct' }],
    })
    await api.loadFeatureToggles()
    const cleanup = api.bindFeatureRefresh()
    const staleLoad = api.loadFeatureToggles()

    emitRouting({
      mode: 'direct',
      capabilities_by_mode: CAPABILITIES_BY_MODE,
      image_input: CAPABILITIES_BY_MODE.direct.image_input,
    })
    staleConfig.resolve(configFor('stale'))
    await staleLoad

    expect(api.modelRoutingMode.value).toBe('off')
    expect(api.routerModels.value.c2).toBe('original-reasoning')
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
    expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(120)
    expect(api.routerModels.value.c2).toBe('replacement-reasoning')
    expect(api.modelRoutingMode.value).toBe('off')
    cleanup()
  })

  it('preserves event mode and capabilities while tier refresh waits for optional usage', async () => {
    prepareRefreshEvents()
    const usage = deferred<void>()
    const { api, emitRouting, loadCurrentSessionUsage } = createHarness({
      configGetResults: [configFor('replacement')],
      routingGetResults: [{
        mode: 'ensemble',
        selection_mode: 'custom_b5',
        capabilities_by_mode: CAPABILITIES_BY_MODE,
        image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
      }],
    })
    loadCurrentSessionUsage.mockImplementation(() => usage.promise)
    const cleanup = api.bindFeatureRefresh()

    emitRouting({
      mode: 'ensemble',
      selection_mode: 'custom_b5',
      capabilities_by_mode: CAPABILITIES_BY_MODE,
      image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
    })
    await vi.advanceTimersByTimeAsync(120)

    expect(api.routerModels.value.c2).toBe('replacement-reasoning')
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.llmEnsembleSelectionMode.value).toBe('custom_b5')
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
    usage.resolve()
    await vi.advanceTimersByTimeAsync(0)
    cleanup()
  })

  it('preserves a newer routing event when an earlier mutation fails', async () => {
    prepareRefreshEvents()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const mutation = deferred<Record<string, unknown>>()
    const { api, emitRouting } = createHarness({
      configGetResults: [configFor('original')],
      routingGetResults: [{ mode: 'router' }],
      patchResults: [mutation.promise],
    })
    await api.loadFeatureToggles()
    const cleanup = api.bindFeatureRefresh()
    const pending = api.setModelRoutingMode('off')
    emitRouting({
      mode: 'ensemble',
      selection_mode: 'custom_b5',
      capabilities_by_mode: CAPABILITIES_BY_MODE,
      image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
    })
    mutation.reject(new Error('earlier mutation failed'))
    await pending

    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.llmEnsembleSelectionMode.value).toBe('custom_b5')
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
    cleanup()
  })

  it('coalesces rapid routing events and cancels pending refreshes on cleanup', async () => {
    prepareRefreshEvents()
    const { api, emitRouting, rpc } = createHarness({
      configGetResults: [configFor('replacement')],
      routingGetResults: [{ mode: 'direct' }],
    })
    const cleanup = api.bindFeatureRefresh()
    emitRouting({ mode: 'router' })
    emitRouting({ mode: 'direct' })
    await vi.advanceTimersByTimeAsync(120)

    expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(1)
    expect(api.modelRoutingMode.value).toBe('off')
    emitRouting({ mode: 'ensemble' })
    cleanup()
    await vi.advanceTimersByTimeAsync(120)
    expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(1)
  })

  it('does not carry scheduled refresh work into a new connection', async () => {
    prepareRefreshEvents()
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const { api, emitRouting, rpc } = scope.run(() => createHarness({ connectionEpoch }))!
      const cleanup = api.bindFeatureRefresh()
      emitRouting({ mode: 'router' })
      connectionEpoch.value = 2
      await vi.advanceTimersByTimeAsync(120)

      expect(rpc.call).not.toHaveBeenCalled()
      expect(api.modelRoutingMode.value).toBe('off')
      cleanup()
    } finally {
      scope.stop()
    }
  })

  it('ignores an in-flight tier refresh after cleanup', async () => {
    prepareRefreshEvents()
    const config = deferred<Record<string, unknown>>()
    const syncHistory = vi.fn()
    const { api, emitRouting, rpc } = createHarness({ configGetResults: [config.promise] })
    const cleanup = api.bindFeatureRefresh(syncHistory)
    emitRouting({ mode: 'router' })
    await vi.advanceTimersByTimeAsync(120)
    expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(1)
    const syncsBeforeCleanup = syncHistory.mock.calls.length

    cleanup()
    config.resolve(configFor('retired'))
    await vi.advanceTimersByTimeAsync(0)

    expect(api.routerModels.value).toEqual({})
    expect(syncHistory).toHaveBeenCalledTimes(syncsBeforeCleanup)
    expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(0)
  })
})

describe('useChatFeatureToggles connection lifecycle', () => {
  it('does not issue feature reads or mutations while the connection is unavailable', async () => {
    const { api, rpc } = createHarness({ connectionAvailable: ref(false) })
    await api.loadFeatureToggles()
    await api.setModelRoutingMode('llm_ensemble')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('clears routing state on connection loss and ignores a late routing response', async () => {
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const connectionAvailable = ref(true)
      const pendingRouting = deferred<Record<string, unknown>>()
      const { api, rpc } = scope.run(() => createHarness({
        connectionEpoch,
        connectionAvailable,
        configGetResults: [{ squilla_router: {
          enabled: true,
          rollout_phase: 'full',
          tiers: { c0: { model: 'original-fast' } },
        } }],
        routingGetResults: [pendingRouting.promise],
      }))!
      const load = api.loadFeatureToggles()
      await vi.waitFor(() => {
        expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(1)
      })
      expect(api.routerModels.value.c0).toBe('original-fast')

      connectionAvailable.value = false
      expect(api.routerSlots.value).toEqual([])
      expect(api.routerModels.value).toEqual({})
      expect(api.routerTierConfigs.value).toEqual({})
      pendingRouting.resolve({
        mode: 'ensemble',
        capabilities_by_mode: CAPABILITIES_BY_MODE,
        image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
      })
      await load

      expect(api.modelRoutingMode.value).toBe('off')
      expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
      expect(api.globalImageInputAdmission.value).toBe('unknown')
    } finally {
      scope.stop()
    }
  })

  it('rejects old tier config after the connection epoch changes without starting a new read', async () => {
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const pendingConfig = deferred<Record<string, unknown>>()
      const { api, rpc } = scope.run(() => createHarness({
        connectionEpoch,
        configGetResults: [pendingConfig.promise],
      }))!
      const load = api.loadFeatureToggles()
      connectionEpoch.value = 2
      pendingConfig.resolve({ squilla_router: {
        enabled: true,
        rollout_phase: 'full',
        tiers: { c0: { model: 'retired-fast' } },
      } })
      await load

      expect(api.routerModels.value).toEqual({})
      expect(api.modelRoutingMode.value).toBe('off')
      expect(rpc.call.mock.calls.map(([method]) => method)).toEqual(['config.get'])
    } finally {
      scope.stop()
    }
  })

  it('does not restore a previous connection capability matrix after a transient routing failure', async () => {
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const { api } = scope.run(() => createHarness({
        connectionEpoch,
        configGetResults: [{}, { squilla_router: {
          enabled: true,
          rollout_phase: 'full',
          tiers: { c0: { model: 'replacement-fast' } },
        } }],
        routingGetResults: [{
          mode: 'ensemble',
          selection_mode: 'custom_b5',
          capabilities_by_mode: CAPABILITIES_BY_MODE,
          image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
        }, new Error('routing unavailable')],
      }))!
      await api.loadFeatureToggles()
      expect(api.modelRoutingMode.value).toBe('llm_ensemble')
      connectionEpoch.value = 2
      await api.loadFeatureToggles()

      expect(api.modelRoutingMode.value).toBe('squilla_router')
      expect(api.routerModels.value.c0).toBe('replacement-fast')
      expect(api.llmEnsembleSelectionMode.value).toBe('')
      expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
      expect(api.globalImageInputAdmission.value).toBe('unknown')
    } finally {
      scope.stop()
    }
  })

  it.each(['success', 'failure'])('ignores an old routing mutation %s after reconnect', async (outcome) => {
    const scope = effectScope()
    try {
      const connectionEpoch = ref(1)
      const mutation = deferred<Record<string, unknown>>()
      const { api, rpc } = scope.run(() => createHarness({
        connectionEpoch,
        configGetResults: [{}],
        routingGetResults: [{ mode: 'router' }],
        patchResults: [mutation.promise],
      }))!
      await api.loadFeatureToggles()
      const pending = api.setModelRoutingMode('llm_ensemble')
      connectionEpoch.value = 2
      if (outcome === 'success') mutation.resolve({})
      else mutation.reject(new Error('old connection request failed'))
      await pending

      expect(api.modelRoutingMode.value).toBe('off')
      expect(api.modelRoutingSettingsBusy.value).toBe(false)
      expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(1)
    } finally {
      scope.stop()
    }
  })
})

describe('useChatFeatureToggles model routing mode', () => {
  it('uses the canonical routing snapshot over legacy config inference', async () => {
    const { api } = createHarness({
      configGetResults: [{ squilla_router: { enabled: true, rollout_phase: 'full' } }],
      routingGetResults: [{ mode: 'direct' }],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingMode.value).toBe('off')
  })

  it('allows capability degradation while preserving real Gateway blocks', async () => {
    const degraded = createHarness({
      configGetResults: [{}],
      routingGetResults: [{
        mode: 'direct',
        image_input: {
          admission: 'blocked',
          reason: 'model_vision_unsupported',
        },
      }],
    })
    await degraded.api.loadFeatureToggles()
    expect(degraded.api.globalImageInputAdmission.value).toBe('allowed')
    expect(degraded.api.globalImageInputAdmissionReason.value).toBe('model_vision_unsupported')

    const policyBlocked = createHarness({
      configGetResults: [{}],
      routingGetResults: [{
        mode: 'direct',
        image_input: {
          admission: 'blocked',
          reason: 'attachment_policy_denied',
        },
      }],
    })
    await policyBlocked.api.loadFeatureToggles()
    expect(policyBlocked.api.globalImageInputAdmission.value).toBe('blocked')
    expect(policyBlocked.api.globalImageInputAdmissionReason.value).toBe('attachment_policy_denied')

    const legacyDirect = createHarness({
      configGetResults: [{ llm_ensemble: { enabled: false } }],
      routingGetResults: [{ mode: 'direct' }],
    })
    await legacyDirect.api.loadFeatureToggles()
    expect(legacyDirect.api.globalImageInputAdmission.value).toBe('unknown')

    const legacyEnsemble = createHarness({
      configGetResults: [{ llm_ensemble: { enabled: true } }],
      routingGetResults: [{ mode: 'ensemble' }],
    })
    await legacyEnsemble.api.loadFeatureToggles()
    expect(legacyEnsemble.api.globalImageInputAdmission.value).toBe('allowed')
    expect(legacyEnsemble.api.globalImageInputAdmissionReason.value).toBe(
      'ensemble_mode_unsupported',
    )
  })

  it('applies Gateway routing changes live and unsubscribes on cleanup', async () => {
    vi.stubGlobal('document', {
      visibilityState: 'visible',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    vi.stubGlobal('window', {
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    const { api, rpc, emitRouting } = createHarness()
    const cleanup = api.bindFeatureRefresh()

    emitRouting({ mode: 'ensemble', selection_mode: 'router_dynamic' })
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.llmEnsembleSelectionMode.value).toBe('router_dynamic')

    emitRouting({
      mode: 'router',
      image_input: {
        admission: 'allowed',
        reason: 'router_image_route_available',
      },
    })
    expect(api.globalImageInputAdmission.value).toBe('allowed')

    cleanup()
    expect(rpc.subscribeChanged).toHaveBeenCalledWith(expect.any(Function))
    emitRouting({ mode: 'direct' })
    expect(api.modelRoutingMode.value).toBe('squilla_router')
  })

  it('atomically applies a complete capability matrix', async () => {
    const { api } = createHarness({
      configGetResults: [{}],
      routingGetResults: [{
        mode: 'ensemble',
        image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
        capabilities_by_mode: CAPABILITIES_BY_MODE,
      }],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
    expect(api.globalImageInputAdmission.value).toBe('allowed')
  })

  it('clears the whole matrix when a later snapshot is missing or partial', async () => {
    const { api } = createHarness({
      configGetResults: [{}, {}, {}],
      routingGetResults: [
        {
          mode: 'ensemble',
          image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
          capabilities_by_mode: CAPABILITIES_BY_MODE,
        },
        {
          mode: 'ensemble',
          image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
          capabilities_by_mode: {
            direct: CAPABILITIES_BY_MODE.direct,
            ensemble: CAPABILITIES_BY_MODE.ensemble,
          },
        },
        {
          mode: 'ensemble',
          image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
        },
      ],
    })

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
  })

  it('normalizes capability limits in the per-mode matrix while retaining policy blocks', async () => {
    const { api } = createHarness({
      configGetResults: [{}],
      routingGetResults: [{
        mode: 'direct',
        capabilities_by_mode: {
          direct: {
            image_input: { admission: 'blocked', reason: 'model_vision_unsupported' },
          },
          router: {
            image_input: { admission: 'blocked', reason: 'attachment_policy_denied' },
          },
          ensemble: CAPABILITIES_BY_MODE.ensemble,
        },
      }],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingCapabilitiesByMode.value).toEqual({
      direct: {
        image_input: { admission: 'allowed', reason: 'model_vision_unsupported' },
      },
      router: {
        image_input: { admission: 'blocked', reason: 'attachment_policy_denied' },
      },
      ensemble: EFFECTIVE_CAPABILITIES_BY_MODE.ensemble,
    })
  })

  it('does not let a late routing GET overwrite a newer changed event', async () => {
    vi.stubGlobal('document', {
      visibilityState: 'visible',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    vi.stubGlobal('window', {
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    const pending = deferred<Record<string, unknown>>()
    const { api, emitRouting, rpc } = createHarness({
      configGetResults: [{}],
      routingGetResults: [pending.promise],
    })
    const cleanup = api.bindFeatureRefresh()
    const load = api.loadFeatureToggles()
    await vi.waitFor(() => {
      expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(1)
    })

    emitRouting({
      mode: 'router',
      image_input: CAPABILITIES_BY_MODE.router.image_input,
      capabilities_by_mode: CAPABILITIES_BY_MODE,
    })
    pending.resolve({
      mode: 'ensemble',
      image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
      capabilities_by_mode: CAPABILITIES_BY_MODE,
    })
    await load

    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.globalImageInputAdmission.value).toBe('allowed')
    cleanup()
  })

  it('only applies the latest overlapping routing GET', async () => {
    const first = deferred<Record<string, unknown>>()
    const second = deferred<Record<string, unknown>>()
    const { api, rpc } = createHarness({
      configGetResults: [{}, {}],
      routingGetResults: [first.promise, second.promise],
    })
    const firstLoad = api.loadFeatureToggles()
    await vi.waitFor(() => {
      expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(1)
    })
    const secondLoad = api.loadFeatureToggles()
    await vi.waitFor(() => {
      expect(rpc.call.mock.calls.filter(([method]) => method === 'models.routing.get')).toHaveLength(2)
    })

    second.resolve({
      mode: 'router',
      image_input: CAPABILITIES_BY_MODE.router.image_input,
      capabilities_by_mode: CAPABILITIES_BY_MODE,
    })
    await secondLoad
    first.resolve({
      mode: 'ensemble',
      image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
      capabilities_by_mode: CAPABILITIES_BY_MODE,
    })
    await firstLoad

    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.globalImageInputAdmission.value).toBe('allowed')
  })

  it('does not let an older overlapping config response overwrite the latest refresh', async () => {
    const firstConfig = deferred<Record<string, unknown>>()
    const { api, rpc } = createHarness({
      configGetResults: [
        firstConfig.promise,
        {
          squilla_router: { enabled: true, rollout_phase: 'full' },
        },
      ],
      routingGetResults: [{
        mode: 'router',
        image_input: CAPABILITIES_BY_MODE.router.image_input,
        capabilities_by_mode: CAPABILITIES_BY_MODE,
      }],
    })
    const firstLoad = api.loadFeatureToggles()
    await vi.waitFor(() => {
      expect(rpc.call.mock.calls.filter(([method]) => method === 'config.get')).toHaveLength(1)
    })

    await api.loadFeatureToggles()
    firstConfig.resolve({
      llm_ensemble: { enabled: true },
    })
    await firstLoad

    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
  })

  it('clears a prior canonical matrix when the connected Gateway lacks routing RPC', async () => {
    let supportsRouting = true
    const { api } = createHarness({
      configGetResults: [
        {},
        { llm_ensemble: { enabled: true } },
      ],
      routingGetResults: [
        {
          mode: 'router',
          image_input: CAPABILITIES_BY_MODE.router.image_input,
          capabilities_by_mode: CAPABILITIES_BY_MODE,
        },
      ],
      hasRpcMethod: method => method !== 'models.routing.get' || supportsRouting,
    })

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)

    supportsRouting = false
    await api.loadFeatureToggles()

    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.globalImageInputAdmission.value).toBe('allowed')
    expect(api.globalImageInputAdmissionReason.value).toBe('ensemble_mode_unsupported')
  })

  it('preserves the last canonical matrix on a transient routing RPC failure', async () => {
    const { api } = createHarness({
      configGetResults: [
        {},
        { llm_ensemble: { enabled: true } },
      ],
      routingGetResults: [
        {
          mode: 'router',
          image_input: CAPABILITIES_BY_MODE.router.image_input,
          capabilities_by_mode: CAPABILITIES_BY_MODE,
        },
        Object.assign(new Error('routing request timed out'), { code: 'RPC_TIMEOUT' }),
      ],
    })

    await api.loadFeatureToggles()
    await api.loadFeatureToggles()

    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(EFFECTIVE_CAPABILITIES_BY_MODE)
    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.globalImageInputAdmission.value).toBe('allowed')
  })

  it('refreshes the capability matrix when the chat reconnects', () => {
    const watcherStart = chatViewSource.indexOf('// Hello refreshes method capabilities on reconnect')
    const watcherEnd = chatViewSource.indexOf('watch(shareableMessageCount', watcherStart)
    const reconnectWatcher = chatViewSource.slice(watcherStart, watcherEnd)

    expect(watcherStart).toBeGreaterThanOrEqual(0)
    expect(watcherEnd).toBeGreaterThan(watcherStart)
    expect(reconnectWatcher).toContain('pendingFeatureToggleRefresh = true')
    expect(reconnectWatcher).toContain('flushSessionOptionalReads()')
    const flushStart = chatViewSource.indexOf('function flushSessionOptionalReads()')
    const flushEnd = chatViewSource.indexOf('function scheduleSessionOptionalReads(', flushStart)
    const admittedRefresh = chatViewSource.slice(flushStart, flushEnd)
    expect(flushStart).toBeGreaterThanOrEqual(0)
    expect(flushEnd).toBeGreaterThan(flushStart)
    expect(admittedRefresh).toContain('if (!optionalSessionRpcAllowed.value) return')
    expect(admittedRefresh).toContain('if (pendingFeatureToggleRefresh) {')
    expect(admittedRefresh).toContain('if (postBootstrapMetadataStarted) void loadFeatureToggles()')
  })

  it.each([
    [{}, 'off', false, false],
    [{ squilla_router: { enabled: true, rollout_phase: 'observe' } }, 'off', false, false],
    [{ squilla_router: { enabled: true, rollout_phase: 'full' } }, 'squilla_router', true, false],
    [{ squilla_router: { enabled: false }, llm_ensemble: { enabled: true } }, 'llm_ensemble', true, true],
    [{ squilla_router: { enabled: true, rollout_phase: 'full' }, llm_ensemble: { enabled: true } }, 'llm_ensemble', true, true],
  ])('maps backend config %j to mode %s', async (config, mode, routerActive, ensembleActive) => {
    const { api } = createHarness({
      configGetResults: [config],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingMode.value).toBe(mode)
    expect(api.routerEnabled.value).toBe(routerActive)
    expect(api.llmEnsembleEnabled.value).toBe(ensembleActive)
  })

  it('maps tier-scoped ensemble execution independently from the global mode', async () => {
    const { api } = createHarness({
      configGetResults: [{
        squilla_router: {
          enabled: true,
          rollout_phase: 'full',
          tiers: {
            c2: {
              model: 'z-ai/glm-5.2',
              ensemble_selection_mode: 'router_dynamic',
            },
            c3: {
              model: 'anthropic/claude-opus-4.8',
              ensemble_enabled: false,
              ensemble_selection_mode: 'router_dynamic',
            },
          },
        },
        llm_ensemble: { enabled: false },
      }],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.routerTierConfigs.value.c2?.ensembleEnabled).toBe(true)
    expect(api.routerTierConfigs.value.c3?.ensembleEnabled).toBe(false)
  })

  it.each<[ModelRoutingMode, string]>([
    ['off', 'direct'],
    ['squilla_router', 'router'],
    ['llm_ensemble', 'ensemble'],
  ])('writes %s through the canonical Gateway state machine', async (mode, gatewayMode) => {
    const { api, rpc } = createHarness({
      configGetResults: [{}, {}],
    })

    await api.loadFeatureToggles()
    await api.setModelRoutingMode(mode)

    expect(rpc.call).toHaveBeenCalledWith('models.routing.set', { mode: gatewayMode })
  })

  it('optimistically reflects the selected routing mode while a write is pending', async () => {
    const pendingPatch = deferred<void>()
    const { api } = createHarness({
      patchResults: [pendingPatch.promise],
      configGetResults: [{ squilla_router: { enabled: true, rollout_phase: 'full' }, llm_ensemble: { enabled: true } }],
    })

    const write = api.setModelRoutingMode('llm_ensemble')
    await Promise.resolve()

    expect(api.modelRoutingSettingsBusy.value).toBe(true)
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.routerEnabled.value).toBe(true)
    expect(api.llmEnsembleEnabled.value).toBe(true)

    pendingPatch.resolve(undefined)
    await write
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
  })

  it('rolls back model routing when the backend patch fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { api } = createHarness({
      configGetResults: [{ squilla_router: { enabled: true, rollout_phase: 'full' } }],
      patchResults: [new Error('patch failed')],
    })

    await api.loadFeatureToggles()
    await api.setModelRoutingMode('off')

    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(warn).toHaveBeenCalledWith('Failed to update model routing:', 'patch failed')
  })

  it('uses the post-patch backend value as authoritative', async () => {
    const { api } = createHarness({
      configGetResults: [{ squilla_router: { enabled: false }, llm_ensemble: { enabled: false } }],
    })

    await api.setModelRoutingMode('llm_ensemble')

    expect(api.modelRoutingMode.value).toBe('off')
  })

  it('prevents overlapping model-routing writes while busy', async () => {
    const pendingPatch = deferred<void>()
    const { api, rpc } = createHarness({
      patchResults: [pendingPatch.promise],
      configGetResults: [{ squilla_router: { enabled: true, rollout_phase: 'full' }, llm_ensemble: { enabled: true } }],
    })

    const firstWrite = api.setModelRoutingMode('llm_ensemble')
    await api.setModelRoutingMode('off')
    await Promise.resolve()

    expect(routingCalls(rpc)).toHaveLength(1)
    expect(rpc.call).toHaveBeenCalledWith('models.routing.set', { mode: 'ensemble' })

    pendingPatch.resolve(undefined)
    await firstWrite
  })

  it('does not persist model routing through browser storage APIs', () => {
    const setterStart = source.indexOf('async function setModelRoutingMode')
    const setterEnd = source.indexOf('function bindFeatureRefresh', setterStart)
    const setterSource = source.slice(setterStart, setterEnd)

    expect(setterSource).toContain('options.modelRouting.setRouting')
    expect(setterSource).not.toMatch(/localStorage|sessionStorage/)
  })
})
