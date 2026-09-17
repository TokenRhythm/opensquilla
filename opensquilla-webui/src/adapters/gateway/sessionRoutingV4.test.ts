import { describe, expect, it, vi } from 'vitest'
import { createV4SessionRouting } from './sessionRoutingV4'

describe('SessionRouting v4 adapter', () => {
  it('keeps method names and CAS payload inside the adapter', async () => {
    const request = vi.fn(<T = unknown>(_method: string, params?: Record<string, unknown>): Promise<T> => Promise.resolve({
      key: params?.sessionKey,
      mode: 'router',
      revision: 2,
      source: 'session',
      initialized: true,
      appliesTo: 'next_accepted_turn',
    } as T))
    const handlers = new Map<string, (value: unknown) => void>()
    const routing = createV4SessionRouting({ request } as unknown as Parameters<typeof createV4SessionRouting>[0], {
      subscribe: vi.fn((event, handler) => {
        handlers.set(event, handler)
        return { close: vi.fn() }
      }),
    })
    await expect(routing.get('agent:main:webchat:a')).resolves.toMatchObject({ mode: 'router', revision: 2 })
    await expect(routing.set({ sessionKey: 'agent:main:webchat:a', mode: 'ensemble', expectedRevision: 2 })).resolves.toMatchObject({ key: 'agent:main:webchat:a' })
    expect(request).toHaveBeenNthCalledWith(1, 'sessions.routing.get', { sessionKey: 'agent:main:webchat:a' }, expect.any(Object))
    expect(request).toHaveBeenNthCalledWith(2, 'sessions.routing.set', { sessionKey: 'agent:main:webchat:a', mode: 'ensemble', expectedRevision: 2 }, expect.any(Object))
  })

  it('decodes live changed events and drops malformed payloads', () => {
    const handlers = new Map<string, (value: unknown) => void>()
    const routing = createV4SessionRouting({ request: vi.fn() }, {
      subscribe: vi.fn((event, handler) => {
        handlers.set(event, handler)
        return { close: vi.fn() }
      }),
    })
    const listener = vi.fn()
    routing.subscribe(listener)
    handlers.get('sessions.routing.changed')?.({ session_key: 'k', mode: 'squilla_router', revision: 3 })
    handlers.get('sessions.routing.changed')?.({ key: 'k' })
    expect(listener).toHaveBeenCalledTimes(1)
    expect(listener).toHaveBeenCalledWith(expect.objectContaining({ key: 'k', mode: 'router', revision: 3 }))
  })

  it('maps CAS conflicts to a domain error and keeps conflict details', async () => {
    const request = vi.fn(async () => { throw { code: 'SESSION_ROUTING_CHANGED', message: 'stale revision', details: { routing: { revision: 4 } } } })
    const routing = createV4SessionRouting({ request } as unknown as Parameters<typeof createV4SessionRouting>[0], { subscribe: vi.fn(() => ({ close: vi.fn() })) })
    await expect(routing.set({ sessionKey: 'k', mode: 'direct', expectedRevision: 3 })).rejects.toMatchObject({ code: 'conflict', details: { routing: { revision: 4 } }, retryable: true })
  })

  it('maps unsupported capabilities and preserves caller cancellation', async () => {
    const request = vi.fn(async () => {
      throw Object.assign(new Error('method unavailable'), { code: 'METHOD_NOT_FOUND' })
    })
    const routing = createV4SessionRouting(
      {
        request,
        supports: method => method !== 'sessions.routing.set',
      },
      { subscribe: vi.fn(() => ({ close: vi.fn() })) },
    )

    expect(routing.available()).toBe(false)
    await expect(routing.get('k')).rejects.toMatchObject({ code: 'unsupported' })

    const controller = new AbortController()
    const aborted = Object.assign(new Error('aborted'), { code: 'RPC_ABORTED' })
    request.mockRejectedValueOnce(aborted)
    const pending = routing.get('k', { signal: controller.signal })
    controller.abort()
    await expect(pending).rejects.toBe(aborted)
  })

  it('closes the single raw event listener when disposed', () => {
    const close = vi.fn()
    const routing = createV4SessionRouting(
      { request: vi.fn() },
      { subscribe: vi.fn(() => ({ close })) },
    )

    routing.dispose()
    routing.dispose()
    expect(close).toHaveBeenCalledOnce()
  })
})
