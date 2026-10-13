import { describe, expect, it, vi } from 'vitest'
import { createV4AgentCatalog } from './agentCatalogV4'

describe('v4 AgentCatalog Adapter', () => {
  it('waits for the connection and lists existing runtime profiles', async () => {
    const profiles = [
      { id: 'main', name: 'Main Agent' },
      { id: 'ops', name: 'Operations', model: 'openai/test' },
    ]
    const request = vi.fn().mockResolvedValue({ agents: profiles })
    const ready = vi.fn(async () => {
      expect(request).not.toHaveBeenCalled()
    })
    const catalog = createV4AgentCatalog({ request, ready })
    const signal = new AbortController().signal

    await expect(catalog.list({ signal })).resolves.toEqual(profiles)
    expect(ready).toHaveBeenCalledWith({ signal })
    expect(request).toHaveBeenCalledWith('agents.list', {}, expect.objectContaining({ signal }))
  })

  it('rejects invalid catalog responses', async () => {
    const catalog = createV4AgentCatalog({
      request: vi.fn().mockResolvedValue({ agents: 'invalid' }),
      ready: vi.fn(async () => {}),
    })

    await expect(catalog.list()).rejects.toMatchObject({ kind: 'invalid' })
  })

  it('preserves connection failures without sending a request', async () => {
    const request = vi.fn()
    const catalog = createV4AgentCatalog({
      request,
      ready: vi.fn(async () => { throw new Error('Connection unavailable') }),
    })

    await expect(catalog.list()).rejects.toMatchObject({
      kind: 'unavailable', message: 'Connection unavailable',
    })
    expect(request).not.toHaveBeenCalled()
  })
})
