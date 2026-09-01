import { describe, expect, it, vi } from 'vitest'
import { createV4AgentCatalog } from './agentCatalogV4'

describe('v4 AgentCatalog Adapter', () => {
  it('projects the catalog and maps semantic mutations', async () => {
    const call = vi.fn(async (method: string) => (
      method === 'agents.list'
        ? { agents: [{ id: 'main', name: 'Main Agent' }] }
        : { status: 'ok' }
    ))
    const catalog = createV4AgentCatalog({
      request: call,
      ready: vi.fn(async () => {}),
    } as Parameters<typeof createV4AgentCatalog>[0])

    await expect(catalog.list()).resolves.toEqual([{ id: 'main', name: 'Main Agent' }])
    await catalog.create({ id: 'researcher', name: 'Researcher' })
    await catalog.update({ id: 'researcher', enabled: false })
    await catalog.remove('researcher')

    expect(call).toHaveBeenNthCalledWith(2, 'agents.create', {
      id: 'researcher',
      name: 'Researcher',
    }, expect.any(Object))
    expect(call).toHaveBeenNthCalledWith(3, 'agents.update', {
      id: 'researcher',
      enabled: false,
    }, expect.any(Object))
    expect(call).toHaveBeenNthCalledWith(4, 'agents.delete', { id: 'researcher' }, expect.any(Object))
  })
})
