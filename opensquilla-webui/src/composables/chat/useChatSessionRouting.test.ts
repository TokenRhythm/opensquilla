import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionRouting } from './useChatSessionRouting'
import type {
  ImageInputAdmission,
  ModelRoutingCapabilitiesByMode,
  ModelRoutingMode,
} from '@/types/modelRouting'
import type { SessionRouting } from '@/modules/sessionRouting'

const SESSION_ONE = 'agent:main:webchat:one'
const SESSION_TWO = 'agent:main:webchat:two'

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

function harness(options: {
  globalMode?: ModelRoutingMode
  draft?: boolean
  getResponse?: unknown
  available?: boolean
  modelSelectionCapable?: boolean
  globalImageInputAdmission?: ImageInputAdmission
  globalImageInputAdmissionReason?: string
  capabilitiesByMode?: ModelRoutingCapabilitiesByMode | null
} = {}) {
  const handlers = new Map<string, (payload: unknown) => void>()
  const rpc = {
    call: vi.fn().mockResolvedValue(options.getResponse),
    on: vi.fn((event: string, handler: (payload: unknown) => void) => {
      handlers.set(event, handler)
      return vi.fn()
    }),
  }
  const routing = {
    available: () => true,
    get: (key: string, options?: { signal?: AbortSignal }) => options
      ? rpc.call('sessions.routing.get', { sessionKey: key }, options)
      : rpc.call('sessions.routing.get', { sessionKey: key }),
    set: (input: { sessionKey: string; mode: string; expectedRevision: number }, options?: { signal?: AbortSignal }) => options
      ? rpc.call('sessions.routing.set', input, options)
      : rpc.call('sessions.routing.set', input),
    subscribe: (handler: (payload: unknown) => void) => ({ close: rpc.on('sessions.routing.changed', handler) }),
  } as unknown as SessionRouting
  const sessionKey = ref(SESSION_ONE)
  const globalMode = ref<ModelRoutingMode>(options.globalMode ?? 'off')
  const globalImageInputAdmission = ref<ImageInputAdmission>(
    options.globalImageInputAdmission ?? 'unknown',
  )
  const globalImageInputAdmissionReason = ref(
    options.globalImageInputAdmissionReason ?? 'capability_unknown',
  )
  const capabilitiesByMode = ref<ModelRoutingCapabilitiesByMode | null>(
    options.capabilitiesByMode ?? null,
  )
  const isStreaming = ref(false)
  const isDraft = ref(options.draft === true)
  const available = ref(options.available !== false)
  const modelSelectionCapable = ref(options.modelSelectionCapable === true)
  const connectionEpoch = ref(1)
  const notifyError = vi.fn()
  const api = useChatSessionRouting({
    routing,
    sessionKey,
    globalMode,
    globalImageInputAdmission,
    globalImageInputAdmissionReason,
    capabilitiesByMode,
    available,
    modelSelectionCapable,
    connectionEpoch,
    isStreaming,
    isDraft: () => isDraft.value,
    notifyError,
  })
  return {
    api,
    available,
    modelSelectionCapable,
    connectionEpoch,
    capabilitiesByMode,
    globalImageInputAdmission,
    globalImageInputAdmissionReason,
    globalMode,
    handlers,
    isDraft,
    isStreaming,
    notifyError,
    rpc,
    sessionKey,
  }
}

describe('useChatSessionRouting', () => {
  it('does not send the global placeholder as an explicit draft override', async () => {
    const { api, rpc } = harness({ draft: true, globalMode: 'llm_ensemble' })

    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.initialRoutingMode.value).toBeNull()

    await expect(api.setMode('llm_ensemble')).resolves.toBe(true)

    expect(api.initialRoutingMode.value).toBe('ensemble')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('accepts an existing revision-zero session mode over its global placeholder', async () => {
    const { api, rpc } = harness({
      globalMode: 'off',
      getResponse: {
        key: SESSION_ONE,
        mode: 'ensemble',
        revision: 0,
      },
    })

    await api.load()

    expect(rpc.call).toHaveBeenCalledWith('sessions.routing.get', { sessionKey: SESSION_ONE })
    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.revision.value).toBe(0)
    expect(api.hasAuthoritativeSnapshot.value).toBe(true)
  })

  it('keeps a draft selection local and supplies its raw mode for first-send creation', async () => {
    const { api, rpc } = harness({ draft: true, globalMode: 'squilla_router' })

    await expect(api.setMode('llm_ensemble')).resolves.toBe(true)

    expect(rpc.call).not.toHaveBeenCalled()
    expect(api.initialRoutingMode.value).toBe('ensemble')
  })

  it('does not let a late draft bootstrap replace an explicit first-send mode', async () => {
    const { api } = harness({ draft: true, globalMode: 'off' })

    await expect(api.setMode('llm_ensemble')).resolves.toBe(true)
    expect(api.applyBootstrap({ mode: 'direct', revision: 0 })).toBe(false)

    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.initialRoutingMode.value).toBe('ensemble')
  })

  it('fails closed when session routing is unavailable', async () => {
    const { api, available, rpc } = harness({ draft: true, globalMode: 'squilla_router' })

    available.value = false
    await expect(api.setMode('llm_ensemble')).resolves.toBe(false)

    expect(api.initialRoutingMode.value).toBeNull()
    expect(api.mode.value).toBe('squilla_router')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('preserves an explicit draft selection across a disconnect', async () => {
    const { api, available, rpc } = harness({ draft: true, globalMode: 'off' })

    await expect(api.setMode('llm_ensemble')).resolves.toBe(true)
    available.value = false

    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.initialRoutingMode.value).toBeNull()

    available.value = true

    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.initialRoutingMode.value).toBe('ensemble')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('freezes a draft selection while its first turn is being accepted', async () => {
    const { api, isStreaming, rpc } = harness({ draft: true, globalMode: 'off' })

    await expect(api.setMode('squilla_router')).resolves.toBe(true)
    isStreaming.value = true

    expect(api.busy.value).toBe(true)
    expect(api.mutationBusy.value).toBe(false)
    await expect(api.setMode('llm_ensemble')).resolves.toBe(false)
    expect(api.mode.value).toBe('squilla_router')
    expect(api.initialRoutingMode.value).toBe('router')
    expect(rpc.call).not.toHaveBeenCalled()

    isStreaming.value = false

    expect(api.busy.value).toBe(false)
    await expect(api.setMode('llm_ensemble')).resolves.toBe(true)
    expect(api.initialRoutingMode.value).toBe('ensemble')
  })

  it('accepts authorized bootstrap and routing events for a read-only session', () => {
    const { api, handlers, rpc } = harness({ available: false, globalMode: 'off' })

    expect(api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 2 })).toBe(true)
    expect(api.mode.value).toBe('squilla_router')
    expect(api.revision.value).toBe(2)
    expect(api.hasAuthoritativeSnapshot.value).toBe(true)

    api.subscribe()
    handlers.get('sessions.routing.changed')?.({
      key: SESSION_ONE,
      mode: 'ensemble',
      revision: 3,
    })

    expect(api.mode.value).toBe('llm_ensemble')
    expect(api.revision.value).toBe(3)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('uses the canonical sessionKey field and CAS revision for durable updates', async () => {
    const { api, rpc } = harness({ getResponse: undefined })
    api.applyBootstrap({ key: SESSION_ONE, mode: 'direct', revision: 0 })
    rpc.call.mockImplementation((method: string) => {
      if (method === 'sessions.routing.set') {
        return Promise.resolve({
          key: SESSION_ONE,
          mode: 'router',
          revision: 1,
        })
      }
      return Promise.resolve(undefined)
    })

    await expect(api.setMode('squilla_router')).resolves.toBe(true)

    const setCalls = rpc.call.mock.calls.filter(([method]) => method === 'sessions.routing.set')
    expect(setCalls).toEqual([['sessions.routing.set', {
      sessionKey: SESSION_ONE,
      mode: 'router',
      expectedRevision: 0,
    }]])
    expect(api.mode.value).toBe('squilla_router')
    expect(api.revision.value).toBe(1)
  })

  it('keeps a repeated durable selection out of the busy mutation path', async () => {
    const { api, rpc } = harness()
    api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 2 })
    rpc.call.mockClear()

    await expect(api.setMode('squilla_router')).resolves.toBe(true)

    expect(api.busy.value).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('holds the mutation lock through initial hydration before its CAS write', async () => {
    const pendingGets: Array<(value: unknown) => void> = []
    const call = vi.fn((method: string, _params?: Record<string, unknown>) => {
      if (method === 'sessions.routing.get') {
        return new Promise(resolve => { pendingGets.push(resolve) })
      }
      return Promise.resolve({ key: SESSION_ONE, mode: 'direct', revision: 1 })
    })
    const rpc = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        call(method, params) as Promise<T>
      ),
      on: vi.fn(() => vi.fn()),
    }
    const api = useChatSessionRouting({
      routing: {
        available: () => true,
        get: key => rpc.call('sessions.routing.get', { sessionKey: key }),
        set: input => rpc.call('sessions.routing.set', input as unknown as Record<string, unknown>),
        subscribe: _handler => ({ close: rpc.on() }),
        dispose: () => undefined,
      },
      sessionKey: ref(SESSION_ONE),
      globalMode: ref<ModelRoutingMode>('off'),
      globalImageInputAdmission: ref<ImageInputAdmission>('unknown'),
      globalImageInputAdmissionReason: ref('capability_unknown'),
      capabilitiesByMode: ref(null),
      isStreaming: ref(false),
      isDraft: () => false,
      notifyError: vi.fn(),
    })

    const selected = api.setMode('off')
    await vi.waitFor(() => expect(pendingGets).toHaveLength(1))
    expect(api.busy.value).toBe(true)
    expect(api.mutationBusy.value).toBe(true)
    await expect(api.setMode('llm_ensemble')).resolves.toBe(false)
    expect(pendingGets).toHaveLength(1)
    pendingGets.forEach(resolve => resolve({ key: SESSION_ONE, mode: 'ensemble', revision: 0 }))

    await expect(selected).resolves.toBe(true)
    expect(api.busy.value).toBe(false)
    expect(api.mutationBusy.value).toBe(false)
    expect(call).toHaveBeenCalledWith('sessions.routing.set', {
      sessionKey: SESSION_ONE,
      mode: 'direct',
      expectedRevision: 0,
    })
  })

  it('does not let equal-revision conflicting events replace an authoritative mode', () => {
    const { api, handlers } = harness()
    api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 0 })
    api.subscribe()

    handlers.get('sessions.routing.changed')?.({
      key: SESSION_ONE,
      mode: 'direct',
      revision: 0,
    })
    handlers.get('sessions.routing.changed')?.({
      key: SESSION_TWO,
      mode: 'ensemble',
      revision: 1,
    })

    expect(api.mode.value).toBe('squilla_router')
    expect(api.revision.value).toBe(0)
  })

  it('selects image admission from the current session mode matrix', () => {
    const { api } = harness({
      globalMode: 'llm_ensemble',
      globalImageInputAdmission: 'blocked',
      globalImageInputAdmissionReason: 'ensemble_mode_unsupported',
      capabilitiesByMode: CAPABILITIES_BY_MODE,
    })

    api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 2 })

    expect(api.imageInputAdmission.value).toBe('allowed')
    expect(api.imageInputAdmissionReason.value).toBe('router_image_route_available')
  })

  it('blocks when the session switches from a globally allowed mode to ensemble', () => {
    const { api } = harness({
      globalMode: 'off',
      globalImageInputAdmission: 'allowed',
      globalImageInputAdmissionReason: 'model_vision_supported',
      capabilitiesByMode: CAPABILITIES_BY_MODE,
    })

    api.applyBootstrap({ key: SESSION_ONE, mode: 'ensemble', revision: 1 })

    expect(api.imageInputAdmission.value).toBe('blocked')
    expect(api.imageInputAdmissionReason.value).toBe('ensemble_mode_unsupported')
  })

  it('uses the matrix for an explicit non-global draft mode', async () => {
    const { api } = harness({
      draft: true,
      globalMode: 'llm_ensemble',
      capabilitiesByMode: CAPABILITIES_BY_MODE,
    })

    await api.setMode('squilla_router')

    expect(api.initialRoutingMode.value).toBe('router')
    expect(api.imageInputAdmission.value).toBe('allowed')
  })

  it('uses a legacy scalar only when session and global modes match', () => {
    const matching = harness({
      globalMode: 'llm_ensemble',
      globalImageInputAdmission: 'blocked',
      globalImageInputAdmissionReason: 'ensemble_mode_unsupported',
    })
    expect(matching.api.imageInputAdmission.value).toBe('blocked')

    matching.api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 1 })

    expect(matching.api.imageInputAdmission.value).toBe('unknown')
    expect(matching.api.imageInputAdmissionReason.value).toBe('capability_unknown')
  })

  it('recomputes capability updates without changing the session revision', () => {
    const { api, capabilitiesByMode } = harness({
      capabilitiesByMode: CAPABILITIES_BY_MODE,
    })
    api.applyBootstrap({ key: SESSION_ONE, mode: 'router', revision: 4 })
    expect(api.imageInputAdmission.value).toBe('allowed')

    capabilitiesByMode.value = {
      ...CAPABILITIES_BY_MODE,
      router: {
        image_input: {
          admission: 'blocked',
          reason: 'router_image_route_unavailable',
        },
      },
    }

    expect(api.revision.value).toBe(4)
    expect(api.imageInputAdmission.value).toBe('blocked')
    expect(api.imageInputAdmissionReason.value).toBe('router_image_route_unavailable')
  })
})


const PIN = { model: 'shared-model-name', provider: 'provider-one' }
const OTHER_PIN = { model: 'shared-model-name', provider: 'provider-two' }
function modelSnapshot(selection: { model: string; provider: string | null } | null, revision = 1, mode = 'direct') {
  return { key: SESSION_ONE, mode, revision, modelSelection: selection }
}
function pending<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

describe('durable session model selection', () => {
  it('replaces a provisional default with the accepted first-turn pin under the same key and revision', async () => {
    const h = harness({ draft: true, modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(null, 0))
    await h.api.setMode('off')
    h.isStreaming.value = true
    h.rpc.call.mockResolvedValue(modelSnapshot(PIN, 0))

    // Acceptance keeps the draft key. No navigation, reload or menu refresh
    // occurs, and the old provisional revision cannot reject the saved pin.
    h.isDraft.value = false
    expect(h.api.hasAuthoritativeSnapshot.value).toBe(false)
    expect(h.api.mode.value).toBe('off')
    expect(h.api.initialRoutingMode.value).toBeNull()
    expect(h.api.modelSelectionSupported.value).toBe(false)
    expect(h.rpc.call).not.toHaveBeenCalled()

    // ChatView loads only after the durable bootstrap's critical frames.
    await expect(h.api.load()).resolves.toBe(true)
    expect(h.rpc.call).toHaveBeenCalledExactlyOnceWith('sessions.routing.get', { sessionKey: SESSION_ONE })
    expect(h.api.modelSelection.value).toEqual(PIN)
    expect(h.api.modelSelectionSupported.value).toBe(true)
    expect(h.api.revision.value).toBe(0)
  })

  it.each(['direct', 'router', 'ensemble'] as const)(
    'accepts the durable %s bootstrap after retiring a same-key provisional snapshot', async mode => {
      const h = harness({ draft: true, modelSelectionCapable: true })
      h.api.applyBootstrap(modelSnapshot(null, 0))
      const selectedMode = mode === 'direct' ? 'off' : mode === 'router' ? 'squilla_router' : 'llm_ensemble'
      await h.api.setMode(selectedMode)
      h.isDraft.value = false

      expect(h.api.mode.value).toBe(selectedMode)
      expect(h.api.applyBootstrap(modelSnapshot(mode === 'direct' ? PIN : null, 0, mode))).toBe(true)
      expect(h.api.mode.value).toBe(selectedMode)
      expect(h.api.modelSelection.value).toEqual(mode === 'direct' ? PIN : null)
      expect(h.api.hasAuthoritativeSnapshot.value).toBe(true)
      expect(h.rpc.call).not.toHaveBeenCalled()
    },
  )

  it('does not carry an accepted draft mode or late pin read across navigation', async () => {
    const h = harness({ draft: true, modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(null, 0))
    await h.api.setMode('llm_ensemble')
    h.isDraft.value = false
    const read = pending<unknown>()
    h.rpc.call.mockReturnValueOnce(read.promise)
    const loading = h.api.load()

    h.sessionKey.value = SESSION_TWO
    h.api.applyBootstrap({ ...modelSnapshot(OTHER_PIN, 0), key: SESSION_TWO })
    read.resolve(modelSnapshot(PIN, 0, 'ensemble'))

    await expect(loading).resolves.toBe(false)
    expect(h.api.mode.value).toBe('off')
    expect(h.api.modelSelection.value).toEqual(OTHER_PIN)
    expect(h.notifyError).not.toHaveBeenCalled()
  })

  it('requires both advertised capability and a model-aware authoritative snapshot', () => {
    const h = harness({ modelSelectionCapable: true })
    expect(h.api.modelSelectionSupported.value).toBe(false)
    h.api.applyBootstrap({ key: SESSION_ONE, mode: 'direct', revision: 0 })
    expect(h.api.modelSelectionSupported.value).toBe(false)
    h.api.applyBootstrap(modelSnapshot(null))
    expect(h.api.modelSelectionSupported.value).toBe(true)
    h.modelSelectionCapable.value = false
    expect(h.api.modelSelectionSupported.value).toBe(false)
  })

  it('updates provider and direct mode atomically without an optimistic model label', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN, 2, 'router'))
    const request = pending<unknown>()
    h.rpc.call.mockReturnValue(request.promise)
    const updating = h.api.setModel(OTHER_PIN)
    expect(h.api.modelSelection.value).toEqual(PIN)
    expect(h.api.mode.value).toBe('squilla_router')
    expect(h.api.mutationBusy.value).toBe(true)
    await expect(h.api.setModel(PIN)).resolves.toBe(false)
    await expect(h.api.setMode('llm_ensemble')).resolves.toBe(false)
    expect(h.rpc.call).toHaveBeenCalledExactlyOnceWith('sessions.routing.set', {
      sessionKey: SESSION_ONE, mode: 'direct', expectedRevision: 2, modelSelection: OTHER_PIN,
    }, { signal: expect.any(AbortSignal) })
    request.resolve(modelSnapshot(OTHER_PIN, 3))
    await expect(updating).resolves.toBe(true)
    expect(h.api.modelSelection.value).toEqual(OTHER_PIN)
    expect(h.api.mode.value).toBe('off')
    expect(h.api.busy.value).toBe(false)
  })

  it('resets a pin to defaults with explicit null instead of dropping the field', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.rpc.call.mockResolvedValue(modelSnapshot(null, 2))
    await expect(h.api.setModel(null)).resolves.toBe(true)
    expect(h.rpc.call).toHaveBeenCalledWith('sessions.routing.set', {
      sessionKey: SESSION_ONE, mode: 'direct', expectedRevision: 1, modelSelection: null,
    }, { signal: expect.any(AbortSignal) })
    expect(h.api.modelSelection.value).toBeNull()
  })

  it('preserves the direct-model pin when choosing a route strategy', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.rpc.call.mockResolvedValue(modelSnapshot(PIN, 2, 'ensemble'))
    await expect(h.api.setMode('llm_ensemble')).resolves.toBe(true)
    expect(h.rpc.call).toHaveBeenCalledExactlyOnceWith('sessions.routing.set', {
      sessionKey: SESSION_ONE, mode: 'ensemble', expectedRevision: 1,
    })
    expect(h.api.modelSelection.value).toEqual(PIN)
  })

  it('treats model and provider together as identity and avoids writes for the same pair', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    await expect(h.api.setModel({ ...PIN })).resolves.toBe(true)
    expect(h.rpc.call).not.toHaveBeenCalled()
    h.rpc.call.mockResolvedValue(modelSnapshot(OTHER_PIN, 2))
    await expect(h.api.setModel(OTHER_PIN)).resolves.toBe(true)
    expect(h.rpc.call).toHaveBeenCalledOnce()
  })

  it('retains legacy model-only identity without inventing its provider or marking it default', () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot({ model: PIN.model, provider: null }))
    expect(h.api.modelSelection.value).toEqual({ model: PIN.model, provider: null })
    expect(h.api.modelSelectionSupported.value).toBe(true)
  })

  it.each([
    { model: '', provider: 'provider' }, { model: ' model', provider: 'provider' },
    { model: 'model', provider: '' }, { model: 'model', provider: 'bad\nprovider' },
  ])('does not dispatch an invalid provider/model pair %j', async selection => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(null))
    await expect(h.api.setModel(selection)).resolves.toBe(false)
    expect(h.rpc.call).not.toHaveBeenCalled()
  })

  it.each(['draft', 'unsupported', 'streaming', 'disconnected'] as const)(
    'does not mutate a concrete model while %s', async state => {
      const h = harness({ modelSelectionCapable: true })
      h.api.applyBootstrap(modelSnapshot(PIN))
      if (state === 'draft') h.isDraft.value = true
      if (state === 'unsupported') h.modelSelectionCapable.value = false
      if (state === 'streaming') h.isStreaming.value = true
      if (state === 'disconnected') h.available.value = false
      await expect(h.api.setModel(OTHER_PIN)).resolves.toBe(false)
      await expect(h.api.setModel(null)).resolves.toBe(false)
      expect(h.rpc.call).not.toHaveBeenCalled()
    },
  )

  it('keeps existing next-turn route switching available while a turn is streaming', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.isStreaming.value = true
    h.rpc.call.mockResolvedValue(modelSnapshot(PIN, 2, 'router'))
    await expect(h.api.setMode('squilla_router')).resolves.toBe(true)
    expect(h.api.modeAppliesNextTurn.value).toBe(true)
    expect(h.api.modelSelection.value).toEqual(PIN)
  })

  it('hydrates model support before an atomic mutation and holds the shared lock', async () => {
    const h = harness({ modelSelectionCapable: true })
    const read = pending<unknown>()
    h.rpc.call.mockReturnValueOnce(read.promise).mockResolvedValueOnce(modelSnapshot(PIN, 2))
    const selection = h.api.setModel(PIN)
    expect(h.api.busy.value).toBe(true)
    await expect(h.api.setMode('llm_ensemble')).resolves.toBe(false)
    read.resolve(modelSnapshot(null))
    await expect(selection).resolves.toBe(true)
    expect(h.api.modelSelection.value).toEqual(PIN)
  })

  it('does not write when a legacy gateway read lacks model selection support', async () => {
    const h = harness({ modelSelectionCapable: true, getResponse: { key: SESSION_ONE, mode: 'direct', revision: 0 } })
    await expect(h.api.setModel(PIN)).resolves.toBe(false)
    expect(h.rpc.call).toHaveBeenCalledExactlyOnceWith('sessions.routing.get', { sessionKey: SESSION_ONE })
  })

  it('refreshes authoritative state after a conflict and reports a failed choice', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.rpc.call.mockRejectedValueOnce(new Error('Another window changed this task'))
      .mockResolvedValueOnce(modelSnapshot(null, 3, 'ensemble'))
    await expect(h.api.setModel(OTHER_PIN)).resolves.toBe(false)
    expect(h.api.modelSelection.value).toBeNull()
    expect(h.api.mode.value).toBe('llm_ensemble')
    expect(h.notifyError).toHaveBeenCalledExactlyOnceWith('Another window changed this task')
    expect(h.api.busy.value).toBe(false)
  })

  it('rejects missing acknowledgement and never invents a saved model', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.rpc.call.mockResolvedValueOnce({ key: SESSION_ONE, mode: 'direct', revision: 2 })
      .mockResolvedValueOnce(modelSnapshot(PIN))
    await expect(h.api.setModel(OTHER_PIN)).resolves.toBe(false)
    expect(h.api.modelSelection.value).toEqual(PIN)
    expect(h.notifyError).toHaveBeenCalledOnce()
  })

  it('ignores equal-revision conflicting model events and accepts newer authoritative updates', () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    h.api.subscribe()
    h.handlers.get('sessions.routing.changed')?.(modelSnapshot(OTHER_PIN))
    expect(h.api.modelSelection.value).toEqual(PIN)
    h.handlers.get('sessions.routing.changed')?.(modelSnapshot(OTHER_PIN, 2))
    expect(h.api.modelSelection.value).toEqual(OTHER_PIN)
  })

  it.each(['navigation', 'reconnection', 'disconnect'] as const)(
    'ignores a late model acknowledgement after %s and aborts its transport wait', async cause => {
      const h = harness({ modelSelectionCapable: true })
      h.api.applyBootstrap(modelSnapshot(PIN))
      const write = pending<unknown>()
      h.rpc.call.mockReturnValueOnce(write.promise).mockResolvedValue(modelSnapshot(PIN))
      const selection = h.api.setModel(OTHER_PIN)
      const signal = h.rpc.call.mock.calls[0]?.[2]?.signal as AbortSignal
      if (cause === 'navigation') {
        h.sessionKey.value = SESSION_TWO
        h.sessionKey.value = SESSION_ONE
      } else if (cause === 'reconnection') h.connectionEpoch.value += 1
      else h.available.value = false
      expect(signal.aborted).toBe(true)
      write.resolve(modelSnapshot(OTHER_PIN, 2))
      await expect(selection).resolves.toBe(false)
      expect(h.api.modelSelection.value).not.toEqual(OTHER_PIN)
      expect(h.notifyError).not.toHaveBeenCalled()
    },
  )

  it('does not let an old model write release a new session mutation lock', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    const first = pending<unknown>()
    const second = pending<unknown>()
    h.rpc.call.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const firstSelection = h.api.setModel(OTHER_PIN)
    h.sessionKey.value = SESSION_TWO
    h.api.applyBootstrap({ ...modelSnapshot(null), key: SESSION_TWO })
    const secondSelection = h.api.setModel(PIN)
    first.resolve(modelSnapshot(OTHER_PIN, 2))
    await expect(firstSelection).resolves.toBe(false)
    expect(h.api.busy.value).toBe(true)
    second.resolve({ ...modelSnapshot(PIN, 2), key: SESSION_TWO })
    await expect(secondSelection).resolves.toBe(true)
    expect(h.api.busy.value).toBe(false)
  })

  it('does not show an old failure toast after navigation during error recovery', async () => {
    const h = harness({ modelSelectionCapable: true })
    h.api.applyBootstrap(modelSnapshot(PIN))
    const recovery = pending<unknown>()
    h.rpc.call.mockRejectedValueOnce(new Error('Old session failed')).mockReturnValueOnce(recovery.promise)
    const updating = h.api.setModel(OTHER_PIN)
    await nextTick()
    h.sessionKey.value = SESSION_TWO
    recovery.resolve(modelSnapshot(PIN))
    await expect(updating).resolves.toBe(false)
    expect(h.notifyError).not.toHaveBeenCalled()
  })
})
