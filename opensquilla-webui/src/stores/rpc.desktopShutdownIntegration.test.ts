// @vitest-environment happy-dom
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, markRaw, watch } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createV4GatewayAccess } from '@/adapters/gateway/gatewayAccessV4'
import { createPrivateGatewayTransports } from '@/adapters/gateway/privateTransports'
import { createV4SessionDirectoryChanges } from '@/adapters/gateway/sessionDirectoryChangesV4'
import { createV4SessionDirectory } from '@/adapters/gateway/sessionDirectoryV4'
import { arrangeSidebarSections, useSessions, useSidebarProjectDisplay } from '@/composables/useSessions'
import { useProjectWorkspaces } from '@/composables/useProjectWorkspaces'
import { createV4WorkspaceCatalog } from '@/adapters/gateway/workspaceCatalogV4'
import { RpcClient } from '@/lib/rpc'
import type { DesktopGatewayConnection } from '@/platform/types'
import { createAppAutomaticRpc } from '@/utils/appAutomaticRpc'
import { useRpcStore } from './rpc'
import appSource from '@/App.vue?raw'

const displayStart = appSource.indexOf('const sidebarProjectDisplay = useSidebarProjectDisplay(')
const displayEnd = appSource.indexOf('const sidebarSections = computed(', displayStart)
if (displayStart < 0 || displayEnd < 0) throw new Error('Missing App project display boundary')
const createAppProjectDisplay = new Function(
  'gatewayAccess', 'projectWorkspaces', 'useSidebarProjectDisplay', 'computed',
  `${appSource.slice(displayStart, displayEnd)}; return sidebarProjectDisplay`,
)

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
const page = (title: string, runStatus = 'idle') => ({
  count: 1, ts: 1, hasMore: true, nextCursor: 'next-page',
  sessions: [{ key: 'agent:main:webchat:one', title, updatedAt: 1, runStatus }],
})

function hello(socket: Socket) {
  socket.receive({ type: 'event', event: 'connect.challenge' })
  socket.receive({
    type: 'hello-ok', protocol: 3,
    server: { version: 'test', conn_id: `conn-${Socket.instances.indexOf(socket)}` },
    features: { methods: ['sessions.list', 'sessions.subscribe', 'sessions.unsubscribe', 'workspaces.list'], events: ['sessions.changed'] },
    snapshot: {}, policy: {},
    auth: { principal: {
      role: 'operator',
      scopes: ['operator.admin', 'operator.approvals', 'operator.pairing', 'operator.read', 'operator.write'],
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

async function setupProjectDisplay(app: App) {
  const scope = effectScope()
  const view = scope.run(() => {
    const access = createV4GatewayAccess(app.store)
    const transports = createPrivateGatewayTransports(app.store)
    const projects = useProjectWorkspaces(createV4WorkspaceCatalog(transports.rpc), access)
    const display = createAppProjectDisplay(access, projects, useSidebarProjectDisplay, computed) as ReturnType<typeof useSidebarProjectDisplay>
    const rows = computed(() => arrangeSidebarSections(app.sessions.sessionsList.value, display.value)[0]!.rows)
    return { access, projects, display, rows }
  })!
  cleanups.push(() => scope.stop())
  const ordinary = app.sessions.sessionsList.value[0]!
  app.sessions.sessionsList.value = [
    { ...ordinary, workspace: '/tasks/20300101-managed', workspaceLabel: '20300101-managed' },
    { ...ordinary, key: 'agent:main:webchat:project', workspaceId: 'project-a' },
    { ...ordinary, key: 'agent:main:webchat:removed', workspaceId: 'removed-project' },
  ]
  expect(view.display.value.kind).toBe('pending')
  expect(view.rows.value.map(row => row.key)).toEqual([ordinary.key])
  const read = view.projects.loadWorkspaces()
  await vi.advanceTimersByTimeAsync(0)
  app.socket.respond('workspaces.list', { workspaces: [
    { id: 'project-b', name: 'Empty pinned project', path: '/repo/b', pinned: true, taskCount: 0, available: true },
    { id: 'project-a', name: 'Project A', path: '/repo/a', pinned: false, taskCount: 1, available: true },
  ] })
  await read
  expect(view.rows.value.filter(row => row.rowKind === 'workspace').map(row => row.title))
    .toEqual(['Empty pinned project', 'Project A'])
  expect(view.rows.value.some(row => row.key.includes('removed'))).toBe(false)
  return view
}

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
  it.each(['stopped', 'socket-close'] as const)('retains canonical projects after %s while retiring directory authority', async transition => {
    const app = await setup()
    const view = await setupProjectDisplay(app)
    const before = view.rows.value
    expect(view.projects.getValidatedWorkspace('project-a', '/repo/a')).toBeDefined()
    if (transition === 'stopped') app.publish(stopped)
    else app.socket.close()
    expect(view.access.canManageProjectWorkspaces).toBe(false)
    expect(view.projects.hasLoaded.value).toBe(false)
    expect(view.projects.workspaces.value).toEqual([])
    expect(view.projects.getValidatedWorkspace('project-a', '/repo/a')).toBeUndefined()
    expect(view.rows.value).toEqual(before)
    if (transition === 'stopped') {
      app.publish({ ...ready, revision: 3 })
      hello(Socket.instances[Socket.instances.length - 1]!)
      await vi.advanceTimersByTimeAsync(0)
      // The same runtime's new Hello restores authority, not its directory
      // validation. Its last display remains until a fresh catalog arrives.
      expect(view.projects.hasLoaded.value).toBe(false)
      expect(view.rows.value).toEqual(before)
    }
  })

  it.each(['owner', 'method'] as const)('does not select legacy grouping when %s access disappears', async authority => {
    const app = await setup()
    const view = await setupProjectDisplay(app)
    if (authority === 'owner') app.store.auth = { principal: { isOwner: false } }
    else app.store.methods = []
    expect(view.access.projectCatalogAvailability).toBe('unavailable')
    expect(view.display.value.kind).toBe('pending')
    expect(view.rows.value.some(row => row.rowKind === 'workspace' || row.workspaceId)).toBe(false)
  })

  it.each(['profile', 'runtime', 'credentials'] as const)('discards the display snapshot when the %s changes', async target => {
    const app = await setup()
    const view = await setupProjectDisplay(app)
    const refresh = view.projects.loadWorkspaces().catch(() => undefined)
    await vi.advanceTimersByTimeAsync(0)
    const lateMessage = app.socket.onmessage
    const requests = app.socket.requests('workspaces.list')
    const request = requests[requests.length - 1]!
    app.publish(stopped)
    expect(view.display.value.kind).toBe('canonical')
    app.publish(target === 'credentials'
      ? { ...ready, revision: 3, authToken: 'replacement-owner-nonce' }
      : { ...stopped, revision: 3,
          ...(target === 'profile' ? { profileFingerprint: 'profile-b' } : { instanceId: 'runtime-b' }) })
    expect(view.display.value.kind).toBe('pending')
    expect(view.rows.value.some(row => row.rowKind === 'workspace')).toBe(false)
    expect(view.rows.value.some(row => row.workspaceId)).toBe(false)
    lateMessage?.({ data: JSON.stringify({ type: 'res', id: request.id, ok: true, payload: { workspaces: [
      { id: 'retired-project', name: 'Retired project', path: '/retired', taskCount: 0, pinned: false, available: true },
    ] } }) } as MessageEvent)
    await refresh
    expect(view.display.value.kind).toBe('pending')
  })

  it('recovers from a missing directory event and failed read without clearing a successor task', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = await setup()
    const initial = app.automatic.load()
    await vi.advanceTimersByTimeAsync(0)
    app.socket.respond('sessions.list', page('Active task', 'running'))
    await initial

    // The chat lane reports the predecessor's cancellation; sessions.changed
    // is deliberately absent. Duplicate terminal notices coalesce to one read.
    for (const event of ['task.cancelled', 'session.event.done']) {
      app.socket.receive({ type: 'event', event, payload: {
        schema_version: 1, key: 'agent:main:webchat:one', task_id: 'predecessor',
      } })
    }
    await vi.advanceTimersByTimeAsync(150)
    expect(app.socket.requests('sessions.list')).toHaveLength(3)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('running')
    app.socket.respond('sessions.list', page('Successor task', 'running'))
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value[0]).toMatchObject({ title: 'Successor task', runStatus: 'running' })

    app.socket.receive({ type: 'event', event: 'task.cancelled', payload: {
      schema_version: 1, key: 'agent:main:webchat:one', task_id: 'successor',
    } })
    await vi.advanceTimersByTimeAsync(150)
    const listRequests = app.socket.requests('sessions.list')
    const failedRequest = listRequests[listRequests.length - 1]!
    app.socket.receive({ type: 'res', id: failedRequest.id, ok: false, error: {
      code: 'INTERNAL_ERROR', message: 'Temporary directory failure',
    } })
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('running')
    await vi.advanceTimersByTimeAsync(500)
    expect(app.socket.requests('sessions.list')).toHaveLength(5)
    app.socket.respond('sessions.list', page('Stopped task', 'killed'))
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('cancelled')
    expect(app.sessions.sessionListError.value).toBe(false)
  })

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
