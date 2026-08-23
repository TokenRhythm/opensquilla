// @vitest-environment happy-dom

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRpcStore } from './rpc'

const connectCalls: Array<{ url: string; token?: string }> = []
const clients: Array<{
  emit: (event: string, ...args: unknown[]) => void
  disconnect: ReturnType<typeof vi.fn>
  waitForConnection: ReturnType<typeof vi.fn>
  updateToken: ReturnType<typeof vi.fn>
}> = []

vi.mock('@/lib/rpc', () => ({
  RpcClient: class {
    state = 'disconnected'
    private listeners = new Map<string, Array<(...args: unknown[]) => void>>()

    constructor() {
      clients.push(this)
    }

    connect(url: string, token?: string) {
      connectCalls.push({ url, token })
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
    waitForConnection = vi.fn()
    call = vi.fn()
    updateToken = vi.fn()
  },
}))

describe('rpc link-token bootstrap', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    connectCalls.length = 0
    clients.length = 0
    localStorage.clear()
    sessionStorage.clear()
    delete window.opensquillaDesktop
    window.history.replaceState(null, '', 'http://localhost:3000/control/sessions')
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
    window.history.replaceState(null, '', '/control/#token=new-token')

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

  it('refuses a query-string token and strips it without connecting', () => {
    // A token in the query has already been transmitted to the server by the
    // time any script runs, so it must never be honoured or persisted.
    window.history.replaceState(null, '', '/control/?token=leaked-token')

    const store = useRpcStore()
    store.init()

    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: undefined }])
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBeNull()
    expect(window.location.href).toBe('http://localhost:3000/control/')
  })

  it('consumes a fragment token and leaves no secret in the address bar', () => {
    window.history.replaceState(null, '', '/control/?session=agent%3Amain#token=frag-token')

    const store = useRpcStore()
    store.init()

    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'frag-token' }])
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('frag-token')
    // The session hint survives; the spent secret does not.
    expect(window.location.href).toBe('http://localhost:3000/control/?session=agent%3Amain')
  })

  it('delegates an aborted wait even when the reactive store is connected', async () => {
    const store = useRpcStore()
    store.init()
    const controller = new AbortController()
    controller.abort()
    const abortError = new Error('aborted')
    clients[0].waitForConnection.mockRejectedValueOnce(abortError)

    await expect(
      store.waitForConnection(123, controller.signal, { abortAction: 'reconnect' }),
    ).rejects.toBe(abortError)
    expect(clients[0].waitForConnection).toHaveBeenCalledWith(
      123,
      controller.signal,
      { abortAction: 'reconnect' },
    )
  })

  it('reconnects with a URL token when an already-loaded app navigates to a token link', () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://localhost:3000/ws')
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:old', 'stale draft')
    sessionStorage.setItem('opensquilla.wsToken', 'old-token')
    sessionStorage.setItem('opensquilla.cachedAuth', 'stale-auth')

    const store = useRpcStore()
    store.init()
    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'old-token' }])

    window.history.replaceState(null, '', '/control/sessions#token=new-token')
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
    expect(store.supportsMethod('usage.query')).toBe(true)
    expect(store.supportsEvent('session.event.turn_committed')).toBe(true)

    store.markMethodUnavailable('usage.query')
    expect(store.supportsMethod('usage.query')).toBe(false)

    window.history.replaceState(null, '', '/control/#token=new-token')

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
    expect(store.supportsMethod('usage.status')).toBe(true)
    expect(store.supportsMethod('usage.query')).toBe(false)
    expect(store.supportsEvent('session.event.turn_committed')).toBe(true)
    expect(store.supportsEvent('session.event.unknown')).toBe(false)

    clients[0].emit('_hello', {})
    expect(store.methods).toEqual([])
    expect(store.events).toEqual([])
    expect(store.supportsEvent('session.event.turn_committed')).toBe(false)
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
      auth: { principal: { isOwner: false, capabilities: ['host.execute'] } },
      features: { methods: ['workspaces.list', 'workspaces.open'] },
    })
    expect(store.isLocalOwner).toBe(false)
    expect(store.canManageProjectWorkspaces).toBe(true)
    expect(store.canChooseProject).toBe(true)

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
    store.init()
    await vi.waitFor(() => expect(window.opensquillaDesktop?.getGatewayConnection).toHaveBeenCalled())
    expect(connectCalls).toEqual([])

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
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBeNull()
  })
})

describe('rpc store device credentials', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    connectCalls.length = 0
    clients.length = 0
    localStorage.clear()
    sessionStorage.clear()
    // The Desktop supervisor case in the previous suite installs this global;
    // leaving it set routes init() down the Desktop branch, which returns
    // before the browser link-token bootstrap ever connects.
    delete window.opensquillaDesktop
    // Reset the fragment too: a leftover #token from an earlier test would be
    // consumed here and silently replace the credential under test.
    window.history.replaceState(null, '', 'http://localhost:3000/control/')
  })

  it('persists the hello deviceToken and drops the spent pairing token', () => {
    sessionStorage.setItem('opensquilla.wsToken', 'osq_pair')
    const store = useRpcStore()
    store.init()

    // Shape mirrors the gateway: the credential rides inside the auth payload
    // (see _websocket_hello_auth_payload), not at the frame's top level.
    clients[0].emit('_hello', {
      auth: { principal: { authenticated: true }, deviceToken: 'osq_dev' },
    })

    expect(localStorage.getItem('opensquilla.deviceToken')).toBe('osq_dev')
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBeNull()
    // Reconnects must replay the device credential, not the spent pairing token.
    expect(clients[0].updateToken).toHaveBeenCalledWith('osq_dev')
  })

  it('reconnects with the stored deviceToken in a fresh browser session', () => {
    localStorage.setItem('opensquilla.deviceToken', 'osq_dev')

    const store = useRpcStore()
    store.init()

    expect(connectCalls.length).toBeGreaterThan(0)
    expect(connectCalls[0].token).toBe('osq_dev')
  })

  it('clears a stale deviceToken when the gateway rejects it', () => {
    localStorage.setItem('opensquilla.deviceToken', 'osq_stale')
    const store = useRpcStore()
    store.init()

    clients[0].emit('_hello', { auth: { principal: { authenticated: false } } })

    expect(localStorage.getItem('opensquilla.deviceToken')).toBeNull()
    expect(clients[0].updateToken).toHaveBeenCalledWith(null)
  })

  it('ignores a top-level deviceToken, which the gateway never sends', () => {
    const store = useRpcStore()
    store.init()

    clients[0].emit('_hello', {
      auth: { principal: { authenticated: true } },
      deviceToken: 'osq_wrong_shape',
    })

    expect(localStorage.getItem('opensquilla.deviceToken')).toBeNull()
  })

  it('preserves the deviceToken across a plain reload without a link token', () => {
    // The phone reloads the bare control URL after a network switch; wiping
    // the credential here is what stranded it with no way back in.
    localStorage.setItem('opensquilla.deviceToken', 'osq_dev')
    window.history.replaceState(null, '', '/control/')

    const store = useRpcStore()
    store.init()

    expect(localStorage.getItem('opensquilla.deviceToken')).toBe('osq_dev')
    expect(connectCalls[0].token).toBe('osq_dev')
  })

  it('discards the previous deviceToken when a fresh pairing link arrives', () => {
    localStorage.setItem('opensquilla.deviceToken', 'osq_old')
    window.history.replaceState(null, '', '/control/#token=osq_fresh')

    const store = useRpcStore()
    store.init()

    expect(connectCalls[0].token).toBe('osq_fresh')
    expect(localStorage.getItem('opensquilla.deviceToken')).toBeNull()
  })
})
