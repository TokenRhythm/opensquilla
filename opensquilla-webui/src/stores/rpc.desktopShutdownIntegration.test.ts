// @vitest-environment happy-dom
import { createPinia, setActivePinia } from 'pinia'
import { markRaw, watch } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createV4GatewayAccess } from '@/adapters/gateway/gatewayAccessV4'
import { createPrivateGatewayTransports } from '@/adapters/gateway/privateTransports'
import { createV4SessionDirectoryChanges } from '@/adapters/gateway/sessionDirectoryChangesV4'
import { createV4SessionDirectory } from '@/adapters/gateway/sessionDirectoryV4'
import { useSessions } from '@/composables/useSessions'
import { RpcClient } from '@/lib/rpc'
import type { DesktopGatewayConnection } from '@/platform/types'
import { createAppAutomaticRpc } from '@/utils/appAutomaticRpc'
import { useRpcStore } from './rpc'

// Only the external socket and Desktop IPC are substitutes. Cancellation,
// connection generations, request rejection and App refreshes are production code.
class Socket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: Socket[] = []
  readonly sent: string[] = []
  readyState = Socket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    // Native WebSocket objects are not made reactive when RpcClient is held in
    // the Pinia store. Keep the substitute's identity equally stable.
    markRaw(this)
    Socket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
    const frame = JSON.parse(data)
    if (frame.type === 'ping') {
      queueMicrotask(() => this.receive({ type: 'pong', ...(frame.nonce ? { nonce: frame.nonce } : {}) }))
    }
  }
  close() {
    this.readyState = Socket.CLOSED
    this.onclose?.({ code: 1000, wasClean: true } as CloseEvent)
  }
  receive(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent) }
  requests(method: string): Array<{ id: string; params: Record<string, unknown> }> {
    return this.sent.map(frame => JSON.parse(frame)).filter(frame => frame.method === method)
  }
  respond(method: string, payload: unknown) {
    const requests = this.requests(method)
    const request = requests[requests.length - 1]
    expect(request, `pending ${method}`).toBeDefined()
    this.receive({ type: 'res', id: request!.id, ok: true, payload })
  }
}

const ready: DesktopGatewayConnection = {
  schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'runtime-a',
  profileFingerprint: 'profile-a', httpUrl: 'http://127.0.0.1:18791',
  wsUrl: 'ws://127.0.0.1:18791/ws', authToken: 'test-owner-nonce', error: null,
}
const stopped: DesktopGatewayConnection = {
  ...ready, revision: 2, status: 'stopped', wsUrl: null, authToken: null,
}
const page = (title: string) => ({
  count: 1, ts: 1, hasMore: true, nextCursor: 'next-page',
  sessions: [{ key: 'agent:main:webchat:one', title, updatedAt: 1 }],
})

function hello(socket: Socket) {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    type: 'hello-ok', protocol: 3,
    server: { version: 'test', conn_id: `conn-${Socket.instances.indexOf(socket)}` },
    features: { methods: ['sessions.list', 'sessions.subscribe', 'sessions.unsubscribe'], events: ['sessions.changed'] },
    snapshot: {}, policy: {},
    auth: { principal: {
      role: 'operator',
      scopes: ['operator.admin', 'operator.approvals', 'operator.pairing', 'operator.proposals', 'operator.read', 'operator.write'],
      capabilities: ['host.execute', 'host.read', 'task.read', 'task.submit'],
      isOwner: true, authenticated: false, authState: 'authenticated', tokenPublicId: null,
    } },
  })
}

const cleanups: Array<() => void> = []

async function setup(cancelPending = true) {
  let publish!: (value: DesktopGatewayConnection) => void
  window.opensquillaDesktop = {
    getGatewayConnection: vi.fn(async () => ready),
    onGatewayConnectionChanged: vi.fn(callback => { publish = callback; return () => {} }),
  } as unknown as OpenSquillaDesktopApi
  const store = useRpcStore()
  store.init()
  const access = createV4GatewayAccess(store)
  const transports = createPrivateGatewayTransports(store)
  const changes = createV4SessionDirectoryChanges(transports.rpc, transports.events)
  const sessions = useSessions(createV4SessionDirectory(transports.rpc))
  const loadAgents = vi.fn(async () => {})
  const automatic = createAppAutomaticRpc({
    available: () => access.isAvailable,
    admitted: () => true,
    resumeDirectory: changes.resume,
    subscribeCron: () => {},
    loadAgents,
    loadSidebar: sessions.loadSessions,
    // Fault control disables only App's cancellation, never the real RPC error.
    cancelSidebar: cancelPending ? sessions.cancelPendingRequests : () => {},
  })
  const listener = changes.subscribe(() => automatic.schedule())
  // Keep this synchronous boundary identical to App.vue: request rejections
  // queue microtasks, so the old directory generation must be retired first.
  const stopWatching = watch(
    () => access.availability,
    () => { void automatic.availabilityChanged() },
    { flush: 'sync' },
  )
  cleanups.push(() => {
    automatic.dispose()
    stopWatching()
    store.disconnect()
    listener.close()
    changes.dispose()
    store.$dispose()
  })
  await automatic.mount()
  await vi.advanceTimersByTimeAsync(0)
  expect(store.client).toBeInstanceOf(RpcClient)
  expect(Socket.instances).toHaveLength(1)
  const socket = Socket.instances[0]!
  hello(socket)
  expect(store.state).toBe('connected')
  await vi.advanceTimersByTimeAsync(0)
  expect(socket.requests('sessions.subscribe')).toHaveLength(1)
  socket.respond('sessions.subscribe', {})
  await vi.advanceTimersByTimeAsync(0)
  expect(socket.requests('sessions.list')).toHaveLength(1)
  socket.respond('sessions.list', page('Saved conversation'))
  await vi.advanceTimersByTimeAsync(0)
  expect(sessions.sessionsList.value.map(item => item.title)).toEqual(['Saved conversation'])
  return { store, sessions, automatic, publish, socket, loadAgents }
}

type App = Awaited<ReturnType<typeof setup>>
async function pendingRead(app: App, kind: 'list' | 'next-page') {
  const completion = kind === 'list' ? app.automatic.load() : app.sessions.loadMoreSessions()
  await vi.advanceTimersByTimeAsync(0)
  expect(app.socket.requests('sessions.list')).toHaveLength(2)
  expect(app.socket.requests('sessions.list')[1]!.params).toEqual({
    limit: 200, view: 'session-list-v1', ...(kind === 'next-page' ? { cursor: 'next-page' } : {}),
  })
  expect(kind === 'list' ? app.sessions.isLoading.value : app.sessions.isLoadingMore.value).toBe(true)
  // Do not await the pending RPC while returning its completion to the caller.
  return { completion }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(1)
  Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  delete window.opensquillaDesktop
  window.history.replaceState(null, '', '/control/sessions')
})
afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  await vi.advanceTimersByTimeAsync(0)
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete window.opensquillaDesktop
})

describe('Desktop shutdown with the real RPC and App directory lifecycle', () => {
  it.each(['list', 'next-page'] as const)('fault control exposes a pending %s rejection without App cancellation', async kind => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = await setup(false)
    const read = await pendingRead(app, kind)
    app.publish(stopped)
    await read.completion
    expect(error).toHaveBeenCalledExactlyOnceWith(
      `[useSessions] session directory${kind === 'next-page' ? ' next-page' : ''} error:`,
      'Disconnected',
    )
    expect(app.sessions.loadMoreError.value).toBe(kind === 'next-page')
  })

  it('fault control reconnects after Gateway closes without the stopped descriptor', async () => {
    const app = await setup()
    app.socket.close()
    await vi.advanceTimersByTimeAsync(500)
    expect(Socket.instances).toHaveLength(2)
    expect(app.store.state).toBe('connecting')
    expect(app.store.lifecycle).toBe('recovering')
  })

  it.each(['list', 'next-page'] as const)('retires pending %s silently during drain and refreshes once if running resumes', async kind => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = await setup()
    const read = await pendingRead(app, kind)
    const oldMessage = app.socket.onmessage
    const oldClose = app.socket.onclose
    const retiredRequest = app.socket.requests('sessions.list')[1]!
    app.publish(stopped)
    expect(app.store.state).toBe('disconnected')
    expect(app.store.lifecycle).toBe('stopped')
    expect(app.sessions.isLoading.value).toBe(false)
    expect(app.sessions.isLoadingMore.value).toBe(false)
    await read.completion
    // Events already queued before shutdown and stale ready IPC cannot revive
    // the retired socket, install an old page, or reopen the connection.
    oldMessage?.({ data: JSON.stringify({
      type: 'res', id: retiredRequest.id, ok: true, payload: page('Retired result'),
    }) } as MessageEvent)
    oldClose?.({ code: 1006, wasClean: false } as CloseEvent)
    app.publish(ready)
    app.automatic.schedule()
    await app.automatic.load()
    app.store.notifyResume()
    await vi.advanceTimersByTimeAsync(35_000)
    expect(Socket.instances).toHaveLength(1)
    expect(app.socket.requests('sessions.list')).toHaveLength(2)
    expect(app.sessions.sessionsList.value.map(item => item.title)).toEqual(['Saved conversation'])
    expect(app.sessions.hasMore.value).toBe(true)
    expect(app.sessions.sessionListError.value).toBe(false)
    expect(app.sessions.loadMoreError.value).toBe(false)
    expect(error).not.toHaveBeenCalled()

    // A failed drain restores the same runtime with a newer revision. Repeated
    // ready observations must share one socket, one live lease and one refresh.
    app.publish({ ...ready, revision: 3 })
    app.publish({ ...ready, revision: 4 })
    expect(Socket.instances).toHaveLength(2)
    const resumed = Socket.instances[1]!
    hello(resumed)
    await vi.advanceTimersByTimeAsync(0)
    expect(resumed.requests('sessions.subscribe')).toHaveLength(1)
    expect(resumed.requests('sessions.list')).toHaveLength(0)
    resumed.respond('sessions.subscribe', {})
    await vi.advanceTimersByTimeAsync(0)
    expect(resumed.requests('sessions.list')).toHaveLength(1)
    resumed.respond('sessions.list', page('Resumed conversation'))
    await vi.advanceTimersByTimeAsync(1_000)
    expect(app.store.state).toBe('connected')
    expect(Socket.instances).toHaveLength(2)
    expect(resumed.requests('sessions.subscribe')).toHaveLength(1)
    expect(resumed.requests('sessions.list')).toHaveLength(1)
    expect(app.sessions.sessionsList.value.map(item => item.title)).toEqual(['Resumed conversation'])
    expect(app.loadAgents).toHaveBeenCalledOnce()
    expect(error).not.toHaveBeenCalled()
  })

  it('cancels an already scheduled reconnect when stopped IPC arrives after socket close', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = await setup()
    app.socket.close()
    expect(app.store.lifecycle).toBe('recovering')
    app.publish(stopped)
    app.store.notifyResume()
    await vi.advanceTimersByTimeAsync(35_000)
    expect(app.store.lifecycle).toBe('stopped')
    expect(Socket.instances).toHaveLength(1)
    expect(error).not.toHaveBeenCalled()
  })
})
