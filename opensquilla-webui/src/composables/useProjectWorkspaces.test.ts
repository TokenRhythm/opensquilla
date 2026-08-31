import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useRpcStore } from '@/stores/rpc'
import type { WorkspaceCatalog } from '@/modules/workspaceCatalog'
import { useProjectWorkspaces } from './useProjectWorkspaces'

const PROJECT_METHODS = [
  'workspaces.list',
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

describe('useProjectWorkspaces', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('distinguishes an unavailable project list from a successfully empty list', async () => {
    const rpc = useRpcStore()
    connectOwner(rpc)
    rpc.client = {
      call: vi.fn().mockRejectedValue(new Error('owner scope required')),
    } as never
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

    const first = projects.loadWorkspaces()
    const second = projects.loadWorkspaces()
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
    await expect(first).resolves.toEqual([
      expect.objectContaining({ id: 'workspace-a' }),
    ])

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

    const first = projects.loadWorkspaces().catch((cause: unknown) => cause)
    const second = projects.loadWorkspaces()
    rejectFirst(new Error('A navigation was aborted'))
    await expect(first).resolves.toBeInstanceOf(Error)

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))
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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))

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
    const projects = useProjectWorkspaces(catalogFromRpc(rpc))
    await projects.loadWorkspaces()

    rpc.auth = { principal: { isOwner: false } }
    await Promise.resolve()

    expect(projects.workspaces.value).toEqual([])
    expect(projects.hasLoaded.value).toBe(false)
  })
})
