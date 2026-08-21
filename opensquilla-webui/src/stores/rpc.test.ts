// @vitest-environment happy-dom

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRpcStore } from './rpc'

const connectCalls: Array<{ url: string; token?: string }> = []
const clients: Array<{
  emit: (event: string, ...args: unknown[]) => void
  disconnect: ReturnType<typeof vi.fn>
  waitForConnection: ReturnType<typeof vi.fn>
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
  },
}))

describe('rpc link-token bootstrap', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    connectCalls.length = 0
    clients.length = 0
    localStorage.clear()
    sessionStorage.clear()
    window.history.replaceState(null, '', '/control/sessions')
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
    expect(store.supportsMethod('usage.query')).toBe(true)
    expect(store.supportsEvent('session.event.turn_committed')).toBe(true)

    store.markMethodUnavailable('usage.query')
    expect(store.supportsMethod('usage.query')).toBe(false)

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
      auth: { principal: { isOwner: false } },
      features: { methods: ['workspaces.list', 'workspaces.open'] },
    })
    expect(store.isLocalOwner).toBe(false)
    expect(store.canManageProjectWorkspaces).toBe(false)
    expect(store.canChooseProject).toBe(false)
  })
})
