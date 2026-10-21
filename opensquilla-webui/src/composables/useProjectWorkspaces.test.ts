import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { effectScope, nextTick, ref, watch } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type { WorkspaceCatalog } from '@/modules/workspaceCatalog'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { useProjectWorkspaces } from './useProjectWorkspaces'
import { applySessionProjectWorkspaceMetadata, useActiveProjectWorkspace } from './useActiveProjectWorkspace'
import chatViewSource from '@/views/ChatView.vue?raw'

const PROJECT_METHODS = [
  'workspaces.list',
  'workspaces.create',
  'workspaces.open',
  'workspaces.update',
  'workspaces.pin',
  'workspaces.remove',
  'workspaces.history.delete',
]

function connectOwner(rpc: ReturnType<typeof useRpcStore>) {
  rpc.state = 'connected'
  rpc.auth = { principal: { isOwner: true } }
  rpc.methods = PROJECT_METHODS
}

function catalogFromRpc(rpc: ReturnType<typeof useRpcStore>): WorkspaceCatalog {
  const call = (method: string, params?: Record<string, unknown>) =>
    rpc.client!.call(method, params)
  return {
    list: async () => ((await call('workspaces.list', undefined) as { workspaces?: unknown }).workspaces ?? []) as never,
    create: async (parentPath, name) => {
      const result = await call('workspaces.create', { parentPath, name, trusted: true }) as { workspace?: unknown }
      return result.workspace as never
    },
    open: async path => {
      const result = await call('workspaces.open', { path, trusted: true }) as { workspace?: unknown }
      return result.workspace as never
    },
    rename: async (id, name) => {
      const result = await call('workspaces.update', { workspaceId: id, name }) as { workspace?: unknown }
      return result.workspace as never
    },
    setPinned: async (id, pinned) => {
      const result = await call('workspaces.pin', { workspaceId: id, pinned }) as { workspace?: unknown }
      return result.workspace as never
    },
    remove: async id => { await call('workspaces.remove', { workspaceId: id }) },
    deleteHistory: async id => await call('workspaces.history.delete', { workspaceId: id }) as never,
    listPath: async () => { throw new Error('not used') },
    createDirectory: async () => { throw new Error('not used') },
    pickPath: async () => { throw new Error('not used') },
  }
}

function accessFromRpc(rpc: ReturnType<typeof useRpcStore>): GatewayAccess {
  return {
    get availability() { return rpc.isConnected ? 'available' : 'unavailable' },
    get connectionHealth() { return rpc.health },
    isRuntimeStarting: false,
    get connectionError() { return rpc.error },
    requiresCredential: false,
    get isAvailable() { return rpc.isConnected },
    get canExecuteHost() { return false },
    hostExecuteUnavailableReason: 'permission',
    get isLocalOwner() { return rpc.isLocalOwner },
    get isAuthenticated() { return rpc.isConnected },
    guestSessionOwnerId: null,
    deliveryIdentity: null,
    get canManageProjectWorkspaces() { return rpc.canManageProjectWorkspaces },
    get canChooseProject() { return rpc.canChooseProject },
    get canCreateProjectWorkspace() { return rpc.canCreateProjectWorkspace },
    runModePolicy: null,
    streamIdleTimeoutMs: null,
    concurrentHistoryReads: false,
    chatSendInitialModel: false,
    sessionsRoutingModelSelection: false,
    detachedSessionHydration: false,
    turnCommittedEvents: false,
    subscriptionEpoch: 0,
    supportBundleUnavailableReason: null,
    loadConnectionEndpoint: () => 'ws://example.invalid/ws',
    connect: async () => undefined,
    disconnect: () => undefined,
    recoverSubscriptionEpoch: () => false,
  }
}

// Exercise the actual ChatView observer together with both real composables.
function observeActiveProject(
  projects: ReturnType<typeof useProjectWorkspaces>,
  access: GatewayAccess,
) {
  const active = useActiveProjectWorkspace()
  const start = chatViewSource.indexOf('watch(projectWorkspaces.workspaces, workspaces => {')
  const end = chatViewSource.indexOf('\n})', start) + '\n})'.length
  expect(start).toBeGreaterThanOrEqual(0)
  const scope = effectScope()
  scope.run(() => new Function(
    'watch', 'projectWorkspaces', 'gatewayAccess', 'boundWorkspaceId',
    'activeProjectWorkspace', 'activeSnapshot', chatViewSource.slice(start, end),
  )(watch, projects, access, active.boundWorkspaceId, active, (item: unknown) => item))
  return { active, stop: () => scope.stop() }
}

describe('useProjectWorkspaces', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const rpc = useRpcStore()
    useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc)).resetWorkspaces()
  })

  it('creates the RPC store without browser globals', () => {
    expect(useRpcStore().state).toBe('disconnected')
  })

  it('shares an in-flight read across consumers of the same catalog and scope', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let finish!: (items: []) => void
    const list = vi.fn(() => new Promise<[]>(resolve => { finish = resolve }))
    const catalog = { ...catalogFromRpc(rpc), list }
    const access = accessFromRpc(rpc)
    const first = useProjectWorkspaces(catalog, access)
    const second = useProjectWorkspaces(catalog, access)
    const a = first.loadWorkspaces()
    const b = second.loadWorkspaces()
    await Promise.resolve()
    expect(list).toHaveBeenCalledOnce()
    finish([])
    await expect(Promise.all([a, b])).resolves.toEqual([[], []])
    await second.loadWorkspaces({ timeoutMs: 1 }).catch(() => {})
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('gives shared readers independent cancellation and aborts transport when all leave', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let signal!: AbortSignal
    const list = vi.fn((options?: { signal?: AbortSignal }) => {
      signal = options!.signal!
      return new Promise<[]>(() => {})
    })
    const projects = useProjectWorkspaces({ ...catalogFromRpc(rpc), list }, accessFromRpc(rpc))
    const a = new AbortController()
    const b = new AbortController()
    const first = projects.loadWorkspaces({ signal: a.signal }).catch(cause => cause)
    const second = projects.loadWorkspaces({ signal: b.signal }).catch(cause => cause)
    await Promise.resolve()
    a.abort()
    expect((await first).name).toBe('AbortError')
    expect(signal.aborted).toBe(false)
    expect(projects.isLoading.value).toBe(true)
    b.abort()
    expect((await second).name).toBe('AbortError')
    expect(signal.aborted).toBe(true)
    expect(projects.isLoading.value).toBe(false)
  })

  it('does not apply a short waiter deadline to another reader', async () => {
    vi.useFakeTimers()
    try {
      const rpc = useRpcStore()
      connectOwner(rpc)
      let finish!: (items: []) => void
      const list = vi.fn(() => new Promise<[]>(resolve => { finish = resolve }))
      const projects = useProjectWorkspaces({ ...catalogFromRpc(rpc), list }, accessFromRpc(rpc))
      const short = projects.loadWorkspaces({ timeoutMs: 10 }).catch(cause => cause)
      const long = projects.loadWorkspaces({ timeoutMs: 100 })
      await vi.advanceTimersByTimeAsync(10)
      expect((await short).name).toBe('TimeoutError')
      expect(list).toHaveBeenCalledWith({ signal: expect.any(AbortSignal), timeoutMs: 0 })
      expect(projects.isLoading.value).toBe(true)
      finish([])
      await expect(long).resolves.toEqual([])
      expect(projects.error.value).toBeNull()
    } finally { vi.useRealTimers() }
  })

  it('preserves the original deadline when a waiter follows fresh preflight', async () => {
    vi.useFakeTimers()
    try {
      const rpc = useRpcStore()
      connectOwner(rpc)
      const completions: Array<(items: []) => void> = []
      const signals: AbortSignal[] = []
      const list = vi.fn((options?: { signal?: AbortSignal }) => {
        signals.push(options!.signal!)
        return new Promise<[]>(resolve => { completions.push(resolve) })
      })
      const projects = useProjectWorkspaces({ ...catalogFromRpc(rpc), list }, accessFromRpc(rpc))
      const old = projects.loadWorkspaces({ timeoutMs: 10 }).catch(cause => cause)
      await vi.advanceTimersByTimeAsync(5)
      const fresh = projects.loadWorkspaces({ timeoutMs: 100, fresh: true })
      await vi.advanceTimersByTimeAsync(5)
      expect((await old).name).toBe('TimeoutError')
      expect(signals[0]!.aborted).toBe(true)
      expect(signals[1]!.aborted).toBe(false)
      completions[1]!([])
      await expect(fresh).resolves.toEqual([])
    } finally { vi.useRealTimers() }
  })

  it('rejects departed-scope results even when transport ignores abort', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const epoch = ref(1)
    const access = { ...accessFromRpc(rpc), get subscriptionEpoch() { return epoch.value } }
    let finish!: (items: []) => void
    const list = vi.fn(() => new Promise<[]>(resolve => { finish = resolve }))
    const projects = useProjectWorkspaces({ ...catalogFromRpc(rpc), list }, access)
    const pending = projects.loadWorkspaces().catch(cause => cause)
    await Promise.resolve()
    epoch.value += 1
    finish([])
    expect((await pending).name).toBe('AbortError')
    expect(projects.hasLoaded.value).toBe(false)
  })

  it('retires reads from before a catalog mutation instead of resurrecting removed workspaces', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let finish!: (items: never[]) => void
    const list = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValue([])
    const catalog = { ...catalogFromRpc(rpc), list, remove: vi.fn(async () => {}) }
    const projects = useProjectWorkspaces(catalog, accessFromRpc(rpc))
    const old = projects.loadWorkspaces().catch(cause => cause)
    await Promise.resolve()
    await projects.removeWorkspace('a')
    finish([{ id: 'a', name: 'A', path: '/a', available: true } as never])
    expect((await old).name).toBe('AbortError')
    expect(projects.workspaces.value).toEqual([])
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('lets a metadata waiter follow a fresh preflight without flashing an error', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const workspace = { id: 'a', name: 'A', path: '/a', available: true, removed: false, taskCount: 0, pinned: false }
    let rejectOld!: (cause: Error) => void
    let finishFresh!: (value: typeof workspace[]) => void
    const list = vi.fn().mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject }))
      .mockImplementationOnce(() => new Promise(resolve => { finishFresh = resolve }))
    const projects = useProjectWorkspaces({ ...catalogFromRpc(rpc), list }, accessFromRpc(rpc))
    const active = useActiveProjectWorkspace()
    active.applySessionSnapshot('session', active.beginSessionResolution('session'), { workspaceId: 'a', projectWorkspace: workspace })
    const pending = applySessionProjectWorkspaceMetadata({
      state: active, sessionKey: 'session', generation: active.beginSessionRefresh('session'),
      metadata: { workspaceId: 'a', projectWorkspaceDeferred: true },
      loadWorkspaces: async () => (await projects.loadWorkspaces()).map(item => ({ ...item, removed: false })),
      isCurrent: () => true,
    })
    await Promise.resolve()
    const preflight = projects.loadWorkspaces({ fresh: true })
    rejectOld(new Error('retired read'))
    await Promise.resolve()
    expect(active.presentationStatus.value).toBe('ready')
    expect(active.sendBlockedReason.value).toBe('resolving')
    finishFresh([workspace])
    await Promise.all([pending, preflight])
    expect(active.status.value).toBe('ready')
    expect(projects.error.value).toBeNull()
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('retains an authoritative creation result when the following refresh fails', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const workspace = { id: 'created', name: 'Demo', path: '/repo/Demo', taskCount: 0, pinned: false, available: true }
    const call = vi.fn().mockResolvedValueOnce({ workspace }).mockRejectedValueOnce(new Error('refresh unavailable'))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))
    projects.resetWorkspaces()

    await expect(projects.createWorkspace('/repo', 'Demo')).resolves.toEqual(workspace)

    expect(projects.byId.value.get('created')).toEqual(workspace)
    expect(projects.error.value).toBe('refresh unavailable')
    expect(call).toHaveBeenNthCalledWith(1, 'workspaces.create', { parentPath: '/repo', name: 'Demo', trusted: true })
  })

  it('refreshes registrations after a lost create reply without adopting or replaying the directory', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const registered = { id: 'registered', name: 'Demo', path: '/repo/Demo', taskCount: 0, pinned: false, available: true }
    const call = vi.fn().mockRejectedValueOnce(new Error('reply lost'))
      .mockResolvedValueOnce({ workspaces: [registered] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))
    projects.resetWorkspaces()

    await expect(projects.createWorkspace('/repo', 'Demo')).rejects.toThrow('reply lost')

    expect(projects.workspaces.value).toEqual([registered])
    expect(call.mock.calls.map(([method]) => method)).toEqual(['workspaces.create', 'workspaces.list'])
  })

  it('does not publish a creation result into another Gateway scope', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let finish!: (value: unknown) => void
    const call = vi.fn().mockReturnValue(new Promise(resolve => { finish = resolve }))
    rpc.client = { call } as never
    const endpoint = ref('ws://first.example/ws')
    const access = { ...accessFromRpc(rpc), loadConnectionEndpoint: () => endpoint.value }
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), access)
    projects.resetWorkspaces()
    const pending = projects.createWorkspace('/repo', 'Demo')
    endpoint.value = 'ws://second.example/ws'
    await nextTick()
    finish({ workspace: { id: 'first', name: 'Demo', path: '/repo/Demo' } })
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(projects.workspaces.value).toEqual([])
    expect(projects.creationDefaults.value).toBeNull()
    expect(call).toHaveBeenCalledOnce()
  })

  it('clears Gateway creation defaults when the active profile changes', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.client = { call: vi.fn().mockResolvedValue({ workspaces: [] }) } as never
    const epoch = ref(1)
    const access = { ...accessFromRpc(rpc), get subscriptionEpoch() { return epoch.value } }
    const defaults = { parentPath: '/home/profile-a', hostName: 'host-a', platform: 'posix' as const }
    const catalog = { ...catalogFromRpc(rpc), creationDefaults: defaults }
    const projects = useProjectWorkspaces(catalog, access)
    await projects.loadWorkspaces()
    expect(projects.creationDefaults.value).toEqual(defaults)

    epoch.value += 1
    await nextTick()

    expect(projects.creationDefaults.value).toBeNull()
    expect(projects.hasLoaded.value).toBe(false)
  })

  it('does not mark a bound project removed while a reconnect catalog is unavailable', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const workspace = {
      id: 'bound', name: 'Bound', path: '/repo/bound', available: true, removed: false,
    }
    const call = vi.fn().mockResolvedValueOnce({ workspaces: [workspace] })
      .mockRejectedValueOnce(new Error('temporary list failure'))
      .mockResolvedValueOnce({ workspaces: [] })
    rpc.client = { call } as never
    const epoch = ref(1)
    const access = { ...accessFromRpc(rpc), get subscriptionEpoch() { return epoch.value } }
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), access)
    const { active, stop } = observeActiveProject(projects, access)
    try {
      active.beginProjectDraft(workspace)
      await projects.loadWorkspaces()
      await nextTick()
      expect(active.status.value).toBe('ready')

      epoch.value += 1
      await nextTick()
      expect(projects.hasLoaded.value).toBe(false)
      expect(projects.workspaces.value).toEqual([])
      expect(active.status.value).toBe('ready')
      await expect(projects.loadWorkspaces()).rejects.toThrow('temporary list failure')
      await nextTick()
      expect(active.status.value).toBe('ready')

      await projects.loadWorkspaces()
      await nextTick()
      expect(projects.hasLoaded.value).toBe(true)
      expect(active.status.value).toBe('removed')
    } finally {
      stop()
    }
  })

  it('marks an explicitly removed bound project before its follow-up list settles', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const workspace = {
      id: 'bound', name: 'Bound', path: '/repo/bound', available: true, removed: false,
    }
    let rejectRefresh!: (cause: Error) => void
    const refresh = new Promise((_resolve, reject) => { rejectRefresh = reject })
    const call = vi.fn().mockResolvedValueOnce({ workspaces: [workspace] })
      .mockResolvedValueOnce({ workspaceId: workspace.id }).mockReturnValueOnce(refresh)
    rpc.client = { call } as never
    const access = accessFromRpc(rpc)
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), access)
    const { active, stop } = observeActiveProject(projects, access)
    try {
      active.beginProjectDraft(workspace)
      await projects.loadWorkspaces()
      await nextTick()
      const removal = projects.removeWorkspace(workspace.id)
      await vi.waitFor(() => expect(active.status.value).toBe('removed'))
      expect(projects.isLoading.value).toBe(true)

      rejectRefresh(new Error('temporary list failure'))
      await removal
      expect(active.status.value).toBe('removed')
    } finally {
      stop()
    }
  })

  it('keeps opening existing projects available when creation is unsupported', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.methods = PROJECT_METHODS.filter(method => method !== 'workspaces.create')
    const workspace = { id: 'existing', name: 'Existing', path: '/repo/existing' }
    const call = vi.fn().mockResolvedValueOnce({ workspace }).mockResolvedValueOnce({ workspaces: [workspace] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))
    await expect(projects.createWorkspace('/repo', 'Demo')).rejects.toThrow('does not support')
    await expect(projects.openWorkspace('/repo/existing')).resolves.toEqual(workspace)
    expect(call.mock.calls.map(([method]) => method)).toEqual(['workspaces.open', 'workspaces.list'])
  })

  it('distinguishes an unavailable project list from a successfully empty list', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.client = {
      call: vi.fn().mockRejectedValue(new Error('owner scope required')),
    } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    await expect(projects.loadWorkspaces()).rejects.toThrow('owner scope required')

    expect(projects.workspaces.value).toEqual([])
    expect(projects.hasLoaded.value).toBe(false)
  })

  it('loads backend project order including empty projects', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.client = { call: vi.fn().mockResolvedValue({
      workspaces: [
        { id: 'b', name: 'B', path: '/repo/b', taskCount: 0, pinned: true, available: true },
        {
          id: 'a',
          name: 'A',
          path: '/repo/a',
          taskCount: 2,
          pinned: false,
          available: false,
          availabilityReason: 'missing',
        },
      ],
    }) } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    await projects.loadWorkspaces()

    expect(projects.workspaces.value.map(item => item.id)).toEqual(['b', 'a'])
    expect(projects.workspaces.value[0].taskCount).toBe(0)
    expect(projects.workspaces.value[1].available).toBe(false)
    expect(projects.workspaces.value[1].availabilityReason).toBe('missing')
    expect(projects.hasLoaded.value).toBe(true)
  })

  it('does not let a late superseded list replace the latest workspace state', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let resolveFirst!: (value: unknown) => void
    let resolveSecond!: (value: unknown) => void
    const firstResponse = new Promise(resolve => { resolveFirst = resolve })
    const secondResponse = new Promise(resolve => { resolveSecond = resolve })
    const call = vi.fn()
      .mockReturnValueOnce(firstResponse)
      .mockReturnValueOnce(secondResponse)
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    const first = projects.loadWorkspaces().catch((cause: unknown) => cause)
    await Promise.resolve()
    const second = projects.loadWorkspaces({ fresh: true })
    resolveSecond({
      workspaces: [{
        id: 'workspace-b',
        name: 'Workspace B',
        path: '/repo/b',
        taskCount: 1,
        available: true,
      }],
    })
    await expect(second).resolves.toEqual([
      expect.objectContaining({ id: 'workspace-b' }),
    ])
    resolveFirst({
      workspaces: [{
        id: 'workspace-a',
        name: 'Workspace A',
        path: '/repo/a',
        taskCount: 1,
        available: true,
      }],
    })
    await expect(first).resolves.toEqual([expect.objectContaining({ id: 'workspace-b' })])

    expect(projects.workspaces.value.map(item => item.id)).toEqual(['workspace-b'])
    expect(projects.error.value).toBeNull()
    expect(projects.isLoading.value).toBe(false)
  })

  it('does not let a superseded abort clear loading or publish an error for the target', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    let rejectFirst!: (cause: unknown) => void
    let resolveSecond!: (value: unknown) => void
    const firstResponse = new Promise((_resolve, reject) => { rejectFirst = reject })
    const secondResponse = new Promise(resolve => { resolveSecond = resolve })
    const call = vi.fn()
      .mockReturnValueOnce(firstResponse)
      .mockReturnValueOnce(secondResponse)
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    const first = projects.loadWorkspaces().catch((cause: unknown) => cause)
    await Promise.resolve()
    const second = projects.loadWorkspaces({ fresh: true })
    rejectFirst(new Error('A navigation was aborted'))
    await Promise.resolve()

    expect(projects.isLoading.value).toBe(true)
    expect(projects.error.value).toBeNull()

    resolveSecond({
      workspaces: [{
        id: 'workspace-b',
        name: 'Workspace B',
        path: '/repo/b',
        taskCount: 1,
        available: true,
      }],
    })
    await second
    await expect(first).resolves.toEqual([expect.objectContaining({ id: 'workspace-b' })])
    expect(projects.workspaces.value.map(item => item.id)).toEqual(['workspace-b'])
    expect(projects.error.value).toBeNull()
    expect(projects.isLoading.value).toBe(false)
  })

  it('calls lifecycle RPCs and refreshes the canonical list', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const call = vi.fn()
      .mockResolvedValueOnce({ workspace: { id: 'a' } })
      .mockResolvedValue({ workspaces: [] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    await projects.openWorkspace('/repo/a')

    expect(call).toHaveBeenNthCalledWith(
      1,
      'workspaces.open',
      { path: '/repo/a', trusted: true },
    )
    expect(call).toHaveBeenNthCalledWith(2, 'workspaces.list', undefined)
  })

  it('returns the exact task keys deleted with project history', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const call = vi.fn()
      .mockResolvedValueOnce({
        workspaceId: 'a',
        deletedTaskCount: 1,
        deletedSessionKeys: ['agent:main:webchat:project-a'],
      })
      .mockResolvedValueOnce({ workspaces: [] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    const result = await projects.deleteWorkspaceHistory('a')

    expect(result.deletedSessionKeys).toEqual(['agent:main:webchat:project-a'])
    expect(call).toHaveBeenNthCalledWith(
      1,
      'workspaces.history.delete',
      { workspaceId: 'a' },
    )
  })

  it('preserves a successful history deletion result when its refresh fails', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const call = vi.fn()
      .mockResolvedValueOnce({
        workspaceId: 'a',
        deletedTaskCount: 1,
        deletedSessionKeys: ['agent:main:webchat:deleted'],
      })
      .mockRejectedValueOnce(new Error('refresh unavailable'))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    const result = await projects.deleteWorkspaceHistory('a')

    expect(result.deletedSessionKeys).toEqual(['agent:main:webchat:deleted'])
    expect(projects.error.value).toBe('refresh unavailable')
  })

  it('removes a project locally when the authoritative removal succeeds but refresh fails', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const call = vi.fn()
      .mockResolvedValueOnce({
        workspaces: [{
          id: 'remove-me',
          name: 'Remove me',
          path: '/repo/remove-me',
          taskCount: 1,
          available: true,
        }],
      })
      .mockResolvedValueOnce({ workspaceId: 'remove-me' })
      .mockRejectedValueOnce(new Error('refresh unavailable'))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))
    await projects.loadWorkspaces()

    await expect(projects.removeWorkspace('remove-me')).resolves.toBeUndefined()

    expect(projects.byId.value.has('remove-me')).toBe(false)
    expect(projects.error.value).toBe('refresh unavailable')
    expect(call).toHaveBeenNthCalledWith(
      2,
      'workspaces.remove',
      { workspaceId: 'remove-me' },
    )
  })

  it('does not call owner-only workspace RPCs for a non-owner principal', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.auth = { principal: { isOwner: false } }
    rpc.methods = PROJECT_METHODS
    const call = vi.fn()
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))

    await expect(projects.loadWorkspaces()).resolves.toEqual([])
    await expect(projects.openWorkspace('/repo/a')).rejects.toThrow(
      'Project workspaces require a local owner.',
    )
    await expect(projects.removeWorkspace('a')).rejects.toThrow(
      'Project workspaces require a local owner.',
    )

    expect(call).not.toHaveBeenCalled()
    expect(projects.hasLoaded.value).toBe(false)
  })

  it('rejects a created workspace if the owner changes during its follow-up read', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const endpoint = ref('ws://first.example/ws')
    const access = { ...accessFromRpc(rpc), loadConnectionEndpoint: () => endpoint.value }
    const workspace = { id: 'shared-id', name: 'First owner', path: '/first', available: true, taskCount: 0 }
    let finish!: (value: unknown) => void
    const call = vi.fn().mockResolvedValueOnce({ workspace })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), access)
    const pending = projects.createWorkspace('/first', 'First owner')
    const outcome = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    endpoint.value = 'ws://second.example/ws'
    finish({ workspaces: [workspace] })
    await outcome
    expect(projects.workspaces.value).toEqual([])
  })

  it('does not remove a same-id workspace from a new owner after its old follow-up read', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    const endpoint = ref('ws://first.example/ws')
    const access = { ...accessFromRpc(rpc), loadConnectionEndpoint: () => endpoint.value }
    let finish!: (value: unknown) => void
    const call = vi.fn().mockResolvedValueOnce({ workspaceId: 'shared-id' })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), access)
    const pending = projects.removeWorkspace('shared-id')
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    endpoint.value = 'ws://second.example/ws'
    const current = { id: 'shared-id', name: 'Second owner', path: '/second', available: true, taskCount: 0, pinned: false }
    projects.workspaces.value = [current]
    finish({ workspaces: [] })
    await pending
    expect(projects.workspaces.value).toEqual([current])
  })

  it('clears owner workspace state when the principal loses owner capability', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.client = {
      call: vi.fn().mockResolvedValue({
        workspaces: [{
          id: 'owner-project',
          name: 'Owner project',
          path: '/repo/owner',
          taskCount: 1,
          available: true,
        }],
      }),
    } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc), accessFromRpc(rpc))
    await projects.loadWorkspaces()

    rpc.auth = { principal: { isOwner: false } }
    await Promise.resolve()

    expect(projects.workspaces.value).toEqual([])
    expect(projects.hasLoaded.value).toBe(false)
  })
})
