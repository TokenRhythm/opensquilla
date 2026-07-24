import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useRpcStore } from '@/stores/rpc'
import { useProjectWorkspaces } from './useProjectWorkspaces'

describe('useProjectWorkspaces', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('loads backend project order including empty projects', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.client = { call: vi.fn().mockResolvedValue({
      workspaces: [
        { id: 'b', name: 'B', path: '/repo/b', taskCount: 0, pinned: true, available: true },
        { id: 'a', name: 'A', path: '/repo/a', taskCount: 2, pinned: false, available: false },
      ],
    }) } as never
    const projects = useProjectWorkspaces()

    await projects.loadWorkspaces()

    expect(projects.workspaces.value.map(item => item.id)).toEqual(['b', 'a'])
    expect(projects.workspaces.value[0].taskCount).toBe(0)
    expect(projects.workspaces.value[1].available).toBe(false)
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
})
