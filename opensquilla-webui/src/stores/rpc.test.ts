// @vitest-environment happy-dom

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useRpcStore } from './rpc'
import { createV4GatewayAccess } from '@/adapters/gateway/gatewayAccessV4'

const connectCalls: Array<{ url: string; token?: string }> = []
const clients: Array<{
  emit: (event: string, ...args: unknown[]) => void
  disconnect: ReturnType<typeof vi.fn>
  ready: ReturnType<typeof vi.fn>
  recoverConnectionGeneration: ReturnType<typeof vi.fn>
  ensureConnected: ReturnType<typeof vi.fn>
  notifyResume: ReturnType<typeof vi.fn>
  readonly lifecycle: string
  connectionGeneration: number
}> = []

function deliveryHello(overrides: Record<string, unknown> = {}) {
  return { auth: { principal: {
    role: 'operator', authState: 'authenticated', authenticated: true, isOwner: true,
    scopes: ['operator.read', 'operator.write'], capabilities: ['chat.send', 'host.execute'],
    tokenPublicId: 'desktop', guestOwnerId: null,
    ...overrides,
  } } }
}

vi.mock('@/lib/rpc', () => ({
  RpcClient: class {
    state = 'disconnected'
    get lifecycle() { return this.state === 'disconnected' ? 'stopped' : this.state }
    connectionGeneration = 0
    private listeners = new Map<string, Array<(...args: unknown[]) => void>>()

    constructor() {
      clients.push(this)
    }

    connect(url: string, token?: string) {
      connectCalls.push({ url, token })
      this.connectionGeneration += 1
      this.state = 'connected'
      this.emit('_state', 'connected')
    }

    emit(event: string, ...args: unknown[]) {
      for (const handler of this.listeners.get(event) || []) handler(...args)
    }

    on(event: string, handler: (...args: unknown[]) => void) {
      const handlers = this.listeners.get(event) || []
      handlers.push(handler)
      this.listeners.set(event, handlers)
      return () => {
        this.listeners.set(event, (this.listeners.get(event) || []).filter(h => h !== handler))
      }
    }

    disconnect = vi.fn(() => {
      this.state = 'disconnected'
      this.emit('_state', 'disconnected')
    })
    ready = vi.fn()
    ensureConnected = vi.fn()
    notifyResume = vi.fn()
    recoverConnectionGeneration = vi.fn(() => true)
    call = vi.fn()
  },
}))

describe('rpc link-token bootstrap', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })
  beforeEach(() => {
    vi.stubGlobal('navigator', {
      locks: { request: vi.fn(async (_name: string, action: () => unknown) => action()) },
    })
    setActivePinia(createPinia())
    connectCalls.length = 0
    clients.length = 0
    localStorage.clear()
    sessionStorage.clear()
    delete window.opensquillaDesktop
    window.history.replaceState(null, '', '/control/sessions')
  })

  it('retains proven delivery identity through transport retry but retires blocked or malformed authority', async () => {
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    expect(access.deliveryIdentity).toBeNull()
    store.init()
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    const identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    clients[0].emit('_state', 'disconnected')
    expect(store.auth).toBeNull()
    expect(access.deliveryIdentity).toBe(identity)
    clients[0].emit('_state', 'connecting')
    expect(access.deliveryIdentity).toBe(identity)
    clients[0].emit('_hello', deliveryHello())
    clients[0].emit('_state', 'connected')
    expect(access.deliveryIdentity).toBe(identity)
    clients[0].emit('_hello', deliveryHello({ scopes: 'not-an-authority-set' }))
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_state', 'disconnected')
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_hello', deliveryHello())
    clients[0].emit('_status', { lifecycle: 'blocked', health: 'suspect', reason: 'authentication_mismatch' })
    expect(access.deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('fences explicit browser targets and credentials without serializing either secret', async () => {
    const endpoint = 'ws://synthetic.example/ws?token=private-query-value'
    localStorage.setItem('opensquilla.wsUrl', endpoint)
    sessionStorage.setItem('opensquilla.wsToken', 'private-form-token')
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    clients[0].emit('_hello', deliveryHello({ tokenPublicId: 'legacy' }))
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    let identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    expect(identity).not.toContain('private-query-value')
    expect(identity).not.toContain('private-form-token')
    await store.connect(endpoint, 'private-form-token')
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    expect(access.deliveryIdentity).not.toBeNull()
    expect(access.deliveryIdentity).toBe(identity)
    identity = access.deliveryIdentity
    for (const [url, token] of [
      [endpoint, 'different-private-token'],
      ['ws://another.example/ws', 'different-private-token'],
    ]) {
      await store.connect(url!, token!)
      expect(access.deliveryIdentity).toBeNull()
      clients[0].emit('_hello', deliveryHello({ tokenPublicId: 'legacy' }))
      await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
      expect(access.deliveryIdentity).not.toBe(identity)
      expect(access.deliveryIdentity).not.toContain('different-private-token')
      identity = access.deliveryIdentity
    }
    store.disconnect()
    expect(access.deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('recovers a same-tab delivery identity after reload only once the same target has a fresh Hello', async () => {
    const first = useRpcStore()
    first.init()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(createV4GatewayAccess(first).deliveryIdentity).not.toBeNull())
    const identity = createV4GatewayAccess(first).deliveryIdentity
    first.$dispose()
    setActivePinia(createPinia())
    const next = useRpcStore()
    next.init()
    expect(createV4GatewayAccess(next).deliveryIdentity).toBeNull()
    clients[1].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(createV4GatewayAccess(next).deliveryIdentity).toBe(identity))
    next.$dispose()
  })

  it('shares queue authority with a fresh tab only for the same target credentials and Hello', async () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://synthetic.example/ws')
    sessionStorage.setItem('opensquilla.wsToken', 'synthetic-owner-token')
    const first = useRpcStore()
    first.init()
    clients[0].emit('_hello', deliveryHello())
    const access = createV4GatewayAccess(first)
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    const original = access.deliveryIdentity
    first.$dispose()
    for (const [index, token] of ['synthetic-owner-token', 'synthetic-other-token'].entries()) {
      // A new tab has no inherited session storage; credentials are supplied
      // independently, while the queue and random salt share the origin.
      sessionStorage.clear()
      sessionStorage.setItem('opensquilla.wsToken', token)
      setActivePinia(createPinia())
      const next = useRpcStore()
      next.init()
      const nextAccess = createV4GatewayAccess(next)
      expect(nextAccess.deliveryIdentity).toBeNull()
      clients[index + 1].emit('_hello', deliveryHello())
      await vi.waitFor(() => expect(nextAccess.deliveryIdentity).not.toBeNull())
      if (index === 0) expect(nextAccess.deliveryIdentity).toBe(original)
      else expect(nextAccess.deliveryIdentity).not.toBe(original)
      next.$dispose()
    }
    expect(sessionStorage.getItem('opensquilla.deliverySalt.v1')).toBeNull()
    expect(localStorage.getItem('opensquilla.deliverySalt.v1')).toMatch(/^[0-9a-f]{32}$/)
  })

  it('serializes concurrent first-tab salt creation without delaying either transport', async () => {
    const waiting: Array<() => void> = []
    const request = vi.fn((_name: string, action: () => unknown) => new Promise(resolve => {
      waiting.push(() => resolve(action()))
    }))
    vi.stubGlobal('navigator', { locks: { request } })
    const first = useRpcStore()
    first.init()
    clients[0].emit('_hello', deliveryHello())
    setActivePinia(createPinia())
    const second = useRpcStore()
    second.init()
    clients[1].emit('_hello', deliveryHello())
    expect(connectCalls).toHaveLength(2)
    expect(request).toHaveBeenCalledTimes(2)
    expect(createV4GatewayAccess(first).deliveryIdentity).toBeNull()
    expect(createV4GatewayAccess(second).deliveryIdentity).toBeNull()
    waiting[0]!()
    const salt = localStorage.getItem('opensquilla.deliverySalt.v1')
    waiting[1]!()
    expect(localStorage.getItem('opensquilla.deliverySalt.v1')).toBe(salt)
    await vi.waitFor(() => expect(createV4GatewayAccess(first).deliveryIdentity).not.toBeNull())
    await vi.waitFor(() => expect(createV4GatewayAccess(second).deliveryIdentity)
      .toBe(createV4GatewayAccess(first).deliveryIdentity))
    first.$dispose()
    second.$dispose()
  })

  it('without Web Locks only reuses an existing shared salt and keeps first-use transport working', async () => {
    vi.stubGlobal('navigator', {})
    const first = useRpcStore()
    first.init()
    clients[0].emit('_hello', deliveryHello())
    await Promise.resolve()
    expect(connectCalls).toHaveLength(1)
    expect(createV4GatewayAccess(first).deliveryIdentity).toBeNull()
    expect(localStorage.getItem('opensquilla.deliverySalt.v1')).toBeNull()
    first.$dispose()
    localStorage.setItem('opensquilla.deliverySalt.v1', '1'.repeat(32))
    setActivePinia(createPinia())
    const next = useRpcStore()
    next.init()
    clients[1].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(createV4GatewayAccess(next).deliveryIdentity).not.toBeNull())
    next.$dispose()
  })

  it('recovers the original target after A to B to A and explicit disconnect only with matching Hello proof', async () => {
    const firstEndpoint = 'ws://synthetic-a.example/ws'
    localStorage.setItem('opensquilla.wsUrl', firstEndpoint)
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    const firstIdentity = access.deliveryIdentity
    await store.connect('ws://synthetic-b.example/ws')
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    expect(access.deliveryIdentity).not.toBe(firstIdentity)
    await store.connect(firstEndpoint)
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).toBe(firstIdentity))
    store.disconnect()
    expect(access.deliveryIdentity).toBeNull()
    await store.connect(firstEndpoint)
    expect(access.deliveryIdentity).toBeNull()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).toBe(firstIdentity))
    store.$dispose()
  })

  it.each(['endpoint', 'credential', 'principal'])(
    'does not recover an old delivery identity after a reload changes the %s', async changed => {
      localStorage.setItem('opensquilla.wsUrl', 'ws://synthetic.example/ws?token=private-query')
      sessionStorage.setItem('opensquilla.wsToken', 'private-credential')
      const first = useRpcStore()
      first.init()
      clients[0].emit('_hello', deliveryHello())
      const access = createV4GatewayAccess(first)
      await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
      const identity = access.deliveryIdentity
      const stored = localStorage.getItem('opensquilla.deliverySalt.v1')!
      expect(stored).not.toContain('private-')
      expect(stored).not.toContain('synthetic.example')
      first.$dispose()
      if (changed === 'endpoint') localStorage.setItem('opensquilla.wsUrl', 'ws://another.example/ws')
      if (changed === 'credential') sessionStorage.setItem('opensquilla.wsToken', 'private-rotated')
      setActivePinia(createPinia())
      const next = useRpcStore()
      next.init()
      clients[1].emit('_hello', deliveryHello(changed === 'principal' ? { tokenPublicId: 'another' } : {}))
      const nextAccess = createV4GatewayAccess(next)
      await vi.waitFor(() => expect(nextAccess.deliveryIdentity).not.toBeNull())
      expect(nextAccess.deliveryIdentity).not.toBe(identity)
      next.$dispose()
    },
  )

  it('does not let delayed target hashing restore a superseded identity', async () => {
    const digest = crypto.subtle.digest.bind(crypto.subtle)
    let resolveOld!: (value: ArrayBuffer) => void
    vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    clients[0].emit('_hello', deliveryHello())
    expect(access.deliveryIdentity).toBeNull()
    await vi.waitFor(() => expect(resolveOld).toBeTypeOf('function'))
    await store.connect('ws://another.example/ws', 'synthetic-token')
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    const identity = access.deliveryIdentity
    const stored = localStorage.getItem('opensquilla.deliverySalt.v1')
    resolveOld(await digest('SHA-256', new TextEncoder().encode('old-target')))
    await Promise.resolve()
    expect(access.deliveryIdentity).toBe(identity)
    expect(localStorage.getItem('opensquilla.deliverySalt.v1')).toBe(stored)
    store.$dispose()
  })

  it('does not let delayed target hashing restore a blocked identity', async () => {
    let finish!: (value: ArrayBuffer) => void
    vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const store = useRpcStore()
    store.init()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    clients[0].emit('_status', { lifecycle: 'blocked', health: 'suspect', reason: 'authentication_mismatch' })
    finish(new ArrayBuffer(32))
    await Promise.resolve()
    expect(createV4GatewayAccess(store).deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('keeps transport startup working without offline authority when hashing is unavailable', async () => {
    const digest = vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('Synthetic crypto restriction'))
    const store = useRpcStore()
    store.init()
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(digest).toHaveBeenCalledOnce())
    expect(connectCalls).toHaveLength(1)
    expect(store.isConnected).toBe(true)
    expect(createV4GatewayAccess(store).deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('keeps transport startup working without offline authority when target storage is unavailable', async () => {
    vi.spyOn(crypto.subtle, 'digest').mockResolvedValue(new ArrayBuffer(32))
    const write = vi.fn(() => { throw new Error('Synthetic storage restriction') })
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: write, removeItem: () => {} })
    const store = useRpcStore()
    store.init()
    clients[0].emit('_hello', deliveryHello())
    await Promise.resolve()
    expect(connectCalls).toHaveLength(1)
    expect(store.isConnected).toBe(true)
    expect(write).toHaveBeenCalled()
    expect(createV4GatewayAccess(store).deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('recovers the same Desktop descriptor after reload and fences a changed profile', async () => {
    let payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'private-runtime-token', error: null,
    }
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => payload),
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    let identity: string | null = null
    for (let index = 0; index < 3; index += 1) {
      setActivePinia(createPinia())
      const store = useRpcStore()
      store.init()
      await vi.waitFor(() => expect(connectCalls).toHaveLength(index + 1))
      const access = createV4GatewayAccess(store)
      expect(access.deliveryIdentity).toBeNull()
      clients[index].emit('_hello', deliveryHello())
      await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
      if (index === 0) identity = access.deliveryIdentity
      else if (index === 1) expect(access.deliveryIdentity).toBe(identity)
      else expect(access.deliveryIdentity).not.toBe(identity)
      expect(localStorage.getItem('opensquilla.deliverySalt.v1')).not.toContain('private-runtime-token')
      store.$dispose()
      if (index === 1) payload = { ...payload, profileFingerprint: 'profile-b' }
    }
  })

  it('fences Desktop profile, process and token changes independently of the browser origin', async () => {
    const base = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'private-runtime-token', error: null,
    }
    let publish!: (value: typeof base) => void
    const getConnection = vi.fn(async () => base)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(handler => { publish = handler; return () => {} }),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    clients[0].emit('_hello', deliveryHello())
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    let identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    publish({ ...base, revision: 2 })
    expect(access.deliveryIdentity).toBe(identity)
    let revision = 2
    for (const changed of [
      { profileFingerprint: 'profile-b' },
      { instanceId: 'runtime-b' },
      { authToken: 'private-rotated-token' },
    ]) {
      publish({ ...base, ...changed, revision: ++revision })
      expect(access.deliveryIdentity).toBeNull()
      clients[0].emit('_hello', deliveryHello())
      await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
      expect(access.deliveryIdentity).not.toBe(identity)
      expect(access.deliveryIdentity).not.toContain('private-')
      identity = access.deliveryIdentity
    }
    store.disconnect()
    expect(access.deliveryIdentity).toBeNull()
    store.$dispose()
  })

  it('uses a URL token over stale browser storage before initial connect', () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://old.example/ws')
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:old', 'stale draft')
    localStorage.setItem('opensquilla.chat.runMode', 'full')
    localStorage.setItem('opensquilla.logs.runTrace', '1')
    localStorage.setItem('opensquilla.shortcuts', '{"new-chat":{"enabled":true}}')
    localStorage.setItem('unrelated.preference', 'keep')
    sessionStorage.setItem('opensquilla.wsToken', 'old-token')
    sessionStorage.setItem('opensquilla.cachedAuth', 'stale-auth')
    window.history.replaceState(null, '', '/control/?token=new-token')

    const store = useRpcStore()
    store.init()

    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'new-token' }])
    expect(localStorage.getItem('opensquilla.wsUrl')).toBe('ws://localhost:3000/ws')
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:old')).toBeNull()
    expect(localStorage.getItem('opensquilla.chat.runMode')).toBe('full')
    expect(localStorage.getItem('opensquilla.logs.runTrace')).toBe('1')
    expect(localStorage.getItem('opensquilla.shortcuts')).toBe('{"new-chat":{"enabled":true}}')
    expect(localStorage.getItem('unrelated.preference')).toBe('keep')
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('new-token')
    expect(sessionStorage.getItem('opensquilla.cachedAuth')).toBeNull()
    expect(window.location.href).toBe('http://localhost:3000/control/')
  })

  it('delegates an aborted wait even when the reactive store is connected', async () => {
    const store = useRpcStore()
    store.init()
    const controller = new AbortController()
    controller.abort()
    const abortError = new Error('aborted')
    clients[0].ready.mockRejectedValueOnce(abortError)

    await expect(
      store.ready(123, controller.signal, { abortAction: 'reconnect' }),
    ).rejects.toBe(abortError)
    expect(clients[0].ready).toHaveBeenCalledWith(
      123,
      controller.signal,
      { abortAction: 'reconnect' },
    )
  })

  it('forwards generation-fenced subscription recovery to the live client', () => {
    const store = useRpcStore()
    store.init()

    expect(store.connectionGeneration).toBe(1)
    expect(store.recoverConnectionGeneration(1, 'subscription release failed')).toBe(true)
    expect(clients[0].recoverConnectionGeneration).toHaveBeenCalledWith(
      1,
      'subscription release failed',
    )

    // RpcClient callbacks mutate the raw class instance. A cached computed
    // getter would stay at generation 1 and incorrectly fence later leases.
    clients[0].connectionGeneration = 3
    clients[0].emit('_state', 'connected')
    expect(store.connectionGeneration).toBe(3)
  })

  it('reconnects with a URL token when an already-loaded app navigates to a token link', () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://localhost:3000/ws')
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:old', 'stale draft')
    sessionStorage.setItem('opensquilla.wsToken', 'old-token')
    sessionStorage.setItem('opensquilla.cachedAuth', 'stale-auth')

    const store = useRpcStore()
    store.init()
    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'old-token' }])

    window.history.replaceState(null, '', '/control/sessions?token=new-token')
    expect(store.applyLinkTokenFromUrl()).toBe(true)

    expect(connectCalls).toEqual([
      { url: 'ws://localhost:3000/ws', token: 'old-token' },
      { url: 'ws://localhost:3000/ws', token: 'new-token' },
    ])
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:old')).toBeNull()
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('new-token')
    expect(sessionStorage.getItem('opensquilla.cachedAuth')).toBeNull()
    expect(window.location.href).toBe('http://localhost:3000/control/sessions')
  })

  it('clears stale identity state before reconnecting with a URL token', () => {
    const store = useRpcStore()
    store.init()
    clients[0].emit('_hello', {
      policy: { allowedRunModes: ['full'] },
      auth: { principal: { isOwner: true } },
      features: {
        methods: ['usage.status', 'usage.query'],
        events: ['session.event.turn_committed'],
      },
    })
    expect(store.policy).toEqual({ allowedRunModes: ['full'] })
    expect(store.auth).toEqual({ principal: { isOwner: true } })
    expect(store.hasRpcMethod('usage.query')).toBe(true)
    expect(store.hasRpcEvent('session.event.turn_committed')).toBe(true)

    store.rememberUnsupportedMethod('usage.query')
    expect(store.hasRpcMethod('usage.query')).toBe(false)

    window.history.replaceState(null, '', '/control/?token=new-token')

    expect(store.applyLinkTokenFromUrl()).toBe(true)
    expect(store.policy).toBeNull()
    expect(store.auth).toBeNull()
    expect(store.methods).toEqual([])
    expect(store.events).toEqual([])
    expect(connectCalls[connectCalls.length - 1]).toEqual({
      url: 'ws://localhost:3000/ws',
      token: 'new-token',
    })
  })

  it('treats missing or malformed Hello capabilities as unsupported', () => {
    const store = useRpcStore()
    store.init()

    clients[0].emit('_hello', {
      features: {
        methods: ['usage.status', 42, null],
        events: ['session.event.turn_committed', 42, null],
      },
    })

    expect(store.methods).toEqual(['usage.status'])
    expect(store.events).toEqual(['session.event.turn_committed'])
    expect(store.hasRpcMethod('usage.status')).toBe(true)
    expect(store.hasRpcMethod('usage.query')).toBe(false)
    expect(store.hasRpcEvent('session.event.turn_committed')).toBe(true)
    expect(store.hasRpcEvent('session.event.unknown')).toBe(false)

    clients[0].emit('_hello', {})
    expect(store.methods).toEqual([])
    expect(store.events).toEqual([])
    expect(store.hasRpcEvent('session.event.turn_committed')).toBe(false)
  })

  it('derives project capabilities from the current Hello owner and methods', () => {
    const store = useRpcStore()
    store.init()

    clients[0].emit('_hello', {
      auth: { principal: { isOwner: true } },
      features: { methods: ['workspaces.list', 'workspaces.open'] },
    })
    expect(store.isLocalOwner).toBe(true)
    expect(store.canManageProjectWorkspaces).toBe(true)
    expect(store.canChooseProject).toBe(true)

    clients[0].emit('_state', 'connecting')
    expect(store.auth).toBeNull()
    expect(store.methods).toEqual([])
    expect(store.events).toEqual([])
    expect(store.canManageProjectWorkspaces).toBe(false)

    clients[0].emit('_state', 'connected')
    clients[0].emit('_hello', {
      auth: { principal: { isOwner: false } },
      features: { methods: ['workspaces.list', 'workspaces.open'] },
    })
    expect(store.isLocalOwner).toBe(false)
    expect(store.canManageProjectWorkspaces).toBe(false)
    expect(store.canChooseProject).toBe(false)
  })

  it('waits for the Desktop supervisor and reconnects only for a ready runtime instance', async () => {
    const publishRef: { current?: (payload: unknown) => void } = {}
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => ({
        schemaVersion: 1,
        revision: 1,
        status: 'starting',
        instanceId: 'runtime-a',
        profileFingerprint: 'profile-a',
        httpUrl: 'http://127.0.0.1:18791',
        wsUrl: null,
        authToken: null,
        error: null,
      })),
      onGatewayConnectionChanged: vi.fn((callback) => {
        publishRef.current = callback as (payload: unknown) => void
        return () => undefined
      }),
    } as unknown as OpenSquillaDesktopApi

    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    await vi.waitFor(() => expect(window.opensquillaDesktop?.getGatewayConnection).toHaveBeenCalled())
    expect(connectCalls).toEqual([])
    expect(store.state).toBe('disconnected')
    expect(access.availability).toBe('preparing')
    expect(access.isRuntimeStarting).toBe(true)
    expect(access.isAvailable).toBe(false)

    publishRef.current?.({
      schemaVersion: 1,
      revision: 2,
      status: 'ready',
      instanceId: 'runtime-a',
      profileFingerprint: 'profile-a',
      httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws',
      authToken: 'desktop-instance-token',
      error: null,
    })
    expect(connectCalls).toEqual([{
      url: 'ws://127.0.0.1:18791/ws',
      token: 'desktop-instance-token',
    }])
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('desktop-instance-token')
    expect(access.isRuntimeStarting).toBe(false)
    expect(localStorage.getItem('opensquilla.wsUrl')).toBeNull()

    publishRef.current?.({
      schemaVersion: 1,
      revision: 3,
      status: 'error',
      instanceId: 'runtime-a',
      profileFingerprint: 'profile-a',
      httpUrl: 'http://127.0.0.1:18791',
      wsUrl: null,
      authToken: null,
      error: 'runtime stopped',
    })
    expect(clients[0].disconnect).toHaveBeenCalledOnce()
    expect(store.error).toBe('runtime stopped')
    expect(store.state).toBe('disconnected')
    expect(access.availability).toBe('unavailable')
    expect(access.isRuntimeStarting).toBe(false)
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBeNull()
  })
  it('applies same-address token rotation and never revives an old token for an empty descriptor', async () => {
    let publish!: (payload: unknown) => void
    const base = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => base),
      onGatewayConnectionChanged: vi.fn(callback => { publish = callback; return () => {} }),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    publish({ ...base, revision: 2 })
    expect(clients[0].ensureConnected).toHaveBeenCalledOnce()
    expect(connectCalls).toHaveLength(1)
    publish({ ...base, revision: 3, authToken: 'token-b' })
    expect(connectCalls[connectCalls.length - 1]?.token).toBe('token-b')
    publish({ ...base, revision: 4, authToken: null })
    expect(connectCalls[connectCalls.length - 1]?.token).toBeUndefined()
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBeNull()
    store.disconnect()
    publish({ ...base, revision: 5, authToken: 'token-c' })
    expect(connectCalls).toHaveLength(3)
    store.$dispose()
  })

  it('ends runtime preparation on stop, failure, or explicit disconnect', async () => {
    let publish!: (payload: unknown) => void
    const starting = {
      schemaVersion: 1, revision: 1, status: 'starting', instanceId: null,
      profileFingerprint: 'profile-a', httpUrl: null, wsUrl: null, error: null,
    }
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => starting),
      onGatewayConnectionChanged: vi.fn(callback => { publish = callback; return () => {} }),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    expect(access.availability).toBe('preparing')
    await vi.waitFor(() => expect(window.opensquillaDesktop?.getGatewayConnection).toHaveBeenCalled())
    let revision = 1
    for (const status of ['stopped', 'error']) {
      publish({ ...starting, revision: ++revision, status })
      expect(access.availability).toBe('unavailable')
      expect(access.isRuntimeStarting).toBe(false)
      publish({ ...starting, revision: ++revision })
      expect(access.isRuntimeStarting).toBe(true)
    }
    store.disconnect()
    expect(access.availability).toBe('unavailable')
    expect(access.isRuntimeStarting).toBe(false)
    publish({ ...starting, revision: ++revision })
    expect(access.isRuntimeStarting).toBe(false)
    expect(connectCalls).toHaveLength(0)
    store.$dispose()
  })

  it('refreshes preparation when reconnecting manually during startup or after failure', async () => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'starting', instanceId: null,
      profileFingerprint: 'profile-a', httpUrl: null, wsUrl: null, error: null,
    }
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => ({ ...payload })),
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    const access = createV4GatewayAccess(store)
    store.init()
    await store.connect('ws://desktop/ws')
    store.disconnect()
    expect(access.isRuntimeStarting).toBe(false)
    await store.connect('ws://desktop/ws')
    expect(access.isRuntimeStarting).toBe(true)
    expect(access.availability).toBe('preparing')
    for (const status of ['stopped', 'starting', 'error']) {
      payload.status = status
      payload.revision++
      await store.connect('ws://desktop/ws')
      expect(access.isRuntimeStarting).toBe(status === 'starting')
    }
    expect(connectCalls).toHaveLength(0)
    expect(access.availability).toBe('unavailable')
    store.$dispose()
  })

  it('refreshes authoritative credentials at most once for the same failed intent', async () => {
    const base = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'bad-token', error: null,
    }
    const getConnection = vi.fn(async () => base)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    const lifecycle = vi.spyOn(clients[0], 'lifecycle', 'get').mockReturnValue('blocked')
    clients[0].emit('_status', { lifecycle: 'blocked', health: 'suspect', reason: 'authentication_mismatch' })
    clients[0].emit('_blocked', { reason: 'authentication_mismatch' })
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledTimes(2))
    expect(store.error).toBe('authentication_mismatch')
    expect(connectCalls).toHaveLength(1)
    clients[0].emit('_blocked', { reason: 'authentication_mismatch' })
    await Promise.resolve()
    expect(getConnection).toHaveBeenCalledTimes(2)
    await store.connect('ws://desktop/ws')
    expect(getConnection).toHaveBeenCalledTimes(3)
    expect(connectCalls).toEqual([
      { url: base.wsUrl, token: base.authToken },
      { url: base.wsUrl, token: base.authToken },
    ])
    lifecycle.mockRestore()
    store.$dispose()
  })

  it('recovers a failed descriptor read without manual retry and ignores late results after stop', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const base = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    const getConnection = vi.fn().mockRejectedValueOnce(new Error('temporary')).mockResolvedValue(base)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.advanceTimersByTimeAsync(500)
    expect(getConnection).toHaveBeenCalledTimes(2)
    expect(connectCalls).toHaveLength(1)
    let resolve!: (value: unknown) => void
    getConnection.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    store.notifyResume()
    await Promise.resolve()
    store.disconnect()
    resolve({ ...base, revision: 2 })
    await vi.advanceTimersByTimeAsync(8_000)
    expect(connectCalls).toHaveLength(1)
    store.$dispose()
  })

  it('preserves pending automatic recovery when a manual Desktop lookup also fails', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    const getConnection = vi.fn()
      .mockRejectedValueOnce(new Error('temporary'))
      .mockRejectedValueOnce(new Error('still unavailable'))
      .mockResolvedValue(payload)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.advanceTimersByTimeAsync(0)
    expect(getConnection).toHaveBeenCalledTimes(1)
    await store.connect('ws://desktop/ws')
    expect(getConnection).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(getConnection).toHaveBeenCalledTimes(3)
    expect(connectCalls).toEqual([{ url: payload.wsUrl, token: payload.authToken }])
    store.disconnect()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(getConnection).toHaveBeenCalledTimes(3)
    store.$dispose()
  })

  it('reconnects the Desktop through its supervisor instead of browser form settings', async () => {
    const first = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    const next = { ...first, revision: 2, instanceId: 'runtime-b',
      wsUrl: 'ws://127.0.0.1:18792/ws', authToken: 'token-b' }
    const getConnection = vi.fn().mockResolvedValueOnce(first).mockResolvedValue(next)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    const access = createV4GatewayAccess(store)
    access.disconnect()
    await access.connect({ endpoint: 'ws://desktop/ws', credential: 'stale-form-token' })
    expect(getConnection).toHaveBeenCalledTimes(2)
    expect(connectCalls[connectCalls.length - 1]).toEqual({ url: next.wsUrl, token: next.authToken })
    expect(localStorage.getItem('opensquilla.wsUrl')).toBeNull()
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe(next.authToken)
    store.$dispose()
  })

  it('keeps a healthy Desktop connection and identity when manual refresh is unchanged or fails', async () => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    const getConnection = vi.fn(async () => payload)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    clients[0].emit('_hello', { ...deliveryHello(), policy: { retained: true } })
    const access = createV4GatewayAccess(store)
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    let identity = access.deliveryIdentity
    expect(identity).not.toBeNull()
    await store.connect('ws://desktop/ws')
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    expect(connectCalls).toHaveLength(1)
    expect(clients[0].disconnect).not.toHaveBeenCalled()
    expect(store.isLocalOwner).toBe(true)
    expect(store.policy).toEqual({ retained: true })
    expect(access.deliveryIdentity).not.toBeNull()
    expect(access.deliveryIdentity).toBe(identity)
    identity = access.deliveryIdentity
    getConnection.mockRejectedValueOnce(new Error('IPC temporarily unavailable'))
    await store.connect('ws://desktop/ws')
    await vi.waitFor(() => expect(access.deliveryIdentity).not.toBeNull())
    expect(store.error).toBeTruthy()
    expect(store.state).toBe('connected')
    expect(store.isLocalOwner).toBe(true)
    expect(connectCalls).toHaveLength(1)
    expect(clients[0].disconnect).not.toHaveBeenCalled()
    expect(access.deliveryIdentity).not.toBeNull()
    expect(access.deliveryIdentity).toBe(identity)
    store.$dispose()
  })

  it('coalesces manual Desktop requests and ignores a cancelled read after a new connection intent', async () => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    let resolveOld!: (value: typeof payload) => void
    let resolveNew!: (value: typeof payload) => void
    const getConnection = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveNew = resolve }))
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    const pending = store.connect('ws://desktop/ws')
    const duplicate = store.connect('ws://desktop/ws')
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledTimes(1))
    expect(connectCalls).toHaveLength(0)
    store.disconnect()
    const current = store.connect('ws://desktop/ws')
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledTimes(2))
    resolveOld({ ...payload, revision: 99, wsUrl: 'ws://127.0.0.1:19999/ws' })
    await Promise.all([pending, duplicate])
    expect(connectCalls).toHaveLength(0)
    resolveNew(payload)
    await current
    expect(connectCalls).toEqual([{ url: payload.wsUrl, token: payload.authToken }])
    store.$dispose()
  })

  it('restarts an explicitly disconnected Desktop even when its descriptor is unchanged', async () => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => payload),
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    store.disconnect()
    await store.connect('ws://desktop/ws')
    expect(connectCalls).toEqual([
      { url: payload.wsUrl, token: payload.authToken },
      { url: payload.wsUrl, token: payload.authToken },
    ])
    expect(store.state).toBe('connected')
    store.$dispose()
  })

  it.each([
    null,
    { schemaVersion: 0 },
    { schemaVersion: 1, revision: 2, status: 'ready', instanceId: 'runtime-a' },
    { schemaVersion: 1, revision: 2, status: 'starting', error: 'Runtime is starting' },
  ])('preserves the live Desktop connection when manual lookup returns unusable data: %j', async (invalid) => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    const getConnection = vi.fn().mockResolvedValueOnce(payload).mockResolvedValueOnce(invalid)
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    await store.connect('ws://desktop/ws')
    expect(store.error).toBeTruthy()
    expect(store.state).toBe('connected')
    expect(clients[0].disconnect).not.toHaveBeenCalled()
    expect(connectCalls).toHaveLength(1)
    store.$dispose()
  })

  it('bounds a manual Desktop lookup and ignores its late result without discarding the live connection', async () => {
    vi.useFakeTimers()
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    let resolve!: (value: typeof payload) => void
    const getConnection = vi.fn().mockResolvedValueOnce(payload)
      .mockImplementationOnce(() => new Promise(done => { resolve = done }))
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(() => () => {}),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.advanceTimersByTimeAsync(0)
    const pending = store.connect('ws://desktop/ws')
    await vi.advanceTimersByTimeAsync(8_000)
    await pending
    expect(store.error).toBeTruthy()
    expect(store.state).toBe('connected')
    expect(clients[0].disconnect).not.toHaveBeenCalled()
    resolve({ ...payload, revision: 99, wsUrl: 'ws://127.0.0.1:19999/ws' })
    await vi.advanceTimersByTimeAsync(15_000)
    expect(connectCalls).toEqual([{ url: payload.wsUrl, token: payload.authToken }])
    expect(getConnection).toHaveBeenCalledTimes(2)
    store.$dispose()
  })

  it('does not replace a newer Desktop notification with a delayed manual snapshot', async () => {
    const payload = {
      schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
      profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
      wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'token-a', error: null,
    }
    let publish!: (value: typeof payload) => void
    let resolve!: (value: typeof payload) => void
    const getConnection = vi.fn().mockResolvedValueOnce(payload)
      .mockImplementationOnce(() => new Promise(done => { resolve = done }))
    window.opensquillaDesktop = {
      getGatewayConnection: getConnection,
      onGatewayConnectionChanged: vi.fn(callback => { publish = callback; return () => {} }),
    } as unknown as OpenSquillaDesktopApi
    const store = useRpcStore()
    store.init()
    await vi.waitFor(() => expect(connectCalls).toHaveLength(1))
    const pending = store.connect('ws://desktop/ws')
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledTimes(2))
    const newest = { ...payload, revision: 3, wsUrl: 'ws://127.0.0.1:18793/ws', authToken: 'token-c' }
    publish(newest)
    resolve({ ...payload, revision: 2, wsUrl: 'ws://127.0.0.1:18792/ws', authToken: 'token-b' })
    await pending
    expect(connectCalls).toEqual([
      { url: payload.wsUrl, token: payload.authToken },
      { url: newest.wsUrl, token: newest.authToken },
    ])
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe(newest.authToken)
    store.$dispose()
  })

  it('continues to use explicit endpoint and credentials for browser connections', async () => {
    const store = useRpcStore()
    store.init()
    await store.connect('wss://gateway.example/ws', 'browser-token')
    expect(connectCalls[connectCalls.length - 1]).toEqual({
      url: 'wss://gateway.example/ws', token: 'browser-token',
    })
    expect(localStorage.getItem('opensquilla.wsUrl')).toBe('wss://gateway.example/ws')
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('browser-token')
    store.$dispose()
  })
})
