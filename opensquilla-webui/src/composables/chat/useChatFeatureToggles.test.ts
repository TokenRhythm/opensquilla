import { afterEach, describe, expect, it, vi } from 'vitest'
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

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function createHarness(options: {
  configGetResults?: RpcResult[]
  routingGetResults?: RpcResult[]
  patchResults?: RpcResult[]
  readCallOptions?: RpcCallOptions
  hasRpcMethod?: (method: string) => boolean
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

function patchCalls(rpc: ReturnType<typeof createHarness>['rpc']) {
  return rpc.call.mock.calls.filter(([method]) => method === 'config.patch.safe')
}

function routingCalls(rpc: ReturnType<typeof createHarness>['rpc']) {
  return rpc.call.mock.calls.filter(([method]) => method === 'models.routing.set')
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useChatFeatureToggles coding mode', () => {
  it('reads enabled coding mode from backend config', async () => {
    const readCallOptions: RpcCallOptions = {
      timeoutMs: 2_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    }
    const { api, rpc } = createHarness({
      configGetResults: [{ skills: { coding_mode: true } }],
      readCallOptions,
    })

    await api.loadFeatureToggles()

    expect(api.codingModeEnabled.value).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith(
      'config.get',
      undefined,
      readCallOptions,
    )
  })

  it.each([
    {},
    { skills: {} },
  ])('defaults missing coding mode to off for %j', async (config) => {
    const { api } = createHarness({
      configGetResults: [config],
    })

    await api.loadFeatureToggles()

    expect(api.codingModeEnabled.value).toBe(false)
  })

  it('writes coding mode on with the safe backend patch path', async () => {
    const { api, rpc } = createHarness({
      configGetResults: [{ skills: { coding_mode: true } }],
    })

    await api.setCodingModeEnabled(true)

    expect(rpc.call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'skills.coding_mode': true },
    })
  })

  it('writes coding mode off with the safe backend patch path', async () => {
    const { api, rpc } = createHarness({
      configGetResults: [{ skills: { coding_mode: false } }],
    })

    await api.setCodingModeEnabled(false)

    expect(rpc.call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'skills.coding_mode': false },
    })
  })

  it('strictly reloads backend config after a successful write', async () => {
    const { api, rpc } = createHarness({
      configGetResults: [{ skills: { coding_mode: true } }],
    })

    await api.setCodingModeEnabled(true)

    const calls = rpc.call.mock.calls
    const patchIndex = calls.findIndex(([method]) => method === 'config.patch.safe')
    const getIndex = calls.findIndex(([method], index) => index > patchIndex && method === 'config.get')
    expect(patchIndex).toBeGreaterThanOrEqual(0)
    expect(getIndex).toBeGreaterThan(patchIndex)
    expect(api.codingModeEnabled.value).toBe(true)
  })

  it('applies the strict post-patch config through the shared feature mapping', async () => {
    const { api, setGlobalElevatedMode } = createHarness({
      configGetResults: [{
        skills: { coding_mode: true },
        squilla_router: { enabled: true, rollout_phase: 'full' },
        permissions: { default_mode: 'bypass' },
      }],
    })

    await api.setCodingModeEnabled(true)

    expect(api.codingModeEnabled.value).toBe(true)
    expect(api.routerEnabled.value).toBe(true)
    expect(setGlobalElevatedMode).toHaveBeenCalledWith('bypass')
  })

  it('keeps coding mode backend-confirmed while a write is pending', async () => {
    const pendingPatch = deferred<void>()
    const { api } = createHarness({
      patchResults: [pendingPatch.promise],
      configGetResults: [{ skills: { coding_mode: true } }],
    })

    const write = api.setCodingModeEnabled(true)
    await Promise.resolve()

    expect(api.codingModeSettingsBusy.value).toBe(true)
    expect(api.codingModeEnabled.value).toBe(false)

    pendingPatch.resolve(undefined)
    await write
    expect(api.codingModeEnabled.value).toBe(true)
  })

  it('rolls back when the backend patch fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { api } = createHarness({
      patchResults: [new Error('patch failed')],
    })

    await api.setCodingModeEnabled(true)

    expect(api.codingModeEnabled.value).toBe(false)
    expect(warn).toHaveBeenCalledWith('Failed to update Coding mode:', 'patch failed')
  })

  it('rolls back when post-patch config reload fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { api } = createHarness({
      configGetResults: [new Error('reload failed')],
    })

    await api.setCodingModeEnabled(true)

    expect(api.codingModeEnabled.value).toBe(false)
    expect(warn).toHaveBeenCalledWith('Failed to update Coding mode:', 'reload failed')
  })

  it('uses the post-patch backend value as authoritative', async () => {
    const { api } = createHarness({
      configGetResults: [{ skills: { coding_mode: false } }],
    })

    await api.setCodingModeEnabled(true)

    expect(api.codingModeEnabled.value).toBe(false)
  })

  it('prevents overlapping coding mode writes while busy', async () => {
    const pendingPatch = deferred<void>()
    const { api, rpc } = createHarness({
      patchResults: [pendingPatch.promise],
      configGetResults: [{ skills: { coding_mode: true } }],
    })

    const firstWrite = api.setCodingModeEnabled(true)
    await api.setCodingModeEnabled(false)
    await Promise.resolve()

    expect(patchCalls(rpc)).toHaveLength(1)
    expect(rpc.call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'skills.coding_mode': true },
    })

    pendingPatch.resolve(undefined)
    await firstWrite
  })

  it('does not persist coding mode through browser storage APIs', () => {
    const setterStart = source.indexOf('async function setCodingModeEnabled')
    const setterEnd = source.indexOf('function bindFeatureRefresh', setterStart)
    const setterSource = source.slice(setterStart, setterEnd)

    expect(setterSource).toContain('skills.coding_mode')
    expect(setterSource).not.toMatch(/localStorage|sessionStorage/)
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

describe('useChatFeatureToggles model routing mode', () => {
  it('uses the canonical routing snapshot over legacy config inference', async () => {
    const { api } = createHarness({
      configGetResults: [{ squilla_router: { enabled: true, rollout_phase: 'full' } }],
      routingGetResults: [{ mode: 'direct' }],
    })

    await api.loadFeatureToggles()

    expect(api.modelRoutingMode.value).toBe('off')
  })

  it('applies canonical image admission and preserves old-Gateway defaults', async () => {
    const blocked = createHarness({
      configGetResults: [{}],
      routingGetResults: [{
        mode: 'direct',
        image_input: {
          admission: 'blocked',
          reason: 'model_vision_unsupported',
        },
      }],
    })
    await blocked.api.loadFeatureToggles()
    expect(blocked.api.globalImageInputAdmission.value).toBe('blocked')
    expect(blocked.api.globalImageInputAdmissionReason.value).toBe('model_vision_unsupported')

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
    expect(legacyEnsemble.api.globalImageInputAdmission.value).toBe('blocked')
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

    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(CAPABILITIES_BY_MODE)
    expect(api.globalImageInputAdmission.value).toBe('blocked')
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
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(CAPABILITIES_BY_MODE)

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()

    await api.loadFeatureToggles()
    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
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
          skills: { coding_mode: false },
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
      skills: { coding_mode: true },
      llm_ensemble: { enabled: true },
    })
    await firstLoad

    expect(api.codingModeEnabled.value).toBe(false)
    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(CAPABILITIES_BY_MODE)
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
    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(CAPABILITIES_BY_MODE)

    supportsRouting = false
    await api.loadFeatureToggles()

    expect(api.modelRoutingCapabilitiesByMode.value).toBeNull()
    expect(api.modelRoutingMode.value).toBe('llm_ensemble')
    expect(api.globalImageInputAdmission.value).toBe('blocked')
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

    expect(api.modelRoutingCapabilitiesByMode.value).toEqual(CAPABILITIES_BY_MODE)
    expect(api.modelRoutingMode.value).toBe('squilla_router')
    expect(api.globalImageInputAdmission.value).toBe('allowed')
  })

  it('refreshes the capability matrix when the chat reconnects', () => {
    const watcherStart = chatViewSource.indexOf('// Hello refreshes method capabilities on reconnect')
    const watcherEnd = chatViewSource.indexOf('watch(shareableMessageCount', watcherStart)
    const reconnectWatcher = chatViewSource.slice(watcherStart, watcherEnd)

    expect(watcherStart).toBeGreaterThanOrEqual(0)
    expect(watcherEnd).toBeGreaterThan(watcherStart)
    expect(reconnectWatcher).toContain('void loadFeatureToggles()')
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
    expect(source).toContain('options.appSettings.patchSafe')
    expect(setterSource).not.toMatch(/localStorage|sessionStorage/)
  })
})
