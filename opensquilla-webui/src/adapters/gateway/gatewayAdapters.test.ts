import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createGatewayAdapters } from './gatewayAdapters'

describe('Gateway Adapter composition', () => {
  it('exposes domain Modules without exposing the private transports', async () => {
    const call = vi.fn(async () => ({ sessions: [] })) as <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ) => Promise<T>
    const adapters = createGatewayAdapters({
      connectionGeneration: 1,
      call,
      on: vi.fn((_event: string, _handler: RpcEventHandler) => vi.fn()),
      supportsMethod: vi.fn(() => true),
      supportsEvent: vi.fn(() => true),
      markMethodUnavailable: vi.fn(),
      waitForConnection: vi.fn(async () => undefined),
    })

    expect(Object.keys(adapters)).toEqual([
      'sessionDirectory',
      'sessionDirectoryChanges',
      'sessionLifecycle',
      'sessionRouting',
      'turnCommands',
    ])
    expect(adapters).not.toHaveProperty('rpc')
    expect(adapters).not.toHaveProperty('events')
    await expect(adapters.sessionDirectory.listPage({ limit: 10 })).resolves.toEqual({
      items: [],
      hasMore: false,
      nextCursor: null,
    })
    expect(call).toHaveBeenCalledOnce()
    const changesSubscription = adapters.sessionDirectoryChanges.subscribe(vi.fn())
    await adapters.sessionDirectoryChanges.resume()
    expect(call).toHaveBeenCalledTimes(2)
    changesSubscription.close()

    await adapters.turnCommands.cancel({ sessionKey: 'agent:main:test', source: 'test' })
    expect(call).toHaveBeenLastCalledWith(
      'chat.abort',
      { sessionKey: 'agent:main:test', source: 'test' },
      undefined,
    )
  })
})
