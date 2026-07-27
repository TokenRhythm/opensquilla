import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useRpcStore } from '@/stores/rpc'
import { useProjectWorkspaces } from './useProjectWorkspaces'

describe('useProjectWorkspaces', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('distinguishes an unavailable project list from a successfully empty list', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.client = {
      call: vi.fn().mockRejectedValue(new Error('owner scope required')),
    } as never
    const projects = useProjectWorkspaces()

    await expect(projects.loadWorkspaces()).rejects.toThrow('owner scope required')

    expect(projects.workspaces.value).toEqual([])
    expect(projects.hasLoaded.value).toBe(false)
  })

  it('loads backend project order including empty projects', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
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
    const projects = useProjectWorkspaces()

    await projects.loadWorkspaces()

    expect(projects.workspaces.value.map(item => item.id)).toEqual(['b', 'a'])
    expect(projects.workspaces.value[0].taskCount).toBe(0)
    expect(projects.workspaces.value[1].available).toBe(false)
    expect(projects.workspaces.value[1].availabilityReason).toBe('missing')
    expect(projects.hasLoaded.value).toBe(true)
  })

  it('calls lifecycle RPCs and refreshes the canonical list', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    const call = vi.fn()
      .mockResolvedValueOnce({ workspace: { id: 'a' } })
      .mockResolvedValue({ workspaces: [] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces()

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
    rpc.state = 'connected'
    const call = vi.fn()
      .mockResolvedValueOnce({
        workspaceId: 'a',
        deletedTaskCount: 1,
        deletedSessionKeys: ['agent:main:webchat:project-a'],
      })
      .mockResolvedValueOnce({ workspaces: [] })
    rpc.client = { call } as never
    const projects = useProjectWorkspaces()

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
    rpc.state = 'connected'
    const call = vi.fn()
      .mockResolvedValueOnce({
        workspaceId: 'a',
        deletedTaskCount: 1,
        deletedSessionKeys: ['agent:main:webchat:deleted'],
      })
      .mockRejectedValueOnce(new Error('refresh unavailable'))
    rpc.client = { call } as never
    const projects = useProjectWorkspaces()

    const result = await projects.deleteWorkspaceHistory('a')

    expect(result.deletedSessionKeys).toEqual(['agent:main:webchat:deleted'])
    expect(projects.error.value).toBe('refresh unavailable')
  })

  it('removes a project locally when the authoritative removal succeeds but refresh fails', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
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
    const projects = useProjectWorkspaces()
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
})
