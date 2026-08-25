import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionRouting } from './useChatSessionRouting'
import type { ModelRoutingMode } from '@/types/modelRouting'

const SESSION_ONE = 'agent:main:webchat:one'
const SESSION_TWO = 'agent:main:webchat:two'

function harness(options: {
  globalMode?: ModelRoutingMode
  draft?: boolean
  getResponse?: unknown
  available?: boolean
} = {}) {
  const handlers = new Map<string, (payload: unknown) => void>()
  const rpc = {
    call: vi.fn().mockResolvedValue(options.getResponse),
    on: vi.fn((event: string, handler: (payload: unknown) => void) => {
      handlers.set(event, handler)
      return vi.fn()
    }),
  }
  const sessionKey = ref(SESSION_ONE)
  const globalMode = ref<ModelRoutingMode>(options.globalMode ?? 'off')
  const isStreaming = ref(false)
  const isDraft = ref(options.draft === true)
  const available = ref(options.available !== false)
  const notifyError = vi.fn()
  const api = useChatSessionRouting({
    rpc,
    sessionKey,
    globalMode,
    available,
    isStreaming,
    isDraft: () => isDraft.value,
    notifyError,
  })
  return { api, available, globalMode, handlers, isDraft, isStreaming, notifyError, rpc, sessionKey }
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
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalled())
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
      rpc,
      sessionKey: ref(SESSION_ONE),
      globalMode: ref<ModelRoutingMode>('off'),
      isStreaming: ref(false),
      isDraft: () => false,
      notifyError: vi.fn(),
    })

    const selected = api.setMode('off')
    await vi.waitFor(() => expect(pendingGets.length).toBeGreaterThanOrEqual(2))
    expect(api.busy.value).toBe(true)
    await expect(api.setMode('llm_ensemble')).resolves.toBe(false)
    expect(pendingGets).toHaveLength(2)
    pendingGets.forEach(resolve => resolve({ key: SESSION_ONE, mode: 'ensemble', revision: 0 }))

    await expect(selected).resolves.toBe(true)
    expect(api.busy.value).toBe(false)
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
})
