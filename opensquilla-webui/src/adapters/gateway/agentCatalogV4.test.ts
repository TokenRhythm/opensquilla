import { describe, expect, it, vi } from 'vitest'
import { createV4AgentCatalog } from './agentCatalogV4'

describe('v4 AgentCatalog Adapter', () => {
  it('projects the catalog and maps semantic mutations', async () => {
    const agentRequestMock = vi.fn(async (method: string) => {
      if (method === 'agents.list') return { agents: [{ id: 'main', name: 'Main Agent' }] }
      if (method === 'agents.delete') return null
      return { id: 'researcher', name: 'Researcher', enabled: false }
    })
    const catalog = createV4AgentCatalog({
      request: agentRequestMock,
      ready: vi.fn(async () => {}),
    } as Parameters<typeof createV4AgentCatalog>[0])

    await expect(catalog.list()).resolves.toEqual([{ id: 'main', name: 'Main Agent' }])
    await expect(catalog.create({ id: 'researcher', name: 'Researcher' })).resolves.toMatchObject({
      id: 'researcher',
    })
    await expect(catalog.update({ id: 'researcher', enabled: false })).resolves.toMatchObject({
      enabled: false,
    })
    await expect(catalog.remove('researcher')).resolves.toBeUndefined()

    expect(agentRequestMock).toHaveBeenNthCalledWith(2, 'agents.create', {
      id: 'researcher',
      name: 'Researcher',
    }, expect.any(Object))
    expect(agentRequestMock).toHaveBeenNthCalledWith(3, 'agents.update', {
      id: 'researcher',
      enabled: false,
    }, expect.any(Object))
    expect(agentRequestMock).toHaveBeenNthCalledWith(
      4,
      'agents.delete',
      { id: 'researcher' },
      expect.any(Object),
    )
  })
})
